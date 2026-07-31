#include <SPI.h>
#include <WiFi.h>
#include <Preferences.h>
#include <FS.h>
#include <LittleFS.h>
#include <Wire.h>
#include "esp_heap_caps.h"
#include <LovyanGFX.hpp>

#include "BatteryTelemetry.h"
#include "EPD_3in6e.h"
#include "GUI_Paint.h"
#include "DEV_Config.h"
#include "fonts.h"
#include "Font48.h"

// ================= WIFI DEFAULTS / USB PROVISIONING =================
static const char* DEFAULT_WIFI_SSID = "EPD-GATEWAY";
static const char* DEFAULT_WIFI_PASS = "epaper123";
static const char* DEFAULT_PI_HOST = "192.168.4.1";
static const uint16_t DEFAULT_PI_PORT = 5000;
static const uint8_t FIRMWARE_VERSION = 10;
static const uint32_t WIFI_RETRY_MS = 15000;
static const uint32_t WIFI_CONNECT_GRACE_MS = 20000;
static const uint32_t PI_RETRY_MS = 3000;
static const uint32_t STATUS_INTERVAL_MS = 5000;
static const uint8_t BATTERY_LOW_THRESHOLD_PERCENT = 20;

char gWifiSsid[64] = "EPD-GATEWAY";
char gWifiPass[96] = "epaper123";
char gPiHost[64] = "192.168.4.1";
uint16_t gPiPort = 5000;

WiFiClient client;
Preferences prefs;
static bool gFsReady = false;
static bool gLcdImageStored = false;
static unsigned long gLastWifiAttemptMs = 0;
static unsigned long gLastPiAttemptMs = 0;
static wl_status_t gLastWifiStatus = WL_IDLE_STATUS;
static bool gPiSessionOnline = false;
static bool gBatteryGaugeReady = false;

// ================= VERIFIED SMART LABEL PIN MAP =================
// Shared SPI bus: LCD and e-paper use the same SCLK/MOSI. Only one CS is active at a time.
#define SHARED_SPI_SCLK_PIN 12
#define SHARED_SPI_MOSI_PIN 11

#define LCD_CS_PIN 10
#define LCD_DC_PIN 9
#define LCD_RST_PIN 8
#define LCD_BL_PIN 13

#define BAT_I2C_SDA_PIN 2
#define BAT_I2C_SCL_PIN 3
#define BAT_LOW_ALERT_PIN 4
#define BAT_PLUG_IND_PIN 6
#define BAT_CHG_IND_PIN 7
#define LED_FULL_PIN 47
#define LED_LOW_PIN 48
#define MAX17048_ADDR 0x36

class LGFX : public lgfx::LGFX_Device {
  lgfx::Panel_ILI9341 _panel;
  lgfx::Bus_SPI _bus;

public:
  LGFX() {
    auto cfg = _bus.config();
    cfg.spi_host = SPI3_HOST;
    cfg.spi_mode = 0;
    cfg.freq_write = 10000000;
    cfg.freq_read = 6000000;
    cfg.pin_sclk = SHARED_SPI_SCLK_PIN;
    cfg.pin_mosi = SHARED_SPI_MOSI_PIN;
    cfg.pin_miso = -1;
    cfg.pin_dc = LCD_DC_PIN;
    _bus.config(cfg);
    _panel.setBus(&_bus);

    auto pcfg = _panel.config();
    pcfg.pin_cs = LCD_CS_PIN;
    pcfg.pin_rst = LCD_RST_PIN;
    pcfg.pin_busy = -1;
    pcfg.panel_width = 240;
    pcfg.panel_height = 320;
    pcfg.offset_x = 0;
    pcfg.offset_y = 0;
    pcfg.invert = false;
    pcfg.rgb_order = false;
    pcfg.dlen_16bit = false;
    _panel.config(pcfg);

    setPanel(&_panel);
  }
};

LGFX tft;
#define LCD_IMG_W 320
#define LCD_IMG_H 240
#define LCD_IMG_BYTES (LCD_IMG_W * LCD_IMG_H * 2)
#define LCD_IMAGE_PATH "/lcd_image.rgb565"
#define LCD_IMAGE_TMP_PATH "/lcd_image.tmp"
#define LCD_FILE_CHUNK_BYTES 1024

uint16_t* lcdImageBuf = nullptr;
static bool lcdPowerOn = true;

// ================= E-PAPER DISPLAY ============================
UBYTE* ImageBuffer = nullptr;

#define DISPLAY_WIDTH EPD_3IN6E_WIDTH
#define DISPLAY_HEIGHT EPD_3IN6E_HEIGHT

#define MARGIN_LEFT 20
#define MARGIN_RIGHT 10
#define START_Y 40
#define SECTION_GAP 14
#define LINE_SPACING 4
#define MIN_SPACE_WIDTH 8
#define VALUE_CONTINUATION_MAX_INDENT 180
#define DISPLAY_BOTTOM_MARGIN 8

// ================= DEVICE ID =========================
char DEVICE_ID[32] = { 0 };

// ================= DATA MODEL =========================
struct DisplayData {
  char name[64];
  char room[24];
  char diet[8][32];
  int dietCount;
  char texture[8][32];
  int textureCount;
  char fluids[8][32];
  int fluidsCount;
  char note[96];
  char drinks[48];
};

static DisplayData gData;
static long lastAppliedSeq = -1;

// ================= HIGHLIGHTS =========================
#define SEC_NAME 0
#define SEC_ROOM 1
#define SEC_DIET 2
#define SEC_TEXTURE 3
#define SEC_FLUIDS 4
#define SEC_NOTE 5
#define SEC_DRINKS 6
#define SEC_UNKNOWN 255

#define HL_SECTION 0
#define HL_VALUE 1

#define C_WHITE 0
#define C_BLACK 1
#define C_RED 2
#define C_YELLOW 3
#define C_BLUE 4
#define C_GREEN 5

struct HighlightRule {
  uint8_t used;
  uint8_t type;
  uint8_t section;
  uint8_t bg;
  uint8_t fg;
  char value[32];
};

#define MAX_HIGHLIGHTS 16
static HighlightRule gHighlights[MAX_HIGHLIGHTS];

// ----------------- debug helpers -----------------
static void stage(const char* s) {
  Serial.print("[STAGE] ");
  Serial.println(s);
}

static void printHeap(const char* tag) {
  Serial.print("[HEAP] ");
  Serial.print(tag);
  Serial.print(" free=");
  Serial.print(ESP.getFreeHeap());
  Serial.print(" min=");
  Serial.print(ESP.getMinFreeHeap());
  Serial.print(" maxAlloc=");
  Serial.println(ESP.getMaxAllocHeap());
}

static bool sendRawToPi(const char* text) {
  if (!client.connected()) return false;
  size_t len = strlen(text);
  size_t sent = client.print(text);
  if (sent != len) {
    Serial.println("[TCP] write failed; closing client");
    client.stop();
    gPiSessionOnline = false;
    return false;
  }
  return true;
}

// ================= BATTERY / CHARGER TELEMETRY =================
static bool max17048Read16(uint8_t reg, uint16_t* out) {
  if (!out) return false;
  Wire.beginTransmission(MAX17048_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }
  uint8_t count = Wire.requestFrom((uint8_t)MAX17048_ADDR, (uint8_t)2);
  if (count != 2) {
    return false;
  }
  *out = ((uint16_t)Wire.read() << 8) | (uint16_t)Wire.read();
  return true;
}

static bool max17048Write16(uint8_t reg, uint16_t value) {
  Wire.beginTransmission(MAX17048_ADDR);
  Wire.write(reg);
  Wire.write((uint8_t)(value >> 8));
  Wire.write((uint8_t)(value & 0xFF));
  return Wire.endTransmission() == 0;
}

static void configureBatteryAlertThreshold(uint8_t thresholdPercent) {
  if (!gBatteryGaugeReady) return;
  if (thresholdPercent > 31) thresholdPercent = 31;
  uint16_t config = 0;
  if (!max17048Read16(0x0C, &config)) {
    return;
  }
  uint8_t athd = 32 - thresholdPercent;
  config = (config & 0xFFE0) | (athd & 0x1F);
  max17048Write16(0x0C, config);
}

static void updateBatteryLeds(bool low, bool full) {
  digitalWrite(LED_LOW_PIN, low ? HIGH : LOW);
  digitalWrite(LED_FULL_PIN, full ? HIGH : LOW);
}

static BatteryTelemetry readBatteryTelemetry() {
  BatteryTelemetry b;
  b.ok = false;
  b.percent = -1;
  b.rawPercentX10 = -1;
  b.millivolts = -1;
  b.alertPinLow = digitalRead(BAT_LOW_ALERT_PIN) == LOW;
  b.usbPresent = digitalRead(BAT_PLUG_IND_PIN) == LOW;
  b.charging = digitalRead(BAT_CHG_IND_PIN) == LOW;
  b.low = b.alertPinLow;
  b.full = false;

  if (gBatteryGaugeReady) {
    uint16_t vcellRaw = 0;
    uint16_t socRaw = 0;
    if (max17048Read16(0x02, &vcellRaw) && max17048Read16(0x04, &socRaw)) {
      float volts = (float)vcellRaw * 0.000078125f;
      float percentRaw = (float)socRaw / 256.0f;
      int percentRounded = (int)(percentRaw + 0.5f);
      if (percentRounded < 0) percentRounded = 0;
      if (percentRounded > 100) percentRounded = 100;
      b.ok = true;
      b.percent = percentRounded;
      b.rawPercentX10 = (int)(percentRaw * 10.0f + 0.5f);
      b.millivolts = (int)(volts * 1000.0f + 0.5f);
      b.low = b.alertPinLow || b.percent <= BATTERY_LOW_THRESHOLD_PERCENT;
      b.full = b.usbPresent && !b.charging && b.percent >= 95;
    }
  }

  updateBatteryLeds(b.low, b.full);
  return b;
}

static void initBatteryMonitor() {
  pinMode(BAT_LOW_ALERT_PIN, INPUT_PULLUP);
  pinMode(BAT_PLUG_IND_PIN, INPUT_PULLUP);
  pinMode(BAT_CHG_IND_PIN, INPUT_PULLUP);
  pinMode(LED_FULL_PIN, OUTPUT);
  pinMode(LED_LOW_PIN, OUTPUT);
  updateBatteryLeds(false, false);

  Wire.begin(BAT_I2C_SDA_PIN, BAT_I2C_SCL_PIN);
  Wire.setClock(400000);

  uint16_t version = 0;
  gBatteryGaugeReady = max17048Read16(0x08, &version);
  Serial.print("[BAT] MAX17048 ");
  Serial.println(gBatteryGaugeReady ? "ready" : "not detected");
  configureBatteryAlertThreshold(BATTERY_LOW_THRESHOLD_PERCENT);
  readBatteryTelemetry();
}

// ================= BASIC HELPERS =================
static void makeDeviceId() {
  uint64_t mac = ESP.getEfuseMac();
  uint8_t m0 = (mac >> 40) & 0xFF;
  uint8_t m1 = (mac >> 32) & 0xFF;
  uint8_t m2 = (mac >> 24) & 0xFF;
  uint8_t m3 = (mac >> 16) & 0xFF;
  uint8_t m4 = (mac >> 8) & 0xFF;
  uint8_t m5 = (mac >> 0) & 0xFF;
  snprintf(DEVICE_ID, sizeof(DEVICE_ID), "EPD-%02X%02X%02X%02X%02X%02X", m0, m1, m2, m3, m4, m5);
}

static void decodeUnderscore(char* s) {
  for (; *s; s++)
    if (*s == '_') *s = ' ';
}

static int hexVal(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

static void decodePercentInPlace(char* s) {
  char* r = s;
  char* w = s;
  while (*r) {
    if (*r == '%' && r[1] && r[2]) {
      int hi = hexVal(r[1]);
      int lo = hexVal(r[2]);
      if (hi >= 0 && lo >= 0) {
        *w++ = (char)((hi << 4) | lo);
        r += 3;
        continue;
      }
    }
    if (*r == '+') {
      *w++ = ' ';
      r++;
      continue;
    }
    *w++ = *r++;
  }
  *w = '\0';
}

static bool getTokenValue(const char* line, const char* key, char* out, size_t outSize) {
  char pattern[32];
  snprintf(pattern, sizeof(pattern), "%s=", key);
  const char* p = strstr(line, pattern);
  if (!p) return false;
  p += strlen(pattern);

  const char* end = strchr(p, ' ');
  size_t len = end ? (size_t)(end - p) : strlen(p);
  if (len >= outSize) len = outSize - 1;
  memcpy(out, p, len);
  out[len] = '\0';
  return true;
}

static void splitPipeToList(const char* src, char outList[][32], int* outCount, int maxItems) {
  *outCount = 0;
  if (!src || !src[0]) return;

  char tmp[256];
  strncpy(tmp, src, sizeof(tmp) - 1);
  tmp[sizeof(tmp) - 1] = '\0';

  char* saveptr = nullptr;
  char* tok = strtok_r(tmp, "|", &saveptr);
  while (tok && *outCount < maxItems) {
    while (*tok == ' ') tok++;
    strncpy(outList[*outCount], tok, 31);
    outList[*outCount][31] = '\0';
    decodeUnderscore(outList[*outCount]);
    (*outCount)++;
    tok = strtok_r(nullptr, "|", &saveptr);
  }

  if (*outCount == 0) {
    strncpy(outList[0], src, 31);
    outList[0][31] = '\0';
    decodeUnderscore(outList[0]);
    *outCount = 1;
  }
}

static uint8_t parseSectionCode(const char* s) {
  if (strcmp(s, "NAME") == 0) return SEC_NAME;
  if (strcmp(s, "ROOM") == 0) return SEC_ROOM;
  if (strcmp(s, "DIET") == 0) return SEC_DIET;
  if (strcmp(s, "TEXTURE") == 0 || strcmp(s, "ALLERGIES") == 0) return SEC_TEXTURE;
  if (strcmp(s, "FLUIDS") == 0 || strcmp(s, "SCHEDULE") == 0) return SEC_FLUIDS;
  if (strcmp(s, "NOTE") == 0) return SEC_NOTE;
  if (strcmp(s, "DRINKS") == 0) return SEC_DRINKS;
  return SEC_UNKNOWN;
}

static bool parseColorName(const char* s, uint8_t* out) {
  if (!s || !out) return false;
  if (strcmp(s, "WHITE") == 0) {
    *out = C_WHITE;
    return true;
  }
  if (strcmp(s, "BLACK") == 0) {
    *out = C_BLACK;
    return true;
  }
  if (strcmp(s, "RED") == 0) {
    *out = C_RED;
    return true;
  }
  if (strcmp(s, "YELLOW") == 0) {
    *out = C_YELLOW;
    return true;
  }
  if (strcmp(s, "BLUE") == 0) {
    *out = C_BLUE;
    return true;
  }
  if (strcmp(s, "GREEN") == 0) {
    *out = C_GREEN;
    return true;
  }
  return false;
}

static UWORD colorCodeToEpd(uint8_t c) {
  switch (c) {
    case C_BLACK: return EPD_3IN6E_BLACK;
    case C_RED: return EPD_3IN6E_RED;
    case C_YELLOW: return EPD_3IN6E_YELLOW;
    case C_BLUE: return EPD_3IN6E_BLUE;
    case C_GREEN: return EPD_3IN6E_GREEN;
    default: return EPD_3IN6E_WHITE;
  }
}

static uint8_t autoFgForBg(uint8_t bg) {
  if (bg == C_RED || bg == C_BLUE || bg == C_GREEN || bg == C_BLACK) return C_WHITE;
  return C_BLACK;
}

static void clearHighlights() {
  memset(gHighlights, 0, sizeof(gHighlights));
}

static bool strEqNoCase(const char* a, const char* b) {
  if (!a || !b) return false;
  while (*a && *b) {
    char ca = *a, cb = *b;
    if (ca >= 'a' && ca <= 'z') ca -= 32;
    if (cb >= 'a' && cb <= 'z') cb -= 32;
    if (ca != cb) return false;
    a++;
    b++;
  }
  return *a == '\0' && *b == '\0';
}

static bool getSectionHighlightByCode(uint8_t sec, uint8_t* bg, uint8_t* fg) {
  for (int i = 0; i < MAX_HIGHLIGHTS; i++) {
    if (!gHighlights[i].used) continue;
    if (gHighlights[i].type == HL_SECTION && gHighlights[i].section == sec) {
      if (bg) *bg = gHighlights[i].bg;
      if (fg) *fg = gHighlights[i].fg;
      return true;
    }
  }
  return false;
}

static bool getValueHighlightByCode(uint8_t sec, const char* value, uint8_t* bg, uint8_t* fg) {
  for (int i = 0; i < MAX_HIGHLIGHTS; i++) {
    if (!gHighlights[i].used) continue;
    if (gHighlights[i].type == HL_VALUE && gHighlights[i].section == sec) {
      if (strEqNoCase(gHighlights[i].value, value)) {
        if (bg) *bg = gHighlights[i].bg;
        if (fg) *fg = gHighlights[i].fg;
        return true;
      }
    }
  }
  return false;
}

static void parseHighlights(const char* hl) {
  clearHighlights();
  if (!hl || !hl[0]) return;

  char buf[512];
  strncpy(buf, hl, sizeof(buf) - 1);
  buf[sizeof(buf) - 1] = '\0';

  int idx = 0;
  char* saveSemi = nullptr;
  char* rule = strtok_r(buf, ";", &saveSemi);

  while (rule && idx < MAX_HIGHLIGHTS) {
    char tmp[128];
    strncpy(tmp, rule, sizeof(tmp) - 1);
    tmp[sizeof(tmp) - 1] = '\0';

    HighlightRule hr{};
    hr.used = 1;
    hr.bg = C_WHITE;
    hr.fg = C_BLACK;
    hr.section = SEC_UNKNOWN;

    char* saveColon = nullptr;
    char* part0 = strtok_r(tmp, ":", &saveColon);
    char* part1 = strtok_r(nullptr, ":", &saveColon);
    char* part2 = strtok_r(nullptr, ":", &saveColon);
    char* part3 = strtok_r(nullptr, ":", &saveColon);
    char* part4 = strtok_r(nullptr, ":", &saveColon);

    if (!part0 || !part1) {
      rule = strtok_r(nullptr, ";", &saveSemi);
      continue;
    }

    if (strcmp(part0, "SEC") == 0) {
      hr.type = HL_SECTION;
      hr.section = parseSectionCode(part1);

      const char* bgPart = part2;
      const char* fgPart = part3;
      char colorBuf[16];

      if (bgPart && strncmp(bgPart, "BG=", 3) == 0) {
        strncpy(colorBuf, bgPart + 3, sizeof(colorBuf) - 1);
        colorBuf[sizeof(colorBuf) - 1] = '\0';
        parseColorName(colorBuf, &hr.bg);
      }
      if (fgPart && strncmp(fgPart, "FG=", 3) == 0) {
        strncpy(colorBuf, fgPart + 3, sizeof(colorBuf) - 1);
        colorBuf[sizeof(colorBuf) - 1] = '\0';
        if (!parseColorName(colorBuf, &hr.fg)) hr.fg = autoFgForBg(hr.bg);
      } else {
        hr.fg = autoFgForBg(hr.bg);
      }

      if (hr.section != SEC_UNKNOWN) gHighlights[idx++] = hr;
    } else if (strcmp(part0, "VAL") == 0) {
      hr.type = HL_VALUE;
      hr.section = parseSectionCode(part1);

      if (part2) {
        strncpy(hr.value, part2, sizeof(hr.value) - 1);
        hr.value[sizeof(hr.value) - 1] = '\0';
        decodeUnderscore(hr.value);
      }

      const char* bgPart = part3;
      const char* fgPart = part4;
      char colorBuf[16];

      if (bgPart && strncmp(bgPart, "BG=", 3) == 0) {
        strncpy(colorBuf, bgPart + 3, sizeof(colorBuf) - 1);
        colorBuf[sizeof(colorBuf) - 1] = '\0';
        parseColorName(colorBuf, &hr.bg);
      }
      if (fgPart && strncmp(fgPart, "FG=", 3) == 0) {
        strncpy(colorBuf, fgPart + 3, sizeof(colorBuf) - 1);
        colorBuf[sizeof(colorBuf) - 1] = '\0';
        if (!parseColorName(colorBuf, &hr.fg)) hr.fg = autoFgForBg(hr.bg);
      } else {
        hr.fg = autoFgForBg(hr.bg);
      }

      if (hr.section != SEC_UNKNOWN && hr.value[0]) gHighlights[idx++] = hr;
    }

    rule = strtok_r(nullptr, ";", &saveSemi);
  }
}

// ================= LIST JOIN =================
static void joinListLineLocal(char list[][32], int count, char* out, size_t outSize) {
  if (!outSize) return;
  out[0] = '\0';

  size_t n = 0;
  for (int i = 0; i < count && i < 8; i++) {
    const char* part = list[i];
    size_t partLen = strlen(part);

    if (i > 0) {
      if (n + 2 >= outSize) break;
      out[n++] = ',';
      out[n++] = ' ';
      out[n] = '\0';
    }

    if (n + partLen >= outSize) break;
    memcpy(out + n, part, partLen);
    n += partLen;
    out[n] = '\0';
  }
}

// ================= E-PAPER RENDER HELPERS =================
static int getCharWidth(char c, sFONT* font) {
  if (font->widths != NULL) {
    uint8_t idx = (uint8_t)(c - ' ');
    if (idx < 95) {
      int w = font->widths[idx];
      if (c == ' ' && w < MIN_SPACE_WIDTH) return MIN_SPACE_WIDTH;
      return w;
    }
  }
  return font->Width;
}

static int getTextWidth(const char* text, sFONT* font) {
  int width = 0;
  while (*text) {
    width += getCharWidth(*text, font);
    text++;
  }
  return width;
}

static int drawString(int x, int y, const char* text, sFONT* font, UWORD bgColor, UWORD fgColor) {
  int currentX = x;
  while (*text) {
    if (*text == ' ') currentX += getCharWidth(' ', font);
    else {
      char buf[2] = { *text, '\0' };
      Paint_DrawString_EN(currentX, y, buf, font, bgColor, fgColor);
      currentX += getCharWidth(*text, font);
    }
    text++;
  }
  return currentX;
}

static void drawHighlightBox(int x, int y, int w, int h, UWORD bg) {
  int x0 = x;
  int y0 = y;
  int x1 = x + w;
  int y1 = y + h;
  if (x0 < 0) x0 = 0;
  if (y0 < 0) y0 = 0;
  if (x1 >= DISPLAY_WIDTH) x1 = DISPLAY_WIDTH - 1;
  if (y1 >= DISPLAY_HEIGHT) y1 = DISPLAY_HEIGHT - 1;
  if (x0 <= x1 && y0 <= y1) {
    Paint_DrawRectangle(x0, y0, x1, y1, bg, DOT_PIXEL_1X1, DRAW_FILL_FULL);
  }
}

static void drawStringWrappedHighlighted(
  int* cx, int* cy,
  const char* text,
  sFONT* font,
  int wrapX, int maxX,
  uint8_t sectionCode,
  bool hasSectionHighlight,
  uint8_t sectionBgCode,
  uint8_t sectionFgCode) {
  const char* ptr = text;
  while (*ptr) {
    if (*cy > DISPLAY_HEIGHT - font->Height - DISPLAY_BOTTOM_MARGIN) return;

    while (*ptr == ' ') {
      int sw = getCharWidth(' ', font);
      if (*cx + sw > maxX) {
        *cx = wrapX;
        *cy += font->Height + LINE_SPACING;
        if (*cy > DISPLAY_HEIGHT - font->Height - DISPLAY_BOTTOM_MARGIN) return;
      }
      *cx += sw;
      ptr++;
    }

    if (!*ptr) break;

    const char* wordStart = ptr;
    int wordWidth = 0;
    while (*ptr && *ptr != ' ') {
      wordWidth += getCharWidth(*ptr, font);
      ptr++;
    }
    int wordLen = (int)(ptr - wordStart);

    if (*cx + wordWidth > maxX && *cx > wrapX) {
      *cx = wrapX;
      *cy += font->Height + LINE_SPACING;
      if (*cy > DISPLAY_HEIGHT - font->Height - DISPLAY_BOTTOM_MARGIN) return;
    }

    char word[64];
    int copyLen = wordLen;
    if (copyLen >= (int)sizeof(word)) copyLen = sizeof(word) - 1;
    memcpy(word, wordStart, copyLen);
    word[copyLen] = '\0';

    uint8_t vbg = C_WHITE;
    uint8_t vfg = C_BLACK;
    bool hasValHl = getValueHighlightByCode(sectionCode, word, &vbg, &vfg);

    UWORD bgColor = EPD_3IN6E_WHITE;
    UWORD fgColor = EPD_3IN6E_BLACK;

    if (hasValHl) {
      bgColor = colorCodeToEpd(vbg);
      fgColor = colorCodeToEpd(vfg);
      drawHighlightBox(*cx - 2, *cy - 2, wordWidth + 4, font->Height + 4, bgColor);
    } else if (hasSectionHighlight) {
      bgColor = colorCodeToEpd(sectionBgCode);
      fgColor = colorCodeToEpd(sectionFgCode);
      drawHighlightBox(*cx - 2, *cy - 2, wordWidth + 4, font->Height + 4, bgColor);
    }

    for (int i = 0; i < copyLen; i++) {
      char ch[2] = { word[i], '\0' };
      Paint_DrawString_EN(*cx, *cy, ch, font, bgColor, fgColor);
      *cx += getCharWidth(word[i], font);
    }
  }
}

static int renderSection(const char* label, const char* value,
                         uint8_t sectionCode,
                         int startX, int y, int maxX, sFONT* font) {
  if (y > DISPLAY_HEIGHT - font->Height - DISPLAY_BOTTOM_MARGIN) {
    return y;
  }

  int currentX = startX, currentY = y;

  uint8_t secBg = C_WHITE;
  uint8_t secFg = C_BLACK;
  bool hasSecHl = getSectionHighlightByCode(sectionCode, &secBg, &secFg);

  char labelText[24];
  snprintf(labelText, sizeof(labelText), "%s: ", label);
  int labelWidth = getTextWidth(labelText, font);

  if (hasSecHl) {
    drawHighlightBox(currentX - 2, currentY - 2, labelWidth + 4, font->Height + 4, colorCodeToEpd(secBg));
    currentX = drawString(currentX, currentY, labelText, font, colorCodeToEpd(secBg), colorCodeToEpd(secFg));
  } else {
    currentX = drawString(currentX, currentY, labelText, font, EPD_3IN6E_WHITE, EPD_3IN6E_BLACK);
  }

  int wrapX = startX;
  drawStringWrappedHighlighted(&currentX, &currentY, value, font, wrapX, maxX,
                               sectionCode, hasSecHl, secBg, secFg);

  return currentY + font->Height + SECTION_GAP;
}

static bool hasDisplayText(const char* value) {
  return value && value[0] != '\0';
}

static int renderSectionIfText(const char* label, const char* value,
                               uint8_t sectionCode,
                               int startX, int y, int maxX, sFONT* font) {
  if (!hasDisplayText(value)) return y;
  return renderSection(label, value, sectionCode, startX, y, maxX, font);
}

// ================= E-PAPER DISPLAY =================
static void displayFromData(const DisplayData& d) {
  stage("epaper: start");
  printHeap("before epaper");

  if (gLcdImageStored) {
    releaseLcdImageBuffer();
  }
  digitalWrite(LCD_CS_PIN, HIGH);

  stage("epaper: init");
  EPD_3IN6E_Init();
  if (!EPD_3IN6E_IsReady()) {
    stage("epaper: skipped busy timeout");
    return;
  }

  const uint32_t bufBytes = ((uint32_t)DISPLAY_WIDTH * (uint32_t)DISPLAY_HEIGHT) / 2;
  if (!ImageBuffer) {
    ImageBuffer = (UBYTE*)heap_caps_malloc(bufBytes, MALLOC_CAP_DMA | MALLOC_CAP_8BIT);
  }
  if (!ImageBuffer) {
    Serial.printf("[EPD] framebuffer alloc failed; need=%u free=%u max=%u\n",
                  (unsigned)bufBytes,
                  (unsigned)ESP.getFreeHeap(),
                  (unsigned)ESP.getMaxAllocHeap());
    return;
  }

  stage("epaper: render");
  Paint_NewImage(ImageBuffer, DISPLAY_WIDTH, DISPLAY_HEIGHT, 0, EPD_3IN6E_WHITE);
  Paint_SelectImage(ImageBuffer);
  Paint_Clear(EPD_3IN6E_WHITE);
  Paint_SetScale(6);

  int startX = MARGIN_LEFT;
  int maxX = DISPLAY_WIDTH - MARGIN_RIGHT;
  int y = START_Y;

  char dietLine[200];
  char textureLine[200];
  char fluidsLine[200];
  joinListLineLocal((char(*)[32])d.diet, d.dietCount, dietLine, sizeof(dietLine));
  joinListLineLocal((char(*)[32])d.texture, d.textureCount, textureLine, sizeof(textureLine));
  joinListLineLocal((char(*)[32])d.fluids, d.fluidsCount, fluidsLine, sizeof(fluidsLine));

  y = renderSectionIfText("NAME", d.name, SEC_NAME, startX, y, maxX, &Font48);
  y = renderSectionIfText("ROOM", d.room, SEC_ROOM, startX, y, maxX, &Font48);
  y = renderSectionIfText("DIET", dietLine, SEC_DIET, startX, y, maxX, &Font48);
  y = renderSectionIfText("TEXTURE", textureLine, SEC_TEXTURE, startX, y, maxX, &Font48);
  y = renderSectionIfText("FLUIDS", fluidsLine, SEC_FLUIDS, startX, y, maxX, &Font48);
  y = renderSectionIfText("NOTE", d.note, SEC_NOTE, startX, y, maxX, &Font48);
  y = renderSectionIfText("DRINKS", d.drinks, SEC_DRINKS, startX, y, maxX, &Font48);

  stage("epaper: display");
  EPD_3IN6E_Display(ImageBuffer);
  if (EPD_3IN6E_IsReady()) {
    EPD_3IN6E_Sleep();
  } else {
    stage("epaper: display busy timeout");
  }

  stage("epaper: done");
  printHeap("after epaper");
  releaseEpaperBuffer();
  delay(200);
}

// ================= LCD DISPLAY =================
static void initLCD() {
  stage("lcd: init");
  digitalWrite(EPD_CS_PIN, HIGH);
  tft.init();
  tft.setRotation(1);
  tft.setSwapBytes(true);
  tft.fillScreen(TFT_BLACK);
}

static void showLCDPlaceholder() {
  digitalWrite(EPD_CS_PIN, HIGH);
  tft.init();
  tft.setRotation(1);
  tft.setSwapBytes(true);
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextSize(2);
  tft.setCursor(20, 20);
  tft.print("LCD Ready");
}

static void setLcdPower(bool on) {
  lcdPowerOn = on;
  digitalWrite(LCD_BL_PIN, on ? HIGH : LOW);
  if (on) {
    delay(20);
  }
}

static uint32_t checksumUpdate(uint32_t hash, const uint8_t* data, size_t len) {
  for (size_t i = 0; i < len; i++) {
    hash ^= data[i];
    hash *= 16777619UL;
  }
  return hash;
}

static uint32_t checksumBytes(const uint8_t* data, size_t len) {
  return checksumUpdate(2166136261UL, data, len);
}

static void releaseLcdImageBuffer() {
  if (lcdImageBuf) {
    free(lcdImageBuf);
    lcdImageBuf = nullptr;
    Serial.println("[LCD] image buffer released");
  }
}

static void releaseEpaperBuffer() {
  if (ImageBuffer) {
    free(ImageBuffer);
    ImageBuffer = nullptr;
    Serial.println("[EPD] framebuffer released");
  }
}

static bool ensureLcdImageBuffer() {
  if (lcdImageBuf) return true;
  lcdImageBuf = (uint16_t*)heap_caps_malloc(LCD_IMG_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (lcdImageBuf) {
    Serial.println("[LCD] image buffer allocated in PSRAM");
    return true;
  }
  lcdImageBuf = (uint16_t*)heap_caps_malloc(LCD_IMG_BYTES, MALLOC_CAP_8BIT);
  if (!lcdImageBuf) {
    Serial.printf("[LCD] image buffer allocation failed; need=%u free=%u max=%u\n",
                  (unsigned)LCD_IMG_BYTES,
                  (unsigned)ESP.getFreeHeap(),
                  (unsigned)ESP.getMaxAllocHeap());
    return false;
  }
  Serial.println("[LCD] image buffer allocated in internal RAM");
  return true;
}

static void initPersistentFileSystem() {
  gFsReady = LittleFS.begin(true);
  if (!gFsReady) {
    Serial.println("[FS] LittleFS mount failed; LCD image will be RAM-only");
    gLcdImageStored = false;
    return;
  }
  File f = LittleFS.open(LCD_IMAGE_PATH, "r");
  gLcdImageStored = f && f.size() == LCD_IMG_BYTES;
  if (f) f.close();
  Serial.print("[FS] LittleFS ready, stored LCD image=");
  Serial.println(gLcdImageStored ? "yes" : "no");
}

static bool saveLcdImageToFlash() {
  if (!gFsReady || !lcdImageBuf) return false;

  LittleFS.remove(LCD_IMAGE_TMP_PATH);
  File f = LittleFS.open(LCD_IMAGE_TMP_PATH, "w");
  if (!f) {
    Serial.println("[FS] Could not open LCD image temp file");
    return false;
  }

  size_t written = f.write((const uint8_t*)lcdImageBuf, LCD_IMG_BYTES);
  f.close();
  if (written != LCD_IMG_BYTES) {
    LittleFS.remove(LCD_IMAGE_TMP_PATH);
    Serial.printf("[FS] LCD image write incomplete: %u/%u\n", (unsigned)written, (unsigned)LCD_IMG_BYTES);
    return false;
  }

  LittleFS.remove(LCD_IMAGE_PATH);
  if (!LittleFS.rename(LCD_IMAGE_TMP_PATH, LCD_IMAGE_PATH)) {
    LittleFS.remove(LCD_IMAGE_TMP_PATH);
    Serial.println("[FS] LCD image rename failed");
    return false;
  }

  gLcdImageStored = true;
  Serial.println("[FS] LCD image saved to flash");
  return true;
}

static bool loadLcdImageFromFlash() {
  if (!gFsReady || !gLcdImageStored) return false;
  if (!ensureLcdImageBuffer()) return false;

  File f = LittleFS.open(LCD_IMAGE_PATH, "r");
  if (!f || f.size() != LCD_IMG_BYTES) {
    if (f) f.close();
    gLcdImageStored = false;
    Serial.println("[FS] Stored LCD image missing or wrong size");
    return false;
  }

  size_t readBytes = f.read((uint8_t*)lcdImageBuf, LCD_IMG_BYTES);
  f.close();
  if (readBytes != LCD_IMG_BYTES) {
    Serial.printf("[FS] LCD image read incomplete: %u/%u\n", (unsigned)readBytes, (unsigned)LCD_IMG_BYTES);
    return false;
  }

  Serial.println("[FS] LCD image loaded from flash");
  return true;
}

static bool receiveExactBytes(uint8_t* dst, size_t totalBytes, uint32_t timeoutMs = 10000) {
  size_t received = 0;
  uint32_t start = millis();

  while (received < totalBytes) {
    if (!client.connected()) {
      gPiSessionOnline = false;
      return false;
    }

    int avail = client.available();
    if (avail > 0) {
      int n = client.read(dst + received, totalBytes - received);
      if (n > 0) {
        received += (size_t)n;
        start = millis();
      }
    } else {
      if (millis() - start > timeoutMs) {
        client.stop();
        gPiSessionOnline = false;
        return false;
      }
      delay(1);
    }
  }
  return true;
}

static bool discardExactBytes(size_t totalBytes, uint32_t timeoutMs = 10000) {
  uint8_t buf[LCD_FILE_CHUNK_BYTES];
  size_t received = 0;
  uint32_t start = millis();

  while (received < totalBytes) {
    if (!client.connected()) {
      gPiSessionOnline = false;
      return false;
    }

    int avail = client.available();
    if (avail > 0) {
      size_t want = totalBytes - received;
      if (want > sizeof(buf)) want = sizeof(buf);
      if ((size_t)avail < want) want = (size_t)avail;
      int n = client.read(buf, want);
      if (n > 0) {
        received += (size_t)n;
        start = millis();
      }
    } else {
      if (millis() - start > timeoutMs) {
        client.stop();
        gPiSessionOnline = false;
        return false;
      }
      delay(1);
    }
  }
  return true;
}

static bool receiveImageToFlash(size_t totalBytes, uint32_t* checksumOut, const char** errOut, uint32_t timeoutMs = 15000) {
  if (checksumOut) *checksumOut = 0;
  if (errOut) *errOut = "";
  if (!gFsReady) {
    if (errOut) *errOut = "nofs";
    discardExactBytes(totalBytes, timeoutMs);
    return false;
  }

  LittleFS.remove(LCD_IMAGE_TMP_PATH);
  File f = LittleFS.open(LCD_IMAGE_TMP_PATH, "w");
  if (!f) {
    if (errOut) *errOut = "fsopen";
    discardExactBytes(totalBytes, timeoutMs);
    return false;
  }

  uint8_t buf[LCD_FILE_CHUNK_BYTES];
  size_t received = 0;
  uint32_t start = millis();
  uint32_t hash = 2166136261UL;

  while (received < totalBytes) {
    if (!client.connected()) {
      f.close();
      LittleFS.remove(LCD_IMAGE_TMP_PATH);
      gPiSessionOnline = false;
      if (errOut) *errOut = "disconnect";
      return false;
    }

    int avail = client.available();
    if (avail > 0) {
      size_t want = totalBytes - received;
      if (want > sizeof(buf)) want = sizeof(buf);
      if ((size_t)avail < want) want = (size_t)avail;
      int n = client.read(buf, want);
      if (n > 0) {
        size_t written = f.write(buf, (size_t)n);
        if (written != (size_t)n) {
          f.close();
          LittleFS.remove(LCD_IMAGE_TMP_PATH);
          discardExactBytes(totalBytes - received - (size_t)n, timeoutMs);
          if (errOut) *errOut = "fswrite";
          return false;
        }
        hash = checksumUpdate(hash, buf, (size_t)n);
        received += (size_t)n;
        start = millis();
      }
    } else {
      if (millis() - start > timeoutMs) {
        f.close();
        LittleFS.remove(LCD_IMAGE_TMP_PATH);
        client.stop();
        gPiSessionOnline = false;
        if (errOut) *errOut = "rxtimeout";
        return false;
      }
      delay(1);
    }
  }

  f.close();
  LittleFS.remove(LCD_IMAGE_PATH);
  if (!LittleFS.rename(LCD_IMAGE_TMP_PATH, LCD_IMAGE_PATH)) {
    LittleFS.remove(LCD_IMAGE_TMP_PATH);
    if (errOut) *errOut = "fsrename";
    return false;
  }

  gLcdImageStored = true;
  if (checksumOut) *checksumOut = hash;
  Serial.println("[FS] LCD image streamed to flash");
  return true;
}

static void displayLCDImage565(const uint16_t* img565) {
  setLcdPower(true);
  digitalWrite(EPD_CS_PIN, HIGH);
  tft.init();
  tft.setRotation(1);
  tft.setSwapBytes(true);
  stage("lcd: pushImage");
  tft.fillScreen(TFT_BLACK);
  tft.pushImage(0, 0, LCD_IMG_W, LCD_IMG_H, img565);
  stage("lcd: done");
}

static bool displayLcdImageFromFlash() {
  if (!gFsReady || !gLcdImageStored) return false;

  File f = LittleFS.open(LCD_IMAGE_PATH, "r");
  if (!f || f.size() != LCD_IMG_BYTES) {
    if (f) f.close();
    gLcdImageStored = false;
    Serial.println("[FS] Stored LCD image missing or wrong size");
    return false;
  }

  setLcdPower(true);
  digitalWrite(EPD_CS_PIN, HIGH);
  tft.init();
  tft.setRotation(1);
  tft.setSwapBytes(true);
  stage("lcd: pushImage flash");
  tft.fillScreen(TFT_BLACK);

  uint16_t row[LCD_IMG_W];
  for (int y = 0; y < LCD_IMG_H; y++) {
    size_t readBytes = f.read((uint8_t*)row, LCD_IMG_W * 2);
    if (readBytes != LCD_IMG_W * 2) {
      f.close();
      Serial.println("[FS] LCD image row read failed");
      return false;
    }
    tft.pushImage(0, y, LCD_IMG_W, 1, row);
  }

  f.close();
  stage("lcd: done");
  return true;
}

static void handleImageLine(const char* line) {
  char seqBuf[16];
  char sizeBuf[16];

  if (!getTokenValue(line, "seq", seqBuf, sizeof(seqBuf))) {
    sendRawToPi("ACKIMG seq=0 ok=0 err=parse\n");
    return;
  }
  if (!getTokenValue(line, "size", sizeBuf, sizeof(sizeBuf))) {
    sendRawToPi("ACKIMG seq=0 ok=0 err=parse\n");
    return;
  }

  long seq = atol(seqBuf);
  long size = atol(sizeBuf);

  if (seq <= 0 || size <= 0) {
    sendRawToPi("ACKIMG seq=0 ok=0 err=parse\n");
    return;
  }

  if (size != LCD_IMG_BYTES) {
    discardExactBytes((size_t)size, 15000);
    char ack[96];
    snprintf(ack, sizeof(ack), "ACKIMG seq=%ld ok=0 err=size\n", seq);
    sendRawToPi(ack);
    return;
  }

  Serial.printf("[LCD] expecting %ld bytes\n", size);
  uint32_t checksum = 0;
  const char* err = "";

  if (gFsReady) {
    if (receiveImageToFlash((size_t)size, &checksum, &err, 15000)) {
      Serial.printf("[LCD] image checksum=0x%08lX\n", (unsigned long)checksum);
      bool shown = displayLcdImageFromFlash();
      releaseLcdImageBuffer();
      char ack[128];
      snprintf(
        ack,
        sizeof(ack),
        "ACKIMG seq=%ld ok=1 persisted=1 lcd_on=%d shown=%d checksum=%08lX\n",
        seq,
        lcdPowerOn ? 1 : 0,
        shown ? 1 : 0,
        (unsigned long)checksum
      );
      sendRawToPi(ack);
      Serial.printf("[ACKIMG] sent seq=%ld\n", seq);
      return;
    }

    char ack[96];
    snprintf(ack, sizeof(ack), "ACKIMG seq=%ld ok=0 err=%s\n", seq, err && err[0] ? err : "fs");
    sendRawToPi(ack);
    return;
  }

  if (!ensureLcdImageBuffer()) {
    discardExactBytes((size_t)size, 15000);
    char ack[96];
    snprintf(ack, sizeof(ack), "ACKIMG seq=%ld ok=0 err=nomem\n", seq);
    sendRawToPi(ack);
    return;
  }

  bool ok = receiveExactBytes((uint8_t*)lcdImageBuf, (size_t)size, 15000);
  if (!ok) {
    char ack[96];
    snprintf(ack, sizeof(ack), "ACKIMG seq=%ld ok=0 err=rx\n", seq);
    sendRawToPi(ack);
    return;
  }

  checksum = checksumBytes((const uint8_t*)lcdImageBuf, LCD_IMG_BYTES);
  Serial.printf("[LCD] image checksum=0x%08lX\n", (unsigned long)checksum);
  bool persisted = saveLcdImageToFlash();
  displayLCDImage565(lcdImageBuf);
  if (persisted) {
    releaseLcdImageBuffer();
  }
  char ack[128];
  snprintf(
    ack,
    sizeof(ack),
    "ACKIMG seq=%ld ok=1 persisted=%d lcd_on=%d checksum=%08lX\n",
    seq,
    persisted ? 1 : 0,
    lcdPowerOn ? 1 : 0,
    (unsigned long)checksum
  );
  sendRawToPi(ack);
  Serial.printf("[ACKIMG] sent seq=%ld\n", seq);
}

static void handleLcdLine(const char* line) {
  char seqBuf[16];
  char cmdBuf[24];

  if (!getTokenValue(line, "seq", seqBuf, sizeof(seqBuf))) {
    sendRawToPi("ACKLCD seq=0 ok=0 err=parse\n");
    return;
  }
  if (!getTokenValue(line, "cmd", cmdBuf, sizeof(cmdBuf))) {
    sendRawToPi("ACKLCD seq=0 ok=0 err=parse\n");
    return;
  }

  long seq = atol(seqBuf);
  if (seq <= 0) {
    sendRawToPi("ACKLCD seq=0 ok=0 err=parse\n");
    return;
  }

  decodeUnderscore(cmdBuf);

  if (strEqNoCase(cmdBuf, "on")) {
    bool hasStoredImage = gLcdImageStored || lcdImageBuf;
    setLcdPower(true);
    if (lcdImageBuf) {
      displayLCDImage565(lcdImageBuf);
    } else if (gLcdImageStored && displayLcdImageFromFlash()) {
      hasStoredImage = true;
    } else {
      showLCDPlaceholder();
    }
    if (gLcdImageStored) {
      releaseLcdImageBuffer();
    }
    char ack[96];
    snprintf(ack, sizeof(ack), "ACKLCD seq=%ld ok=1 state=on lcd_image=%d\n", seq, hasStoredImage ? 1 : 0);
    sendRawToPi(ack);
    return;
  }

  if (strEqNoCase(cmdBuf, "off")) {
    setLcdPower(false);
    char ack[96];
    snprintf(ack, sizeof(ack), "ACKLCD seq=%ld ok=1 state=off\n", seq);
    sendRawToPi(ack);
    return;
  }

  char ack[96];
  snprintf(ack, sizeof(ack), "ACKLCD seq=%ld ok=0 err=badcmd\n", seq);
  sendRawToPi(ack);
}

// ================= SAVE / LOAD =================
static void saveStateToFlash() {
  prefs.begin("epdstate", false);
  prefs.putBytes("data", &gData, sizeof(gData));
  prefs.putBytes("hls", &gHighlights, sizeof(gHighlights));
  prefs.end();
  Serial.println("[FLASH] state saved");
}

static bool loadStateFromFlash() {
  prefs.begin("epdstate", true);

  size_t dataLen = prefs.getBytesLength("data");
  size_t hlsLen = prefs.getBytesLength("hls");
  bool ok = false;

  if (dataLen == sizeof(gData)) {
    prefs.getBytes("data", &gData, sizeof(gData));
    ok = true;
  }

  if (hlsLen == sizeof(gHighlights)) {
    prefs.getBytes("hls", &gHighlights, sizeof(gHighlights));
  } else {
    clearHighlights();
  }

  lastAppliedSeq = -1;
  prefs.end();
  return ok;
}

static void loadNetworkConfig() {
  prefs.begin("epdnet", true);
  String ssid = prefs.getString("ssid", DEFAULT_WIFI_SSID);
  String pass = prefs.getString("pass", DEFAULT_WIFI_PASS);
  String host = prefs.getString("pi", DEFAULT_PI_HOST);
  uint16_t port = prefs.getUShort("port", DEFAULT_PI_PORT);
  prefs.end();

  ssid.toCharArray(gWifiSsid, sizeof(gWifiSsid));
  pass.toCharArray(gWifiPass, sizeof(gWifiPass));
  host.toCharArray(gPiHost, sizeof(gPiHost));
  gPiPort = port ? port : DEFAULT_PI_PORT;
}

static void startWifiAttempt(const char* reason);

static void saveNetworkConfig(const char* ssid, const char* pass, const char* piHost, uint16_t piPort) {
  prefs.begin("epdnet", false);
  prefs.putString("ssid", ssid ? ssid : "");
  prefs.putString("pass", pass ? pass : "");
  prefs.putString("pi", piHost ? piHost : DEFAULT_PI_HOST);
  prefs.putUShort("port", piPort ? piPort : DEFAULT_PI_PORT);
  prefs.end();
}

static void printNetworkConfig() {
  Serial.print("WWCFG id=");
  Serial.print(DEVICE_ID);
  Serial.print(" ssid=");
  Serial.print(gWifiSsid);
  Serial.print(" pi=");
  Serial.print(gPiHost);
  Serial.print(" port=");
  Serial.print(gPiPort);
  Serial.print(" wifi=");
  Serial.print(WiFi.status() == WL_CONNECTED ? "connected" : "offline");
  Serial.print(" ip=");
  Serial.println(WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : "-");
}

static void prepareWifiRadioForConfig() {
  if (client.connected()) client.stop();
  gPiSessionOnline = false;

  WiFi.setAutoReconnect(false);
  WiFi.scanDelete();
  WiFi.disconnect(false, false);
  delay(150);
  WiFi.mode(WIFI_OFF);
  delay(250);
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setSleep(false);
  WiFi.setHostname(DEVICE_ID);

  gLastWifiAttemptMs = 0;
  gLastPiAttemptMs = 0;
  gLastWifiStatus = WiFi.status();
}

static void scanWifiForSerial() {
  Serial.println("WWSCAN begin=1");
  prepareWifiRadioForConfig();
  int count = WiFi.scanNetworks(false, true);
  if (count < 0) {
    delay(350);
    WiFi.scanDelete();
    count = WiFi.scanNetworks(false, true);
  }
  if (count < 0) {
    Serial.println("WWERR scan_failed");
    return;
  }
  for (int i = 0; i < count; i++) {
    Serial.print("WWSSID index=");
    Serial.print(i);
    Serial.print(" ssid=");
    Serial.print(WiFi.SSID(i));
    Serial.print(" rssi=");
    Serial.print(WiFi.RSSI(i));
    Serial.print(" enc=");
    Serial.println((int)WiFi.encryptionType(i));
  }
  Serial.print("WWEND count=");
  Serial.println(count);
}

static void handleSerialCommandLine(char* line) {
  if (!line || !line[0]) return;
  while (*line == ' ') line++;

  if (strcmp(line, "WWCFG?") == 0) {
    printNetworkConfig();
    return;
  }

  if (strcmp(line, "WWSCAN") == 0) {
    scanWifiForSerial();
    return;
  }

  if (strncmp(line, "WWSET ", 6) == 0) {
    char ssid[64] = {0};
    char pass[96] = {0};
    char pi[64] = {0};
    char portBuf[12] = {0};

    if (!getTokenValue(line, "ssid", ssid, sizeof(ssid))) {
      Serial.println("WWERR missing_ssid");
      return;
    }
    getTokenValue(line, "pass", pass, sizeof(pass));
    if (!getTokenValue(line, "pi", pi, sizeof(pi))) {
      strncpy(pi, DEFAULT_PI_HOST, sizeof(pi) - 1);
    }
    getTokenValue(line, "port", portBuf, sizeof(portBuf));

    decodePercentInPlace(ssid);
    decodePercentInPlace(pass);
    decodePercentInPlace(pi);
    uint16_t port = (uint16_t)atoi(portBuf);
    if (!port) port = DEFAULT_PI_PORT;

    prepareWifiRadioForConfig();
    saveNetworkConfig(ssid, pass, pi, port);
    strncpy(gWifiSsid, ssid, sizeof(gWifiSsid) - 1);
    gWifiSsid[sizeof(gWifiSsid) - 1] = '\0';
    strncpy(gWifiPass, pass, sizeof(gWifiPass) - 1);
    gWifiPass[sizeof(gWifiPass) - 1] = '\0';
    strncpy(gPiHost, pi, sizeof(gPiHost) - 1);
    gPiHost[sizeof(gPiHost) - 1] = '\0';
    gPiPort = port;
    Serial.println("WWOK saved=1 reconnecting=1");
    Serial.flush();
    startWifiAttempt("provisioned credentials");
    return;
  }

  Serial.println("WWERR unknown_command");
}

static void handleSerialProvisioning() {
  static char line[256];
  static size_t len = 0;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line[len] = '\0';
      handleSerialCommandLine(line);
      len = 0;
      continue;
    }
    if (len < sizeof(line) - 1) {
      line[len++] = c;
    }
  }
}

static void announceSerialProvisioningReady() {
  Serial.print("WWREADY id=");
  Serial.print(DEVICE_ID);
  Serial.print(" fw=");
  Serial.print(FIRMWARE_VERSION);
  Serial.print(" ssid=");
  Serial.print(gWifiSsid);
  Serial.print(" pi=");
  Serial.print(gPiHost);
  Serial.print(" port=");
  Serial.println(gPiPort);
}

static void serviceSerialProvisioningWindow(uint32_t windowMs) {
  announceSerialProvisioningReady();
  uint32_t start = millis();
  while (millis() - start < windowMs) {
    handleSerialProvisioning();
    delay(20);
  }
}

// ================= NETWORK =================
static void startWifiAttempt(const char* reason) {
  Serial.print("[WIFI] connect attempt: ");
  Serial.println(reason);
  prepareWifiRadioForConfig();
  WiFi.setAutoReconnect(true);
  WiFi.begin(gWifiSsid, gWifiPass);
  gLastWifiAttemptMs = millis();
}

static bool maintainWiFi() {
  wl_status_t status = WiFi.status();

  if (status != gLastWifiStatus) {
    gLastWifiStatus = status;
    Serial.print("[WIFI] status=");
    Serial.println((int)status);
    if (status != WL_CONNECTED) {
      if (client.connected()) client.stop();
      gPiSessionOnline = false;
    }
  }

  if (status == WL_CONNECTED) {
    return true;
  }

  if (
    gLastWifiAttemptMs != 0 &&
    millis() - gLastWifiAttemptMs < WIFI_CONNECT_GRACE_MS &&
    (status == WL_IDLE_STATUS || status == WL_DISCONNECTED)
  ) {
    return false;
  }

  if (gLastWifiAttemptMs == 0 || millis() - gLastWifiAttemptMs >= WIFI_RETRY_MS) {
    startWifiAttempt("offline retry");
  }

  return false;
}

static bool waitForWiFi(uint32_t maxWaitMs) {
  stage("wifi: wait");
  unsigned long start = millis();
  startWifiAttempt("boot");
  while (WiFi.status() != WL_CONNECTED && millis() - start < maxWaitMs) {
    handleSerialProvisioning();
    maintainWiFi();
    delay(100);
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WIFI] boot wait timed out; continuing with background reconnect");
    return false;
  }
  Serial.println("[WIFI] connected");
  return true;
}

static bool connectToPi() {
  if (client.connected()) return true;
  if (gPiSessionOnline) {
    Serial.println("[TCP] Pi session lost; reconnecting");
  }
  gPiSessionOnline = false;

  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }

  if (gLastPiAttemptMs != 0 && millis() - gLastPiAttemptMs < PI_RETRY_MS) {
    return false;
  }
  gLastPiAttemptMs = millis();

  stage("tcp: connect");
  client.setTimeout(1);

  if (!client.connect(gPiHost, gPiPort)) {
    Serial.println("Pi connection failed.");
    stage("tcp: failed");
    return false;
  }

  char hello[96];
  snprintf(hello, sizeof(hello), "HELLO id=%s fw=%u\n", DEVICE_ID, (unsigned)FIRMWARE_VERSION);
  if (!sendRawToPi(hello)) {
    stage("tcp: hello failed");
    return false;
  }

  Serial.println("Connected to Pi. Sent HELLO.");
  gPiSessionOnline = true;
  lastAppliedSeq = -1;
  stage("tcp: connected");
  return true;
}

// ================= TEXT UPDATE HANDLER =================
static bool applyUpdateLine(const char* line, long* outSeq) {
  char seqBuf[16];
  if (!getTokenValue(line, "seq", seqBuf, sizeof(seqBuf))) return false;

  long seq = atol(seqBuf);
  if (seq <= 0) return false;
  *outSeq = seq;

  DisplayData nd = gData;
  char tmp[512];

  if (getTokenValue(line, "name", tmp, sizeof(tmp))) {
    decodeUnderscore(tmp);
    strncpy(nd.name, tmp, sizeof(nd.name) - 1);
    nd.name[sizeof(nd.name) - 1] = '\0';
  }
  if (getTokenValue(line, "room", tmp, sizeof(tmp))) {
    decodeUnderscore(tmp);
    strncpy(nd.room, tmp, sizeof(nd.room) - 1);
    nd.room[sizeof(nd.room) - 1] = '\0';
  }
  if (getTokenValue(line, "diet", tmp, sizeof(tmp))) { splitPipeToList(tmp, nd.diet, &nd.dietCount, 8); }
  if (getTokenValue(line, "texture", tmp, sizeof(tmp)) || getTokenValue(line, "allergies", tmp, sizeof(tmp))) {
    splitPipeToList(tmp, nd.texture, &nd.textureCount, 8);
  }
  if (getTokenValue(line, "fluids", tmp, sizeof(tmp)) || getTokenValue(line, "schedule", tmp, sizeof(tmp))) {
    splitPipeToList(tmp, nd.fluids, &nd.fluidsCount, 8);
  }
  if (getTokenValue(line, "note", tmp, sizeof(tmp))) {
    decodeUnderscore(tmp);
    strncpy(nd.note, tmp, sizeof(nd.note) - 1);
    nd.note[sizeof(nd.note) - 1] = '\0';
  }
  if (getTokenValue(line, "drinks", tmp, sizeof(tmp))) {
    decodeUnderscore(tmp);
    strncpy(nd.drinks, tmp, sizeof(nd.drinks) - 1);
    nd.drinks[sizeof(nd.drinks) - 1] = '\0';
  }

  if (getTokenValue(line, "hl", tmp, sizeof(tmp))) {
    parseHighlights(tmp);
  } else {
    clearHighlights();
  }

  gData = nd;
  return true;
}

static void handleUpdateLine(const char* line) {
  long seq = -1;
  if (!applyUpdateLine(line, &seq)) {
    char ack[96];
    snprintf(ack, sizeof(ack), "ACK seq=%ld ok=0 err=parse\n", seq);
    sendRawToPi(ack);
    return;
  }

  if (seq == lastAppliedSeq) {
    char ack[96];
    snprintf(ack, sizeof(ack), "ACK seq=%ld ok=1 dup=1\n", seq);
    sendRawToPi(ack);
    return;
  }

  displayFromData(gData);
  lastAppliedSeq = seq;
  saveStateToFlash();

  char ack[96];
  snprintf(ack, sizeof(ack), "ACK seq=%ld ok=1\n", seq);
  sendRawToPi(ack);
  Serial.printf("[ACK] sent seq=%ld\n", seq);
}

static void pollPiMessages() {
  if (!client.connected()) return;

  while (client.available()) {
    String s = client.readStringUntil('\n');
    s.trim();
    if (!s.length()) continue;

    Serial.print("RX: ");
    Serial.println(s);

    if (s.startsWith("UPDATE ")) {
      char line[768];
      s.toCharArray(line, sizeof(line));
      handleUpdateLine(line);
    } else if (s.startsWith("IMAGE ")) {
      char line[128];
      s.toCharArray(line, sizeof(line));
      handleImageLine(line);
    } else if (s.startsWith("LCD ")) {
      char line[128];
      s.toCharArray(line, sizeof(line));
      handleLcdLine(line);
    } else if (s == "PING") {
      sendRawToPi("PONG\n");
    }
  }
}

static void sendStatusIfDue() {
  static unsigned long lastStatusMs = 0;
  if (!client.connected()) return;
  if (millis() - lastStatusMs < STATUS_INTERVAL_MS) return;
  lastStatusMs = millis();

  BatteryTelemetry battery = readBatteryTelemetry();
  String ipText = WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : "-";
  char line[320];
  snprintf(
    line,
    sizeof(line),
    "STATUS battery=%d battery_ok=%d battery_mv=%d battery_raw_x10=%d battery_low=%d battery_alert=%d battery_plugged=%d battery_charging=%d battery_full=%d heap=%u wifi=%s ip=%s rssi=%d lcd_image=%d uptime_ms=%lu\n",
    battery.percent,
    battery.ok ? 1 : 0,
    battery.millivolts,
    battery.rawPercentX10,
    battery.low ? 1 : 0,
    battery.alertPinLow ? 1 : 0,
    battery.usbPresent ? 1 : 0,
    battery.charging ? 1 : 0,
    battery.full ? 1 : 0,
    (unsigned)ESP.getFreeHeap(),
    WiFi.status() == WL_CONNECTED ? "connected" : "offline",
    ipText.c_str(),
    WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0,
    (lcdImageBuf || gLcdImageStored) ? 1 : 0,
    (unsigned long)millis()
  );
  sendRawToPi(line);
}

// ================= DEFAULT SAMPLE =================
static void loadSampleData() {
  memset(&gData, 0, sizeof(gData));
  clearHighlights();

  strncpy(gData.name, "GOUTHAM KRISHNA", sizeof(gData.name) - 1);
  strncpy(gData.room, "29-2", sizeof(gData.room) - 1);
  gData.dietCount = 3;
  strncpy(gData.diet[0], "MECHANICAL SOFT", 31);
  strncpy(gData.diet[1], "LOW SODIUM", 31);
  strncpy(gData.diet[2], "DIABETIC", 31);
  gData.textureCount = 1;
  strncpy(gData.texture[0], "MINCED AND MOIST", 31);
  gData.fluidsCount = 1;
  strncpy(gData.fluids[0], "MILDLY THICK", 31);
  strncpy(gData.note, "NO FISH", sizeof(gData.note) - 1);
  strncpy(gData.drinks, "COFFEE", sizeof(gData.drinks) - 1);
}

// ================= SETUP / LOOP =================
void setup() {
  Serial.begin(115200);
  delay(400);

  makeDeviceId();
  loadNetworkConfig();
  Serial.print("DEVICE_ID: ");
  Serial.println(DEVICE_ID);
  printNetworkConfig();
  serviceSerialProvisioningWindow(5000);

  printHeap("boot");
  initBatteryMonitor();

  // LCD init
  pinMode(LCD_CS_PIN, OUTPUT);
  digitalWrite(LCD_CS_PIN, HIGH);
  pinMode(EPD_CS_PIN, OUTPUT);
  digitalWrite(EPD_CS_PIN, HIGH);
  pinMode(LCD_BL_PIN, OUTPUT);
  setLcdPower(false);
  initLCD();
  initPersistentFileSystem();
  if (displayLcdImageFromFlash()) {
    releaseLcdImageBuffer();
  } else {
    Serial.println("[LCD] no stored image; LCD backlight kept off");
  }

  // E-paper init memory
  stage("setup: DEV_Module_Init");
  DEV_Module_Init();

  const uint32_t bufBytes = ((uint32_t)DISPLAY_WIDTH * (uint32_t)DISPLAY_HEIGHT) / 2;
  Serial.print("Framebuffer bytes (scale=6): ");
  Serial.println(bufBytes);

  stage("setup: load saved or sample");
  if (!loadStateFromFlash()) {
    Serial.println("[FLASH] no saved state, using sample");
    loadSampleData();
    saveStateToFlash();
  } else {
    Serial.println("[FLASH] loaded saved state");
  }

  stage("setup: connectWiFi");
  waitForWiFi(30000);

  stage("setup: connectToPi");
  connectToPi();

  stage("setup: first epaper display");
  displayFromData(gData);

  stage("setup: done");
  Serial.println("Waiting for updates...");
}

void loop() {
  handleSerialProvisioning();
  if (!maintainWiFi()) {
    delay(100);
    return;
  }
  if (!connectToPi()) {
    delay(100);
    return;
  }

  pollPiMessages();
  sendStatusIfDue();
  delay(20);
}
