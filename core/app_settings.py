import base64
import ctypes
import json
import platform
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List, Optional

from config import (
    APP_DATA_DIR,
    DEFAULT_CONTROL_SERVICE_API_KEY,
    DEFAULT_CONTROL_SERVICE_HOST,
    DEFAULT_CONTROL_SERVICE_PORT,
)


SETTINGS_PATH = APP_DATA_DIR / "app_settings.json"
APP_MODE_SERVER = "server"
APP_MODE_DEMO = "offline_demo"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_available() -> bool:
    return platform.system().lower() == "windows"


def _protect_secret(value: str) -> Dict[str, str]:
    value = value or ""
    if not value:
        return {"scheme": "empty", "value": ""}
    if not _dpapi_available():
        return {"scheme": "plain", "value": value}

    raw = value.encode("utf-8")
    in_blob = _DataBlob(len(raw), ctypes.cast(ctypes.create_string_buffer(raw), ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        return {"scheme": "plain", "value": value}
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return {"scheme": "dpapi", "value": base64.b64encode(encrypted).decode("ascii")}
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _unprotect_secret(payload: Dict[str, str]) -> str:
    if not payload:
        return ""
    scheme = payload.get("scheme")
    value = payload.get("value") or ""
    if scheme in {"empty", None}:
        return ""
    if scheme != "dpapi" or not _dpapi_available():
        return value
    try:
        encrypted = base64.b64decode(value.encode("ascii"))
    except Exception:
        return ""
    in_blob = _DataBlob(len(encrypted), ctypes.cast(ctypes.create_string_buffer(encrypted), ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        return ""
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


class AppSettingsStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or SETTINGS_PATH

    def default_settings(self) -> Dict:
        profiles = [
            self._profile(
                "Whisperwood Pi",
                DEFAULT_CONTROL_SERVICE_HOST,
                DEFAULT_CONTROL_SERVICE_PORT,
                DEFAULT_CONTROL_SERVICE_API_KEY,
                "Default first-run Raspberry Pi Control Service.",
            ),
            self._profile(
                "Production Pi",
                DEFAULT_CONTROL_SERVICE_HOST,
                DEFAULT_CONTROL_SERVICE_PORT,
                DEFAULT_CONTROL_SERVICE_API_KEY,
                "Primary site Raspberry Pi Control Service.",
            ),
            self._profile("Development Pi", "", DEFAULT_CONTROL_SERVICE_PORT, "", "Developer or staging Raspberry Pi."),
        ]
        profiles[0]["is_active"] = True
        return {
            "app_mode": APP_MODE_SERVER,
            "active_profile_id": profiles[0]["id"],
            "profiles": profiles,
        }

    def _profile(self, name: str, host: str, port: int, api_key: str, description: str) -> Dict:
        return {
            "id": str(uuid.uuid4()),
            "profile_name": name,
            "host": host,
            "port": int(port or 7000),
            "api_key_protected": _protect_secret(api_key),
            "description": description,
            "is_active": False,
        }

    def load(self) -> Dict:
        if not self.path.exists():
            settings = self.default_settings()
            self.save(settings)
            return settings
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                settings = json.load(fh)
        except Exception:
            settings = self.default_settings()
        return self._normalize(settings)

    def save(self, settings: Dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        safe = dict(settings)
        for profile in safe.get("profiles", []):
            profile.pop("api_key", None)
        with self.path.open("w", encoding="utf-8") as fh:
            json.dump(safe, fh, indent=2)

    def _normalize(self, settings: Dict) -> Dict:
        settings = settings or {}
        settings.setdefault("app_mode", APP_MODE_SERVER)
        if settings.get("app_mode") not in {APP_MODE_SERVER, APP_MODE_DEMO}:
            settings["app_mode"] = APP_MODE_SERVER
        profiles = settings.get("profiles") or self.default_settings()["profiles"]
        normalized = []
        active_id = settings.get("active_profile_id")
        for profile in profiles:
            profile = dict(profile)
            profile.setdefault("id", str(uuid.uuid4()))
            profile.setdefault("profile_name", profile.get("name") or "Raspberry Pi")
            profile.setdefault("host", "")
            try:
                profile["port"] = int(profile.get("port") or 7000)
            except (TypeError, ValueError):
                profile["port"] = 7000
            if "api_key_protected" not in profile:
                profile["api_key_protected"] = _protect_secret(profile.get("api_key") or "")
            profile.pop("api_key", None)
            profile.setdefault("description", "")
            profile["is_active"] = bool(profile.get("is_active") or profile.get("id") == active_id)
            normalized.append(profile)
        if not any(p.get("is_active") for p in normalized) and normalized:
            normalized[0]["is_active"] = True
        if normalized and not any((p.get("host") or "").strip() for p in normalized):
            normalized[0].update({
                "profile_name": "Whisperwood Pi",
                "host": DEFAULT_CONTROL_SERVICE_HOST,
                "port": DEFAULT_CONTROL_SERVICE_PORT,
                "api_key_protected": _protect_secret(DEFAULT_CONTROL_SERVICE_API_KEY),
                "description": "Default first-run Raspberry Pi Control Service.",
                "is_active": True,
            })
            for profile in normalized[1:]:
                profile["is_active"] = False
        active = next((p for p in normalized if p.get("is_active")), normalized[0] if normalized else None)
        settings["profiles"] = normalized
        settings["active_profile_id"] = active.get("id") if active else None
        return settings

    def get_mode(self) -> str:
        return self.load().get("app_mode", APP_MODE_SERVER)

    def set_mode(self, mode: str):
        settings = self.load()
        settings["app_mode"] = APP_MODE_DEMO if mode == APP_MODE_DEMO else APP_MODE_SERVER
        self.save(settings)

    def is_server_mode(self) -> bool:
        return self.get_mode() == APP_MODE_SERVER

    def list_profiles(self) -> List[Dict]:
        return [self._public_profile(p) for p in self.load().get("profiles", [])]

    def get_active_profile(self) -> Dict:
        profiles = self.list_profiles()
        return next((p for p in profiles if p.get("is_active")), profiles[0] if profiles else {})

    def save_profile(self, profile_id, profile_name, host, port, api_key, description, is_active=True):
        settings = self.load()
        profiles = settings.get("profiles", [])
        profile_id = str(profile_id or uuid.uuid4())
        try:
            port = int(port or 7000)
        except (TypeError, ValueError):
            raise ValueError("Control Service port must be a number.")
        if port <= 0 or port > 65535:
            raise ValueError("Control Service port must be between 1 and 65535.")
        if not (profile_name or "").strip():
            raise ValueError("Connection profile name is required.")

        found = False
        for profile in profiles:
            if str(profile.get("id")) == profile_id:
                profile.update({
                    "profile_name": profile_name.strip(),
                    "host": (host or "").strip(),
                    "port": port,
                    "api_key_protected": _protect_secret(api_key or ""),
                    "description": (description or "").strip(),
                    "is_active": bool(is_active),
                })
                found = True
            elif is_active:
                profile["is_active"] = False
        if not found:
            new_profile = self._profile(profile_name.strip(), (host or "").strip(), port, api_key or "", (description or "").strip())
            new_profile["id"] = profile_id
            new_profile["is_active"] = bool(is_active)
            profiles.append(new_profile)
        if is_active:
            settings["active_profile_id"] = profile_id
        settings["profiles"] = profiles
        self.save(settings)
        return profile_id

    def set_active_profile(self, profile_id):
        settings = self.load()
        for profile in settings.get("profiles", []):
            profile["is_active"] = str(profile.get("id")) == str(profile_id)
        settings["active_profile_id"] = str(profile_id)
        self.save(settings)

    def _public_profile(self, profile: Dict) -> Dict:
        row = dict(profile)
        row["api_key"] = _unprotect_secret(row.get("api_key_protected") or {})
        row.pop("api_key_protected", None)
        return row
