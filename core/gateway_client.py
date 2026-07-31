import os
from typing import Dict, Any, List

import requests

from core.models import Device


class GatewayClient:
    def __init__(self):
        self.session = requests.Session()

    def get_devices(self, base_url: str) -> List[Device]:
        r = self.session.get(f"{base_url.rstrip('/')}/devices", timeout=3)
        r.raise_for_status()
        data = r.json()
        rows = data.get("devices") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            rows = []

        devices = []
        for d in rows:
            devices.append(Device(
                id=d.get("device_id") or d.get("id", ""),
                ip=d.get("ip", ""),
                port=int(d.get("port", 0)),
                fw=d.get("fw"),
                pending_seq=d.get("pending_seq"),
                pending_img_seq=d.get("pending_img_seq"),
                last_seen_s=int(d.get("last_seen_s", 9999)),
                battery_level=d.get("battery_level"),
                online=d.get("is_online", d.get("online")),
                battery_ok=d.get("battery_ok"),
                battery_mv=d.get("battery_mv"),
                battery_voltage=d.get("battery_voltage"),
                battery_raw_percent=d.get("battery_raw_percent"),
                battery_low=d.get("battery_low"),
                battery_alert=d.get("battery_alert"),
                battery_plugged=d.get("battery_plugged"),
                battery_charging=d.get("battery_charging"),
                battery_full=d.get("battery_full"),
                rssi=d.get("rssi"),
                heap=d.get("heap"),
                last_status_at=d.get("last_status_at"),
            ))
        return devices

    def send_text(self, base_url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        r = self.session.post(f"{base_url.rstrip('/')}/send", json=payload, timeout=100)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}
        return {"status_code": r.status_code, "body": body}

    def send_image(self, base_url: str, device_id: str, image_path: str) -> Dict[str, Any]:
        with open(image_path, "rb") as f:
            files = {
                "image": (os.path.basename(image_path), f, "application/octet-stream")
            }
            data = {"id": device_id}
            r = self.session.post(
                f"{base_url.rstrip('/')}/send_image",
                data=data,
                files=files,
                timeout=30,
            )
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}
        return {"status_code": r.status_code, "body": body}

    def send_lcd_command(self, base_url: str, device_id: str, command: str) -> Dict[str, Any]:
        payload = {"id": device_id, "command": command}
        r = self.session.post(f"{base_url.rstrip('/')}/lcd", json=payload, timeout=8)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}
        return {"status_code": r.status_code, "body": body}

    def save_schedule(self, base_url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        r = self.session.post(f"{base_url.rstrip('/')}/schedule", json=payload, timeout=8)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}
        return {"status_code": r.status_code, "body": body}
