from dataclasses import dataclass
import os
from typing import Any, Dict, Optional

import requests


FACILITY_NETWORK_GUIDANCE = (
    "Connect this computer to the dedicated facility network, then try again. "
    "If you are already on that network, ask IT to check the Raspberry Pi Control Service."
)


def friendly_error_message(
    error: str = "",
    endpoint: str = "",
    status_code: Optional[int] = None,
    data: Optional[Any] = None,
) -> str:
    raw = str(error or "").strip()
    detail = raw
    if isinstance(data, dict):
        value = data.get("detail") or data.get("err") or data.get("error") or data.get("message")
        if isinstance(value, dict):
            value = value.get("message") or value.get("err") or value.get("error") or value.get("line")
        if value:
            detail = str(value)
    lowered = detail.lower()
    endpoint = endpoint or ""

    if status_code in {401} and endpoint.startswith("/auth/login"):
        return "Username or password was not accepted. Check the details or ask IT/Admin for a temporary password."
    if status_code in {401, 403}:
        return "This workstation is not authorized to use the Raspberry Pi Control Service. Ask IT to verify the Control API key."
    if status_code == 404:
        return "The Raspberry Pi server is reachable, but this feature is not available on the server yet. Ask IT to update the Pi backend."
    if "e-paper is updating" in lowered or "epaper is updating" in lowered or "epaper_busy" in lowered:
        return "The e-paper screen is still updating. Wait until the text finishes, then send the LCD photo again."
    if "lcd photo is updating" in lowered:
        return "The LCD photo is still uploading. Wait until it finishes, then send the e-paper text again."
    if status_code == 409 or "busy" in lowered:
        return "The device is busy finishing the previous request. Wait a few seconds, then try again."
    if status_code in {502, 504} or "timeout" in lowered or "timed out" in lowered:
        return f"The Raspberry Pi server did not complete the request in time. {FACILITY_NETWORK_GUIDANCE}"
    if status_code and status_code >= 500:
        return "The Raspberry Pi server reported an internal service error. Ask IT to check the Control Service and Operation Manager logs."
    if "connection refused" in lowered:
        return "The Raspberry Pi was found, but the Control Service is not accepting connections. Ask IT to restart or check the Raspberry Pi Control Service."
    if "cannot connect to network database" in lowered:
        return f"Cannot reach the network database. {FACILITY_NETWORK_GUIDANCE}"
    if any(marker in lowered for marker in (
        "failed to establish",
        "max retries exceeded",
        "no route to host",
        "network is unreachable",
        "offline",
        "unreachable",
        "name resolution",
        "temporary failure",
    )):
        return f"Cannot reach the Raspberry Pi Control Service. {FACILITY_NETWORK_GUIDANCE}"
    if "malformed" in lowered or "invalid response" in lowered:
        return "The Raspberry Pi server replied with an unreadable response. Ask IT to check the Control Service logs."
    if not detail:
        return f"The request could not be completed. {FACILITY_NETWORK_GUIDANCE}"
    return detail


@dataclass
class ControlServiceProfile:
    name: str
    host: str
    port: int
    api_key: str = ""
    description: str = ""

    @property
    def base_url(self) -> str:
        try:
            port = int(self.port)
        except (TypeError, ValueError):
            port = 7000
        return f"http://{self.host.strip()}:{port}"


class ControlServiceClient:
    def __init__(self, host: str, port: int, api_key: str = "", timeout: float = 4.0):
        self.host = (host or "").strip()
        try:
            self.port = int(port or 7000)
        except (TypeError, ValueError):
            self.port = 0
        self.api_key = api_key or ""
        self.timeout = timeout
        self.session = requests.Session()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def configured(self) -> bool:
        return bool(self.host and self.port)

    def masked_api_key(self) -> str:
        key = self.api_key or ""
        if not key:
            return "Not configured"
        tail = key[-4:] if len(key) >= 4 else key
        return f"{'*' * max(8, len(key) - len(tail))}{tail}"

    def _headers(self) -> Dict[str, str]:
        return {"X-Whisperwood-Key": self.api_key}

    def _result(
        self,
        ok: bool,
        endpoint: str,
        status_code: Optional[int] = None,
        data: Optional[Any] = None,
        error: str = "",
    ) -> Dict[str, Any]:
        return {
            "ok": ok,
            "endpoint": endpoint,
            "status_code": status_code,
            "data": data,
            "error": error,
            "url": f"{self.base_url}{endpoint}" if self.configured() else "",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        json_body: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        require_key: bool = True,
    ) -> Dict[str, Any]:
        if not self.host:
            return self._result(False, endpoint, error="Control Service host is not configured.")
        if not self.port or self.port < 1 or self.port > 65535:
            return self._result(False, endpoint, error="Control Service port is not valid.")
        if require_key and not self.api_key:
            return self._result(False, endpoint, error="Missing Control Service API key.")

        try:
            response = self.session.request(
                method,
                f"{self.base_url}{endpoint}",
                headers=self._headers() if self.api_key else {},
                json=json_body,
                files=files,
                data=data,
                params=params,
                timeout=self.timeout,
            )
        except requests.Timeout:
            return self._result(False, endpoint, error=friendly_error_message("Control Service request timed out.", endpoint))
        except requests.ConnectionError as exc:
            return self._result(False, endpoint, error=friendly_error_message(str(exc), endpoint))
        except requests.RequestException as exc:
            return self._result(False, endpoint, error=friendly_error_message(str(exc), endpoint))

        if response.status_code == 403:
            return self._result(False, endpoint, response.status_code, error=friendly_error_message("Unauthorized Control Service request.", endpoint, response.status_code))

        try:
            data = response.json()
        except ValueError:
            return self._result(False, endpoint, response.status_code, error=friendly_error_message("Malformed Control Service response.", endpoint, response.status_code))

        if response.status_code >= 400:
            message = data.get("err") or data.get("error") or data.get("message") or response.reason
            return self._result(False, endpoint, response.status_code, data, friendly_error_message(str(message), endpoint, response.status_code, data))

        return self._result(True, endpoint, response.status_code, data)

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/health", require_key=False)

    def login(self, username: str, password: str) -> Dict[str, Any]:
        return self._request("POST", "/auth/login", {"username": username, "password": password})

    def change_password(self, user_id: int, current_password: str, new_password: str, username: str = "") -> Dict[str, Any]:
        return self._request("POST", "/auth/change-password", {
            "username": username,
            "old_password": current_password,
            "new_password": new_password,
        })

    def set_temporary_password(self, user_id: int, temporary_password: str, username: str = "") -> Dict[str, Any]:
        return self._request("POST", "/auth/temp-password", {"username": username})

    def get_users(self) -> Dict[str, Any]:
        return self._request("GET", "/users")

    def create_user(
        self,
        username: str,
        password: str,
        role: str,
        full_name: str = "",
        must_change_password: bool = True,
    ) -> Dict[str, Any]:
        payload = {
            "username": username,
            "password": password,
            "role": role,
            "is_active": True,
            "force_password_change": bool(must_change_password),
        }
        if full_name:
            payload["full_name"] = full_name
        return self._request("POST", "/users", payload)

    def set_user_status(self, username: str, active: bool) -> Dict[str, Any]:
        return self._request("PUT", f"/users/{username}/status", {"is_active": bool(active)})

    def get_residents(self) -> Dict[str, Any]:
        return self._request("GET", "/residents")

    def create_resident(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/residents", payload)

    def update_resident(self, resident_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"/residents/{resident_id}", payload)

    def archive_resident(self, resident_id: int) -> Dict[str, Any]:
        return self._request("PUT", f"/residents/{resident_id}/archive", {"archived": True, "active": False})

    def upload_document(self, resident_id: int, document_path: str) -> Dict[str, Any]:
        if not document_path or not os.path.isfile(document_path):
            return self._result(False, f"/residents/{resident_id}/document", error="Document file was not found.")
        with open(document_path, "rb") as fh:
            files = {"document": (os.path.basename(document_path), fh, "application/octet-stream")}
            return self._request("POST", f"/residents/{resident_id}/document", files=files)

    def get_document(self, resident_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/residents/{resident_id}/document")

    def upload_image(self, resident_id: int, image_path: str) -> Dict[str, Any]:
        if not image_path or not os.path.isfile(image_path):
            return self._result(False, f"/residents/{resident_id}/image", error="Image file was not found.")
        with open(image_path, "rb") as fh:
            files = {"image": (os.path.basename(image_path), fh, "application/octet-stream")}
            return self._request("POST", f"/residents/{resident_id}/image", files=files)

    def get_image(self, resident_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/residents/{resident_id}/image")

    def get_devices(self) -> Dict[str, Any]:
        return self._request("GET", "/devices")

    def upsert_device(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/devices", payload)

    def pair_device(self, resident_id: int, device_id: str) -> Dict[str, Any]:
        return self._request("POST", "/devices/pair", {"resident_id": resident_id, "device_id": device_id})

    def unpair_device(self, device_id: str) -> Dict[str, Any]:
        return self._request("POST", "/devices/unpair", {"device_id": device_id})

    def get_schedules(self) -> Dict[str, Any]:
        return self._request("GET", "/schedules")

    def save_schedule(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/schedules", payload)

    def get_dropdown_options(self) -> Dict[str, Any]:
        primary = self._request("GET", "/resident-dropdown-options")
        if primary.get("ok") or primary.get("status_code") not in {404, 405}:
            return primary
        return self._request("GET", "/dropdown-options")

    def save_dropdown_options(self, options: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"options": options or {}}
        attempts = [
            ("PUT", "/resident-dropdown-options"),
            ("POST", "/resident-dropdown-options"),
            ("PUT", "/dropdown-options"),
            ("POST", "/dropdown-options"),
        ]
        last = None
        for method, endpoint in attempts:
            last = self._request(method, endpoint, payload)
            if last.get("ok") or last.get("status_code") not in {404, 405}:
                return last
        return last or self._result(False, "/resident-dropdown-options", error="Dropdown options endpoint is not available.")

    def get_battery_alert_settings(self) -> Dict[str, Any]:
        return self._request("GET", "/battery-alert-settings")

    def save_battery_alert_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(settings or {})
        primary = self._request("PUT", "/battery-alert-settings", payload)
        if primary.get("ok") or primary.get("status_code") not in {404, 405}:
            return primary
        return self._request("POST", "/battery-alert-settings", payload)

    def get_dashboard_summary(self) -> Dict[str, Any]:
        return self._request("GET", "/dashboard/summary")

    def create_change_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/resident-change-requests", payload)

    def get_change_requests(self, status: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        params = {"limit": limit}
        if status:
            params["status"] = status
        return self._request("GET", "/resident-change-requests", params=params)

    def decide_change_request(self, request_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"/resident-change-requests/{request_id}/decision", payload)

    def get_resident_audit(self, limit: int = 200) -> Dict[str, Any]:
        return self._request("GET", "/resident-audit", params={"limit": limit})

    def get_resident_audit_for_resident(self, resident_id: int, limit: int = 200) -> Dict[str, Any]:
        return self._request("GET", f"/residents/{resident_id}/audit", params={"limit": limit})

    def create_verification_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/verification-checks", payload)

    def get_verification_checks(self, limit: int = 100) -> Dict[str, Any]:
        return self._request("GET", "/verification-checks", params={"limit": limit})

    def get_logs(self, limit: Optional[int] = None) -> Dict[str, Any]:
        return self._request("GET", "/logs", params={"limit": limit} if limit else None)

    def get_log(self, log_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/logs/{log_id}")

    def get_it_audit_logs(self, limit: int = 100) -> Dict[str, Any]:
        return self._request("GET", "/it-audit-logs", params={"limit": limit})

    def create_it_audit_log(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/it-audit-logs", payload)

    def system_status(self) -> Dict[str, Any]:
        return self._request("GET", "/system")

    def network_status(self) -> Dict[str, Any]:
        return self._request("GET", "/network")

    def tailscale_status(self) -> Dict[str, Any]:
        return self._request("GET", "/tailscale")

    def operation_status(self) -> Dict[str, Any]:
        return self._request("GET", "/operation/status")

    def restart_operation(self) -> Dict[str, Any]:
        return self._request("POST", "/operation/restart")

    def operation_devices(self) -> Dict[str, Any]:
        return self._request("GET", "/operation/devices")

    def operation_send_text(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/operation/send", payload)

    def operation_send_image(self, device_id: str, image_path: str) -> Dict[str, Any]:
        if not image_path or not os.path.isfile(image_path):
            return self._result(False, "/operation/send_image", error="Image file was not found.")
        with open(image_path, "rb") as fh:
            files = {"image": (os.path.basename(image_path), fh, "application/octet-stream")}
            return self._request("POST", "/operation/send_image", files=files, data={"id": device_id})

    def operation_lcd_command(self, device_id: str, command: str) -> Dict[str, Any]:
        return self._request("POST", "/operation/lcd", {"id": device_id, "command": command})

    def operation_schedule(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/operation/schedule", payload)

    def operation_resident_display(self, resident_id: int, device_id: str = "") -> Dict[str, Any]:
        return self._request("POST", "/operation/resident-display", {
            "resident_id": resident_id,
            "device_id": device_id,
        })

    def bootstrap_info(self) -> Dict[str, Any]:
        return self._request("GET", "/bootstrap/info")

    def pending(self, feature_name: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "pending": True,
            "feature": feature_name,
            "message": "Pending backend implementation",
        }

    def logs(self) -> Dict[str, Any]:
        return self.get_logs()

    def create_backup(self) -> Dict[str, Any]:
        return self.pending("create_backup")

    def restore_backup(self) -> Dict[str, Any]:
        return self.pending("restore_backup")

    def ota_status(self) -> Dict[str, Any]:
        return self.pending("ota_status")

    def upload_firmware(self) -> Dict[str, Any]:
        return self.pending("upload_firmware")

    def release_firmware(self) -> Dict[str, Any]:
        return self.pending("release_firmware")

    def ai_debug_summary(self) -> Dict[str, Any]:
        return self.pending("ai_debug_summary")
