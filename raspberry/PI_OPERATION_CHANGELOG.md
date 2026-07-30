# Raspberry Pi Operation Changes

## LCD photo persistence and resync

- Operation Manager now caches the last successfully ACKed LCD RGB565 image per ESP32 device in:
  `/opt/whisperwood/data/lcd_images/<device_id>.rgb565`
- If an ESP32 reports `lcd_image=0` in its `STATUS` line and the Pi has a cached image, the Pi queues a background resend.
- Normal resident display flow remains text first, then photo after text ACK.

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
