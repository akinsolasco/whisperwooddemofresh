from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form, Body
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Any
from sqlalchemy import create_engine, text
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from email.message import EmailMessage
from pathlib import Path
import hashlib, os, json, platform, subprocess, shutil, psutil, requests, secrets, smtplib, string, tarfile, tempfile, time

app = FastAPI(title="Whisperwood Control Service", version="0.6.2")
STARTED_AT = datetime.utcnow()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CONTROL_API_KEY = os.getenv("CONTROL_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
OPERATION_BASE_URL = os.getenv("OPERATION_BASE_URL", "http://127.0.0.1:8000")
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
ph = PasswordHasher()

DOC_DIR = "/opt/whisperwood/data/documents"
IMG_DIR = "/opt/whisperwood/data/images"
DATA_DIR = "/opt/whisperwood/data"
BACKUP_DIR = "/opt/whisperwood/data/backups"
FIRMWARE_DIR = "/opt/whisperwood/data/firmware"
os.makedirs(DOC_DIR, exist_ok=True)
os.makedirs(IMG_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(FIRMWARE_DIR, exist_ok=True)

GDRIVE_BACKUP_TARGET = os.getenv("GDRIVE_BACKUP_TARGET", "").strip()
SMTP_HOST = os.getenv("WHISPERWOOD_SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("WHISPERWOOD_SMTP_PORT", "587") or "587")
SMTP_USERNAME = os.getenv("WHISPERWOOD_SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("WHISPERWOOD_SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("WHISPERWOOD_SMTP_FROM_EMAIL", SMTP_USERNAME).strip()
SMTP_FROM_NAME = os.getenv("WHISPERWOOD_SMTP_FROM_NAME", "Enhanced Living Whisperwood").strip()
SMTP_USE_SSL = os.getenv("WHISPERWOOD_SMTP_SSL", "0").strip().lower() in {"1", "true", "yes", "on"}
SMTP_USE_TLS = os.getenv("WHISPERWOOD_SMTP_TLS", "1").strip().lower() in {"1", "true", "yes", "on"}

BATTERY_ROLE_DEFAULTS = ["IT_ADMIN"]
DEFAULT_BATTERY_ALERT_SETTINGS = {
    "enabled": True,
    "low_threshold": 20,
    "critical_threshold": 10,
    "popup_cooldown_minutes": 30,
    "recipient_roles": BATTERY_ROLE_DEFAULTS,
    "email_enabled": False,
    "recipient_emails": [],
    "email_cooldown_minutes": 60,
    "email_subject_prefix": "Whisperwood Battery Alert",
}
DEFAULT_INTEGRATION_SETTINGS = {
    "smtp_host": SMTP_HOST,
    "smtp_port": SMTP_PORT,
    "smtp_username": SMTP_USERNAME,
    "smtp_password": SMTP_PASSWORD,
    "smtp_from_email": SMTP_FROM_EMAIL,
    "smtp_from_name": SMTP_FROM_NAME,
    "smtp_use_ssl": SMTP_USE_SSL,
    "smtp_use_tls": SMTP_USE_TLS,
    "gdrive_backup_target": GDRIVE_BACKUP_TARGET,
    "gdrive_folder_link": "",
    "gdrive_service_account_path": "",
}

def now():
    return datetime.utcnow().isoformat()

def require_key(x_whisperwood_key: str | None):
    if not CONTROL_API_KEY:
        raise HTTPException(status_code=500, detail="Control key not configured")
    if x_whisperwood_key != CONTROL_API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

def db_exec(sql, params=None):
    with engine.begin() as conn:
        return conn.execute(text(sql), params or {})

def db_all(sql, params=None):
    with engine.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), params or {})]

def db_one(sql, params=None):
    rows = db_all(sql, params)
    return rows[0] if rows else None

def hash_pw(password: str):
    return ph.hash(password)

def verify_pw(hash_value: str, password: str):
    try:
        return ph.verify(hash_value, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def json_value(value):
    return json.dumps(value or {}, default=str)


def log_action(username="system", action="", target="", result="success", message="", payload=None, response=None):
    db_exec("""
        INSERT INTO audit_logs(timestamp, username, action, target, result, message, payload_json, response_json)
        VALUES(:timestamp, :username, :action, :target, :result, :message, :payload_json, :response_json)
    """, {
        "timestamp": now(),
        "username": username,
        "action": action,
        "target": target,
        "result": result,
        "message": message,
        "payload_json": json.dumps(payload or {}),
        "response_json": json.dumps(response or {})
    })


def log_resident_audit(username="system", action="", resident=None, old_values=None, new_values=None, reason="", source_document_path=""):
    resident = resident or {}
    db_exec("""
        INSERT INTO resident_audit(
            resident_id, resident_uid, changed_by, change_type,
            old_values, new_values, reason, source_document_path, created_at
        )
        VALUES(
            :resident_id, :resident_uid, :changed_by, :change_type,
            :old_values, :new_values, :reason, :source_document_path, :created_at
        )
    """, {
        "resident_id": resident.get("id"),
        "resident_uid": resident.get("resident_uid") or "",
        "changed_by": username or "system",
        "change_type": action,
        "old_values": json_value(old_values),
        "new_values": json_value(new_values),
        "reason": reason or "",
        "source_document_path": source_document_path or resident.get("source_document_path") or "",
        "created_at": now(),
    })


def operation_url(path: str) -> str:
    return f"{OPERATION_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def operation_request(method: str, path: str, **kwargs):
    try:
        response = requests.request(method, operation_url(path), timeout=kwargs.pop("timeout", 35), **kwargs)
    except requests.Timeout as exc:
        raise HTTPException(status_code=504, detail=f"Operation Manager timeout: {exc}") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Operation Manager unreachable: {exc}") from exc

    try:
        data = response.json()
    except Exception:
        data = {"ok": response.ok, "text": response.text}

    if not response.ok:
        raise HTTPException(status_code=response.status_code, detail=data)
    return data


def first_value(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def bool_value(*values):
    for value in values:
        if value is not None:
            return bool(value)
    return False


def normalize_role_key(role: str) -> str:
    raw = str(role or "").strip()
    upper = raw.upper()
    lower = raw.lower()
    if upper == "ADMIN" or lower in {"admin", "nurse_admin", "nurseadmin"}:
        return "NURSE_ADMIN"
    if upper == "STAFF" or lower in {"staff", "nurse", "user"}:
        return "NURSE"
    if upper in {"IT_ADMIN", "ITADMIN"} or lower in {"it_admin", "itadmin", "it"}:
        return "IT_ADMIN"
    if upper in {"VERIFIER", "DISPLAY_VERIFIER"}:
        return "VERIFIER"
    return upper or "IT_ADMIN"


def normalize_battery_alert_settings(raw: Any = None) -> dict[str, Any]:
    data = dict(DEFAULT_BATTERY_ALERT_SETTINGS)
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if isinstance(raw, dict):
        data.update(raw)
    try:
        data["low_threshold"] = max(1, min(100, int(data.get("low_threshold", 20))))
    except Exception:
        data["low_threshold"] = 20
    try:
        data["critical_threshold"] = max(1, min(data["low_threshold"], int(data.get("critical_threshold", 10))))
    except Exception:
        data["critical_threshold"] = 10
    try:
        data["popup_cooldown_minutes"] = max(1, min(1440, int(data.get("popup_cooldown_minutes", 30))))
    except Exception:
        data["popup_cooldown_minutes"] = 30
    roles = data.get("recipient_roles") or BATTERY_ROLE_DEFAULTS
    if isinstance(roles, str):
        roles = [role.strip() for role in roles.split(",")]
    allowed = {"IT_ADMIN", "NURSE_ADMIN", "NURSE", "VERIFIER"}
    normalized = []
    for role in roles or []:
        key = normalize_role_key(role)
        if key in allowed and key not in normalized:
            normalized.append(key)
    data["recipient_roles"] = normalized or list(BATTERY_ROLE_DEFAULTS)
    data["enabled"] = bool(data.get("enabled", True))
    emails = data.get("recipient_emails") or []
    if isinstance(emails, str):
        emails = [part.strip() for part in emails.replace(";", ",").split(",")]
    clean_emails = []
    for email in emails or []:
        value = str(email or "").strip()
        marker = value.lower()
        if value and "@" in value and marker not in [existing.lower() for existing in clean_emails]:
            clean_emails.append(value)
    data["recipient_emails"] = clean_emails
    data["email_enabled"] = bool(data.get("email_enabled", False))
    try:
        data["email_cooldown_minutes"] = max(5, min(1440, int(data.get("email_cooldown_minutes", 60))))
    except Exception:
        data["email_cooldown_minutes"] = 60
    data["email_subject_prefix"] = str(data.get("email_subject_prefix") or "Whisperwood Battery Alert").strip()[:120]
    return data


def normalize_integration_settings(raw: Any = None, previous: Any = None, preserve_blank_password: bool = False) -> dict[str, Any]:
    data = dict(DEFAULT_INTEGRATION_SETTINGS)
    if isinstance(previous, str) and previous.strip():
        try:
            previous = json.loads(previous)
        except Exception:
            previous = {}
    if isinstance(previous, dict):
        data.update(previous)
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    raw_dict = raw if isinstance(raw, dict) else {}
    data.update(raw_dict)

    if preserve_blank_password and not str(raw_dict.get("smtp_password") or "").strip():
        previous_password = ""
        if isinstance(previous, dict):
            previous_password = str(previous.get("smtp_password") or "")
        data["smtp_password"] = previous_password or str(DEFAULT_INTEGRATION_SETTINGS.get("smtp_password") or "")

    for key in ["smtp_host", "smtp_username", "smtp_password", "smtp_from_email", "smtp_from_name", "gdrive_backup_target", "gdrive_folder_link", "gdrive_service_account_path"]:
        data[key] = str(data.get(key) or "").strip()
    try:
        data["smtp_port"] = max(1, min(65535, int(data.get("smtp_port") or 587)))
    except Exception:
        data["smtp_port"] = 587
    data["smtp_use_ssl"] = bool(data.get("smtp_use_ssl", False))
    data["smtp_use_tls"] = bool(data.get("smtp_use_tls", True)) and not data["smtp_use_ssl"]
    if not data["smtp_from_email"] and data["smtp_username"]:
        data["smtp_from_email"] = data["smtp_username"]
    if not data["smtp_from_name"]:
        data["smtp_from_name"] = "Enhanced Living Whisperwood"
    return data


def get_integration_settings() -> dict[str, Any]:
    return normalize_integration_settings(get_system_setting("integration_settings", {}))


def public_integration_settings(settings: Any = None) -> dict[str, Any]:
    data = normalize_integration_settings(settings if settings is not None else get_integration_settings())
    public = dict(data)
    public.pop("smtp_password", None)
    public["smtp_password_configured"] = bool(data.get("smtp_password"))
    public["email_configured"] = smtp_configured(data)
    public["google_drive_configured"] = bool(data.get("gdrive_backup_target"))
    public["rclone_available"] = bool(shutil.which("rclone"))
    return public


def get_system_setting(key: str, default: Any = None) -> Any:
    row = db_one("SELECT value_json FROM system_settings WHERE key=:key", {"key": key})
    if not row:
        return default
    try:
        return json.loads(row.get("value_json") or "")
    except Exception:
        return default


def save_system_setting(key: str, value: Any) -> None:
    payload = json.dumps(value or {}, default=str)
    db_exec("""
        INSERT INTO system_settings(key, value_json, updated_at)
        VALUES(:key, :value_json, :updated_at)
        ON CONFLICT (key)
        DO UPDATE SET value_json=EXCLUDED.value_json, updated_at=EXCLUDED.updated_at
    """, {"key": key, "value_json": payload, "updated_at": now()})


def smtp_configured(settings: Any = None) -> bool:
    cfg = normalize_integration_settings(settings if settings is not None else get_integration_settings())
    return bool(cfg.get("smtp_host") and cfg.get("smtp_from_email") and (cfg.get("smtp_username") or not cfg.get("smtp_password")))


def send_email_message(subject: str, body: str, recipients: list[str], settings: Any = None) -> dict[str, Any]:
    cfg = normalize_integration_settings(settings if settings is not None else get_integration_settings())
    recipients = [str(email or "").strip() for email in recipients or [] if str(email or "").strip()]
    if not recipients:
        return {"ok": False, "skipped": True, "reason": "No email recipients configured"}
    if not smtp_configured(cfg):
        return {"ok": False, "skipped": True, "reason": "SMTP is not configured on the Raspberry Pi"}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{cfg.get('smtp_from_name')} <{cfg.get('smtp_from_email')}>"
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    try:
        if cfg.get("smtp_use_ssl"):
            with smtplib.SMTP_SSL(cfg.get("smtp_host"), int(cfg.get("smtp_port") or 465), timeout=20) as server:
                if cfg.get("smtp_username"):
                    server.login(cfg.get("smtp_username"), cfg.get("smtp_password"))
                server.send_message(msg)
        else:
            with smtplib.SMTP(cfg.get("smtp_host"), int(cfg.get("smtp_port") or 587), timeout=20) as server:
                if cfg.get("smtp_use_tls"):
                    server.starttls()
                if cfg.get("smtp_username"):
                    server.login(cfg.get("smtp_username"), cfg.get("smtp_password"))
                server.send_message(msg)
        return {"ok": True, "recipients": recipients}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "recipients": recipients}


def device_battery_level(device: dict[str, Any], settings: dict[str, Any]) -> Optional[str]:
    try:
        percent = int(device.get("battery_level"))
    except Exception:
        percent = None
    low_flag = bool(device.get("battery_low"))
    if percent is None and not low_flag:
        return None
    if percent is not None and percent <= int(settings.get("critical_threshold", 10)):
        return "critical"
    if low_flag or (percent is not None and percent <= int(settings.get("low_threshold", 20))):
        return "low"
    return None


def process_battery_email_alerts(devices: list[dict[str, Any]]) -> dict[str, Any]:
    settings = normalize_battery_alert_settings(get_system_setting("battery_alert_settings", DEFAULT_BATTERY_ALERT_SETTINGS))
    if not settings.get("enabled") or not settings.get("email_enabled"):
        return {"ok": True, "skipped": True, "reason": "Battery email alerts disabled"}
    recipients = settings.get("recipient_emails") or []
    if not recipients:
        return {"ok": True, "skipped": True, "reason": "No battery alert email recipients"}

    state = get_system_setting("battery_email_alert_state", {})
    if not isinstance(state, dict):
        state = {}
    cooldown_s = int(settings.get("email_cooldown_minutes", 60)) * 60
    now_ts = time.time()
    due = []
    for device in devices:
        if not device.get("is_online"):
            continue
        level = device_battery_level(device, settings)
        if not level:
            continue
        device_id = str(device.get("device_id") or device.get("id") or "unknown")
        key = f"{device_id}:{level}"
        try:
            last_sent = float(state.get(key, 0))
        except Exception:
            last_sent = 0
        if now_ts - last_sent < cooldown_s:
            continue
        state[key] = now_ts
        due.append({
            "level": level,
            "device_id": device_id,
            "resident": device.get("resident_name") or device.get("paired_resident_name") or "Unassigned",
            "battery": device.get("battery_level"),
            "voltage": device.get("battery_voltage"),
            "power": "Charging" if device.get("battery_charging") else "Plugged in" if device.get("battery_plugged") else "On battery",
            "last_seen_s": device.get("last_seen_s"),
        })

    if not due:
        save_system_setting("battery_email_alert_state", state)
        return {"ok": True, "skipped": True, "reason": "No battery alerts due"}

    critical = any(item["level"] == "critical" for item in due)
    subject_prefix = settings.get("email_subject_prefix") or "Whisperwood Battery Alert"
    subject = f"{subject_prefix}: {'Critical' if critical else 'Low'} smart label battery"
    lines = [
        "Enhanced Living Whisperwood battery alert",
        "",
        "One or more smart resident display labels need battery attention.",
        "",
    ]
    for item in due:
        lines.append(
            f"- {item['device_id']} | {item['resident']} | {str(item['level']).upper()} | "
            f"{item['battery']}% | {item['power']} | last seen {item['last_seen_s']}s ago"
        )
    lines.extend([
        "",
        "Please check the listed display label(s), charger connection, and battery status from the IT Control Center.",
        "",
        f"Generated by {platform.node()} at {now()} UTC.",
    ])
    result = send_email_message(subject, "\n".join(lines), recipients)
    save_system_setting("battery_email_alert_state", state)
    log_action("system", "battery_email_alert", "devices", "success" if result.get("ok") else "skipped", result.get("error") or result.get("reason") or "Battery alert email processed", payload={"due": due}, response=result)
    return {**result, "alerts": due}


def pg_connection_url() -> str:
    return DATABASE_URL.replace("postgresql+psycopg2://", "postgresql://")


def backup_file_info(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "created_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
        "type": "local",
        "status": "available",
    }


def list_local_backups() -> list[dict[str, Any]]:
    root = Path(BACKUP_DIR)
    rows = [backup_file_info(path) for path in root.glob("*.tar.gz") if path.is_file()]
    rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return rows


def run_backup_upload(path: Path) -> dict[str, Any]:
    settings = get_integration_settings()
    target = settings.get("gdrive_backup_target") or ""
    if not target:
        return {"ok": True, "skipped": True, "reason": "Google Drive backup target is not configured"}
    if not shutil.which("rclone"):
        return {"ok": False, "skipped": True, "reason": "rclone is not installed on the Raspberry Pi"}
    result = subprocess.run(
        ["rclone", "copy", str(path), target],
        text=True,
        capture_output=True,
        timeout=600,
    )
    return {
        "ok": result.returncode == 0,
        "target": target,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
    }


def create_backup_archive(created_by: str = "system", upload_to_drive: bool = True) -> dict[str, Any]:
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    archive_path = Path(BACKUP_DIR) / f"whisperwood-demo-backup-{timestamp}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        metadata = {
            "created_at": now(),
            "created_by": created_by or "system",
            "hostname": platform.node(),
            "control_version": "0.6.2",
            "database_url_configured": bool(DATABASE_URL),
        }
        (tmp_path / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        dump_path = tmp_path / "control_database.dump"
        pg_dump = shutil.which("pg_dump")
        if pg_dump and DATABASE_URL:
            result = subprocess.run(
                [pg_dump, "-Fc", "-f", str(dump_path), pg_connection_url()],
                text=True,
                capture_output=True,
                timeout=600,
            )
            if result.returncode != 0:
                (tmp_path / "pg_dump_error.txt").write_text(result.stderr or result.stdout or "pg_dump failed", encoding="utf-8")
        else:
            (tmp_path / "pg_dump_error.txt").write_text("pg_dump or DATABASE_URL is not configured", encoding="utf-8")

        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(tmp_path / "metadata.json", arcname="metadata.json")
            if dump_path.exists():
                tar.add(dump_path, arcname="control_database.dump")
            error_file = tmp_path / "pg_dump_error.txt"
            if error_file.exists():
                tar.add(error_file, arcname="pg_dump_error.txt")
            for folder_name in ("documents", "images"):
                folder = Path(DATA_DIR) / folder_name
                if folder.exists():
                    tar.add(folder, arcname=f"data/{folder_name}")
            schedule_file = Path(DATA_DIR) / "lcd_schedule.json"
            if schedule_file.exists():
                tar.add(schedule_file, arcname="data/lcd_schedule.json")

    upload = run_backup_upload(archive_path) if upload_to_drive else {"ok": True, "skipped": True, "reason": "Drive upload disabled for this backup"}
    info = backup_file_info(archive_path)
    info["upload"] = upload
    log_action(created_by or "system", "backup_create", archive_path.name, "success" if upload.get("ok") else "warning", upload.get("reason") or "Backup created", payload={"upload_to_drive": upload_to_drive}, response=info)
    return {"ok": True, "backup": info}


def restore_backup_archive(path_text: str, confirm_text: str, restored_by: str = "system") -> dict[str, Any]:
    if confirm_text != "RESTORE WHISPERWOOD BACKUP":
        raise HTTPException(status_code=400, detail="Restore confirmation text did not match")
    archive_path = Path(path_text or "")
    if not archive_path.is_file() or archive_path.parent != Path(BACKUP_DIR):
        raise HTTPException(status_code=404, detail="Backup file was not found in the local backup folder")
    pre_restore = create_backup_archive(f"{restored_by or 'system'} pre-restore", upload_to_drive=False)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(tmp_path)
        dump_path = tmp_path / "control_database.dump"
        pg_restore = shutil.which("pg_restore")
        if dump_path.exists() and pg_restore and DATABASE_URL:
            result = subprocess.run(
                [pg_restore, "--clean", "--if-exists", "--no-owner", "--dbname", pg_connection_url(), str(dump_path)],
                text=True,
                capture_output=True,
                timeout=900,
            )
            if result.returncode != 0:
                raise HTTPException(status_code=500, detail=f"Database restore failed: {result.stderr or result.stdout}")
        for folder_name in ("documents", "images"):
            src = tmp_path / "data" / folder_name
            dst = Path(DATA_DIR) / folder_name
            if src.exists():
                shutil.copytree(src, dst, dirs_exist_ok=True)
    log_action(restored_by or "system", "backup_restore", archive_path.name, "success", "Backup restored", payload={"path": str(archive_path)}, response={"pre_restore_backup": pre_restore.get("backup")})
    return {"ok": True, "restored": str(archive_path), "pre_restore_backup": pre_restore.get("backup")}


def resident_display_payload(row, device_id=""):
    if not row:
        return {}
    texture = first_value(row.get("texture"), row.get("allergies"))
    fluids = first_value(row.get("fluids"), row.get("schedule"))
    target = device_id or row.get("paired_device_id") or ""
    return {
        "id": target,
        "device_id": target,
        "resident_id": row.get("id"),
        "resident_uid": row.get("resident_uid"),
        "name": row.get("full_name") or "",
        "room": row.get("room") or "",
        "diet": [x.strip() for x in str(row.get("diet") or "").split(",") if x.strip()],
        "texture": [x.strip() for x in str(texture or "").split(",") if x.strip()],
        "allergies": [x.strip() for x in str(texture or "").split(",") if x.strip()],
        "fluids": fluids,
        "schedule": fluids,
        "note": row.get("note") or "",
        "drinks": row.get("drinks") or "",
        "image_path": row.get("image_path") or row.get("lcd_image_path") or "",
    }


def sync_operation_devices():
    try:
        payload = operation_request("GET", "/devices", timeout=5)
    except HTTPException:
        return []
    devices = payload.get("devices") or payload.get("items") or []
    for device in devices:
        device_id = device.get("device_id") or device.get("id")
        if not device_id:
            continue
        existing = db_one("SELECT id FROM devices WHERE device_id=:d", {"d": device_id})
        data = {
            "device_id": device_id,
            "ip_address": device.get("ip") or device.get("reported_ip") or "",
            "port": device.get("port") or 5000,
            "firmware_version": device.get("fw") or device.get("firmware") or "",
            "battery_level": str(device.get("battery_level") if device.get("battery_level") is not None else ""),
            "battery_percent": device.get("battery_level") if isinstance(device.get("battery_level"), int) else None,
            "battery_ok": device.get("battery_ok"),
            "battery_mv": device.get("battery_mv"),
            "battery_voltage": device.get("battery_voltage"),
            "battery_raw_percent": device.get("battery_raw_percent"),
            "battery_low": device.get("battery_low"),
            "battery_alert": device.get("battery_alert"),
            "battery_plugged": device.get("battery_plugged"),
            "battery_charging": device.get("battery_charging"),
            "battery_full": device.get("battery_full"),
            "rssi": device.get("rssi"),
            "heap": device.get("heap"),
            "last_status_at": device.get("last_status_at") or "",
            "status": "online" if device.get("is_online") or device.get("online") else "offline",
            "last_seen": device.get("last_seen_at") or now(),
            "t": now(),
        }
        if existing:
            db_exec("""
                UPDATE devices SET ip_address=:ip_address, port=:port, firmware_version=:firmware_version,
                battery_level=:battery_level, battery_percent=:battery_percent, status=:status,
                battery_ok=:battery_ok, battery_mv=:battery_mv, battery_voltage=:battery_voltage,
                battery_raw_percent=:battery_raw_percent, battery_low=:battery_low, battery_alert=:battery_alert,
                battery_plugged=:battery_plugged, battery_charging=:battery_charging, battery_full=:battery_full,
                rssi=:rssi, heap=:heap, last_status_at=:last_status_at,
                last_seen=:last_seen, updated_at=:t WHERE device_id=:device_id
            """, data)
        else:
            db_exec("""
                INSERT INTO devices(
                    device_id,ip_address,port,firmware_version,battery_level,battery_percent,status,last_seen,
                    battery_ok,battery_mv,battery_voltage,battery_raw_percent,battery_low,battery_alert,
                    battery_plugged,battery_charging,battery_full,rssi,heap,last_status_at,created_at,updated_at
                )
                VALUES(
                    :device_id,:ip_address,:port,:firmware_version,:battery_level,:battery_percent,:status,:last_seen,
                    :battery_ok,:battery_mv,:battery_voltage,:battery_raw_percent,:battery_low,:battery_alert,
                    :battery_plugged,:battery_charging,:battery_full,:rssi,:heap,:last_status_at,:t,:t
                )
            """, data)
    return devices


def merged_devices():
    operation_devices = sync_operation_devices()
    operation_by_id = {
        str(device.get("device_id") or device.get("id")): device
        for device in operation_devices
        if device.get("device_id") or device.get("id")
    }
    rows = db_all("""
        SELECT d.*, r.full_name AS resident_name, r.resident_uid AS resident_uid,
               r.full_name AS paired_resident_name, r.resident_uid AS paired_resident_uid
        FROM devices d LEFT JOIN residents r ON d.paired_resident_id=r.id
        ORDER BY d.id DESC
    """)
    seen = set()
    out = []
    for row in rows:
        device_id = str(row.get("device_id") or "")
        seen.add(device_id)
        live = operation_by_id.get(device_id, {})
        is_online = bool(live.get("is_online") or live.get("online"))
        item = {
            **row,
            "id": device_id,
            "device_id": device_id,
            "ip": live.get("ip") or row.get("ip_address") or "",
            "lan_ip": live.get("ip") or row.get("ip_address") or "",
            "port": live.get("port") or row.get("port"),
            "fw": live.get("fw") or row.get("firmware_version") or "",
            "firmware": live.get("fw") or row.get("firmware_version") or "",
            "status": "online" if is_online else "offline",
            "is_online": is_online,
            "online": is_online,
            "last_seen_s": live.get("last_seen_s") if live else 9999,
            "battery_level": live.get("battery_level") if live.get("battery_level") is not None else row.get("battery_percent"),
            "battery": live.get("battery_level") if live.get("battery_level") is not None else row.get("battery_percent"),
            "battery_ok": first_value(live.get("battery_ok"), row.get("battery_ok")),
            "battery_mv": first_value(live.get("battery_mv"), row.get("battery_mv")),
            "battery_voltage": first_value(live.get("battery_voltage"), row.get("battery_voltage")),
            "battery_raw_percent": first_value(live.get("battery_raw_percent"), row.get("battery_raw_percent")),
            "battery_low": first_value(live.get("battery_low"), row.get("battery_low")),
            "battery_alert": first_value(live.get("battery_alert"), row.get("battery_alert")),
            "battery_plugged": first_value(live.get("battery_plugged"), row.get("battery_plugged")),
            "battery_charging": first_value(live.get("battery_charging"), row.get("battery_charging")),
            "battery_full": first_value(live.get("battery_full"), row.get("battery_full")),
            "rssi": first_value(live.get("rssi"), row.get("rssi")),
            "heap": first_value(live.get("heap"), row.get("heap")),
            "wifi": live.get("wifi"),
            "last_status_at": first_value(live.get("last_status_at"), row.get("last_status_at")),
            "lcd_image_cached": live.get("lcd_image_cached"),
            "epaper_busy": live.get("epaper_busy"),
            "pi_cached_image": live.get("pi_cached_image"),
            "connection_state": live.get("connection_state") or ("online" if is_online else "offline"),
            "offline_reason": live.get("offline_reason") or "",
            "pending_seq": live.get("pending_seq"),
            "pending_img_seq": live.get("pending_img_seq"),
            "pending_lcd_seq": live.get("pending_lcd_seq"),
        }
        out.append(item)
    for device_id, live in operation_by_id.items():
        if device_id in seen:
            continue
        is_online = bool(live.get("is_online") or live.get("online"))
        out.append({
            "id": device_id,
            "device_id": device_id,
            "ip": live.get("ip") or "",
            "lan_ip": live.get("ip") or "",
            "port": live.get("port") or 5000,
            "fw": live.get("fw") or "",
            "firmware": live.get("fw") or "",
            "status": "online" if is_online else "offline",
            "is_online": is_online,
            "online": is_online,
            "last_seen_s": live.get("last_seen_s") or 0,
            "battery_level": live.get("battery_level"),
            "battery": live.get("battery_level"),
            "battery_ok": live.get("battery_ok"),
            "battery_mv": live.get("battery_mv"),
            "battery_voltage": live.get("battery_voltage"),
            "battery_raw_percent": live.get("battery_raw_percent"),
            "battery_low": live.get("battery_low"),
            "battery_alert": live.get("battery_alert"),
            "battery_plugged": live.get("battery_plugged"),
            "battery_charging": live.get("battery_charging"),
            "battery_full": live.get("battery_full"),
            "rssi": live.get("rssi"),
            "heap": live.get("heap"),
            "wifi": live.get("wifi"),
            "last_status_at": live.get("last_status_at"),
            "lcd_image_cached": live.get("lcd_image_cached"),
            "epaper_busy": live.get("epaper_busy"),
            "pi_cached_image": live.get("pi_cached_image"),
            "connection_state": live.get("connection_state") or ("online" if is_online else "offline"),
            "offline_reason": live.get("offline_reason") or "",
            "paired_resident_id": None,
            "resident_name": "",
            "resident_uid": "",
        })
    return out


def init_db():
    db_exec("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'staff',
        full_name TEXT,
        is_active BOOLEAN DEFAULT TRUE,
        force_password_change BOOLEAN DEFAULT FALSE,
        created_at TEXT,
        last_login TEXT
    );
    """)

    db_exec("""
    CREATE TABLE IF NOT EXISTS residents (
        id SERIAL PRIMARY KEY,
        resident_uid TEXT UNIQUE NOT NULL,
        full_name TEXT NOT NULL,
        room TEXT,
        status_alert TEXT,
        diet TEXT,
        allergies TEXT,
        note TEXT,
        drinks TEXT,
        active BOOLEAN DEFAULT TRUE,
        archived BOOLEAN DEFAULT FALSE,
        source_document_path TEXT,
        source_document_name TEXT,
        safety_review_flag BOOLEAN DEFAULT FALSE,
        safety_review_note TEXT,
        image_path TEXT,
        image_name TEXT,
        created_at TEXT,
        updated_at TEXT
    );
    """)

    for sql in [
        "ALTER TABLE residents ADD COLUMN IF NOT EXISTS texture TEXT",
        "ALTER TABLE residents ADD COLUMN IF NOT EXISTS fluids TEXT",
        "ALTER TABLE residents ADD COLUMN IF NOT EXISTS schedule TEXT",
        "ALTER TABLE residents ADD COLUMN IF NOT EXISTS lcd_schedule_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE residents ADD COLUMN IF NOT EXISTS lcd_on_time TEXT",
        "ALTER TABLE residents ADD COLUMN IF NOT EXISTS lcd_off_time TEXT",
        "ALTER TABLE residents ADD COLUMN IF NOT EXISTS sleep_if_no_image BOOLEAN DEFAULT FALSE",
    ]:
        db_exec(sql)

    db_exec("""
        UPDATE residents
        SET texture = allergies
        WHERE (texture IS NULL OR texture = '') AND allergies IS NOT NULL AND allergies <> ''
    """)
    db_exec("""
        UPDATE residents
        SET allergies = texture
        WHERE (allergies IS NULL OR allergies = '') AND texture IS NOT NULL AND texture <> ''
    """)
    db_exec("""
        UPDATE residents
        SET fluids = schedule
        WHERE (fluids IS NULL OR fluids = '') AND schedule IS NOT NULL AND schedule <> ''
    """)
    db_exec("""
        UPDATE residents
        SET schedule = fluids
        WHERE (schedule IS NULL OR schedule = '') AND fluids IS NOT NULL AND fluids <> ''
    """)

    db_exec("""
    CREATE TABLE IF NOT EXISTS resident_audit (
        id SERIAL PRIMARY KEY,
        resident_id INTEGER,
        resident_uid TEXT,
        changed_by TEXT,
        change_type TEXT,
        old_values JSONB,
        new_values JSONB,
        reason TEXT,
        source_document_path TEXT,
        created_at TEXT
    );
    """)

    db_exec("""
    CREATE TABLE IF NOT EXISTS resident_change_requests (
        id SERIAL PRIMARY KEY,
        resident_id INTEGER,
        resident_uid TEXT,
        requested_by TEXT,
        status TEXT DEFAULT 'pending',
        proposed_old JSONB,
        proposed_new JSONB,
        reason TEXT,
        decision_by TEXT,
        decision_note TEXT,
        created_at TEXT,
        decided_at TEXT
    );
    """)

    for sql in [
        "ALTER TABLE resident_change_requests ADD COLUMN IF NOT EXISTS proposed_payload JSONB",
        "ALTER TABLE resident_change_requests ADD COLUMN IF NOT EXISTS comment TEXT",
        "ALTER TABLE resident_change_requests ADD COLUMN IF NOT EXISTS requested_by_user_id INTEGER",
        "ALTER TABLE resident_change_requests ADD COLUMN IF NOT EXISTS requested_by_username TEXT",
        "ALTER TABLE resident_change_requests ADD COLUMN IF NOT EXISTS reviewed_by_user_id INTEGER",
        "ALTER TABLE resident_change_requests ADD COLUMN IF NOT EXISTS reviewed_by_username TEXT",
        "ALTER TABLE resident_change_requests ADD COLUMN IF NOT EXISTS review_note TEXT",
        "ALTER TABLE resident_change_requests ADD COLUMN IF NOT EXISTS reviewed_at TEXT",
    ]:
        db_exec(sql)

    db_exec("""
    CREATE TABLE IF NOT EXISTS devices (
        id SERIAL PRIMARY KEY,
        device_id TEXT UNIQUE NOT NULL,
        ip_address TEXT,
        port INTEGER DEFAULT 5000,
        firmware_version TEXT,
        battery_level TEXT,
        battery_percent INTEGER,
        status TEXT DEFAULT 'offline',
        last_seen TEXT,
        paired_resident_id INTEGER,
        created_at TEXT,
        updated_at TEXT
    );
    """)

    for sql in [
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS battery_ok BOOLEAN",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS battery_mv INTEGER",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS battery_voltage REAL",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS battery_raw_percent REAL",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS battery_low BOOLEAN",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS battery_alert BOOLEAN",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS battery_plugged BOOLEAN",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS battery_charging BOOLEAN",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS battery_full BOOLEAN",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS rssi INTEGER",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS heap INTEGER",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_status_at TEXT",
    ]:
        db_exec(sql)

    db_exec("""
    CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at TEXT
    );
    """)

    db_exec("""
    CREATE TABLE IF NOT EXISTS schedules (
        id SERIAL PRIMARY KEY,
        resident_id INTEGER,
        device_id TEXT,
        enabled BOOLEAN DEFAULT FALSE,
        lcd_on_time TEXT,
        lcd_off_time TEXT,
        sleep_if_no_image BOOLEAN DEFAULT TRUE,
        created_at TEXT,
        updated_at TEXT
    );
    """)

    db_exec("""
    CREATE TABLE IF NOT EXISTS verification_checks (
        id SERIAL PRIMARY KEY,
        resident_id INTEGER,
        resident_uid TEXT,
        device_id TEXT,
        status TEXT,
        note TEXT,
        checked_by TEXT,
        checked_at TEXT
    );
    """)

    for sql in [
        "ALTER TABLE verification_checks ADD COLUMN IF NOT EXISTS checked_by_user_id INTEGER",
        "ALTER TABLE verification_checks ADD COLUMN IF NOT EXISTS checked_by_username TEXT",
        "ALTER TABLE verification_checks ADD COLUMN IF NOT EXISTS created_at TEXT",
    ]:
        db_exec(sql)

    db_exec("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        id SERIAL PRIMARY KEY,
        timestamp TEXT,
        username TEXT,
        action TEXT,
        target TEXT,
        result TEXT,
        message TEXT,
        payload_json JSONB,
        response_json JSONB
    );
    """)

    db_exec("""
    CREATE TABLE IF NOT EXISTS it_audit_logs (
        id SERIAL PRIMARY KEY,
        timestamp TEXT,
        username TEXT,
        action TEXT,
        target TEXT,
        result TEXT,
        message TEXT,
        payload_json JSONB,
        response_json JSONB
    );
    """)

    db_exec("""
    CREATE TABLE IF NOT EXISTS resident_dropdown_options (
        id SERIAL PRIMARY KEY,
        category TEXT NOT NULL,
        option_text TEXT NOT NULL,
        sort_order INTEGER DEFAULT 0,
        active BOOLEAN DEFAULT TRUE,
        created_at TEXT,
        updated_at TEXT,
        UNIQUE(category, option_text)
    );
    """)

    db_exec("""
    CREATE TABLE IF NOT EXISTS firmware_releases (
        id SERIAL PRIMARY KEY,
        version TEXT NOT NULL,
        filename TEXT NOT NULL,
        path TEXT NOT NULL,
        size_bytes INTEGER,
        sha256 TEXT,
        md5 TEXT,
        notes TEXT,
        status TEXT DEFAULT 'uploaded',
        uploaded_by TEXT,
        released_by TEXT,
        target TEXT,
        last_result JSONB,
        created_at TEXT,
        released_at TEXT
    );
    """)

    if not db_one("SELECT * FROM users WHERE username='admin'"):
        db_exec("""
            INSERT INTO users(username,password_hash,role,full_name,is_active,force_password_change,created_at)
            VALUES('admin',:p,'admin','System Admin',TRUE,TRUE,:t)
        """, {"p": hash_pw("admin123"), "t": now()})

    if not db_one("SELECT * FROM users WHERE username='itadmin'"):
        db_exec("""
            INSERT INTO users(username,password_hash,role,full_name,is_active,force_password_change,created_at)
            VALUES('itadmin',:p,'it_admin','IT Admin',TRUE,TRUE,:t)
        """, {"p": hash_pw("itadmin123"), "t": now()})

init_db()

class LoginPayload(BaseModel):
    username: str
    password: str

class ChangePasswordPayload(BaseModel):
    username: str
    old_password: str
    new_password: str

class TempPasswordPayload(BaseModel):
    username: str

class UserPayload(BaseModel):
    username: str
    password: str
    role: str
    full_name: Optional[str] = ""
    is_active: bool = True
    force_password_change: bool = True

class UserStatusPayload(BaseModel):
    is_active: bool

class ResidentPayload(BaseModel):
    resident_uid: str
    full_name: str
    room: Optional[str] = ""
    status_alert: Optional[str] = ""
    diet: Optional[str] = ""
    texture: Optional[str] = ""
    allergies: Optional[str] = ""
    note: Optional[str] = ""
    drinks: Optional[str] = ""
    fluids: Optional[str] = ""
    schedule: Optional[str] = ""
    active: bool = True
    safety_review_flag: bool = False
    needs_safety_review: bool = False
    safety_review_note: Optional[str] = ""
    lcd_schedule_enabled: bool = False
    lcd_on_time: Optional[str] = None
    lcd_off_time: Optional[str] = None
    sleep_if_no_image: bool = False

class ArchivePayload(BaseModel):
    archived: bool = True
    reason: Optional[str] = ""

class DevicePayload(BaseModel):
    device_id: str
    ip_address: Optional[str] = ""
    port: int = 5000
    firmware_version: Optional[str] = ""
    battery_level: Optional[str] = "Medium"
    battery_percent: Optional[int] = None
    battery_ok: Optional[bool] = None
    battery_mv: Optional[int] = None
    battery_voltage: Optional[float] = None
    battery_raw_percent: Optional[float] = None
    battery_low: Optional[bool] = None
    battery_alert: Optional[bool] = None
    battery_plugged: Optional[bool] = None
    battery_charging: Optional[bool] = None
    battery_full: Optional[bool] = None
    rssi: Optional[int] = None
    heap: Optional[int] = None
    last_status_at: Optional[str] = ""
    status: Optional[str] = "offline"

class PairPayload(BaseModel):
    resident_id: int
    device_id: str

class UnpairPayload(BaseModel):
    device_id: str

class SchedulePayload(BaseModel):
    resident_id: Optional[int] = None
    device_id: Optional[str] = ""
    enabled: bool = False
    lcd_on_time: str = "7:00 am"
    lcd_off_time: str = "7:00 pm"
    sleep_if_no_image: bool = True

class ChangeRequestPayload(BaseModel):
    resident_id: int
    resident_uid: Optional[str] = ""
    proposed_payload: Optional[dict[str, Any]] = None
    proposed_new: Optional[dict[str, Any]] = None
    comment: Optional[str] = ""
    requested_by_user_id: Optional[int] = None
    requested_by_username: Optional[str] = ""
    requested_by: Optional[str] = ""
    reason: Optional[str] = ""

class DecisionPayload(BaseModel):
    decision: Optional[str] = ""
    status: Optional[str] = ""
    decision_by: Optional[str] = ""
    decision_note: Optional[str] = ""
    reviewed_by_user_id: Optional[int] = None
    reviewed_by_username: Optional[str] = ""
    review_note: Optional[str] = ""

class VerificationPayload(BaseModel):
    resident_id: int
    resident_uid: str
    device_id: Optional[str] = ""
    status: str
    note: Optional[str] = ""
    checked_by: Optional[str] = ""
    checked_by_user_id: Optional[int] = None
    checked_by_username: Optional[str] = ""

class ItLogPayload(BaseModel):
    username: str
    action: str
    target: Optional[str] = ""
    result: Optional[str] = "success"
    message: Optional[str] = ""
    payload_json: Optional[dict[str, Any]] = None
    response_json: Optional[dict[str, Any]] = None


class DropdownOptionsPayload(BaseModel):
    options: dict[str, list[str]] = {}
    payload_json: Optional[dict] = {}
    response_json: Optional[dict] = {}


class BatteryAlertSettingsPayload(BaseModel):
    enabled: bool = True
    low_threshold: int = 20
    critical_threshold: int = 10
    popup_cooldown_minutes: int = 30
    recipient_roles: list[str] = Field(default_factory=lambda: ["IT_ADMIN"])
    email_enabled: bool = False
    recipient_emails: list[str] = Field(default_factory=list)
    email_cooldown_minutes: int = 60
    email_subject_prefix: str = "Whisperwood Battery Alert"


class BatteryTestEmailPayload(BaseModel):
    recipients: list[str] = Field(default_factory=list)


class IntegrationSettingsPayload(BaseModel):
    smtp_host: Optional[str] = ""
    smtp_port: int = 587
    smtp_username: Optional[str] = ""
    smtp_password: Optional[str] = ""
    smtp_from_email: Optional[str] = ""
    smtp_from_name: Optional[str] = "Enhanced Living Whisperwood"
    smtp_use_ssl: bool = False
    smtp_use_tls: bool = True
    gdrive_backup_target: Optional[str] = ""
    gdrive_folder_link: Optional[str] = ""
    gdrive_service_account_path: Optional[str] = ""
    clear_smtp_password: bool = False


class FirmwareReleasePayload(BaseModel):
    device_id: str = "all"
    released_by: Optional[str] = "system"


class BackupCreatePayload(BaseModel):
    created_by: Optional[str] = "system"
    upload_to_drive: bool = True


class BackupRestorePayload(BaseModel):
    path: str
    confirm_text: str
    restored_by: Optional[str] = "system"


def resident_sql_values(payload: ResidentPayload, resident_id: int | None = None):
    data = payload.dict()
    texture = first_value(data.get("texture"), data.get("allergies"))
    fluids = first_value(data.get("fluids"), data.get("schedule"))
    data["texture"] = texture
    data["allergies"] = texture
    data["fluids"] = fluids
    data["schedule"] = fluids
    data["safety_review_flag"] = bool_value(data.get("safety_review_flag"), data.get("needs_safety_review"))
    data["status_alert"] = data.get("status_alert") or ""
    data["id"] = resident_id
    data["t"] = now()
    return data


@app.get("/health")
def health():
    uptime_s = int((datetime.utcnow() - STARTED_AT).total_seconds())
    return {
        "ok": True,
        "service": "control",
        "version": "0.6.2",
        "hostname": platform.node(),
        "time": now(),
        "uptime": f"{uptime_s}s",
        "uptime_s": uptime_s,
        "last_restart": STARTED_AT.isoformat(),
    }

@app.post("/auth/login")
def login(payload: LoginPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    user = db_one("SELECT * FROM users WHERE username=:u AND is_active=TRUE", {"u": payload.username})
    if not user or not verify_pw(user["password_hash"], payload.password):
        log_action(payload.username, "login", "auth", "failed", "Invalid credentials")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    db_exec("UPDATE users SET last_login=:t WHERE id=:id", {"t": now(), "id": user["id"]})
    log_action(payload.username, "login", "auth", "success", "Login successful")
    return {"ok": True, "user": {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "full_name": user["full_name"],
        "is_active": user["is_active"],
        "force_password_change": user["force_password_change"],
        "password_must_change": user["force_password_change"]
    }}

@app.post("/auth/change-password")
def change_password(payload: ChangePasswordPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    user = db_one("SELECT * FROM users WHERE username=:u", {"u": payload.username})
    if not user or not verify_pw(user["password_hash"], payload.old_password):
        raise HTTPException(status_code=401, detail="Invalid current password")
    db_exec("UPDATE users SET password_hash=:p, force_password_change=FALSE WHERE username=:u",
            {"p": hash_pw(payload.new_password), "u": payload.username})
    return {"ok": True}

@app.post("/auth/temp-password")
def temp_password(payload: TempPasswordPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    alphabet = string.ascii_letters + string.digits
    temp = "".join(secrets.choice(alphabet) for _ in range(12))
    db_exec("UPDATE users SET password_hash=:p, force_password_change=TRUE WHERE username=:u",
            {"p": hash_pw(temp), "u": payload.username})
    return {"ok": True, "username": payload.username, "temporary_password": temp, "force_password_change": True}

@app.get("/users")
def users(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return {"ok": True, "users": db_all("""
        SELECT id, username, role, full_name, is_active, force_password_change, created_at, last_login
        FROM users ORDER BY id
    """)}

@app.post("/users")
def create_user(payload: UserPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    db_exec("""
        INSERT INTO users(username,password_hash,role,full_name,is_active,force_password_change,created_at)
        VALUES(:u,:p,:r,:f,:a,:c,:t)
    """, {"u": payload.username, "p": hash_pw(payload.password), "r": payload.role, "f": payload.full_name,
          "a": payload.is_active, "c": payload.force_password_change, "t": now()})
    return {"ok": True}

@app.put("/users/{username}/status")
def set_user_status(username: str, payload: UserStatusPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    db_exec("UPDATE users SET is_active=:a WHERE username=:u", {"a": payload.is_active, "u": username})
    return {"ok": True}

@app.get("/residents")
def residents(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return {"ok": True, "residents": db_all("SELECT * FROM residents WHERE archived=FALSE ORDER BY id DESC")}

@app.post("/residents")
def create_resident(payload: ResidentPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    row = db_one("""
        INSERT INTO residents(resident_uid,full_name,room,status_alert,diet,texture,allergies,note,drinks,fluids,schedule,active,
        safety_review_flag,safety_review_note,lcd_schedule_enabled,lcd_on_time,lcd_off_time,sleep_if_no_image,created_at,updated_at)
        VALUES(:resident_uid,:full_name,:room,:status_alert,:diet,:texture,:allergies,:note,:drinks,:fluids,:schedule,:active,
        :safety_review_flag,:safety_review_note,:lcd_schedule_enabled,:lcd_on_time,:lcd_off_time,:sleep_if_no_image,:t,:t)
        RETURNING *
    """, resident_sql_values(payload))
    log_resident_audit("system", "resident_create", row, old_values={}, new_values=row, reason="Resident created")
    log_action("system", "resident_create", row.get("resident_uid") or "", "success", "Resident created", payload=payload.dict(), response=row)
    return {"ok": True, "resident": row}

@app.put("/residents/{resident_id}")
def update_resident(resident_id: int, payload: ResidentPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    old = db_one("SELECT * FROM residents WHERE id=:id", {"id": resident_id})
    if not old:
        raise HTTPException(status_code=404, detail="Resident not found")
    row = db_one("""
        UPDATE residents SET resident_uid=:resident_uid, full_name=:full_name, room=:room, status_alert=:status_alert,
        diet=:diet, texture=:texture, allergies=:allergies, note=:note, drinks=:drinks,
        fluids=:fluids, schedule=:schedule, active=:active,
        safety_review_flag=:safety_review_flag, safety_review_note=:safety_review_note,
        lcd_schedule_enabled=:lcd_schedule_enabled, lcd_on_time=:lcd_on_time,
        lcd_off_time=:lcd_off_time, sleep_if_no_image=:sleep_if_no_image, updated_at=:t
        WHERE id=:id
        RETURNING *
    """, resident_sql_values(payload, resident_id))
    log_resident_audit("system", "resident_update", row, old_values=old, new_values=row, reason="Resident updated")
    log_action("system", "resident_update", row.get("resident_uid") or "", "success", "Resident updated", payload={"before": old, "after": payload.dict()}, response=row)
    return {"ok": True, "resident": row}

@app.put("/residents/{resident_id}/archive")
def archive_resident(resident_id: int, payload: ArchivePayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    old = db_one("SELECT * FROM residents WHERE id=:id", {"id": resident_id})
    db_exec("UPDATE residents SET archived=:a, updated_at=:t WHERE id=:id", {"a": payload.archived, "t": now(), "id": resident_id})
    row = db_one("SELECT * FROM residents WHERE id=:id", {"id": resident_id}) or old or {"id": resident_id}
    log_resident_audit("system", "resident_delete", row, old_values=old or {}, new_values=row, reason=payload.reason or "Resident archived")
    log_action("system", "resident_delete", row.get("resident_uid") or str(resident_id), "success", "Resident archived", payload=payload.dict(), response=row)
    return {"ok": True}

@app.post("/residents/{resident_id}/document")
async def upload_document(
    resident_id: int,
    document: UploadFile | None = File(default=None),
    file: UploadFile | None = File(default=None),
    x_whisperwood_key: str | None = Header(default=None),
):
    require_key(x_whisperwood_key)
    file = document or file
    if not file:
        raise HTTPException(status_code=400, detail="Document file is required")
    safe_name = file.filename.replace("/", "_")
    path = os.path.join(DOC_DIR, f"resident_{resident_id}_{safe_name}")
    with open(path, "wb") as f:
        f.write(await file.read())
    db_exec("UPDATE residents SET source_document_path=:p, source_document_name=:n, updated_at=:t WHERE id=:id",
            {"p": path, "n": safe_name, "t": now(), "id": resident_id})
    row = db_one("SELECT * FROM residents WHERE id=:id", {"id": resident_id}) or {"id": resident_id}
    log_resident_audit("system", "resident_document_upload", row, old_values={}, new_values={"source_document_path": path, "source_document_name": safe_name}, reason="Source document uploaded", source_document_path=path)
    return {"ok": True, "filename": safe_name}

@app.get("/residents/{resident_id}/document")
def get_document(resident_id: int, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    r = db_one("SELECT source_document_path, source_document_name FROM residents WHERE id=:id", {"id": resident_id})
    if not r or not r["source_document_path"] or not os.path.exists(r["source_document_path"]):
        raise HTTPException(status_code=404, detail="Document not found")
    return FileResponse(r["source_document_path"], filename=r["source_document_name"] or "document")

@app.post("/residents/{resident_id}/image")
async def upload_image(
    resident_id: int,
    image: UploadFile | None = File(default=None),
    file: UploadFile | None = File(default=None),
    x_whisperwood_key: str | None = Header(default=None),
):
    require_key(x_whisperwood_key)
    file = image or file
    if not file:
        raise HTTPException(status_code=400, detail="Image file is required")
    safe_name = file.filename.replace("/", "_")
    path = os.path.join(IMG_DIR, f"resident_{resident_id}_{safe_name}")
    with open(path, "wb") as f:
        f.write(await file.read())
    db_exec("UPDATE residents SET image_path=:p, image_name=:n, updated_at=:t WHERE id=:id",
            {"p": path, "n": safe_name, "t": now(), "id": resident_id})
    row = db_one("SELECT * FROM residents WHERE id=:id", {"id": resident_id}) or {"id": resident_id}
    log_resident_audit("system", "resident_photo_upload", row, old_values={}, new_values={"image_path": path, "image_name": safe_name}, reason="Resident photo uploaded")
    return {"ok": True, "filename": safe_name}

@app.get("/residents/{resident_id}/image")
def get_image(resident_id: int, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    r = db_one("SELECT image_path, image_name FROM residents WHERE id=:id", {"id": resident_id})
    if not r or not r["image_path"] or not os.path.exists(r["image_path"]):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(r["image_path"], filename=r["image_name"] or "image")

@app.get("/devices")
def devices(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    rows = merged_devices()
    try:
        process_battery_email_alerts(rows)
    except Exception as exc:
        log_action("system", "battery_email_alert", "devices", "warning", f"Battery email alert check failed: {exc}")
    return {"ok": True, "devices": rows}

@app.post("/devices")
def upsert_device(payload: DevicePayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    existing = db_one("SELECT id FROM devices WHERE device_id=:d", {"d": payload.device_id})
    data = {**payload.dict(), "t": now()}
    if existing:
        db_exec("""
            UPDATE devices SET ip_address=:ip_address, port=:port, firmware_version=:firmware_version,
            battery_level=:battery_level, battery_percent=:battery_percent,
            battery_ok=:battery_ok, battery_mv=:battery_mv, battery_voltage=:battery_voltage,
            battery_raw_percent=:battery_raw_percent, battery_low=:battery_low, battery_alert=:battery_alert,
            battery_plugged=:battery_plugged, battery_charging=:battery_charging, battery_full=:battery_full,
            rssi=:rssi, heap=:heap, last_status_at=:last_status_at,
            status=:status, updated_at=:t
            WHERE device_id=:device_id
        """, data)
    else:
        db_exec("""
            INSERT INTO devices(
                device_id,ip_address,port,firmware_version,battery_level,battery_percent,
                battery_ok,battery_mv,battery_voltage,battery_raw_percent,battery_low,battery_alert,
                battery_plugged,battery_charging,battery_full,rssi,heap,last_status_at,status,created_at,updated_at
            )
            VALUES(
                :device_id,:ip_address,:port,:firmware_version,:battery_level,:battery_percent,
                :battery_ok,:battery_mv,:battery_voltage,:battery_raw_percent,:battery_low,:battery_alert,
                :battery_plugged,:battery_charging,:battery_full,:rssi,:heap,:last_status_at,:status,:t,:t
            )
        """, data)
    return {"ok": True}

@app.post("/devices/pair")
def pair_device(payload: PairPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    db_exec("UPDATE devices SET paired_resident_id=:r, updated_at=:t WHERE device_id=:d",
            {"r": payload.resident_id, "d": payload.device_id, "t": now()})
    row = db_one("SELECT resident_uid, full_name FROM residents WHERE id=:id", {"id": payload.resident_id}) or {}
    log_action(
        "system",
        "pair_device",
        payload.device_id,
        "success",
        f"Device paired to {row.get('full_name') or payload.resident_id}",
        payload=payload.dict(),
        response={"resident": row},
    )
    return {"ok": True}

@app.post("/devices/unpair")
def unpair_device(payload: UnpairPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    old = db_one("SELECT paired_resident_id FROM devices WHERE device_id=:d", {"d": payload.device_id}) or {}
    db_exec("UPDATE devices SET paired_resident_id=NULL, updated_at=:t WHERE device_id=:d",
            {"d": payload.device_id, "t": now()})
    log_action("system", "unpair_device", payload.device_id, "success", "Device unpaired", payload=payload.dict(), response=old)
    return {"ok": True}

@app.get("/schedules")
def schedules(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return {"ok": True, "schedules": db_all("SELECT * FROM schedules ORDER BY id DESC")}

@app.post("/schedules")
def save_schedule(payload: SchedulePayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    existing = db_one("""
        SELECT id FROM schedules
        WHERE (resident_id=:r) OR (resident_id IS NULL AND device_id=:d)
        ORDER BY id DESC LIMIT 1
    """, {"r": payload.resident_id, "d": payload.device_id or "all"})
    data = {**payload.dict(), "t": now()}
    if existing:
        db_exec("""
            UPDATE schedules SET device_id=:device_id, enabled=:enabled, lcd_on_time=:lcd_on_time,
            lcd_off_time=:lcd_off_time, sleep_if_no_image=:sleep_if_no_image, updated_at=:t
            WHERE id=:id
        """, {**data, "id": existing["id"]})
    else:
        db_exec("""
            INSERT INTO schedules(resident_id,device_id,enabled,lcd_on_time,lcd_off_time,sleep_if_no_image,created_at,updated_at)
            VALUES(:resident_id,:device_id,:enabled,:lcd_on_time,:lcd_off_time,:sleep_if_no_image,:t,:t)
        """, data)
    return {"ok": True}

@app.get("/dashboard/summary")
def dashboard_summary(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    devices = merged_devices()
    today = datetime.utcnow().date().isoformat() + "%"
    return {"ok": True, "summary": {
        "active_residents": db_one("SELECT COUNT(*) c FROM residents WHERE active=TRUE AND archived=FALSE")["c"],
        "inactive_residents": db_one("SELECT COUNT(*) c FROM residents WHERE active=FALSE AND archived=FALSE")["c"],
        "known_devices": len(devices),
        "online_devices": sum(1 for device in devices if device.get("is_online")),
        "paired_devices": db_one("SELECT COUNT(*) c FROM devices WHERE paired_resident_id IS NOT NULL")["c"],
        "recent_activity": db_one("SELECT COUNT(*) c FROM audit_logs")["c"],
        "recent_activity_total": db_one("SELECT COUNT(*) c FROM audit_logs")["c"],
        "recent_activity_today": db_one("SELECT COUNT(*) c FROM audit_logs WHERE timestamp LIKE :today", {"today": today})["c"],
        "failed_updates": db_one("SELECT COUNT(*) c FROM audit_logs WHERE lower(result) NOT IN ('success','ok','true','1')")["c"],
        "safety_reviews": db_one("SELECT COUNT(*) c FROM residents WHERE archived=FALSE AND safety_review_flag=TRUE")["c"],
        "pending_requests": db_one("SELECT COUNT(*) c FROM resident_change_requests WHERE upper(status)='PENDING' OR lower(status)='pending'")["c"],
        "verification_checks": db_one("SELECT COUNT(*) c FROM verification_checks")["c"],
        "verification_mismatches": db_one("SELECT COUNT(*) c FROM verification_checks WHERE upper(status)='MISMATCH'")["c"],
        "database_mode": "server",
    }}

@app.get("/logs")
def logs(limit: int = 500, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return {"ok": True, "logs": db_all("SELECT * FROM audit_logs ORDER BY id DESC LIMIT :limit", {"limit": limit})}

@app.get("/logs/{log_id}")
def get_log(log_id: int, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    row = db_one("SELECT * FROM audit_logs WHERE id=:id", {"id": log_id})
    if not row:
        raise HTTPException(status_code=404, detail="Log not found")
    return {"ok": True, "log": row}

@app.get("/resident-audit")
def resident_audit(limit: int = 200, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    rows = db_all("""
        SELECT id,
               created_at,
               change_type AS action_type,
               resident_id,
               resident_uid,
               changed_by AS pushed_by_username,
               '' AS device_id,
               TRUE AS success,
               reason AS message,
               old_values AS payload_json,
               new_values AS response_json,
               source_document_path
        FROM resident_audit
        ORDER BY id DESC
        LIMIT :limit
    """, {"limit": limit})
    return {"ok": True, "audit": rows, "logs": rows}

@app.get("/residents/{resident_id}/audit")
def resident_audit_for_resident(resident_id: int, limit: int = 200, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    rows = db_all("""
        SELECT id,
               created_at,
               change_type AS action_type,
               resident_id,
               resident_uid,
               changed_by AS pushed_by_username,
               '' AS device_id,
               TRUE AS success,
               reason AS message,
               old_values AS payload_json,
               new_values AS response_json,
               source_document_path
        FROM resident_audit
        WHERE resident_id=:resident_id
        ORDER BY id DESC
        LIMIT :limit
    """, {"resident_id": resident_id, "limit": limit})
    return {"ok": True, "audit": rows, "logs": rows}

@app.post("/resident-change-requests")
def create_change_request(payload: ChangeRequestPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    row = db_one("SELECT * FROM residents WHERE id=:id", {"id": payload.resident_id}) or {}
    proposed = payload.proposed_payload or payload.proposed_new or {}
    comment = payload.comment or payload.reason or ""
    requested_by = payload.requested_by_username or payload.requested_by or "staff"
    inserted = db_one("""
        INSERT INTO resident_change_requests(
            resident_id, resident_uid, requested_by, status,
            proposed_new, proposed_payload, reason, comment,
            requested_by_user_id, requested_by_username, created_at
        )
        VALUES(
            :resident_id, :resident_uid, :requested_by, 'PENDING',
            :proposed_new, :proposed_payload, :reason, :comment,
            :requested_by_user_id, :requested_by_username, :created_at
        )
        RETURNING *
    """, {
        "resident_id": payload.resident_id,
        "resident_uid": payload.resident_uid or row.get("resident_uid") or "",
        "requested_by": requested_by,
        "proposed_new": json_value(proposed),
        "proposed_payload": json_value(proposed),
        "reason": comment,
        "comment": comment,
        "requested_by_user_id": payload.requested_by_user_id,
        "requested_by_username": requested_by,
        "created_at": now(),
    })
    log_action(requested_by, "resident_review_request", row.get("resident_uid") or "", "success", "Resident review request submitted", payload=payload.dict(), response=inserted)
    return {"ok": True, "request": inserted}

@app.get("/resident-change-requests")
def change_requests(status: Optional[str] = None, limit: int = 100, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    where = ""
    params = {"limit": limit}
    if status:
        where = "WHERE upper(cr.status)=upper(:status)"
        params["status"] = status
    rows = db_all(f"""
        SELECT cr.*,
               upper(cr.status) AS status,
               COALESCE(cr.proposed_payload, cr.proposed_new) AS proposed_payload,
               COALESCE(cr.comment, cr.reason, '') AS comment,
               COALESCE(cr.requested_by_username, cr.requested_by, '') AS requested_by_username,
               COALESCE(cr.reviewed_by_username, cr.decision_by, '') AS reviewed_by_username,
               COALESCE(cr.review_note, cr.decision_note, '') AS review_note,
               COALESCE(cr.reviewed_at, cr.decided_at, '') AS reviewed_at,
               r.full_name,
               r.room
        FROM resident_change_requests cr
        LEFT JOIN residents r ON r.id = cr.resident_id
        {where}
        ORDER BY cr.id DESC
        LIMIT :limit
    """, params)
    return {"ok": True, "requests": rows}

@app.put("/resident-change-requests/{request_id}/decision")
def decide_change_request(request_id: int, payload: DecisionPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    status = (payload.status or payload.decision or "REVIEWED").upper()
    reviewed_by = payload.reviewed_by_username or payload.decision_by or "admin"
    note = payload.review_note or payload.decision_note or ""
    db_exec("""
        UPDATE resident_change_requests
        SET status=:status,
            decision_by=:decision_by,
            decision_note=:decision_note,
            decided_at=:decided_at,
            reviewed_by_user_id=:reviewed_by_user_id,
            reviewed_by_username=:reviewed_by_username,
            review_note=:review_note,
            reviewed_at=:reviewed_at
        WHERE id=:id
    """, {
        "status": status,
        "decision_by": reviewed_by,
        "decision_note": note,
        "decided_at": now(),
        "reviewed_by_user_id": payload.reviewed_by_user_id,
        "reviewed_by_username": reviewed_by,
        "review_note": note,
        "reviewed_at": now(),
        "id": request_id,
    })
    request = db_one("SELECT * FROM resident_change_requests WHERE id=:id", {"id": request_id}) or {}
    log_action(reviewed_by, "resident_review_decision", request.get("resident_uid") or "", "success", f"Review request {status.lower()}", payload=payload.dict(), response=request)
    return {"ok": True, "request": request}

@app.post("/verification-checks")
def create_verification_check(payload: VerificationPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    checked_by = payload.checked_by_username or payload.checked_by or "verifier"
    row = db_one("""
        INSERT INTO verification_checks(
            resident_id, resident_uid, device_id, status, note,
            checked_by, checked_at, checked_by_user_id, checked_by_username, created_at
        )
        VALUES(
            :resident_id, :resident_uid, :device_id, :status, :note,
            :checked_by, :checked_at, :checked_by_user_id, :checked_by_username, :created_at
        )
        RETURNING *
    """, {
        "resident_id": payload.resident_id,
        "resident_uid": payload.resident_uid,
        "device_id": payload.device_id or "",
        "status": payload.status.upper(),
        "note": payload.note or "",
        "checked_by": checked_by,
        "checked_at": now(),
        "checked_by_user_id": payload.checked_by_user_id,
        "checked_by_username": checked_by,
        "created_at": now(),
    })
    log_action(checked_by, "display_verification", payload.device_id or "", "success", "Display verification recorded", payload=payload.dict(), response=row)
    return {"ok": True, "check": row}

@app.get("/verification-checks")
def verification_checks(limit: int = 100, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    rows = db_all("""
        SELECT vc.*,
               COALESCE(vc.created_at, vc.checked_at) AS created_at,
               COALESCE(vc.checked_by_username, vc.checked_by, '') AS checked_by_username,
               r.full_name,
               r.room
        FROM verification_checks vc
        LEFT JOIN residents r ON r.id = vc.resident_id
        ORDER BY vc.id DESC
        LIMIT :limit
    """, {"limit": limit})
    return {"ok": True, "checks": rows}

@app.post("/it-audit-logs")
def create_it_audit(payload: ItLogPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    row = db_one("""
        INSERT INTO it_audit_logs(timestamp, username, action, target, result, message, payload_json, response_json)
        VALUES(:timestamp, :username, :action, :target, :result, :message, :payload_json, :response_json)
        RETURNING *
    """, {
        "timestamp": now(),
        "username": payload.username,
        "action": payload.action,
        "target": payload.target or "",
        "result": payload.result or "success",
        "message": payload.message or "",
        "payload_json": json_value(payload.payload_json),
        "response_json": json_value(payload.response_json),
    })
    return {"ok": True, "log": row}

@app.get("/it-audit-logs")
def it_audit_logs(limit: int = 100, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    rows = db_all("""
        SELECT id, timestamp AS created_at, timestamp, username, action, target, result, message, payload_json, response_json
        FROM it_audit_logs
        ORDER BY id DESC
        LIMIT :limit
    """, {"limit": limit})
    return {"ok": True, "logs": rows}


def grouped_dropdown_options():
    rows = db_all("""
        SELECT category, option_text
        FROM resident_dropdown_options
        WHERE active=TRUE
        ORDER BY category, sort_order, option_text
    """)
    options = {}
    for row in rows:
        options.setdefault(row["category"], []).append(row["option_text"])
    return options


def save_grouped_dropdown_options(options: dict[str, list[str]]):
    db_exec("UPDATE resident_dropdown_options SET active=FALSE, updated_at=:t", {"t": now()})
    for category, values in (options or {}).items():
        for order, value in enumerate(values or []):
            text_value = str(value or "").strip()
            category_value = str(category or "").strip()
            if not category_value or not text_value:
                continue
            db_exec("""
                INSERT INTO resident_dropdown_options(category, option_text, sort_order, active, created_at, updated_at)
                VALUES(:category, :option_text, :sort_order, TRUE, :t, :t)
                ON CONFLICT(category, option_text)
                DO UPDATE SET sort_order=:sort_order, active=TRUE, updated_at=:t
            """, {
                "category": category_value,
                "option_text": text_value,
                "sort_order": order,
                "t": now(),
            })


@app.get("/resident-dropdown-options")
def resident_dropdown_options(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return {"ok": True, "options": grouped_dropdown_options()}


@app.post("/resident-dropdown-options")
@app.put("/resident-dropdown-options")
def save_resident_dropdown_options(payload: DropdownOptionsPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    save_grouped_dropdown_options(payload.options or {})
    return {"ok": True, "options": grouped_dropdown_options()}


@app.get("/dropdown-options")
def dropdown_options_alias(x_whisperwood_key: str | None = Header(default=None)):
    return resident_dropdown_options(x_whisperwood_key)


@app.post("/dropdown-options")
@app.put("/dropdown-options")
def save_dropdown_options_alias(payload: DropdownOptionsPayload, x_whisperwood_key: str | None = Header(default=None)):
    return save_resident_dropdown_options(payload, x_whisperwood_key)


@app.get("/battery-alert-settings")
def battery_alert_settings(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    settings = normalize_battery_alert_settings(get_system_setting("battery_alert_settings", DEFAULT_BATTERY_ALERT_SETTINGS))
    return {"ok": True, "settings": settings, "email_configured": smtp_configured()}


@app.post("/battery-alert-settings")
@app.put("/battery-alert-settings")
def save_battery_alert_settings(payload: BatteryAlertSettingsPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    settings = normalize_battery_alert_settings(payload.dict())
    save_system_setting("battery_alert_settings", settings)
    log_action(
        "system",
        "battery_alert_settings",
        "devices",
        "success",
        "Battery alert policy updated",
        payload=settings,
        response={"ok": True},
    )
    return {"ok": True, "settings": settings, "email_configured": smtp_configured()}


@app.post("/battery-alert-settings/test-email")
def battery_alert_test_email(payload: BatteryTestEmailPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    settings = normalize_battery_alert_settings(get_system_setting("battery_alert_settings", DEFAULT_BATTERY_ALERT_SETTINGS))
    recipients = payload.recipients or settings.get("recipient_emails") or []
    result = send_email_message(
        f"{settings.get('email_subject_prefix') or 'Whisperwood Battery Alert'}: Test email",
        "This is a test battery notification from Enhanced Living Whisperwood.\n\nIf you received this, Raspberry Pi email alerts are configured.",
        recipients,
    )
    log_action("system", "battery_test_email", "devices", "success" if result.get("ok") else "failed", result.get("error") or result.get("reason") or "Battery test email sent", payload={"recipients": recipients}, response=result)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.get("/integration-settings")
def integration_settings(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return {"ok": True, "settings": public_integration_settings()}


@app.post("/integration-settings")
@app.put("/integration-settings")
def save_integration_settings(payload: IntegrationSettingsPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    previous = get_integration_settings()
    incoming = payload.dict()
    if incoming.pop("clear_smtp_password", False):
        incoming["smtp_password"] = ""
        settings = normalize_integration_settings(incoming, previous=previous)
    else:
        settings = normalize_integration_settings(incoming, previous=previous, preserve_blank_password=True)
    save_system_setting("integration_settings", settings)
    log_action(
        "system",
        "integration_settings",
        "settings",
        "success",
        "Email and backup integration settings updated",
        payload={**public_integration_settings(settings), "smtp_password_configured": bool(settings.get("smtp_password"))},
        response={"ok": True},
    )
    return {"ok": True, "settings": public_integration_settings(settings)}


@app.post("/integration-settings/test-email")
def integration_settings_test_email(payload: BatteryTestEmailPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    settings = get_integration_settings()
    recipients = payload.recipients or normalize_battery_alert_settings(get_system_setting("battery_alert_settings", DEFAULT_BATTERY_ALERT_SETTINGS)).get("recipient_emails") or []
    result = send_email_message(
        "Enhanced Living Whisperwood: Email settings test",
        "This is a test email from the Raspberry Pi Control Service.\n\nIf you received this, the saved SMTP settings are working.",
        recipients,
        settings=settings,
    )
    log_action("system", "integration_test_email", "settings", "success" if result.get("ok") else "failed", result.get("error") or result.get("reason") or "Integration test email sent", payload={"recipients": recipients}, response=result)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result

@app.get("/system")
def system_status(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    disk = shutil.disk_usage("/")
    return {"hostname": platform.node(), "platform": platform.platform(),
            "cpu_percent": psutil.cpu_percent(interval=0.2),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": round((disk.used/disk.total)*100,2)}

@app.get("/network")
def network_status(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    lan_ips = []
    try:
        lan_ips = [ip for ip in subprocess.check_output(["hostname","-I"], text=True).strip().split() if "." in ip and not ip.startswith("127.")]
    except Exception:
        pass
    try:
        ts_ip = subprocess.check_output(["tailscale","ip","-4"], text=True).strip()
    except Exception:
        ts_ip = ""
    return {"hostname": platform.node(), "lan_ips": lan_ips, "tailscale_ip": ts_ip}


@app.get("/tailscale")
def tailscale_status(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    try:
        ts_ip = subprocess.check_output(["tailscale", "ip", "-4"], text=True, timeout=4).strip()
    except Exception:
        ts_ip = ""
    try:
        status_text = subprocess.check_output(["tailscale", "status", "--self"], text=True, timeout=4).strip()
    except Exception:
        status_text = ""
    return {
        "ok": bool(ts_ip),
        "ip": ts_ip,
        "tailscale_ip": ts_ip,
        "status": "connected" if ts_ip else "not available",
        "raw": status_text,
    }

@app.get("/operation/status")
def operation_status(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    result = subprocess.run(["systemctl","is-active","whisperwood-operation"], text=True, capture_output=True)
    health_payload = {}
    try:
        health_payload = operation_request("GET", "/health", timeout=4)
    except HTTPException as exc:
        health_payload = {"ok": False, "error": str(exc.detail)}
    return {"status": result.stdout.strip(), "operation": health_payload}

@app.post("/operation/restart")
def restart_operation(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    subprocess.run(["sudo","systemctl","restart","whisperwood-operation"], check=True)
    return {"ok": True, "message": "Operation Manager restarted"}

@app.get("/operation/devices")
def operation_devices(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return operation_request("GET", "/devices", timeout=6)

@app.post("/operation/send")
def operation_send(payload: Optional[dict] = Body(default=None), x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return operation_request("POST", "/send", json=payload or {}, timeout=100)

@app.post("/operation/send_image")
async def operation_send_image(id: str = Form(default=""), image: UploadFile = File(...), x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    raw = await image.read()
    files = {"image": (image.filename or "resident_photo", raw, image.content_type or "application/octet-stream")}
    return operation_request("POST", "/send_image", files=files, data={"id": id}, timeout=100)

@app.post("/operation/lcd")
def operation_lcd(payload: Optional[dict] = Body(default=None), x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return operation_request("POST", "/lcd", json=payload or {}, timeout=45)

@app.post("/operation/schedule")
def operation_schedule(payload: Optional[dict] = Body(default=None), x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    data = dict(payload or {})
    if not data.get("device_id"):
        data["device_id"] = "all"
    result = operation_request("POST", "/schedule", json=data, timeout=15)
    try:
        schedule_payload = SchedulePayload(**{
            "resident_id": data.get("resident_id"),
            "device_id": data.get("device_id") or "all",
            "enabled": bool(data.get("enabled")),
            "lcd_on_time": data.get("lcd_on_time") or "07:00",
            "lcd_off_time": data.get("lcd_off_time") or "20:00",
            "sleep_if_no_image": bool(data.get("sleep_if_no_image", True)),
        })
        save_schedule(schedule_payload, x_whisperwood_key)
    except Exception:
        pass
    return result

@app.post("/operation/resident-display")
def operation_resident_display(payload: Optional[dict] = Body(default=None), x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    resident_id = payload.get("resident_id")
    device_id = payload.get("device_id") or payload.get("id") or ""
    if not resident_id:
        raise HTTPException(status_code=400, detail="resident_id is required")
    row = db_one("""
        SELECT r.*, d.device_id AS paired_device_id
        FROM residents r
        LEFT JOIN devices d ON d.paired_resident_id = r.id
        WHERE r.id=:id
        LIMIT 1
    """, {"id": resident_id})
    if not row:
        raise HTTPException(status_code=404, detail="Resident not found")
    outbound = resident_display_payload(row, device_id)
    if not outbound.get("device_id"):
        raise HTTPException(status_code=400, detail="Resident is not paired to a device")
    result = operation_request("POST", "/resident_display", json=outbound, timeout=150)
    log_action(
        "system",
        "resident_display",
        outbound.get("device_id") or "",
        "success" if result.get("ok", True) else "failed",
        "Resident display payload forwarded to Operation Manager",
        payload=outbound,
        response=result,
    )
    return result


@app.get("/firmware/releases")
def firmware_releases(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    rows = db_all("""
        SELECT id, version, filename, path, size_bytes, sha256, md5, notes, status,
               uploaded_by, released_by, target, last_result, created_at, released_at
        FROM firmware_releases
        ORDER BY id DESC
    """)
    return {"ok": True, "releases": rows}


@app.post("/firmware/upload")
async def firmware_upload(
    firmware: UploadFile = File(...),
    version: str = Form(default=""),
    notes: str = Form(default=""),
    uploaded_by: str = Form(default="system"),
    x_whisperwood_key: str | None = Header(default=None),
):
    require_key(x_whisperwood_key)
    filename = (firmware.filename or "firmware.bin").replace("/", "_").replace("\\", "_")
    if not filename.lower().endswith(".bin"):
        raise HTTPException(status_code=400, detail="Upload the compiled ESP32 firmware .bin file")
    raw = await firmware.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Firmware file is empty")
    version_text = (version or Path(filename).stem).strip()
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    safe_name = f"{stamp}_{filename}"
    path = Path(FIRMWARE_DIR) / safe_name
    path.write_bytes(raw)
    sha256 = hashlib.sha256(raw).hexdigest()
    md5 = hashlib.md5(raw).hexdigest()
    row = db_one("""
        INSERT INTO firmware_releases(version, filename, path, size_bytes, sha256, md5, notes, status, uploaded_by, created_at)
        VALUES(:version, :filename, :path, :size_bytes, :sha256, :md5, :notes, 'uploaded', :uploaded_by, :created_at)
        RETURNING *
    """, {
        "version": version_text,
        "filename": filename,
        "path": str(path),
        "size_bytes": len(raw),
        "sha256": sha256,
        "md5": md5,
        "notes": notes or "",
        "uploaded_by": uploaded_by or "system",
        "created_at": now(),
    })
    log_action(uploaded_by or "system", "firmware_upload", version_text, "success", "ESP32 firmware uploaded", payload={"filename": filename, "size": len(raw)}, response=row)
    return {"ok": True, "release": row}


@app.post("/firmware/releases/{release_id}/release")
def firmware_release(release_id: int, payload: FirmwareReleasePayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    row = db_one("SELECT * FROM firmware_releases WHERE id=:id", {"id": release_id})
    if not row:
        raise HTTPException(status_code=404, detail="Firmware release not found")
    path = row.get("path") or ""
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Firmware .bin file is missing from the Raspberry Pi")
    with open(path, "rb") as fh:
        raw = fh.read()
    files = {"firmware": (row.get("filename") or "firmware.bin", raw, "application/octet-stream")}
    data = {"device_id": payload.device_id or "all", "version": row.get("version") or ""}
    result = operation_request("POST", "/firmware/ota", files=files, data=data, timeout=600)
    db_exec("""
        UPDATE firmware_releases
        SET status=:status, released_by=:released_by, target=:target, last_result=:last_result, released_at=:released_at
        WHERE id=:id
    """, {
        "status": "released" if result.get("ok") else "failed",
        "released_by": payload.released_by or "system",
        "target": payload.device_id or "all",
        "last_result": json_value(result),
        "released_at": now(),
        "id": release_id,
    })
    log_action(payload.released_by or "system", "firmware_release", row.get("version") or str(release_id), "success" if result.get("ok") else "failed", "ESP32 OTA firmware release sent", payload=payload.dict(), response=result)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result)
    return {"ok": True, "result": result}


@app.get("/backups")
def backups(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    integrations = public_integration_settings()
    return {
        "ok": True,
        "backups": list_local_backups(),
        "google_drive": {
            "configured": bool(integrations.get("gdrive_backup_target")),
            "target": integrations.get("gdrive_backup_target") or "",
            "folder_link": integrations.get("gdrive_folder_link") or "",
            "service_account_path": integrations.get("gdrive_service_account_path") or "",
            "rclone_available": bool(integrations.get("rclone_available")),
        },
    }


@app.post("/backups")
def create_backup(payload: BackupCreatePayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return create_backup_archive(payload.created_by or "system", upload_to_drive=payload.upload_to_drive)


@app.post("/backups/restore")
def restore_backup(payload: BackupRestorePayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return restore_backup_archive(payload.path, payload.confirm_text, payload.restored_by or "system")

@app.get("/bootstrap/info")
def bootstrap_info(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return {
        "ok": True,
        "version": "0.6.2",
        "database_user": "whisperwood_app",
        "default_users": [
            {"username": "admin", "password": "admin123", "role": "admin"},
            {"username": "itadmin", "password": "itadmin123", "role": "it_admin"}
        ],
        "roles": ["admin", "staff", "it_admin", "verifier"],
        "note": "Desktop software must connect through Control Service APIs only."
    }
