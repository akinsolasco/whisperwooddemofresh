#ifndef WHISPERWOOD_BATTERY_TELEMETRY_H
#define WHISPERWOOD_BATTERY_TELEMETRY_H

struct BatteryTelemetry {
  bool ok;
  int percent;
  int rawPercentX10;
  int millivolts;
  bool low;
  bool alertPinLow;
  bool usbPresent;
  bool charging;
  bool full;
};

#endif
