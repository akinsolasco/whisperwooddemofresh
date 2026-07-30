#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="whisperwood-download-site"
INSTALL_DIR="/opt/${SERVICE_NAME}"
DATA_DIR="/var/lib/${SERVICE_NAME}"
PORT="${PORT:-8090}"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
SLUG="${SLUG:-download}"
USE_HOSTNAME="${USE_HOSTNAME:-1}"
PUBLIC_HOST="${PUBLIC_HOST:-}"
ENABLE_HTTPS="${ENABLE_HTTPS:-0}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo:"
  echo "  sudo bash scripts/install_raspberry_download_site.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_SCRIPT="${SCRIPT_DIR}/raspberry_download_site.py"
SOURCE_LOGO=""
for candidate in \
  "${SCRIPT_DIR}/../assets/enhanced_living_whisperwood_logo_transparent.png" \
  "${SCRIPT_DIR}/assets/enhanced_living_whisperwood_logo_transparent.png" \
  "${SCRIPT_DIR}/enhanced_living_whisperwood_logo_transparent.png"; do
  if [[ -f "${candidate}" ]]; then
    SOURCE_LOGO="${candidate}"
    break
  fi
done

if [[ ! -f "${SOURCE_SCRIPT}" ]]; then
  echo "Missing ${SOURCE_SCRIPT}"
  exit 1
fi

install -d -m 0755 "${INSTALL_DIR}"
install -d -m 0755 "${INSTALL_DIR}/assets"
install -d -m 0755 "${DATA_DIR}"
install -m 0755 "${SOURCE_SCRIPT}" "${INSTALL_DIR}/raspberry_download_site.py"
if [[ -n "${SOURCE_LOGO}" && -f "${SOURCE_LOGO}" ]]; then
  install -m 0644 "${SOURCE_LOGO}" "${INSTALL_DIR}/assets/enhanced_living_whisperwood_logo_transparent.png"
else
  echo "Logo not found beside installer script; the page will use text branding."
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}"

EXTRA_ARGS="--slug ${SLUG}"
if [[ "${USE_HOSTNAME}" != "0" ]]; then
  EXTRA_ARGS="${EXTRA_ARGS} --use-hostname"
fi
if [[ -n "${PUBLIC_HOST}" ]]; then
  EXTRA_ARGS="${EXTRA_ARGS} --public-host ${PUBLIC_HOST}"
fi
if [[ "${ENABLE_HTTPS}" == "1" ]]; then
  EXTRA_ARGS="${EXTRA_ARGS} --generate-self-signed"
fi

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Enhanced Living Whisperwood desktop installer download site
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/raspberry_download_site.py --host 0.0.0.0 --port ${PORT} --root ${DATA_DIR} --refresh-minutes 60 ${EXTRA_ARGS}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"

echo "Installed ${SERVICE_NAME}."
echo "Default path is /${SLUG}/."
if [[ "${ENABLE_HTTPS}" == "1" ]]; then
  echo "HTTPS is enabled with a self-signed certificate."
fi
echo "Check status:"
echo "  systemctl status ${SERVICE_NAME}"
echo "View the generated link:"
echo "  journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
