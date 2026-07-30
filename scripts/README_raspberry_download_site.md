# Raspberry Pi Download Site

This provides a local-network page where staff can download the latest Windows desktop installer.

## Quick Run

Copy the whole `scripts` folder to the Raspberry Pi, then run:

```bash
python3 scripts/raspberry_download_site.py
```

The terminal prints a simple link like:

```text
http://whisperwood-epd.local:8090/download/
```

It also prints an IP fallback link like:

```text
http://192.168.2.37:8090/download/
```

Share the link with staff while their computer is connected to the same network.

Open the link exactly as printed. If the service prints `http://`, do not change it to `https://`.

## Install As A Service

From the repo folder on the Raspberry Pi:

```bash
sudo bash scripts/install_raspberry_download_site.sh
```

Then view the generated download link:

```bash
journalctl -u whisperwood-download-site -n 50 --no-pager
```

The install script copies the Enhanced Living Whisperwood logo automatically from `scripts/assets`.

## Useful Commands

Restart:

```bash
sudo systemctl restart whisperwood-download-site
```

Stop:

```bash
sudo systemctl stop whisperwood-download-site
```

Use a private-looking random link instead of `/download/`:

```bash
python3 raspberry_download_site.py --unique-link
```

Rotate that private-looking link:

```bash
python3 raspberry_download_site.py --unique-link --rotate-link
```

Use a different port:

```bash
python3 raspberry_download_site.py --port 8088
```

Use a simple hostname link:

```bash
python3 raspberry_download_site.py --use-hostname
```

If `hostname.local` does not resolve from Windows, use the IP fallback link printed by the script.

## HTTPS

For encrypted HTTPS with a self-signed certificate:

```bash
python3 raspberry_download_site.py --generate-self-signed
```

As a service:

```bash
sudo ENABLE_HTTPS=1 bash scripts/install_raspberry_download_site.sh
```

Browsers will warn for self-signed certificates unless that certificate is trusted on each staff computer.

For proper trusted SSL, provide a certificate and key:

```bash
python3 raspberry_download_site.py --ssl-cert /path/fullchain.pem --ssl-key /path/privkey.pem --public-host your-hostname
```

## Invalid Response In Browser

If the browser says the site sent an invalid response, the most common cause is a protocol mismatch:

- Use `http://whisperwood-epd.local:8090/download/` when HTTPS is not enabled.
- Use `https://whisperwood-epd.local:8090/download/` only when the service was installed with HTTPS.

Check what the service printed:

```bash
journalctl -u whisperwood-download-site -n 50 --no-pager
```

## Notes

- The script checks GitHub for the latest release and caches `WhisperwoodDemoSetup.exe` on the Pi.
- If GitHub is temporarily unavailable, it keeps serving the last cached installer.
- The page itself is static HTML. The Python process only serves the folder and refreshes the cached installer.
