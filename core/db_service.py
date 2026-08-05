import json
import mimetypes
import os
import sqlite3
import uuid
from datetime import datetime
from core.time_utils import format_readable_datetime

import psycopg2
from psycopg2.extras import RealDictCursor, Json

from config import APP_DATA_DIR, DATABASE_MODE, LOCAL_DB_PATH
from db_config import DB_CONFIG


def generate_resident_uid() -> str:
    return f"RES-{uuid.uuid4().hex[:8].upper()}"


class DatabaseService:
    def __init__(self):
        self.conn = None
        self.backend = None

    def connect(self):
        if self.conn is not None:
            if self.backend == "sqlite":
                return
            if not self.conn.closed:
                return

        if DATABASE_MODE.lower() in {"sqlite", "local", "demo"}:
            LOCAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(LOCAL_DB_PATH))
            self.conn.row_factory = sqlite3.Row
            self.backend = "sqlite"
            return

        raise RuntimeError("Direct PostgreSQL access is disabled. Use Server Mode through the Raspberry Pi Control Service.")

    def close(self):
        if self.conn and (self.backend == "sqlite" or not self.conn.closed):
            self.conn.close()
        self.conn = None
        self.backend = None

    def _cursor(self, dict_rows=False):
        self.connect()
        if self.backend == "sqlite":
            return self.conn.cursor()
        if dict_rows:
            return self.conn.cursor(cursor_factory=RealDictCursor)
        return self.conn.cursor()

    def _rows(self, rows):
        if self.backend == "sqlite":
            return [dict(row) for row in rows]
        return rows

    def _row(self, row):
        if row is None:
            return None
        if self.backend == "sqlite":
            return dict(row)
        return row

    def _json_value(self, value):
        if value is None:
            return None
        if self.backend == "sqlite":
            return json.dumps(value)
        return Json(value)

    def _parse_json_field(self, value):
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return value
        return value

    def ensure_tables(self):
        self.connect()
        cur = self.conn.cursor()

        if self.backend == "postgres":
            cur.execute("""
            CREATE TABLE IF NOT EXISTS residents (
                id SERIAL PRIMARY KEY,
                resident_uid VARCHAR(32) UNIQUE NOT NULL,
                full_name VARCHAR(255) NOT NULL,
                room VARCHAR(64),
                diet TEXT,
                texture TEXT,
                allergies TEXT,
                note TEXT,
                drinks TEXT,
                fluids TEXT,
                schedule TEXT,
                source_document TEXT,
                safety_review_note TEXT,
                needs_safety_review BOOLEAN NOT NULL DEFAULT FALSE,
                lcd_image_path TEXT,
                resident_photo_data BYTEA,
                resident_photo_mime TEXT,
                resident_photo_name TEXT,
                lcd_schedule_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                lcd_on_time TEXT,
                lcd_off_time TEXT,
                sleep_if_no_image BOOLEAN NOT NULL DEFAULT FALSE,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """)
            for column_sql in [
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS texture TEXT",
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS fluids TEXT",
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS schedule TEXT",
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS source_document TEXT",
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS safety_review_note TEXT",
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS needs_safety_review BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS lcd_image_path TEXT",
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS resident_photo_data BYTEA",
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS resident_photo_mime TEXT",
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS resident_photo_name TEXT",
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS lcd_schedule_enabled BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS lcd_on_time TEXT",
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS lcd_off_time TEXT",
                "ALTER TABLE residents ADD COLUMN IF NOT EXISTS sleep_if_no_image BOOLEAN NOT NULL DEFAULT FALSE",
            ]:
                cur.execute(column_sql)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS device_registry (
                id SERIAL PRIMARY KEY,
                device_id VARCHAR(128) UNIQUE NOT NULL,
                ip VARCHAR(64),
                port INTEGER,
                fw VARCHAR(64),
                last_seen_s INTEGER DEFAULT 9999,
                is_online BOOLEAN DEFAULT FALSE,
                battery_level INTEGER,
                paired_resident_id INTEGER NULL REFERENCES residents(id) ON DELETE SET NULL,
                last_sync_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS display_updates (
                id SERIAL PRIMARY KEY,
                action_type VARCHAR(64) NOT NULL,
                resident_id INTEGER NULL REFERENCES residents(id) ON DELETE SET NULL,
                resident_uid VARCHAR(32),
                device_id VARCHAR(128),
                pushed_by_user_id INTEGER,
                pushed_by_username VARCHAR(255),
                payload_json JSONB,
                response_json JSONB,
                success BOOLEAN NOT NULL DEFAULT FALSE,
                message TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS resident_change_requests (
                id SERIAL PRIMARY KEY,
                resident_id INTEGER NULL REFERENCES residents(id) ON DELETE SET NULL,
                resident_uid VARCHAR(32),
                proposed_payload JSONB,
                comment TEXT NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
                requested_by_user_id INTEGER,
                requested_by_username VARCHAR(255),
                reviewed_by_user_id INTEGER,
                reviewed_by_username VARCHAR(255),
                review_note TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                reviewed_at TIMESTAMP
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS verification_checks (
                id SERIAL PRIMARY KEY,
                resident_id INTEGER NULL REFERENCES residents(id) ON DELETE SET NULL,
                resident_uid VARCHAR(32),
                device_id VARCHAR(128),
                status VARCHAR(32) NOT NULL,
                note TEXT,
                checked_by_user_id INTEGER,
                checked_by_username VARCHAR(255),
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """)
            for column_sql in [
                "ALTER TABLE device_registry ADD COLUMN IF NOT EXISTS battery_level INTEGER",
                "ALTER TABLE device_registry ADD COLUMN IF NOT EXISTS battery_ok BOOLEAN",
                "ALTER TABLE device_registry ADD COLUMN IF NOT EXISTS battery_mv INTEGER",
                "ALTER TABLE device_registry ADD COLUMN IF NOT EXISTS battery_voltage REAL",
                "ALTER TABLE device_registry ADD COLUMN IF NOT EXISTS battery_raw_percent REAL",
                "ALTER TABLE device_registry ADD COLUMN IF NOT EXISTS battery_low BOOLEAN",
                "ALTER TABLE device_registry ADD COLUMN IF NOT EXISTS battery_alert BOOLEAN",
                "ALTER TABLE device_registry ADD COLUMN IF NOT EXISTS battery_plugged BOOLEAN",
                "ALTER TABLE device_registry ADD COLUMN IF NOT EXISTS battery_charging BOOLEAN",
                "ALTER TABLE device_registry ADD COLUMN IF NOT EXISTS battery_full BOOLEAN",
                "ALTER TABLE device_registry ADD COLUMN IF NOT EXISTS rssi INTEGER",
                "ALTER TABLE device_registry ADD COLUMN IF NOT EXISTS heap INTEGER",
                "ALTER TABLE device_registry ADD COLUMN IF NOT EXISTS last_status_at TEXT",
            ]:
                cur.execute(column_sql)
        else:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS residents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resident_uid TEXT UNIQUE NOT NULL,
                full_name TEXT NOT NULL,
                room TEXT,
                diet TEXT,
                texture TEXT,
                allergies TEXT,
                note TEXT,
                drinks TEXT,
                fluids TEXT,
                schedule TEXT,
                source_document TEXT,
                safety_review_note TEXT,
                needs_safety_review INTEGER NOT NULL DEFAULT 0,
                lcd_image_path TEXT,
                resident_photo_data BLOB,
                resident_photo_mime TEXT,
                resident_photo_name TEXT,
                lcd_schedule_enabled INTEGER NOT NULL DEFAULT 0,
                lcd_on_time TEXT,
                lcd_off_time TEXT,
                sleep_if_no_image INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """)
            self._add_resident_columns(cur)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS device_registry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT UNIQUE NOT NULL,
                ip TEXT,
                port INTEGER,
                fw TEXT,
                last_seen_s INTEGER DEFAULT 9999,
                is_online INTEGER DEFAULT 0,
                battery_level INTEGER,
                paired_resident_id INTEGER NULL REFERENCES residents(id) ON DELETE SET NULL,
                last_sync_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """)
            device_columns = {row["name"] for row in cur.execute("PRAGMA table_info(device_registry)").fetchall()}
            extra_device_columns = {
                "battery_level": "INTEGER",
                "battery_ok": "INTEGER",
                "battery_mv": "INTEGER",
                "battery_voltage": "REAL",
                "battery_raw_percent": "REAL",
                "battery_low": "INTEGER",
                "battery_alert": "INTEGER",
                "battery_plugged": "INTEGER",
                "battery_charging": "INTEGER",
                "battery_full": "INTEGER",
                "rssi": "INTEGER",
                "heap": "INTEGER",
                "last_status_at": "TEXT",
            }
            for name, col_type in extra_device_columns.items():
                if name not in device_columns:
                    cur.execute(f"ALTER TABLE device_registry ADD COLUMN {name} {col_type}")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS display_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                resident_id INTEGER NULL REFERENCES residents(id) ON DELETE SET NULL,
                resident_uid TEXT,
                device_id TEXT,
                pushed_by_user_id INTEGER,
                pushed_by_username TEXT,
                payload_json TEXT,
                response_json TEXT,
                success INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS resident_change_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resident_id INTEGER NULL REFERENCES residents(id) ON DELETE SET NULL,
                resident_uid TEXT,
                proposed_payload TEXT,
                comment TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                requested_by_user_id INTEGER,
                requested_by_username TEXT,
                reviewed_by_user_id INTEGER,
                reviewed_by_username TEXT,
                review_note TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS verification_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resident_id INTEGER NULL REFERENCES residents(id) ON DELETE SET NULL,
                resident_uid TEXT,
                device_id TEXT,
                status TEXT NOT NULL,
                note TEXT,
                checked_by_user_id INTEGER,
                checked_by_username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """)

        self._migrate_resident_alias_columns(cur)
        self._ensure_dropdown_options_table(cur)
        self._ensure_it_control_tables(cur)
        self.conn.commit()
        cur.close()

    def _ensure_dropdown_options_table(self, cur):
        if self.backend == "postgres":
            cur.execute("""
            CREATE TABLE IF NOT EXISTS resident_dropdown_options (
                id SERIAL PRIMARY KEY,
                category VARCHAR(64) NOT NULL,
                option_text TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE(category, option_text)
            );
            """)
        else:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS resident_dropdown_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                option_text TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(category, option_text)
            );
            """)

    def _ensure_it_control_tables(self, cur):
        if self.backend == "postgres":
            cur.execute("""
            CREATE TABLE IF NOT EXISTS control_service_profiles (
                id SERIAL PRIMARY KEY,
                profile_name VARCHAR(128) UNIQUE NOT NULL,
                host VARCHAR(255) NOT NULL DEFAULT '',
                port INTEGER NOT NULL DEFAULT 7000,
                api_key TEXT,
                description TEXT,
                is_active BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS it_audit_logs (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255),
                action VARCHAR(128) NOT NULL,
                target VARCHAR(255),
                result VARCHAR(64) NOT NULL,
                message TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """)
            cur.execute("SELECT COUNT(*) FROM control_service_profiles")
            count = cur.fetchone()[0]
            if count == 0:
                cur.execute("""
                    INSERT INTO control_service_profiles (profile_name, host, port, api_key, description, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                """, ("Demo Pi", "", 7000, "", "Configure the Raspberry Pi Control Service connection.",))
        else:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS control_service_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_name TEXT UNIQUE NOT NULL,
                host TEXT NOT NULL DEFAULT '',
                port INTEGER NOT NULL DEFAULT 7000,
                api_key TEXT,
                description TEXT,
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS it_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                action TEXT NOT NULL,
                target TEXT,
                result TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """)
            cur.execute("SELECT COUNT(*) AS count FROM control_service_profiles")
            count = cur.fetchone()["count"]
            if count == 0:
                cur.execute("""
                    INSERT INTO control_service_profiles (profile_name, host, port, api_key, description, is_active)
                    VALUES (?, ?, ?, ?, ?, 1)
                """, ("Demo Pi", "", 7000, "", "Configure the Raspberry Pi Control Service connection."))

    def _add_resident_columns(self, cur):
        existing = {row["name"] for row in cur.execute("PRAGMA table_info(residents)").fetchall()}
        columns = {
            "texture": "TEXT",
            "fluids": "TEXT",
            "schedule": "TEXT",
            "source_document": "TEXT",
            "safety_review_note": "TEXT",
            "needs_safety_review": "INTEGER NOT NULL DEFAULT 0",
            "lcd_image_path": "TEXT",
            "resident_photo_data": "BLOB",
            "resident_photo_mime": "TEXT",
            "resident_photo_name": "TEXT",
            "lcd_schedule_enabled": "INTEGER NOT NULL DEFAULT 0",
            "lcd_on_time": "TEXT",
            "lcd_off_time": "TEXT",
            "sleep_if_no_image": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in columns.items():
            if name not in existing:
                cur.execute(f"ALTER TABLE residents ADD COLUMN {name} {definition}")

    def _migrate_resident_alias_columns(self, cur):
        if self.backend == "postgres":
            statements = [
                "UPDATE residents SET texture = allergies WHERE (texture IS NULL OR texture = '') AND allergies IS NOT NULL AND allergies <> ''",
                "UPDATE residents SET fluids = schedule WHERE (fluids IS NULL OR fluids = '') AND schedule IS NOT NULL AND schedule <> ''",
                "UPDATE residents SET allergies = texture WHERE (allergies IS NULL OR allergies = '') AND texture IS NOT NULL AND texture <> ''",
                "UPDATE residents SET schedule = fluids WHERE (schedule IS NULL OR schedule = '') AND fluids IS NOT NULL AND fluids <> ''",
            ]
        else:
            statements = [
                "UPDATE residents SET texture = allergies WHERE (texture IS NULL OR texture = '') AND allergies IS NOT NULL AND allergies <> ''",
                "UPDATE residents SET fluids = schedule WHERE (fluids IS NULL OR fluids = '') AND schedule IS NOT NULL AND schedule <> ''",
                "UPDATE residents SET allergies = texture WHERE (allergies IS NULL OR allergies = '') AND texture IS NOT NULL AND texture <> ''",
                "UPDATE residents SET schedule = fluids WHERE (schedule IS NULL OR schedule = '') AND fluids IS NOT NULL AND fluids <> ''",
            ]
        for statement in statements:
            cur.execute(statement)

    def _resident_texture(self, data):
        return data.get("texture") or data.get("allergies")

    def _resident_fluids(self, data):
        return data.get("fluids") or data.get("schedule")

    def _resident_photo_values(self, data):
        photo_data = data.get("resident_photo_data")
        photo_mime = data.get("resident_photo_mime")
        photo_name = data.get("resident_photo_name")
        image_path = data.get("lcd_image_path") or data.get("resident_photo_path")

        if photo_data is None and image_path and os.path.isfile(str(image_path)):
            with open(image_path, "rb") as fh:
                photo_data = fh.read()
            photo_name = photo_name or os.path.basename(str(image_path))
            photo_mime = photo_mime or mimetypes.guess_type(str(image_path))[0] or "application/octet-stream"

        return photo_data, photo_mime, photo_name

    def _normalize_resident_fields(self, row):
        if not row:
            return row
        row = dict(row)
        texture = row.get("texture") or row.get("allergies") or ""
        fluids = row.get("fluids") or row.get("schedule") or ""
        row["texture"] = texture
        row["allergies"] = texture
        row["fluids"] = fluids
        row["schedule"] = fluids
        return self._materialize_resident_photo(row)

    def _materialize_resident_photo(self, row):
        photo_data = row.get("resident_photo_data")
        if not photo_data:
            return row

        current_path = row.get("lcd_image_path") or ""
        if current_path and os.path.isfile(str(current_path)):
            return row

        photo_name = row.get("resident_photo_name") or ""
        photo_mime = row.get("resident_photo_mime") or ""
        suffix = os.path.splitext(photo_name)[1] or mimetypes.guess_extension(photo_mime) or ".jpg"
        base = str(row.get("resident_uid") or row.get("id") or "resident_photo")
        safe_base = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in base)
        photo_dir = APP_DATA_DIR / "resident_photos"
        photo_dir.mkdir(parents=True, exist_ok=True)
        cache_path = photo_dir / f"{safe_base}{suffix}"

        try:
            with open(cache_path, "wb") as fh:
                fh.write(bytes(photo_data))
            row["lcd_image_path"] = str(cache_path)
        except Exception:
            pass
        return row

    def create_resident(self, data):
        cur = self._cursor()
        texture = self._resident_texture(data)
        fluids = self._resident_fluids(data)
        photo_data, photo_mime, photo_name = self._resident_photo_values(data)
        values = (
            data["resident_uid"],
            data["full_name"],
            data.get("room"),
            data.get("diet"),
            texture,
            data.get("allergies") or texture,
            data.get("note"),
            data.get("drinks"),
            fluids,
            data.get("schedule") or fluids,
            data.get("source_document"),
            data.get("safety_review_note"),
            data.get("needs_safety_review", False),
            data.get("lcd_image_path"),
            photo_data,
            photo_mime,
            photo_name,
            data.get("lcd_schedule_enabled", False),
            data.get("lcd_on_time"),
            data.get("lcd_off_time"),
            data.get("sleep_if_no_image", False),
            data.get("active", True),
        )
        if self.backend == "postgres":
            cur.execute("""
                INSERT INTO residents (
                    resident_uid, full_name, room, diet, texture, allergies, note, drinks,
                    fluids, schedule, source_document, safety_review_note, needs_safety_review,
                    lcd_image_path, resident_photo_data, resident_photo_mime, resident_photo_name,
                    lcd_schedule_enabled, lcd_on_time, lcd_off_time, sleep_if_no_image, active
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, values)
            resident_id = cur.fetchone()[0]
        else:
            cur.execute("""
                INSERT INTO residents (
                    resident_uid, full_name, room, diet, texture, allergies, note, drinks,
                    fluids, schedule, source_document, safety_review_note, needs_safety_review,
                    lcd_image_path, resident_photo_data, resident_photo_mime, resident_photo_name,
                    lcd_schedule_enabled, lcd_on_time, lcd_off_time, sleep_if_no_image, active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, values)
            resident_id = cur.lastrowid
        self.conn.commit()
        cur.close()
        return resident_id

    def update_resident(self, resident_id, data):
        cur = self._cursor()
        texture = self._resident_texture(data)
        fluids = self._resident_fluids(data)
        photo_data, photo_mime, photo_name = self._resident_photo_values(data)
        values = (
            data["full_name"],
            data.get("room"),
            data.get("diet"),
            texture,
            data.get("allergies") or texture,
            data.get("note"),
            data.get("drinks"),
            fluids,
            data.get("schedule") or fluids,
            data.get("source_document"),
            data.get("safety_review_note"),
            data.get("needs_safety_review", False),
            data.get("lcd_image_path"),
            photo_data,
            photo_mime,
            photo_name,
            data.get("lcd_schedule_enabled", False),
            data.get("lcd_on_time"),
            data.get("lcd_off_time"),
            data.get("sleep_if_no_image", False),
            data.get("active", True),
            resident_id,
        )
        if self.backend == "postgres":
            cur.execute("""
                UPDATE residents
                SET full_name=%s,
                    room=%s,
                    diet=%s,
                    texture=%s,
                    allergies=%s,
                    note=%s,
                    drinks=%s,
                    fluids=%s,
                    schedule=%s,
                    source_document=%s,
                    safety_review_note=%s,
                    needs_safety_review=%s,
                    lcd_image_path=%s,
                    resident_photo_data=%s,
                    resident_photo_mime=%s,
                    resident_photo_name=%s,
                    lcd_schedule_enabled=%s,
                    lcd_on_time=%s,
                    lcd_off_time=%s,
                    sleep_if_no_image=%s,
                    active=%s,
                    updated_at=NOW()
                WHERE id=%s
            """, values)
        else:
            cur.execute("""
                UPDATE residents
                SET full_name=?,
                    room=?,
                    diet=?,
                    texture=?,
                    allergies=?,
                    note=?,
                    drinks=?,
                    fluids=?,
                    schedule=?,
                    source_document=?,
                    safety_review_note=?,
                    needs_safety_review=?,
                    lcd_image_path=?,
                    resident_photo_data=?,
                    resident_photo_mime=?,
                    resident_photo_name=?,
                    lcd_schedule_enabled=?,
                    lcd_on_time=?,
                    lcd_off_time=?,
                    sleep_if_no_image=?,
                    active=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, values)
        self.conn.commit()
        cur.close()

    def get_residents(self):
        cur = self._cursor(dict_rows=True)
        cur.execute("""
            SELECT r.*,
                   d.device_id AS paired_device_id,
                   d.is_online AS paired_device_online
            FROM residents r
            LEFT JOIN device_registry d ON d.paired_resident_id = r.id
            ORDER BY r.full_name ASC
        """)
        rows = self._rows(cur.fetchall())
        cur.close()
        return [self._normalize_resident_fields(row) for row in rows]

    def get_resident(self, resident_id):
        cur = self._cursor(dict_rows=True)
        marker = "%s" if self.backend == "postgres" else "?"
        cur.execute(f"""
            SELECT r.*,
                   d.device_id AS paired_device_id,
                   d.is_online AS paired_device_online
            FROM residents r
            LEFT JOIN device_registry d ON d.paired_resident_id = r.id
            WHERE r.id = {marker}
        """, (resident_id,))
        row = self._row(cur.fetchone())
        cur.close()
        return self._normalize_resident_fields(row)

    def upsert_devices(self, devices):
        cur = self._cursor()
        timestamp = "NOW()" if self.backend == "postgres" else "CURRENT_TIMESTAMP"
        if self.backend == "postgres":
            cur.execute(f"UPDATE device_registry SET is_online = FALSE, last_seen_s = 9999, updated_at = {timestamp}")
        else:
            cur.execute(f"UPDATE device_registry SET is_online = 0, last_seen_s = 9999, updated_at = {timestamp}")

        for d in devices:
            if self.backend == "postgres":
                cur.execute("""
                    INSERT INTO device_registry (
                        device_id, ip, port, fw, last_seen_s, is_online, battery_level,
                        battery_ok, battery_mv, battery_voltage, battery_raw_percent, battery_low, battery_alert,
                        battery_plugged, battery_charging, battery_full, rssi, heap, last_status_at,
                        last_sync_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (device_id)
                    DO UPDATE SET
                        ip = EXCLUDED.ip,
                        port = EXCLUDED.port,
                        fw = EXCLUDED.fw,
                        last_seen_s = EXCLUDED.last_seen_s,
                        is_online = EXCLUDED.is_online,
                        battery_level = EXCLUDED.battery_level,
                        battery_ok = EXCLUDED.battery_ok,
                        battery_mv = EXCLUDED.battery_mv,
                        battery_voltage = EXCLUDED.battery_voltage,
                        battery_raw_percent = EXCLUDED.battery_raw_percent,
                        battery_low = EXCLUDED.battery_low,
                        battery_alert = EXCLUDED.battery_alert,
                        battery_plugged = EXCLUDED.battery_plugged,
                        battery_charging = EXCLUDED.battery_charging,
                        battery_full = EXCLUDED.battery_full,
                        rssi = EXCLUDED.rssi,
                        heap = EXCLUDED.heap,
                        last_status_at = EXCLUDED.last_status_at,
                        last_sync_at = NOW(),
                        updated_at = NOW()
                """, (
                    d.id, d.ip, d.port, d.fw, d.last_seen_s, bool(d.is_online), d.battery_level,
                    d.battery_ok, d.battery_mv, d.battery_voltage, d.battery_raw_percent, d.battery_low, d.battery_alert,
                    d.battery_plugged, d.battery_charging, d.battery_full, d.rssi, d.heap, d.last_status_at,
                ))
            else:
                cur.execute("""
                    INSERT INTO device_registry (
                        device_id, ip, port, fw, last_seen_s, is_online, battery_level,
                        battery_ok, battery_mv, battery_voltage, battery_raw_percent, battery_low, battery_alert,
                        battery_plugged, battery_charging, battery_full, rssi, heap, last_status_at,
                        last_sync_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(device_id)
                    DO UPDATE SET
                        ip = excluded.ip,
                        port = excluded.port,
                        fw = excluded.fw,
                        last_seen_s = excluded.last_seen_s,
                        is_online = excluded.is_online,
                        battery_level = excluded.battery_level,
                        battery_ok = excluded.battery_ok,
                        battery_mv = excluded.battery_mv,
                        battery_voltage = excluded.battery_voltage,
                        battery_raw_percent = excluded.battery_raw_percent,
                        battery_low = excluded.battery_low,
                        battery_alert = excluded.battery_alert,
                        battery_plugged = excluded.battery_plugged,
                        battery_charging = excluded.battery_charging,
                        battery_full = excluded.battery_full,
                        rssi = excluded.rssi,
                        heap = excluded.heap,
                        last_status_at = excluded.last_status_at,
                        last_sync_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                """, (
                    d.id, d.ip, d.port, d.fw, d.last_seen_s, int(bool(d.is_online)), d.battery_level,
                    int(bool(d.battery_ok)) if d.battery_ok is not None else None,
                    d.battery_mv, d.battery_voltage, d.battery_raw_percent,
                    int(bool(d.battery_low)) if d.battery_low is not None else None,
                    int(bool(d.battery_alert)) if d.battery_alert is not None else None,
                    int(bool(d.battery_plugged)) if d.battery_plugged is not None else None,
                    int(bool(d.battery_charging)) if d.battery_charging is not None else None,
                    int(bool(d.battery_full)) if d.battery_full is not None else None,
                    d.rssi, d.heap, d.last_status_at,
                ))
        self.conn.commit()
        cur.close()

    def get_devices(self):
        cur = self._cursor(dict_rows=True)
        cur.execute("""
            SELECT d.*,
                   r.full_name AS resident_name,
                   r.resident_uid
            FROM device_registry d
            LEFT JOIN residents r ON r.id = d.paired_resident_id
            ORDER BY d.device_id ASC
        """)
        rows = self._rows(cur.fetchall())
        cur.close()
        return rows

    def pair_resident_to_device(self, resident_id, device_id):
        cur = self._cursor()
        marker = "%s" if self.backend == "postgres" else "?"
        timestamp = "NOW()" if self.backend == "postgres" else "CURRENT_TIMESTAMP"
        cur.execute(f"""
            UPDATE device_registry
            SET paired_resident_id = NULL, updated_at = {timestamp}
            WHERE paired_resident_id = {marker}
        """, (resident_id,))
        cur.execute(f"""
            UPDATE device_registry
            SET paired_resident_id = {marker}, updated_at = {timestamp}
            WHERE device_id = {marker}
        """, (resident_id, device_id))
        self.conn.commit()
        cur.close()

    def unpair_device(self, device_id):
        cur = self._cursor()
        marker = "%s" if self.backend == "postgres" else "?"
        timestamp = "NOW()" if self.backend == "postgres" else "CURRENT_TIMESTAMP"
        cur.execute(f"""
            UPDATE device_registry
            SET paired_resident_id = NULL, updated_at = {timestamp}
            WHERE device_id = {marker}
        """, (device_id,))
        self.conn.commit()
        cur.close()

    def delete_resident(self, resident_id):
        cur = self._cursor()
        marker = "%s" if self.backend == "postgres" else "?"
        timestamp = "NOW()" if self.backend == "postgres" else "CURRENT_TIMESTAMP"
        cur.execute(f"""
            UPDATE device_registry
            SET paired_resident_id = NULL, updated_at = {timestamp}
            WHERE paired_resident_id = {marker}
        """, (resident_id,))
        cur.execute(f"DELETE FROM residents WHERE id = {marker}", (resident_id,))
        self.conn.commit()
        cur.close()

    def log_update(self, action_type, resident_id, resident_uid, device_id,
                   pushed_by_user_id, pushed_by_username, payload, response, success, message):
        cur = self._cursor()
        values = (
            action_type,
            resident_id,
            resident_uid,
            device_id,
            pushed_by_user_id,
            pushed_by_username,
            self._json_value(payload),
            self._json_value(response),
            success,
            message,
        )
        marker = "%s" if self.backend == "postgres" else "?"
        cur.execute(f"""
            INSERT INTO display_updates (
                action_type, resident_id, resident_uid, device_id,
                pushed_by_user_id, pushed_by_username,
                payload_json, response_json, success, message
            )
            VALUES ({", ".join([marker] * 10)})
        """, values)
        self.conn.commit()
        cur.close()

    def create_change_request(self, resident_id, resident_uid, proposed_payload, comment, requested_by_user_id, requested_by_username):
        cur = self._cursor()
        payload_value = self._json_value(proposed_payload)
        if self.backend == "postgres":
            cur.execute("""
                INSERT INTO resident_change_requests (
                    resident_id, resident_uid, proposed_payload, comment,
                    requested_by_user_id, requested_by_username
                )
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (resident_id, resident_uid, payload_value, comment, requested_by_user_id, requested_by_username))
        else:
            cur.execute("""
                INSERT INTO resident_change_requests (
                    resident_id, resident_uid, proposed_payload, comment,
                    requested_by_user_id, requested_by_username
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (resident_id, resident_uid, payload_value, comment, requested_by_user_id, requested_by_username))
        self.conn.commit()
        cur.close()

    def get_change_requests(self, status=None, limit=100):
        cur = self._cursor(dict_rows=True)
        marker = "%s" if self.backend == "postgres" else "?"
        order_clause = "cr.created_at DESC, cr.id DESC" if self.backend == "postgres" else "datetime(cr.created_at) DESC, cr.id DESC"
        if status:
            cur.execute(f"""
                SELECT cr.*,
                       r.full_name,
                       r.room
                FROM resident_change_requests cr
                LEFT JOIN residents r ON r.id = cr.resident_id
                WHERE cr.status = {marker}
                ORDER BY {order_clause}
                LIMIT {marker}
            """, (status, limit))
        else:
            cur.execute(f"""
                SELECT cr.*,
                       r.full_name,
                       r.room
                FROM resident_change_requests cr
                LEFT JOIN residents r ON r.id = cr.resident_id
                ORDER BY {order_clause}
                LIMIT {marker}
            """, (limit,))
        rows = self._rows(cur.fetchall())
        cur.close()
        return rows

    def update_change_request_status(self, request_id, status, reviewed_by_user_id, reviewed_by_username, review_note=""):
        cur = self._cursor()
        marker = "%s" if self.backend == "postgres" else "?"
        timestamp = "NOW()" if self.backend == "postgres" else "CURRENT_TIMESTAMP"
        cur.execute(f"""
            UPDATE resident_change_requests
            SET status = {marker},
                reviewed_by_user_id = {marker},
                reviewed_by_username = {marker},
                review_note = {marker},
                reviewed_at = {timestamp}
            WHERE id = {marker}
        """, (status, reviewed_by_user_id, reviewed_by_username, review_note, request_id))
        self.conn.commit()
        cur.close()

    def create_verification_check(self, resident_id, resident_uid, device_id, status, note, checked_by_user_id, checked_by_username):
        cur = self._cursor()
        marker = "%s" if self.backend == "postgres" else "?"
        cur.execute(f"""
            INSERT INTO verification_checks (
                resident_id, resident_uid, device_id, status, note,
                checked_by_user_id, checked_by_username
            )
            VALUES ({", ".join([marker] * 7)})
        """, (resident_id, resident_uid, device_id, status, note, checked_by_user_id, checked_by_username))
        self.conn.commit()
        cur.close()

    def get_verification_checks(self, limit=50):
        cur = self._cursor(dict_rows=True)
        marker = "%s" if self.backend == "postgres" else "?"
        order_clause = "vc.created_at DESC, vc.id DESC" if self.backend == "postgres" else "datetime(vc.created_at) DESC, vc.id DESC"
        cur.execute(f"""
            SELECT vc.*,
                   r.full_name,
                   r.room
            FROM verification_checks vc
            LEFT JOIN residents r ON r.id = vc.resident_id
            ORDER BY {order_clause}
            LIMIT {marker}
        """, (limit,))
        rows = self._rows(cur.fetchall())
        cur.close()
        return rows

    def get_recent_logs(self, limit=50):
        cur = self._cursor(dict_rows=True)
        marker = "%s" if self.backend == "postgres" else "?"
        order_clause = "created_at DESC, id DESC" if self.backend == "postgres" else "datetime(created_at) DESC, id DESC"
        cur.execute(f"""
            SELECT id, created_at, action_type, resident_uid, device_id,
                   pushed_by_username, success, message, payload_json, response_json
            FROM display_updates
            ORDER BY {order_clause}
            LIMIT {marker}
        """, (limit,))
        rows = self._rows(cur.fetchall())
        cur.close()
        return rows

    def get_resident_audit_logs(self, limit=200):
        cur = self._cursor(dict_rows=True)
        marker = "%s" if self.backend == "postgres" else "?"
        order_clause = "du.created_at DESC, du.id DESC" if self.backend == "postgres" else "datetime(du.created_at) DESC, du.id DESC"
        resident_actions = [
            "resident_create",
            "resident_update",
            "resident_delete",
            "resident_review_request",
            "resident_review_decision",
        ]
        placeholders = ", ".join([marker] * len(resident_actions))
        cur.execute(f"""
            SELECT du.id,
                   du.created_at,
                   du.action_type,
                   du.resident_id,
                   du.resident_uid,
                   du.pushed_by_username,
                   du.payload_json,
                   du.response_json,
                   du.success,
                   du.message,
                   r.full_name,
                   r.room,
                   r.source_document AS current_source_document
            FROM display_updates du
            LEFT JOIN residents r ON r.id = du.resident_id
            WHERE du.action_type IN ({placeholders})
            ORDER BY {order_clause}
            LIMIT {marker}
        """, (*resident_actions, limit))
        rows = self._rows(cur.fetchall())
        cur.close()
        for row in rows:
            row["payload_json"] = self._parse_json_field(row.get("payload_json"))
            row["response_json"] = self._parse_json_field(row.get("response_json"))
        return rows

    def get_log(self, log_id):
        cur = self._cursor(dict_rows=True)
        marker = "%s" if self.backend == "postgres" else "?"
        cur.execute(f"""
            SELECT *
            FROM display_updates
            WHERE id = {marker}
        """, (log_id,))
        row = self._row(cur.fetchone())
        cur.close()
        return row

    def save_resident_schedule(self, resident_id, enabled, on_time, off_time, sleep_if_no_image):
        cur = self._cursor()
        marker = "%s" if self.backend == "postgres" else "?"
        timestamp = "NOW()" if self.backend == "postgres" else "CURRENT_TIMESTAMP"
        cur.execute(f"""
            UPDATE residents
            SET lcd_schedule_enabled = {marker},
                lcd_on_time = {marker},
                lcd_off_time = {marker},
                sleep_if_no_image = {marker},
                updated_at = {timestamp}
            WHERE id = {marker}
        """, (enabled, on_time, off_time, sleep_if_no_image, resident_id))
        self.conn.commit()
        cur.close()

    def get_schedule_rows(self):
        cur = self._cursor(dict_rows=True)
        cur.execute("""
            SELECT r.id,
                   r.resident_uid,
                   r.full_name,
                   r.lcd_schedule_enabled,
                   r.lcd_on_time,
                   r.lcd_off_time,
                   r.sleep_if_no_image,
                   r.lcd_image_path,
                   r.resident_photo_data,
                   r.resident_photo_mime,
                   r.resident_photo_name,
                   d.device_id,
                   d.is_online
            FROM residents r
            LEFT JOIN device_registry d ON d.paired_resident_id = r.id
            ORDER BY r.full_name ASC
        """)
        rows = self._rows(cur.fetchall())
        cur.close()
        return [self._normalize_resident_fields(row) for row in rows]

    def get_dashboard_summary(self):
        cur = self._cursor(dict_rows=True)
        active_filter = "active = TRUE" if self.backend == "postgres" else "active = 1"
        inactive_filter = "active = FALSE" if self.backend == "postgres" else "active = 0"
        online_filter = "is_online = TRUE" if self.backend == "postgres" else "is_online = 1"
        failed_filter = "success = FALSE" if self.backend == "postgres" else "success = 0"
        review_filter = "needs_safety_review = TRUE" if self.backend == "postgres" else "needs_safety_review = 1"
        today_logs_filter = "DATE(created_at) = CURRENT_DATE" if self.backend == "postgres" else "DATE(created_at, 'localtime') = DATE('now', 'localtime')"

        cur.execute(f"SELECT COUNT(*) AS count FROM residents WHERE {active_filter}")
        active = self._row(cur.fetchone())["count"]
        cur.execute(f"SELECT COUNT(*) AS count FROM residents WHERE {inactive_filter}")
        inactive = self._row(cur.fetchone())["count"]
        cur.execute(f"SELECT COUNT(*) AS count FROM device_registry WHERE {online_filter}")
        online = self._row(cur.fetchone())["count"]
        cur.execute("SELECT COUNT(*) AS count FROM device_registry")
        known_devices = self._row(cur.fetchone())["count"]
        cur.execute("SELECT COUNT(*) AS count FROM device_registry WHERE paired_resident_id IS NOT NULL")
        paired = self._row(cur.fetchone())["count"]
        cur.execute(f"SELECT COUNT(*) AS count FROM display_updates WHERE {failed_filter}")
        failed_updates = self._row(cur.fetchone())["count"]
        cur.execute(f"SELECT COUNT(*) AS count FROM display_updates WHERE {today_logs_filter}")
        recent_activity_today = self._row(cur.fetchone())["count"]
        cur.execute("SELECT COUNT(*) AS count FROM display_updates")
        recent_activity_total = self._row(cur.fetchone())["count"]
        cur.execute(f"SELECT COUNT(*) AS count FROM residents WHERE {review_filter}")
        safety_reviews = self._row(cur.fetchone())["count"]
        cur.execute("SELECT COUNT(*) AS count FROM resident_change_requests WHERE status = 'PENDING'")
        pending_requests = self._row(cur.fetchone())["count"]
        cur.execute("SELECT COUNT(*) AS count FROM verification_checks")
        verification_checks = self._row(cur.fetchone())["count"]
        cur.execute("SELECT COUNT(*) AS count FROM verification_checks WHERE status = 'MISMATCH'")
        verification_mismatches = self._row(cur.fetchone())["count"]
        cur.close()
        return {
            "active_residents": active,
            "inactive_residents": inactive,
            "online_devices": online,
            "known_devices": known_devices,
            "paired_devices": paired,
            "failed_updates": failed_updates,
            "recent_activity": recent_activity_today,
            "recent_activity_today": recent_activity_today,
            "recent_activity_total": recent_activity_total,
            "safety_reviews": safety_reviews,
            "pending_requests": pending_requests,
            "verification_checks": verification_checks,
            "verification_mismatches": verification_mismatches,
            "database_mode": self.backend or "unknown",
        }

    def list_control_profiles(self):
        cur = self._cursor(dict_rows=True)
        order_clause = "is_active DESC, profile_name ASC"
        cur.execute(f"""
            SELECT id, profile_name, host, port, api_key, description, is_active, created_at, updated_at
            FROM control_service_profiles
            ORDER BY {order_clause}
        """)
        rows = self._rows(cur.fetchall())
        cur.close()
        return rows

    def get_active_control_profile(self):
        rows = self.list_control_profiles()
        if not rows:
            cur = self._cursor()
            if self.backend == "postgres":
                cur.execute("""
                    INSERT INTO control_service_profiles (profile_name, host, port, api_key, description, is_active)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                """, ("Demo Pi", "", 7000, "", "Configure the Raspberry Pi Control Service connection."))
            else:
                cur.execute("""
                    INSERT INTO control_service_profiles (profile_name, host, port, api_key, description, is_active)
                    VALUES (?, ?, ?, ?, ?, 1)
                """, ("Demo Pi", "", 7000, "", "Configure the Raspberry Pi Control Service connection."))
            self.conn.commit()
            cur.close()
            rows = self.list_control_profiles()
        for row in rows:
            if row.get("is_active"):
                return row
        return rows[0] if rows else None

    def save_control_profile(self, profile_id, profile_name, host, port, api_key, description, is_active=True):
        profile_name = (profile_name or "").strip()
        host = (host or "").strip()
        api_key = api_key or ""
        description = (description or "").strip()
        if not profile_name:
            raise ValueError("Connection profile name is required.")
        try:
            port = int(port or 7000)
        except ValueError:
            raise ValueError("Control Service port must be a number.")
        if port <= 0 or port > 65535:
            raise ValueError("Control Service port must be between 1 and 65535.")

        cur = self._cursor()
        marker = "%s" if self.backend == "postgres" else "?"
        active_value = bool(is_active) if self.backend == "postgres" else int(bool(is_active))
        timestamp = "NOW()" if self.backend == "postgres" else "CURRENT_TIMESTAMP"
        if is_active:
            cur.execute(f"UPDATE control_service_profiles SET is_active = {marker}, updated_at = {timestamp}", (False if self.backend == "postgres" else 0,))

        if profile_id:
            cur.execute(f"""
                UPDATE control_service_profiles
                SET profile_name = {marker},
                    host = {marker},
                    port = {marker},
                    api_key = {marker},
                    description = {marker},
                    is_active = {marker},
                    updated_at = {timestamp}
                WHERE id = {marker}
            """, (profile_name, host, port, api_key, description, active_value, profile_id))
            saved_id = profile_id
        else:
            cur.execute(f"""
                INSERT INTO control_service_profiles (
                    profile_name, host, port, api_key, description, is_active
                )
                VALUES ({", ".join([marker] * 6)})
            """, (profile_name, host, port, api_key, description, active_value))
            if self.backend == "postgres":
                cur.execute("SELECT LASTVAL()")
                saved_id = cur.fetchone()[0]
            else:
                saved_id = cur.lastrowid
        self.conn.commit()
        cur.close()
        return saved_id

    def set_active_control_profile(self, profile_id):
        cur = self._cursor()
        marker = "%s" if self.backend == "postgres" else "?"
        timestamp = "NOW()" if self.backend == "postgres" else "CURRENT_TIMESTAMP"
        cur.execute(f"UPDATE control_service_profiles SET is_active = {marker}, updated_at = {timestamp}", (False if self.backend == "postgres" else 0,))
        cur.execute(f"""
            UPDATE control_service_profiles
            SET is_active = {marker}, updated_at = {timestamp}
            WHERE id = {marker}
        """, (True if self.backend == "postgres" else 1, profile_id))
        self.conn.commit()
        cur.close()

    def get_dropdown_options(self):
        cur = self._cursor(dict_rows=True)
        active_filter = "active = TRUE" if self.backend == "postgres" else "active = 1"
        cur.execute(f"""
            SELECT category, option_text, sort_order
            FROM resident_dropdown_options
            WHERE {active_filter}
            ORDER BY category ASC, sort_order ASC, option_text ASC
        """)
        rows = self._rows(cur.fetchall())
        cur.close()
        options = {}
        for row in rows:
            category = str(row.get("category") or "").strip()
            text = str(row.get("option_text") or "").strip()
            if not category or not text:
                continue
            options.setdefault(category, []).append(text)
        return options

    def save_dropdown_options(self, options):
        options = options or {}
        cur = self._cursor()
        marker = "%s" if self.backend == "postgres" else "?"
        timestamp = "NOW()" if self.backend == "postgres" else "CURRENT_TIMESTAMP"
        for category, values in options.items():
            category = str(category or "").strip()
            if not category:
                continue
            cur.execute(
                f"UPDATE resident_dropdown_options SET active = {marker}, updated_at = {timestamp} WHERE category = {marker}",
                (False if self.backend == "postgres" else 0, category),
            )
            seen = set()
            order_index = 0
            for value in values or []:
                text = str(value or "").strip()
                key = text.lower()
                if not text or key in seen:
                    continue
                seen.add(key)
                if self.backend == "postgres":
                    cur.execute("""
                        INSERT INTO resident_dropdown_options (category, option_text, sort_order, active, updated_at)
                        VALUES (%s, %s, %s, TRUE, NOW())
                        ON CONFLICT (category, option_text)
                        DO UPDATE SET
                            sort_order = EXCLUDED.sort_order,
                            active = TRUE,
                            updated_at = NOW()
                    """, (category, text, order_index))
                else:
                    cur.execute("""
                        INSERT INTO resident_dropdown_options (category, option_text, sort_order, active, updated_at)
                        VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
                        ON CONFLICT(category, option_text)
                        DO UPDATE SET
                            sort_order = excluded.sort_order,
                            active = 1,
                            updated_at = CURRENT_TIMESTAMP
                    """, (category, text, order_index))
                order_index += 1
        self.conn.commit()
        cur.close()

    def log_it_audit(self, username, action, target, result, message):
        cur = self._cursor()
        marker = "%s" if self.backend == "postgres" else "?"
        cur.execute(f"""
            INSERT INTO it_audit_logs (username, action, target, result, message)
            VALUES ({", ".join([marker] * 5)})
        """, (username, action, target, result, message))
        self.conn.commit()
        cur.close()

    def get_it_audit_logs(self, limit=100):
        cur = self._cursor(dict_rows=True)
        marker = "%s" if self.backend == "postgres" else "?"
        order_clause = "created_at DESC, id DESC" if self.backend == "postgres" else "datetime(created_at) DESC, id DESC"
        cur.execute(f"""
            SELECT id, created_at, username, action, target, result, message
            FROM it_audit_logs
            ORDER BY {order_clause}
            LIMIT {marker}
        """, (limit,))
        rows = self._rows(cur.fetchall())
        cur.close()
        return rows

    @staticmethod
    def format_timestamp(value):
        return format_readable_datetime(value)
