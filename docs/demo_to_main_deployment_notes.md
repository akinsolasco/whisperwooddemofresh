# Demo To Main Deployment Notes

Current demo checkpoint: `demo-v2.1.17`.

## LCD Schedule Scope

- LCD Schedule must keep server-backed manual LCD ON/OFF controls.
- Manual LCD ON/OFF targets all connected LCD devices with `device_id=all`.
- Global LCD Schedule remains server-backed through the Control Service and Operation Manager.
- The schedule section is global only: no resident-specific photo upload or photo-send action belongs there.

## Resident Photo Scope

- Resident photos are attached in Resident Records.
- Resident photos are sent from Resident Records with the `Send Photo` button.
- LCD Schedule must not display resident photo file paths, filenames, or attachment links.
- Saving a resident sends text only; photo sending is a separate deliberate action.

## Pi Backend Support Required On Main

- Control Service keeps `/operation/lcd` and forwards LCD ON/OFF commands to the Operation Manager.
- Control Service keeps `/operation/schedule` and stores the global schedule in the backend database.
- Operation Manager keeps `/lcd`, `/schedule`, and `send_lcd_to_target("all", command)`.
- Operation Manager keeps the LCD/e-paper busy guards so LCD photo transfer is rejected while e-paper is busy.
- Operation Manager keeps device status telemetry for online/offline, pending text/image/LCD jobs, RSSI, heap, and cached LCD image state.

## ESP32 Firmware Required On Main

- Use firmware `fw=17` or newer.
- Keep the e-paper framebuffer clear order: `Paint_SetScale(6)` before `Paint_Clear(...)`.
- Keep LCD photo ACK behavior: only ACK success after the image is persisted and drawn.
- Keep status messages reporting `lcd_image`, `epaper_busy`, WiFi state, RSSI, heap, and battery fields.

## Data Safety

- Main deployment must not drop, truncate, or recreate resident tables.
- Main deployment should migrate code and schema additions only.
- Existing resident records, documents, resident photos, dropdown options, users, audit records, and device pairings must remain intact.
