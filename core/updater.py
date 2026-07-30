import requests
from pathlib import Path

from config import (
    APP_VERSION,
    GITHUB_OWNER,
    GITHUB_REPO,
    INSTALLER_NAME,
    RELEASE_TAG_PREFIX,
    UPDATE_DOWNLOAD_DIR,
)


class UpdaterService:
    def __init__(self):
        self.session = requests.Session()
        self.api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
        self.download_dir = Path(UPDATE_DOWNLOAD_DIR)
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def parse_version(self, v: str):
        v = v.lower().strip()
        prefix = RELEASE_TAG_PREFIX.lower()
        if prefix and v.startswith(prefix):
            v = v[len(prefix):]
        elif v.startswith("v"):
            v = v[1:]
        return tuple(int(x) for x in v.split("."))

    def latest_release(self):
        r = self.session.get(self.api_url, timeout=6)
        r.raise_for_status()
        releases = r.json()
        if isinstance(releases, dict):
            releases = [releases]

        matching = [
            release for release in releases
            if str(release.get("tag_name", "")).lower().startswith(RELEASE_TAG_PREFIX.lower())
        ]
        if not matching:
            return None
        return max(matching, key=lambda release: self.parse_version(release.get("tag_name", "0.0.0")))

    def installer_url_for_release(self, release):
        for asset in release.get("assets", []):
            if asset.get("name") == INSTALLER_NAME and asset.get("browser_download_url"):
                return asset["browser_download_url"]
        for asset in release.get("assets", []):
            name = str(asset.get("name") or "").lower()
            if name.endswith(".exe") and asset.get("browser_download_url"):
                return asset["browser_download_url"]
        tag = release.get("tag_name", f"{RELEASE_TAG_PREFIX}{APP_VERSION}")
        return f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/download/{tag}/{INSTALLER_NAME}"

    def check_for_updates(self, latest_version=None):
        print("Installed APP_VERSION:", APP_VERSION)
        print("Latest GitHub version:", latest_version)
        try:
            data = self.latest_release()
            if not data:
                return {
                    "enabled": True,
                    "has_update": False,
                    "latest_version": APP_VERSION,
                    "message": "No release found",
                }

            latest_tag = data.get("tag_name", f"{RELEASE_TAG_PREFIX}{APP_VERSION}")
            latest_version = latest_tag.replace(RELEASE_TAG_PREFIX, "").strip()

            has_update = self.parse_version(latest_version) > self.parse_version(APP_VERSION)

            download_url = self.installer_url_for_release(data)

            return {
                "enabled": True,
                "has_update": has_update,
                "latest_version": latest_version,
                "download_url": download_url,
                "message": "Update available" if has_update else "App is up to date",
            }

        except Exception as e:
            return {
                "enabled": True,
                "has_update": False,
                "message": f"Update check failed: {e}",
            }

    def download_update(self):
        update = self.check_for_updates()
        download_url = update.get("download_url")
        if not download_url:
            return {
                "success": False,
                "path": None,
                "message": update.get("message", "No update download is available"),
            }
        target_path = self.download_dir / INSTALLER_NAME

        try:
            with self.session.get(download_url, stream=True, timeout=60) as r:
                r.raise_for_status()

                with open(target_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 64):
                        if chunk:
                            f.write(chunk)

            return {
                "success": True,
                "path": str(target_path),
                "message": "Update downloaded successfully",
            }

        except Exception as e:
            return {
                "success": False,
                "path": None,
                "message": f"Download failed: {e}",
            }
