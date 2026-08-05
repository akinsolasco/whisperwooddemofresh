import requests
import subprocess
import sys
import os
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from config import (
    APP_NAME,
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
from core.time_utils import format_local_now, format_readable_datetime


class UpdaterService:
    def __init__(self):
        self.session = requests.Session()
        self.api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
        self.download_dir = Path(UPDATE_DOWNLOAD_DIR)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.download_dir / "update_state.json"

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

    def read_update_state(self) -> Dict:
        try:
            if self.state_file.exists():
                return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def write_update_state(self, state: Dict):
        try:
            self.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            pass

    def clear_update_state(self):
        try:
            if self.state_file.exists():
                self.state_file.unlink()
        except Exception:
            pass

    def current_version_at_least(self, version: str) -> bool:
        try:
            return self.parse_version(APP_VERSION) >= self.parse_version(version)
        except Exception:
            return False

    def pending_install_state(self) -> Optional[Dict]:
        state = self.read_update_state()
        if state.get("status") != "installing":
            return None
        target_version = str(state.get("target_version") or "").strip()
        if not target_version:
            self.clear_update_state()
            return None
        if self.current_version_at_least(target_version):
            self.clear_update_state()
            return None
        try:
            age_s = time.time() - float(state.get("started_at") or 0)
        except Exception:
            age_s = 999999
        if age_s > 20 * 60:
            self.clear_update_state()
            return None
        return state

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
            "checked_at": format_local_now(),
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
                result = self.update_result(
                    "Raspberry Pi download site",
                    tag_name,
                    f"{base_url}/{asset_name}",
                    data.get("release_url") or "",
                )
                reliable_time = data.get("release_published_at") or data.get("generated_at")
                result["pi_generated_at"] = data.get("generated_at") or ""
                result["release_published_at"] = data.get("release_published_at") or ""
                result["remote_time_readable"] = format_readable_datetime(reliable_time)
                result["display_checked_at"] = result["checked_at"]
                return result
            except Exception:
                continue
        return None

    def check_for_updates(self, latest_version=None):
        pending = self.pending_install_state()
        if pending:
            # If the app is able to start again, do not trap it on an old
            # "installing" marker. The handoff window owns the install UI.
            self.clear_update_state()

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

    def install_update_silently(self, installer_path: str, target_version: str = "") -> Dict:
        path = Path(installer_path or "")
        if not path.exists():
            return {"success": False, "message": "Downloaded installer was not found."}

        app_path = Path(sys.executable)
        installer = str(path)
        target_version = str(target_version or "").strip()
        if target_version:
            self.write_update_state({
                "status": "installing",
                "target_version": target_version,
                "started_at": time.time(),
                "source_version": APP_VERSION,
                "installer_path": installer,
            })

        handoff = self.download_dir / "silent_update_handoff.ps1"
        target_app = Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / APP_NAME / app_path.name
        if app_path.suffix.lower() != ".exe":
            target_app = app_path
        handoff.write_text(self.handoff_script(installer, str(target_app), target_version), encoding="utf-8")
        try:
            flags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                flags = subprocess.CREATE_NO_WINDOW
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-WindowStyle",
                    "Hidden",
                    "-File",
                    str(handoff),
                ],
                creationflags=flags,
            )
            return {"success": True, "message": "Silent update started."}
        except Exception as exc:
            return {"success": False, "message": f"Could not start silent update: {exc}"}

    def handoff_script(self, installer: str, target_app: str, target_version: str) -> str:
        installer_json = json.dumps(installer)
        target_json = json.dumps(target_app)
        version_json = json.dumps(target_version or "latest")
        return f"""
$ErrorActionPreference = "SilentlyContinue"
$installer = {installer_json}
$target = {target_json}
$targetVersion = {version_json}
$installArgs = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = "Enhanced Living Whisperwood Update"
$form.StartPosition = "CenterScreen"
$form.Size = New-Object System.Drawing.Size(560, 245)
$form.FormBorderStyle = "FixedDialog"
$form.ControlBox = $false
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::FromArgb(243, 247, 251)

$title = New-Object System.Windows.Forms.Label
$title.Text = "Installing Enhanced Living Whisperwood"
$title.Font = New-Object System.Drawing.Font("Segoe UI", 15, [System.Drawing.FontStyle]::Bold)
$title.ForeColor = [System.Drawing.Color]::FromArgb(15, 23, 42)
$title.AutoSize = $false
$title.Location = New-Object System.Drawing.Point(28, 28)
$title.Size = New-Object System.Drawing.Size(500, 32)
$form.Controls.Add($title)

$message = New-Object System.Windows.Forms.Label
$message.Text = "Updating to v$targetVersion. Please keep this computer on and do not reopen the app."
$message.Font = New-Object System.Drawing.Font("Segoe UI", 10)
$message.ForeColor = [System.Drawing.Color]::FromArgb(51, 65, 85)
$message.AutoSize = $false
$message.Location = New-Object System.Drawing.Point(30, 72)
$message.Size = New-Object System.Drawing.Size(500, 46)
$form.Controls.Add($message)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Style = "Marquee"
$progress.MarqueeAnimationSpeed = 35
$progress.Location = New-Object System.Drawing.Point(32, 132)
$progress.Size = New-Object System.Drawing.Size(492, 18)
$form.Controls.Add($progress)

$detail = New-Object System.Windows.Forms.Label
$detail.Text = "Preparing the silent installer..."
$detail.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$detail.ForeColor = [System.Drawing.Color]::FromArgb(100, 116, 139)
$detail.AutoSize = $false
$detail.Location = New-Object System.Drawing.Point(30, 166)
$detail.Size = New-Object System.Drawing.Size(500, 22)
$form.Controls.Add($detail)

$script:tickCount = 0
$script:installerProcess = $null
$script:finished = $false
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 700
$timer.Add_Tick({{
    $script:tickCount += 1
    if ($script:tickCount -lt 3) {{
        return
    }}
    if ($null -eq $script:installerProcess) {{
        $detail.Text = "Installing files and applying the update..."
        $script:installerProcess = Start-Process -FilePath $installer -ArgumentList $installArgs -PassThru
        return
    }}
    if ($script:installerProcess.HasExited) {{
        $detail.Text = "Opening the updated application..."
        Get-Process WhisperwoodDemo -ErrorAction SilentlyContinue |
            Where-Object {{ $_.Path -and ($_.Path -ne $target) }} |
            Stop-Process -Force -ErrorAction SilentlyContinue
        $running = Get-Process WhisperwoodDemo -ErrorAction SilentlyContinue |
            Where-Object {{ $_.Path -and ($_.Path -eq $target) }} |
            Select-Object -First 1
        if ((-not $running) -and (Test-Path $target)) {{
            Start-Process $target
        }}
        $script:finished = $true
        $timer.Stop()
        $form.Close()
    }}
}})

$form.Add_FormClosing({{
    param($sender, $eventArgs)
    if (-not $script:finished) {{
        $eventArgs.Cancel = $true
        $detail.Text = "Please wait while the update finishes..."
    }}
}})

$form.Add_Shown({{ $timer.Start() }})
[System.Windows.Forms.Application]::Run($form)
"""
