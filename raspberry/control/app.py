from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form, Body
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Any
from sqlalchemy import create_engine, text
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
import os, json, platform, subprocess, shutil, psutil, requests, secrets, string, time

app = FastAPI(title="Whisperwood Control Service", version="0.4.0")

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
os.makedirs(DOC_DIR, exist_ok=True)
os.makedirs(IMG_DIR, exist_ok=True)

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
            "status": "online" if device.get("is_online") or device.get("online") else "offline",
            "last_seen": now(),
            "t": now(),
        }
        if existing:
            db_exec("""
                UPDATE devices SET ip_address=:ip_address, port=:port, firmware_version=:firmware_version,
                battery_level=:battery_level, battery_percent=:battery_percent, status=:status,
                last_seen=:last_seen, updated_at=:t WHERE device_id=:device_id
            """, data)
        else:
            db_exec("""
                INSERT INTO devices(device_id,ip_address,port,firmware_version,battery_level,battery_percent,status,last_seen,created_at,updated_at)
                VALUES(:device_id,:ip_address,:port,:firmware_version,:battery_level,:battery_percent,:status,:last_seen,:t,:t)
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
            "pending_seq": live.get("pending_seq"),
            "pending_img_seq": live.get("pending_img_seq"),
            "pending_lcd_seq": live.get("pending_lcd_seq"),
        }
        out.append(item)
    for device_id, live in operation_by_id.items():
        if device_id in seen:
            continue
        out.append({
            "id": device_id,
            "device_id": device_id,
            "ip": live.get("ip") or "",
            "lan_ip": live.get("ip") or "",
            "port": live.get("port") or 5000,
            "fw": live.get("fw") or "",
            "firmware": live.get("fw") or "",
            "status": "online",
            "is_online": True,
            "online": True,
            "last_seen_s": live.get("last_seen_s") or 0,
            "battery_level": live.get("battery_level"),
            "battery": live.get("battery_level"),
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
    requested_by: str
    proposed_new: dict[str, Any]
    reason: Optional[str] = ""

class DecisionPayload(BaseModel):
    decision: str
    decision_by: str
    decision_note: Optional[str] = ""

class VerificationPayload(BaseModel):
    resident_id: int
    resident_uid: str
    device_id: Optional[str] = ""
    status: str
    note: Optional[str] = ""
    checked_by: str

class ItLogPayload(BaseModel):
    username: str
    action: str
    target: Optional[str] = ""
    result: Optional[str] = "success"
    message: Optional[str] = ""
    payload_json: Optional[dict] = {}
    response_json: Optional[dict] = {}


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
    return {"ok": True, "service": "control", "version": "0.4.0", "time": now()}

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
    return {"ok": True, "resident": row}

@app.put("/residents/{resident_id}/archive")
def archive_resident(resident_id: int, payload: ArchivePayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    db_exec("UPDATE residents SET archived=:a, updated_at=:t WHERE id=:id", {"a": payload.archived, "t": now(), "id": resident_id})
    return {"ok": True}

@app.post("/residents/{resident_id}/document")
async def upload_document(resident_id: int, file: UploadFile = File(...), x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    safe_name = file.filename.replace("/", "_")
    path = os.path.join(DOC_DIR, f"resident_{resident_id}_{safe_name}")
    with open(path, "wb") as f:
        f.write(await file.read())
    db_exec("UPDATE residents SET source_document_path=:p, source_document_name=:n, updated_at=:t WHERE id=:id",
            {"p": path, "n": safe_name, "t": now(), "id": resident_id})
    return {"ok": True, "filename": safe_name}

@app.get("/residents/{resident_id}/document")
def get_document(resident_id: int, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    r = db_one("SELECT source_document_path, source_document_name FROM residents WHERE id=:id", {"id": resident_id})
    if not r or not r["source_document_path"] or not os.path.exists(r["source_document_path"]):
        raise HTTPException(status_code=404, detail="Document not found")
    return FileResponse(r["source_document_path"], filename=r["source_document_name"] or "document")

@app.post("/residents/{resident_id}/image")
async def upload_image(resident_id: int, file: UploadFile = File(...), x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    safe_name = file.filename.replace("/", "_")
    path = os.path.join(IMG_DIR, f"resident_{resident_id}_{safe_name}")
    with open(path, "wb") as f:
        f.write(await file.read())
    db_exec("UPDATE residents SET image_path=:p, image_name=:n, updated_at=:t WHERE id=:id",
            {"p": path, "n": safe_name, "t": now(), "id": resident_id})
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
    return {"ok": True, "devices": merged_devices()}

@app.post("/devices")
def upsert_device(payload: DevicePayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    existing = db_one("SELECT id FROM devices WHERE device_id=:d", {"d": payload.device_id})
    data = {**payload.dict(), "t": now()}
    if existing:
        db_exec("""
            UPDATE devices SET ip_address=:ip_address, port=:port, firmware_version=:firmware_version,
            battery_level=:battery_level, battery_percent=:battery_percent, status=:status, updated_at=:t
            WHERE device_id=:device_id
        """, data)
    else:
        db_exec("""
            INSERT INTO devices(device_id,ip_address,port,firmware_version,battery_level,battery_percent,status,created_at,updated_at)
            VALUES(:device_id,:ip_address,:port,:firmware_version,:battery_level,:battery_percent,:status,:t,:t)
        """, data)
    return {"ok": True}

@app.post("/devices/pair")
def pair_device(payload: PairPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    db_exec("UPDATE devices SET paired_resident_id=:r, updated_at=:t WHERE device_id=:d",
            {"r": payload.resident_id, "d": payload.device_id, "t": now()})
    return {"ok": True}

@app.post("/devices/unpair")
def unpair_device(payload: UnpairPayload, x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    db_exec("UPDATE devices SET paired_resident_id=NULL, updated_at=:t WHERE device_id=:d",
            {"d": payload.device_id, "t": now()})
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
    return {"ok": True, "summary": {
        "active_residents": db_one("SELECT COUNT(*) c FROM residents WHERE active=TRUE AND archived=FALSE")["c"],
        "inactive_residents": db_one("SELECT COUNT(*) c FROM residents WHERE active=FALSE AND archived=FALSE")["c"],
        "known_devices": len(devices),
        "online_devices": sum(1 for device in devices if device.get("is_online")),
        "paired_devices": db_one("SELECT COUNT(*) c FROM devices WHERE paired_resident_id IS NOT NULL")["c"]
    }}

@app.get("/logs")
def logs(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return {"ok": True, "logs": db_all("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 500")}

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
    return operation_request("POST", "/send", json=payload or {}, timeout=40)

@app.post("/operation/send_image")
async def operation_send_image(id: str = Form(default=""), image: UploadFile = File(...), x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    raw = await image.read()
    files = {"image": (image.filename or "resident_photo", raw, image.content_type or "application/octet-stream")}
    return operation_request("POST", "/send_image", files=files, data={"id": id}, timeout=55)

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
    result = operation_request("POST", "/resident_display", json=outbound, timeout=80)
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

@app.get("/bootstrap/info")
def bootstrap_info(x_whisperwood_key: str | None = Header(default=None)):
    require_key(x_whisperwood_key)
    return {
        "ok": True,
        "version": "0.4.0",
        "database_user": "whisperwood_app",
        "default_users": [
            {"username": "admin", "password": "admin123", "role": "admin"},
            {"username": "itadmin", "password": "itadmin123", "role": "it_admin"}
        ],
        "roles": ["admin", "staff", "it_admin", "verifier"],
        "note": "Desktop software must connect through Control Service APIs only."
    }
