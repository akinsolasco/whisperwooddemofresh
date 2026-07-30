#!/usr/bin/env bash
set -euo pipefail

# Configure a Raspberry Pi to run the Whisperwood Control Service,
# Operation Manager, ESP32 TCP gateway, and local installer download site.
#
# Run from the repository root on the Raspberry Pi:
#   sudo CONTROL_API_KEY="..." DATABASE_URL="postgresql+psycopg2://..." bash raspberry/install_whisperwood_pi.sh
#
# Required:
#   DATABASE_URL        PostgreSQL SQLAlchemy URL for the control database.
#
# Recommended:
#   CONTROL_API_KEY     API key used by desktop clients. If omitted, one is generated.
#
# Optional:
#   OPERATION_DATABASE_URL  DB URL reserved for operation-manager future use.
#   SERVICE_USER            Linux user for services. Default: whisperwood.
#   CONTROL_PORT            Public Control Service port. Default: 7000.
#   OPERATION_HTTP_PORT     Local Operation Manager HTTP port. Default: 8000.
#   ESP32_TCP_PORT          ESP32 TCP gateway port. Default: 5000.
#   DOWNLOAD_PORT           Installer download-site port. Default: 8090.
#   INSTALL_DOWNLOAD_SITE   1 to install download page, 0 to skip. Default: 1.
#   ENABLE_UFW              1 to configure firewall, 0 to skip. Default: 1.
#   HOSTNAME_VALUE          Optional system hostname to set.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SERVICE_USER="${SERVICE_USER:-whisperwood}"
APP_ROOT="${APP_ROOT:-/opt/whisperwood}"
CONTROL_DIR="${APP_ROOT}/control"
OPERATION_DIR="${APP_ROOT}/operation"
DATA_DIR="${APP_ROOT}/data"
DOWNLOAD_SERVICE_DIR="/opt/whisperwood-download-site"

CONTROL_PORT="${CONTROL_PORT:-7000}"
OPERATION_HTTP_PORT="${OPERATION_HTTP_PORT:-8000}"
ESP32_TCP_PORT="${ESP32_TCP_PORT:-5000}"
DOWNLOAD_PORT="${DOWNLOAD_PORT:-8090}"
INSTALL_DOWNLOAD_SITE="${INSTALL_DOWNLOAD_SITE:-1}"
ENABLE_UFW="${ENABLE_UFW:-1}"
HOSTNAME_VALUE="${HOSTNAME_VALUE:-}"

CONTROL_SOURCE="${REPO_ROOT}/raspberry/control/app.py"
OPERATION_SOURCE="${REPO_ROOT}/raspberry/operation/app.py"
DOWNLOAD_INSTALLER="${REPO_ROOT}/scripts/install_raspberry_download_site.sh"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root with sudo."
  exit 1
fi

if [[ ! -f "${CONTROL_SOURCE}" ]]; then
  echo "Missing ${CONTROL_SOURCE}. Run this script from the repo root."
  exit 1
fi

if [[ ! -f "${OPERATION_SOURCE}" ]]; then
  echo "Missing ${OPERATION_SOURCE}. Run this script from the repo root."
  exit 1
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is required."
  echo "Example:"
  echo "  sudo CONTROL_API_KEY='...' DATABASE_URL='postgresql+psycopg2://user:password@host:5432/whisperwood_control' bash raspberry/install_whisperwood_pi.sh"
  exit 1
fi

random_hex() {
  python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
}

CONTROL_API_KEY="${CONTROL_API_KEY:-$(random_hex)}"
JWT_SECRET="${JWT_SECRET:-$(random_hex)}"
OPERATION_INTERNAL_KEY="${OPERATION_INTERNAL_KEY:-$(random_hex)}"
DEVICE_SHARED_KEY="${DEVICE_SHARED_KEY:-$(random_hex)}"
OPERATION_DATABASE_URL="${OPERATION_DATABASE_URL:-${DATABASE_URL}}"

env_quote() {
  python3 - "$1" <<'PY'
import sys
value = sys.argv[1]
print('"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"')
PY
}

timestamp() {
  date +"%Y%m%d-%H%M%S"
}

backup_existing() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    local backup="${path}.bak-$(timestamp)"
    cp -a "${path}" "${backup}"
    echo "Backup created: ${backup}"
  fi
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y \
    bash \
    build-essential \
    ca-certificates \
    curl \
    libjpeg-dev \
    libpq-dev \
    python3 \
    python3-dev \
    python3-full \
    python3-pip \
    python3-venv \
    ufw \
    zlib1g-dev
}

ensure_user() {
  if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
  fi
}

write_requirements() {
  install -d -m 0755 "${CONTROL_DIR}" "${OPERATION_DIR}"

  cat > "${CONTROL_DIR}/requirements.txt" <<'EOF'
argon2-cffi==25.1.0
fastapi==0.139.0
psutil==7.2.2
psycopg2-binary==2.9.12
python-multipart==0.0.32
requests==2.34.2
SQLAlchemy==2.0.51
uvicorn[standard]==0.49.0
EOF

  cat > "${OPERATION_DIR}/requirements.txt" <<'EOF'
fastapi==0.139.0
pillow==12.3.0
psutil==7.2.2
psycopg2-binary==2.9.12
python-multipart==0.0.32
requests==2.34.2
SQLAlchemy==2.0.51
uvicorn[standard]==0.49.0
EOF
}

install_python_apps() {
  install -d -m 0755 "${APP_ROOT}" "${CONTROL_DIR}" "${OPERATION_DIR}" \
    "${DATA_DIR}" "${DATA_DIR}/documents" "${DATA_DIR}/images"

  backup_existing "${CONTROL_DIR}/app.py"
  backup_existing "${OPERATION_DIR}/app.py"

  install -m 0644 "${CONTROL_SOURCE}" "${CONTROL_DIR}/app.py"
  install -m 0644 "${OPERATION_SOURCE}" "${OPERATION_DIR}/app.py"
  write_requirements

  python3 -m venv "${CONTROL_DIR}/venv"
  "${CONTROL_DIR}/venv/bin/python" -m pip install --upgrade pip
  "${CONTROL_DIR}/venv/bin/pip" install -r "${CONTROL_DIR}/requirements.txt"

  python3 -m venv "${OPERATION_DIR}/venv"
  "${OPERATION_DIR}/venv/bin/python" -m pip install --upgrade pip
  "${OPERATION_DIR}/venv/bin/pip" install -r "${OPERATION_DIR}/requirements.txt"

  "${CONTROL_DIR}/venv/bin/python" -m py_compile "${CONTROL_DIR}/app.py"
  "${OPERATION_DIR}/venv/bin/python" -m py_compile "${OPERATION_DIR}/app.py"
}

write_env_files() {
  cat > "${CONTROL_DIR}/.env" <<EOF
CONTROL_API_KEY=$(env_quote "${CONTROL_API_KEY}")
JWT_SECRET=$(env_quote "${JWT_SECRET}")
OPERATION_INTERNAL_KEY=$(env_quote "${OPERATION_INTERNAL_KEY}")
DATABASE_URL=$(env_quote "${DATABASE_URL}")
OPERATION_BASE_URL=$(env_quote "http://127.0.0.1:${OPERATION_HTTP_PORT}")
EOF

  cat > "${OPERATION_DIR}/.env" <<EOF
OPERATION_INTERNAL_KEY=$(env_quote "${OPERATION_INTERNAL_KEY}")
DEVICE_SHARED_KEY=$(env_quote "${DEVICE_SHARED_KEY}")
DATABASE_URL=$(env_quote "${OPERATION_DATABASE_URL}")
FIRMWARE_PATH=$(env_quote "${DATA_DIR}/firmware")
IMAGE_CACHE_PATH=$(env_quote "${DATA_DIR}/images")
WHISPERWOOD_DATA_DIR=$(env_quote "${DATA_DIR}")
WHISPERWOOD_TCP_HOST=$(env_quote "0.0.0.0")
WHISPERWOOD_TCP_PORT=$(env_quote "${ESP32_TCP_PORT}")
EOF

  chmod 0600 "${CONTROL_DIR}/.env" "${OPERATION_DIR}/.env"
}

write_systemd_units() {
  cat > /etc/systemd/system/whisperwood-control.service <<EOF
[Unit]
Description=Whisperwood Control Service
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
WorkingDirectory=${CONTROL_DIR}
ExecStart=${CONTROL_DIR}/venv/bin/uvicorn app:app --host 0.0.0.0 --port ${CONTROL_PORT}
Restart=always
RestartSec=5
User=${SERVICE_USER}
EnvironmentFile=${CONTROL_DIR}/.env

[Install]
WantedBy=multi-user.target
EOF

  cat > /etc/systemd/system/whisperwood-operation.service <<EOF
[Unit]
Description=Whisperwood Operation Manager
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
WorkingDirectory=${OPERATION_DIR}
ExecStart=${OPERATION_DIR}/venv/bin/uvicorn app:app --host 127.0.0.1 --port ${OPERATION_HTTP_PORT}
Restart=always
RestartSec=5
User=${SERVICE_USER}
EnvironmentFile=${OPERATION_DIR}/.env

[Install]
WantedBy=multi-user.target
EOF
}

configure_firewall() {
  if [[ "${ENABLE_UFW}" != "1" ]]; then
    return
  fi
  ufw allow 22/tcp
  ufw allow "${CONTROL_PORT}/tcp"
  ufw allow "${ESP32_TCP_PORT}/tcp"
  if [[ "${INSTALL_DOWNLOAD_SITE}" == "1" ]]; then
    ufw allow "${DOWNLOAD_PORT}/tcp"
  fi
  ufw --force enable
}

install_download_site() {
  if [[ "${INSTALL_DOWNLOAD_SITE}" != "1" ]]; then
    return
  fi
  if [[ ! -f "${DOWNLOAD_INSTALLER}" ]]; then
    echo "Download-site installer not found at ${DOWNLOAD_INSTALLER}; skipping."
    return
  fi
  SUDO_USER="${SERVICE_USER}" PORT="${DOWNLOAD_PORT}" SLUG="${DOWNLOAD_SLUG:-download}" USE_HOSTNAME="${USE_HOSTNAME:-1}" \
    bash "${DOWNLOAD_INSTALLER}"
}

set_optional_hostname() {
  if [[ -z "${HOSTNAME_VALUE}" ]]; then
    return
  fi
  hostnamectl set-hostname "${HOSTNAME_VALUE}"
}

start_services() {
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_ROOT}"
  systemctl daemon-reload
  systemctl enable --now whisperwood-operation.service
  systemctl enable --now whisperwood-control.service
  systemctl restart whisperwood-operation.service
  systemctl restart whisperwood-control.service
}

verify_services() {
  sleep 5
  systemctl --no-pager --full status whisperwood-operation.service >/dev/null
  systemctl --no-pager --full status whisperwood-control.service >/dev/null

  curl -fsS "http://127.0.0.1:${OPERATION_HTTP_PORT}/health" >/dev/null
  curl -fsS -H "X-Whisperwood-Key: ${CONTROL_API_KEY}" "http://127.0.0.1:${CONTROL_PORT}/health" >/dev/null

  if [[ "${INSTALL_DOWNLOAD_SITE}" == "1" ]]; then
    systemctl --no-pager --full status whisperwood-download-site.service >/dev/null
    curl -fsS "http://127.0.0.1:${DOWNLOAD_PORT}/" >/dev/null
  fi
}

print_summary() {
  local lan_ips
  lan_ips="$(hostname -I 2>/dev/null || true)"
  echo
  echo "Whisperwood Raspberry Pi configuration complete."
  echo
  echo "Services:"
  echo "  whisperwood-control      http://0.0.0.0:${CONTROL_PORT}"
  echo "  whisperwood-operation    http://127.0.0.1:${OPERATION_HTTP_PORT}"
  echo "  ESP32 TCP gateway        0.0.0.0:${ESP32_TCP_PORT}"
  if [[ "${INSTALL_DOWNLOAD_SITE}" == "1" ]]; then
    echo "  download site            http://<pi-ip>:${DOWNLOAD_PORT}/download/"
  fi
  echo
  echo "LAN IPs:"
  echo "  ${lan_ips}"
  echo
  echo "Control API key:"
  echo "  ${CONTROL_API_KEY}"
  echo
  echo "Keep that key secure. Desktop clients must use it to connect to this Pi."
}

install_packages
ensure_user
set_optional_hostname
install_python_apps
write_env_files
write_systemd_units
configure_firewall
install_download_site
start_services
verify_services
print_summary
