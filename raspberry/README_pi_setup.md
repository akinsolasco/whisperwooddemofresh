# Whisperwood Raspberry Pi Setup

This folder contains the deployable Raspberry Pi services for Whisperwood:

- `control/app.py`: public Control Service used by the desktop app on port `7000`.
- `operation/app.py`: local Operation Manager API on port `8000` and ESP32 TCP gateway on port `5000`.
- `install_whisperwood_pi.sh`: installs the same layout on another Raspberry Pi.

## Current Production Shape

The checked Pi configuration uses:

- OS: Debian/Raspberry Pi OS arm64
- Python: `3.13`
- Service user: `whisperwood`
- Control Service: `/opt/whisperwood/control`, public port `7000`
- Operation Manager: `/opt/whisperwood/operation`, local API port `8000`
- ESP32 TCP gateway: public port `5000`
- Download site: `/opt/whisperwood-download-site`, public port `8090`
- Data folders: `/opt/whisperwood/data/documents` and `/opt/whisperwood/data/images`
- Firewall: UFW allows `22/tcp`, `5000/tcp`, `7000/tcp`, and `8090/tcp`
- Battery telemetry: ESP32 reports MAX17048 battery/charger status through Operation Manager `/devices`; Control Service persists it and desktop IT Admin controls popup alert policy through `/battery-alert-settings`.

Secrets are not stored in this repo. They must be supplied when installing.

## Install On Another Pi

Copy or clone this repository onto the new Raspberry Pi, then run from the repo root:

```bash
sudo CONTROL_API_KEY="your-control-api-key" \
  DATABASE_URL="postgresql+psycopg2://user:password@database-host:5432/whisperwood_control" \
  bash raspberry/install_whisperwood_pi.sh
```

If you omit `CONTROL_API_KEY`, the script generates one and prints it at the end. Use that key in the desktop app Control Service profile.

## Optional Settings

```bash
sudo SERVICE_USER="whisperwood" \
  CONTROL_PORT="7000" \
  OPERATION_HTTP_PORT="8000" \
  ESP32_TCP_PORT="5000" \
  DOWNLOAD_PORT="8090" \
  INSTALL_DOWNLOAD_SITE="1" \
  ENABLE_UFW="1" \
  HOSTNAME_VALUE="Whisperwood-EPD" \
  CONTROL_API_KEY="your-control-api-key" \
  DATABASE_URL="postgresql+psycopg2://user:password@database-host:5432/whisperwood_control" \
  bash raspberry/install_whisperwood_pi.sh
```

## Verify

```bash
systemctl is-active whisperwood-control whisperwood-operation whisperwood-download-site
ss -ltnp | grep -E ':(5000|7000|8000|8090)\b'
curl http://127.0.0.1:8000/health
curl -H "X-Whisperwood-Key: your-control-api-key" http://127.0.0.1:7000/health
```

From a Windows laptop on the same network:

```powershell
Test-NetConnection <pi-ip> -Port 7000
Test-NetConnection <pi-ip> -Port 5000
Test-NetConnection <pi-ip> -Port 8090
```

## Notes

- The script backs up existing `app.py` files before replacing them.
- It does not create or wipe PostgreSQL databases.
- Resident records remain in the configured PostgreSQL database.
- The Operation Manager currently does not require direct internet access; the download site does need GitHub access to refresh the latest installer.
