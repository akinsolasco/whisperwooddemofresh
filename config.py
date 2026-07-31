from pathlib import Path
import os
import sys

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent

ASSETS_DIR = BASE_DIR / "assets"

APP_NAME = "Enhanced Living Whisperwood Demo"
APP_VERSION = "2.1.9"
APP_CHANNEL = "demo"
RELEASE_TAG_PREFIX = "demo-v"
DEFAULT_PI_BASE_URL = "http://localhost:8080"
DEFAULT_CONTROL_SERVICE_HOST = "192.168.2.37"
DEFAULT_CONTROL_SERVICE_PORT = 7000
DEFAULT_CONTROL_SERVICE_API_KEY = "43f116facc11ab5d12d572d90d7514193f4628374a7cd9d331aba9af54e31c79"
DEFAULT_DOWNLOAD_SITE_PORT = 8090
DEFAULT_DOWNLOAD_SITE_SLUG = "download"

GITHUB_OWNER = "akinsolasco"
GITHUB_REPO = "whisperwooddemofresh"
INSTALLER_NAME = "WhisperwoodDemoSetup.exe"

APP_DATA_DIR = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "WhisperwoodDemo"
UPDATE_DOWNLOAD_DIR = APP_DATA_DIR / "updates"

DATABASE_MODE = "sqlite"
LOCAL_DB_PATH = APP_DATA_DIR / "whisperwood.sqlite3"
DEMO_DEFAULT_USERNAME = "admin"
DEMO_DEFAULT_PASSWORD = "admin123"

ROLE_LABELS = {
    "ADMIN": "Admin",
    "NURSE_ADMIN": "Admin",
    "NURSE": "Staff",
    "STAFF": "Staff",
    "VERIFIER": "Display Verifier",
    "IT_ADMIN": "IT Admin",
    "IT_ADMIN_BACKEND": "IT Admin",
}

DEMO_USERS = [
    ("admin", "admin123", "NURSE_ADMIN"),
    ("itadmin", "itadmin123", "IT_ADMIN"),
]
