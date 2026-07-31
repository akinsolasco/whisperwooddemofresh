from typing import Any, Dict, List

from core.app_settings import AppSettingsStore
from core.control_service_client import ControlServiceClient
from core.models import Device


class ServerGatewayClient:
    def __init__(self):
        self.settings = AppSettingsStore()

    def client(self, timeout=4.0) -> ControlServiceClient:
        profile = self.settings.get_active_profile()
        return ControlServiceClient(
            profile.get("host") or "",
            profile.get("port") or 7000,
            profile.get("api_key") or "",
            timeout=timeout,
        )

    def _items(self, result: Dict[str, Any], *keys) -> List[Dict[str, Any]]:
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "Control Service offline or unreachable")
        data = result.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in keys:
                value = data.get(key)
                if isinstance(value, list):
                    return value
            nested = data.get("data")
            if isinstance(nested, list):
                return nested
        return []

    def get_devices(self, _base_url: str = "") -> List[Device]:
        rows = self._items(self.client(timeout=3.0).get_devices(), "devices", "items")
        devices = []
        for row in rows:
            device_id = row.get("device_id") or row.get("id") or ""
            devices.append(Device(
                id=device_id,
                ip=row.get("ip") or row.get("lan_ip") or "",
                port=int(row.get("port") or 0),
                fw=row.get("fw") or row.get("firmware"),
                pending_seq=row.get("pending_seq"),
                pending_img_seq=row.get("pending_img_seq"),
                last_seen_s=int(row.get("last_seen_s") or row.get("last_seen") or 9999),
                battery_level=row.get("battery_level") or row.get("battery"),
                online=row.get("is_online", row.get("online")),
                battery_ok=row.get("battery_ok"),
                battery_mv=row.get("battery_mv"),
                battery_voltage=row.get("battery_voltage"),
                battery_raw_percent=row.get("battery_raw_percent"),
                battery_low=row.get("battery_low"),
                battery_alert=row.get("battery_alert"),
                battery_plugged=row.get("battery_plugged"),
                battery_charging=row.get("battery_charging"),
                battery_full=row.get("battery_full"),
                rssi=row.get("rssi"),
                heap=row.get("heap"),
                last_status_at=row.get("last_status_at"),
            ))
        return devices

    def send_text(self, _base_url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self.client(timeout=12.0).operation_send_text(payload)
        return {
            "status_code": result.get("status_code") or (200 if result.get("ok") else 500),
            "body": result.get("data") if result.get("ok") else {"ok": False, "message": result.get("error")},
        }

    def send_image(self, _base_url: str, device_id: str, image_path: str) -> Dict[str, Any]:
        result = self.client(timeout=45.0).operation_send_image(device_id, image_path)
        return {
            "status_code": result.get("status_code") or (200 if result.get("ok") else 500),
            "body": result.get("data") if result.get("ok") else {"ok": False, "message": result.get("error")},
        }

    def send_lcd_command(self, _base_url: str, device_id: str, command: str) -> Dict[str, Any]:
        result = self.client(timeout=12.0).operation_lcd_command(device_id, command)
        return {
            "status_code": result.get("status_code") or (200 if result.get("ok") else 500),
            "body": result.get("data") if result.get("ok") else {"ok": False, "message": result.get("error")},
        }

    def save_schedule(self, _base_url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self.client(timeout=12.0).operation_schedule(payload)
        if not result.get("ok") and result.get("status_code") in {404, 405}:
            result = self.client(timeout=8.0).save_schedule(payload)
        return {
            "status_code": result.get("status_code") or (200 if result.get("ok") else 500),
            "body": result.get("data") if result.get("ok") else {"ok": False, "message": result.get("error")},
        }

    def send_resident_display(self, _base_url: str, resident_id: int, device_id: str = "") -> Dict[str, Any]:
        result = self.client(timeout=70.0).operation_resident_display(resident_id, device_id)
        return {
            "status_code": result.get("status_code") or (200 if result.get("ok") else 500),
            "body": result.get("data") if result.get("ok") else {"ok": False, "message": result.get("error")},
        }
