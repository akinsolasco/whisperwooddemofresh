# Raspberry Pi Operation Changes

## Battery telemetry and alert policy

- ESP32 firmware now reports MAX17048 battery data in the normal `STATUS` heartbeat:
  - `battery`
  - `battery_ok`
  - `battery_mv`
  - `battery_raw_x10`
  - `battery_low`
  - `battery_alert`
  - `battery_plugged`
  - `battery_charging`
  - `battery_full`
- Operation Manager stores that telemetry per live connection and exposes it through `/devices`.
- Control Service syncs and persists the latest battery state on the `devices` table with additive columns only.
- Control Service adds `/battery-alert-settings` so IT Admin can control popup threshold, critical threshold, cooldown, and recipient roles.
- Desktop IT Admin > Devices now shows battery/power state and can save the shared alert policy.

## Verified smart-label pin map

- Shared SPI bus:
  - SCLK: IO12
  - MOSI: IO11
- E-paper:
  - CS: IO15
  - DC: IO14
  - RST: IO16
  - BUSY: IO17
  - PWR: IO18
- LCD:
  - CS: IO10
  - DC: IO9
  - RST: IO8
  - BL: IO13
- Battery/charger:
  - MAX17048 SDA: IO2
  - MAX17048 SCL: IO3
  - LOW_ALERT: IO4
  - PLUG_IND: IO6
  - CHG_IND: IO7
  - Full LED: IO47
  - Low LED: IO48

## LCD photo persistence and resync

- Operation Manager now caches the last successfully ACKed LCD RGB565 image per ESP32 device in:
  `/opt/whisperwood/data/lcd_images/<device_id>.rgb565`
- If an ESP32 reports `lcd_image=0` in its `STATUS` line and the Pi has a cached image, the Pi queues a background resend.
- Resident Save and Pairing now send e-paper text only.
- LCD resident photos are sent manually from the desktop "Send Photo Only" action.
- Operation Manager rejects LCD photo uploads while e-paper text is still pending/busy, and rejects text while a photo upload is pending.
- ESP32 firmware reports `epaper_busy` in `STATUS` so the Pi and desktop can avoid overlapping screen jobs.

## Online/offline detection

- Operation Manager keeps last-known devices after disconnect instead of removing them from `/devices`.
- Devices are reported with:
  - `is_online`
  - `status`
  - `connection_state`
  - `last_seen_s`
  - `last_seen_at`
  - `offline_reason`
  - `rssi`
  - `heap`
  - `uptime_ms`
  - `lcd_image_cached`
- Default heartbeat interval is 5 seconds.
- Default online timeout is 30 seconds.

## Files changed for the Pi

- `raspberry/operation/app.py`
- `raspberry/control/app.py`

## Apply to another Pi

Copy the updated files into the Pi install location, then restart:

```bash
sudo install -m 644 raspberry/operation/app.py /opt/whisperwood/operation/app.py
sudo install -m 644 raspberry/control/app.py /opt/whisperwood/control/app.py
sudo systemctl restart whisperwood-operation
sudo systemctl restart whisperwood-control
```

Verify:

```bash
systemctl is-active whisperwood-operation whisperwood-control
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/devices
```
