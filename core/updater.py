import requests
from pathlib import Path
from typing import Dict, List, Optional

from config import (
    APP_VERSION,
    DEFAULT_CONTROL_SERVICE_HOST,
    DEFAULT_DOWNLOAD_SITE_PORT,
    DEFAULT_DOWNLOAD_SITE_SLUG,
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

    def release_version(self, tag_name: str) -> str:
        tag_name = str(tag_name or "").strip()
        prefix = RELEASE_TAG_PREFIX
        if prefix and tag_name.lower().startswith(prefix.lower()):
            return tag_name[len(prefix):].strip()
        if tag_name.lower().startswith("v"):
            return tag_name[1:].strip()
        return tag_name

    def update_result(self, source: str, tag_name: str, download_url: str, release_url: str = "") -> Dict:
        latest_version = self.release_version(tag_name)
        has_update = self.parse_version(latest_version) > self.parse_version(APP_VERSION)
        return {
            "enabled": True,
            "has_update": has_update,
            "latest_version": latest_version,
            "download_url": download_url,
            "release_url": release_url,
            "source": source,
            "message": f"Update available from {source}" if has_update else "App is up to date",
        }

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

    def github_update_result(self) -> Optional[Dict]:
        data = self.latest_release()
        if not data:
            return None
        latest_tag = data.get("tag_name", f"{RELEASE_TAG_PREFIX}{APP_VERSION}")
        return self.update_result(
            "GitHub",
            latest_tag,
            self.installer_url_for_release(data),
            data.get("html_url") or "",
        )

    def configured_pi_hosts(self) -> List[str]:
        hosts = []
        try:
            from core.app_settings import AppSettingsStore
            profile = AppSettingsStore().get_active_profile()
            hosts.append(profile.get("host") or "")
        except Exception:
            pass
        hosts.append(DEFAULT_CONTROL_SERVICE_HOST)
        out = []
        seen = set()
        for host in hosts:
            host = str(host or "").strip()
            marker = host.lower()
            if host and marker not in seen:
                seen.add(marker)
                out.append(host)
        return out

    def local_download_site_result(self) -> Optional[Dict]:
        slug = str(DEFAULT_DOWNLOAD_SITE_SLUG or "download").strip("/")
        for host in self.configured_pi_hosts():
            base_url = f"http://{host}:{int(DEFAULT_DOWNLOAD_SITE_PORT)}/{slug}"
            try:
                response = self.session.get(f"{base_url}/latest.json", timeout=3)
                response.raise_for_status()
                data = response.json()
                tag_name = data.get("tag_name")
                asset_name = data.get("asset_name") or INSTALLER_NAME
                if not tag_name:
                    continue
                return self.update_result(
                    "Raspberry Pi download site",
                    tag_name,
                    f"{base_url}/{asset_name}",
                    data.get("release_url") or "",
                )
            except Exception:
                continue
        return None

    def check_for_updates(self, latest_version=None):
        candidates = []
        errors = []
        try:
            github = self.github_update_result()
            if github:
                candidates.append(github)
        except Exception as e:
            errors.append(f"GitHub: {e}")

        local = self.local_download_site_result()
        if local:
            candidates.append(local)

        if candidates:
            return max(candidates, key=lambda item: self.parse_version(item.get("latest_version", "0.0.0")))

        message = "No update source is reachable"
        if errors:
            message += f" ({'; '.join(errors)})"
        return {
            "enabled": True,
            "has_update": False,
            "latest_version": APP_VERSION,
            "message": message,
        }

    def download_update(self, update=None):
        update = update or self.check_for_updates()
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
                "source": update.get("source", ""),
            }
        except Exception as e:
            return {
                "success": False,
                "path": None,
                "message": f"Download failed: {e}",
            }
