# Demo Transfer Notes

Last updated: 2026-07-30

## Verified on Demo Pi

- Global LCD schedule endpoint works through the full Control Service path:
  - `POST /operation/schedule`
  - saved as `device_id: all`
  - mirrored into Control `/schedules`
- A harmless disabled schedule test was used:
  - `enabled: false`
  - `lcd_on_time: 07:00`
  - `lcd_off_time: 20:00`
  - `sleep_if_no_image: true`
- ESP32 device telemetry reports online/offline through Operation Manager status/PONG/STATUS messages.

## Demo Software Changes

- User-facing network/server errors are normalized in `core/control_service_client.py`.
- Backend button actions now show immediate busy text and repaint before network calls.
- USB ESP32 WiFi provisioning now gives visible inline status plus clear popup messages.
- Global LCD schedule save no longer silently reports success from a database-only fallback if Operation Manager fails.

## ESP32 Demo Firmware Changes

- Firmware identity bumped to `fw=5`.
- WiFi boot logging avoids the hostname/IP print path that triggered crashes on the current board.
- LCD backlight stays off at boot if no saved image exists.

## Transfer Rules for Main

- Transfer source code only.
- Do not copy demo database files, resident records, uploaded resident images, logs, API keys, or local app settings into the live deployment.
- Apply main Pi credentials/configuration separately on the live machine.
- Before pushing main, compare demo source against main and review only intentional UI, error-handling, Operation Manager, and ESP32 firmware changes.
