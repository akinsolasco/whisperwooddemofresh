#include <SPI.h>
#include <WiFi.h>
#include <Preferences.h>
#include <FS.h>
#include <LittleFS.h>
#include "esp_heap_caps.h"
#include <LovyanGFX.hpp>

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
static const uint32_t WIFI_RETRY_MS = 5000;
static const uint32_t PI_RETRY_MS = 3000;
static const uint32_t STATUS_INTERVAL_MS = 5000;

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

// ================= LCD FROM YOUR UPLOADED CODE =================
#define LCD_BL_PIN 48

class LGFX : public lgfx::LGFX_Device {
  lgfx::Panel_ILI9341 _panel;
  lgfx::Bus_SPI _bus;

public:
  LGFX() {
    auto cfg = _bus.config();
    cfg.spi_host = SPI3_HOST;  // separate from e-paper bus
    cfg.spi_mode = 0;
    cfg.freq_write = 10000000;
    cfg.freq_read = 6000000;
    cfg.pin_sclk = 18;  // LCD SCK
    cfg.pin_mosi = 17;  // LCD MOSI
    cfg.pin_miso = -1;  // not used
    cfg.pin_dc = 15;    // LCD DC
    _bus.config(cfg);
    _panel.setBus(&_bus);

    auto pcfg = _panel.config();
    pcfg.pin_cs = 16;   // LCD CS
    pcfg.pin_rst = 21;  // LCD RST
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
  Paint_DrawRectangle(x, y, x + w, y + h, bg, DOT_PIXEL_1X1, DRAW_FILL_FULL);
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
    while (*ptr == ' ') {
      int sw = getCharWidth(' ', font);
      if (*cx + sw > maxX) {
        *cx = wrapX;
        *cy += font->Height + LINE_SPACING;
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

// ================= E-PAPER DISPLAY =================
static void displayFromData(const DisplayData& d) {
  stage("epaper: start");
  printHeap("before epaper");

  EPD_3IN6E_Init();
  EPD_3IN6E_Clear(EPD_3IN6E_WHITE);

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

  y = renderSection("NAME", d.name, SEC_NAME, startX, y, maxX, &Font48);
  y = renderSection("ROOM", d.room, SEC_ROOM, startX, y, maxX, &Font48);
  y = renderSection("DIET", dietLine, SEC_DIET, startX, y, maxX, &Font48);
  y = renderSection("TEXTURE", textureLine, SEC_TEXTURE, startX, y, maxX, &Font48);
  y = renderSection("FLUIDS", fluidsLine, SEC_FLUIDS, startX, y, maxX, &Font48);
  y = renderSection("NOTE", d.note, SEC_NOTE, startX, y, maxX, &Font48);
  y = renderSection("DRINKS", d.drinks, SEC_DRINKS, startX, y, maxX, &Font48);

  stage("epaper: display");
  EPD_3IN6E_Display(ImageBuffer);
  EPD_3IN6E_Sleep();

  stage("epaper: done");
  printHeap("after epaper");
  delay(200);
}

// ================= LCD DISPLAY =================
static void initLCD() {
  stage("lcd: init");
  tft.init();
  tft.setRotation(1);
  tft.setSwapBytes(true);
  tft.fillScreen(TFT_BLACK);
}

static void showLCDPlaceholder() {
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextSize(2);
  tft.setCursor(20, 20);
  tft.print("LCD Ready");
}

static void setLcdPower(bool on) {
  lcdPowerOn = on;
  digitalWrite(LCD_BL_PIN, on ? HIGH : LOW);
}

static bool ensureLcdImageBuffer() {
  if (lcdImageBuf) return true;
  lcdImageBuf = (uint16_t*)ps_malloc(LCD_IMG_BYTES);
  if (!lcdImageBuf) {
    Serial.println("[LCD] PSRAM image buffer allocation failed");
    return false;
  }
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

static void displayLCDImage565(const uint16_t* img565) {
  stage("lcd: pushImage");
  tft.pushImage(0, 0, LCD_IMG_W, LCD_IMG_H, img565);
  stage("lcd: done");
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
    char ack[96];
    snprintf(ack, sizeof(ack), "ACKIMG seq=%ld ok=0 err=size\n", seq);
    sendRawToPi(ack);
    return;
  }

  if (!ensureLcdImageBuffer()) {
    char ack[96];
    snprintf(ack, sizeof(ack), "ACKIMG seq=%ld ok=0 err=nomem\n", seq);
    sendRawToPi(ack);
    return;
  }

  Serial.printf("[LCD] expecting %ld bytes\n", size);
  bool ok = receiveExactBytes((uint8_t*)lcdImageBuf, (size_t)size, 15000);
  if (!ok) {
    char ack[96];
    snprintf(ack, sizeof(ack), "ACKIMG seq=%ld ok=0 err=rx\n", seq);
    sendRawToPi(ack);
    return;
  }

  bool persisted = saveLcdImageToFlash();
  if (lcdPowerOn) {
    displayLCDImage565(lcdImageBuf);
  }
  char ack[128];
  snprintf(ack, sizeof(ack), "ACKIMG seq=%ld ok=1 persisted=%d\n", seq, persisted ? 1 : 0);
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
    setLcdPower(true);
    if (!lcdImageBuf && gLcdImageStored) {
      loadLcdImageFromFlash();
    }
    if (lcdImageBuf) {
      displayLCDImage565(lcdImageBuf);
    } else {
      showLCDPlaceholder();
    }
    char ack[96];
    snprintf(ack, sizeof(ack), "ACKLCD seq=%ld ok=1 state=on lcd_image=%d\n", seq, lcdImageBuf ? 1 : 0);
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

static void scanWifiForSerial() {
  Serial.println("WWSCAN begin=1");
  int count = WiFi.scanNetworks(false, true);
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

    saveNetworkConfig(ssid, pass, pi, port);
    strncpy(gWifiSsid, ssid, sizeof(gWifiSsid) - 1);
    gWifiSsid[sizeof(gWifiSsid) - 1] = '\0';
    strncpy(gWifiPass, pass, sizeof(gWifiPass) - 1);
    gWifiPass[sizeof(gWifiPass) - 1] = '\0';
    strncpy(gPiHost, pi, sizeof(gPiHost) - 1);
    gPiHost[sizeof(gPiHost) - 1] = '\0';
    gPiPort = port;
    client.stop();
    gPiSessionOnline = false;
    WiFi.disconnect(false, false);
    gLastWifiAttemptMs = 0;
    gLastPiAttemptMs = 0;
    Serial.println("WWOK saved=1 reconnecting=1");
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
  Serial.print(" fw=4 ssid=");
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
static void configureWifiStation() {
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setSleep(false);
  WiFi.setHostname(DEVICE_ID);
  WiFi.setAutoReconnect(true);
}

static void startWifiAttempt(const char* reason) {
  configureWifiStation();
  Serial.print("[WIFI] connect attempt: ");
  Serial.println(reason);
  WiFi.disconnect(false, false);
  delay(50);
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
  Serial.println("\nWiFi connected!");
  Serial.print("IP: ");
  Serial.println(WiFi.localIP());
  Serial.print("Hostname: ");
  Serial.println(WiFi.getHostname());
  stage("wifi: connected");
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
  snprintf(hello, sizeof(hello), "HELLO id=%s fw=4\n", DEVICE_ID);
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

  String ipText = WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : "-";
  char line[192];
  snprintf(
    line,
    sizeof(line),
    "STATUS battery=-1 heap=%u wifi=%s ip=%s rssi=%d lcd_image=%d uptime_ms=%lu\n",
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

  // LCD init
  pinMode(LCD_BL_PIN, OUTPUT);
  setLcdPower(true);
  initLCD();
  initPersistentFileSystem();
  if (loadLcdImageFromFlash()) {
    displayLCDImage565(lcdImageBuf);
  } else {
    showLCDPlaceholder();
  }

  // E-paper init memory
  stage("setup: DEV_Module_Init");
  DEV_Module_Init();

  const uint32_t bufBytes = ((uint32_t)DISPLAY_WIDTH * (uint32_t)DISPLAY_HEIGHT) / 2;
  Serial.print("Framebuffer bytes (scale=6): ");
  Serial.println(bufBytes);

  ImageBuffer = (UBYTE*)heap_caps_malloc(bufBytes, MALLOC_CAP_DMA | MALLOC_CAP_8BIT);
  if (!ImageBuffer) {
    Serial.println("ERROR: DMA framebuffer alloc failed.");
    while (1) delay(1000);
  }

  stage("setup: Paint_NewImage");
  Paint_NewImage(ImageBuffer, DISPLAY_WIDTH, DISPLAY_HEIGHT, 0, EPD_3IN6E_WHITE);
  Paint_SetScale(6);

  stage("setup: load saved or sample");
  if (!loadStateFromFlash()) {
    Serial.println("[FLASH] no saved state, using sample");
    loadSampleData();
    saveStateToFlash();
  } else {
    Serial.println("[FLASH] loaded saved state");
  }

  stage("setup: first epaper display");
  displayFromData(gData);

  stage("setup: connectWiFi");
  waitForWiFi(30000);

  stage("setup: connectToPi");
  connectToPi();

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
