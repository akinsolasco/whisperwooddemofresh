import os
import re
import json
import secrets
import string
import threading
import time
from typing import Optional, List, Dict, Any

from PyQt6.QtCore import Qt, QTimer, QTime, QUrl, pyqtSignal, QEvent, QPoint, QSize
from PyQt6.QtGui import QCursor, QPixmap, QGuiApplication, QTextDocument, QPageSize, QDesktopServices
from PyQt6.QtPrintSupport import QPrinter
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFrame, QLabel, QPushButton, QLineEdit, QTextEdit,
    QComboBox, QCheckBox, QListWidget, QListWidgetItem, QMessageBox,
    QFileDialog, QStackedWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QDialog, QHBoxLayout, QTimeEdit, QAbstractSpinBox, QScrollArea, QStyle, QMenu, QSpinBox,
    QInputDialog, QApplication
)

from config import APP_NAME, APP_VERSION, DEFAULT_PI_BASE_URL, ASSETS_DIR, ROLE_LABELS, APP_DATA_DIR
from auth.auth_service import AuthService
from core.app_settings import APP_MODE_DEMO, APP_MODE_SERVER, AppSettingsStore
from core.control_service_client import ControlServiceClient, friendly_error_message
from core.db_service import DatabaseService, generate_resident_uid
from core.gateway_client import GatewayClient
from core.models import HighlightRule, auto_fg_for_bg, PALETTE, SECTIONS
from core.server_data_service import ServerDataService
from core.server_gateway_client import ServerGatewayClient


class DashboardWindow(QWidget):
    logout_requested = pyqtSignal()
    resident_display_finished = pyqtSignal(dict)

    def __init__(self, current_user: Optional[Dict[str, Any]] = None):
        super().__init__()
        self.current_user = current_user or {"id": None, "username": "admin", "role": "ADMIN"}
        self.current_role = self.normalize_role(self.current_user.get("role", "NURSE_ADMIN"))
        self.dropdown_option_defaults = {
            "diet": ["Regular", "Diabetic", "Low sodium", "Vegetarian", "High protein", "Texture modified", "Nil by mouth", "Custom"],
            "texture": ["Regular", "Easy to chew", "Soft and bite-sized", "Minced and moist", "Pureed", "Liquidised", "Thickened", "Custom"],
            "fluids": ["Regular fluids", "Encourage fluids", "Fluid restriction", "Thickened fluids", "Slightly thick", "Mildly thick", "Moderately thick", "Custom"],
        }
        self.dropdown_option_buttons = []
        self.settings = AppSettingsStore()
        self.server_mode = self.current_user.get("data_source") == "server" or self.settings.get_mode() == APP_MODE_SERVER
        self.db = ServerDataService() if self.server_mode else DatabaseService()
        self.db.ensure_tables()
        self.dropdown_options = self.load_dropdown_options()
        self.gateway = ServerGatewayClient() if self.server_mode else GatewayClient()
        self.gateway_online = False
        self.control_service_online = False
        self.control_last_results: Dict[str, Dict[str, Any]] = {}
        self.battery_alert_settings = self.default_battery_alert_settings()
        self.battery_alert_last_popup: Dict[str, float] = {}

        self.drag_pos = None
        self.normal_geometry = None
        self.is_custom_maximized = False
        self.selected_resident_id: Optional[int] = None
        self.selected_pair_resident_id: Optional[int] = None
        self.selected_pair_device_id: Optional[str] = None
        self.selected_image_path: Optional[str] = None
        self.selected_source_document: Optional[str] = None
        self.selected_review_request_id: Optional[int] = None
        self.selected_verification_resident_id: Optional[int] = None
        self.selected_audit_log: Optional[Dict[str, Any]] = None
        self.rules: List[HighlightRule] = []
        self.global_schedule_enabled = False
        self.global_schedule_on = "07:00"
        self.global_schedule_off = "20:00"
        self.global_schedule_sleep_if_no_image = False
        self.logo_path = ASSETS_DIR / "enhanced_living_whisperwood_logo_transparent.png"
        self.page_base_width = 1218

        self.setWindowTitle(f"{APP_NAME} Dashboard")
        self.setMinimumSize(1120, 760)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet("""
            QWidget {
                background-color: #f3f7fb;
                color: #0f172a;
            }
            QLabel {
                border: none;
                background: transparent;
                color: #0f172a;
            }
        """)

        self.build_ui()
        self.bind_events()
        self.resident_display_finished.connect(self.on_resident_display_finished)
        self.fit_to_screen()
        self.apply_role_permissions()
        self.apply_write_lock()

        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.refresh_all)

        self.control_status_timer = QTimer(self)
        self.control_status_timer.setInterval(5000)
        self.control_status_timer.timeout.connect(self.refresh_control_connection_status)

        self.new_resident()
        self.refresh_all()
        self.load_residents()
        self.load_recent_logs()
        self.refresh_dashboard_summary()
        self.load_approvals()
        self.load_resident_audit()
        self.load_verification_page()
        self.load_it_health()
        self.control_status_timer.start()
        QTimer.singleShot(0, self.position_window_controls)
        QTimer.singleShot(200, self.refresh_control_connection_status)
        QTimer.singleShot(650, lambda: self.load_battery_alert_settings())
        if self.current_user.get("password_must_change"):
            QTimer.singleShot(300, self.show_required_password_change)
        elif self.current_user.get("force_password_change_warning"):
            QTimer.singleShot(300, self.show_force_password_change_warning)

    # ---------------------------- roles ----------------------------

    def normalize_role(self, role: str) -> str:
        role = (role or "NURSE").strip()
        role_upper = role.upper()
        role_lower = role.lower()
        if role_upper == "ADMIN" or role_lower in {"admin", "nurse_admin", "nurseadmin"}:
            return "NURSE_ADMIN"
        if role_upper == "STAFF" or role_lower in {"staff", "nurse", "user"}:
            return "NURSE"
        if role_upper in {"IT_ADMIN", "ITADMIN"} or role_lower in {"it_admin", "itadmin", "it"}:
            return "IT_ADMIN"
        return role_upper

    def role_label(self, role: Optional[str] = None) -> str:
        return ROLE_LABELS.get(self.normalize_role(role or self.current_role), role or self.current_role)

    def is_nurse_admin(self) -> bool:
        return self.current_role in {"NURSE_ADMIN"}

    def is_nurse(self) -> bool:
        return self.current_role == "NURSE"

    def is_verifier(self) -> bool:
        return self.current_role == "VERIFIER"

    def is_it_admin(self) -> bool:
        return self.current_role == "IT_ADMIN"

    def can_edit_residents(self) -> bool:
        return self.is_nurse_admin()

    def can_view_residents(self) -> bool:
        return self.current_role in {"NURSE_ADMIN", "NURSE", "VERIFIER"}

    def can_manage_devices(self) -> bool:
        return self.current_role in {"NURSE_ADMIN", "IT_ADMIN"}

    def can_view_technical(self) -> bool:
        return self.current_role == "IT_ADMIN"

    # ---------------------------- battery alert policy ----------------------------

    def default_battery_alert_settings(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "low_threshold": 20,
            "critical_threshold": 10,
            "popup_cooldown_minutes": 30,
            "recipient_roles": ["IT_ADMIN"],
        }

    def normalize_battery_alert_settings(self, raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        settings = self.default_battery_alert_settings()
        if isinstance(raw, dict):
            settings.update(raw)
        try:
            settings["low_threshold"] = max(1, min(100, int(settings.get("low_threshold", 20))))
        except Exception:
            settings["low_threshold"] = 20
        try:
            settings["critical_threshold"] = max(1, min(settings["low_threshold"], int(settings.get("critical_threshold", 10))))
        except Exception:
            settings["critical_threshold"] = 10
        try:
            settings["popup_cooldown_minutes"] = max(1, min(1440, int(settings.get("popup_cooldown_minutes", 30))))
        except Exception:
            settings["popup_cooldown_minutes"] = 30
        roles = settings.get("recipient_roles") or ["IT_ADMIN"]
        if isinstance(roles, str):
            roles = [r.strip() for r in roles.split(",")]
        normalized_roles = []
        for role in roles:
            key = self.normalize_role(role)
            if key in {"IT_ADMIN", "NURSE_ADMIN", "NURSE", "VERIFIER"} and key not in normalized_roles:
                normalized_roles.append(key)
        settings["recipient_roles"] = normalized_roles or ["IT_ADMIN"]
        settings["enabled"] = bool(settings.get("enabled", True))
        return settings

    def role_allows_battery_popup(self) -> bool:
        settings = self.normalize_battery_alert_settings(self.battery_alert_settings)
        return settings.get("enabled", True) and self.current_role in set(settings.get("recipient_roles") or [])

    def truthy(self, value) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}

    def battery_display_text(self, device: Dict[str, Any], compact: bool = False) -> str:
        level = device.get("battery_level")
        if level is None or level == "":
            return "N/A"
        text = f"{level}%"
        voltage = device.get("battery_voltage")
        if voltage not in (None, "") and not compact:
            try:
                text += f" | {float(voltage):.2f}V"
            except Exception:
                pass
        if self.truthy(device.get("battery_low")):
            text += " | LOW" if not compact else " low"
        return text

    def power_state_text(self, device: Dict[str, Any]) -> str:
        if self.truthy(device.get("battery_full")):
            return "Fully charged"
        if self.truthy(device.get("battery_charging")):
            return "Charging"
        if self.truthy(device.get("battery_plugged")):
            return "Plugged in"
        if device.get("battery_ok") is False or str(device.get("battery_ok")).lower() == "false":
            return "Gauge not detected"
        return "On battery"

    # ---------------------------- styles ----------------------------

    def primary_btn_style(self):
        return """
            QPushButton {
                background-color: #0f766e;
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 700;
            }
            QPushButton:hover {
                background-color: #11857c;
            }
            QPushButton:pressed {
                background-color: #0b5f59;
            }
            QPushButton:disabled {
                background-color: #cbd5e1;
                color: #64748b;
            }
        """

    def secondary_btn_style(self):
        return """
            QPushButton {
                background-color: #ffffff;
                color: #0f172a;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton:hover {
                background-color: #f1f5f9;
                border: 1px solid #94a3b8;
            }
            QPushButton:disabled {
                background-color: #f1f5f9;
                color: #94a3b8;
                border: 1px solid #d8e1ea;
            }
        """

    def input_style(self):
        return """
            QLineEdit, QTextEdit, QComboBox, QTimeEdit, QSpinBox {
                background-color: #ffffff;
                color: #0f172a;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 10px 12px;
                font-size: 14px;
            }
            QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QTimeEdit:focus, QSpinBox:focus {
                border: 1px solid #0f766e;
            }
            QLineEdit:disabled, QTextEdit:disabled, QComboBox:disabled, QTimeEdit:disabled, QSpinBox:disabled {
                background-color: #f1f5f9;
                color: #94a3b8;
                border: 1px solid #d8e1ea;
            }
        """

    def label_style(self):
        return "font-size: 13px; font-weight: 700; color: #334155; background: transparent; border: none;"

    def checkbox_style(self):
        return """
            QCheckBox {
                color: #0f172a;
                font-size: 13px;
                font-weight: 700;
                spacing: 8px;
                background: transparent;
                border: none;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 5px;
                border: 1px solid #94a3b8;
                background-color: #ffffff;
            }
            QCheckBox::indicator:checked {
                background-color: #0f766e;
                border: 1px solid #0f766e;
            }
        """

    def dropdown_options_file(self):
        return APP_DATA_DIR / "resident_dropdown_options.json"

    def normalize_option_list(self, values):
        seen = set()
        out = []
        for value in values or []:
            text = str(value or "").strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            out.append(text)
        return out

    def load_local_dropdown_options(self):
        stored = {}
        path = self.dropdown_options_file()
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    stored = json.load(fh)
            except Exception:
                stored = {}
        return stored if isinstance(stored, dict) else {}

    def load_shared_dropdown_options(self):
        if not hasattr(self, "db") or not hasattr(self.db, "get_dropdown_options"):
            return None
        try:
            options = self.db.get_dropdown_options()
        except Exception:
            return None
        return options if isinstance(options, dict) else None

    def load_dropdown_options(self):
        local_options = self.load_local_dropdown_options()
        shared_options = self.load_shared_dropdown_options()
        stored = shared_options if shared_options is not None else local_options
        merged = {
            key: self.normalize_option_list(
                list(defaults) +
                list(stored.get(key, [])) +
                list(local_options.get(key, []))
            )
            for key, defaults in self.dropdown_option_defaults.items()
        }
        if shared_options is not None and merged != shared_options:
            self.save_shared_dropdown_options(merged, notify=False)
        return merged

    def save_local_dropdown_options(self):
        path = self.dropdown_options_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(self.dropdown_options, fh, indent=2)

    def save_shared_dropdown_options(self, options, notify=True):
        if not hasattr(self, "db") or not hasattr(self.db, "save_dropdown_options"):
            return False
        try:
            self.db.save_dropdown_options(options)
            return True
        except Exception as exc:
            if notify:
                self.show_error(
                    "Shared dropdown not saved",
                    f"The option was saved locally, but could not be shared with other users yet.\n\n{exc}",
                )
            return False

    def save_dropdown_options(self, notify=True):
        self.save_local_dropdown_options()
        self.save_shared_dropdown_options(self.dropdown_options, notify=notify)

    def editable_dropdown(self, parent, options, placeholder="Select or type custom"):
        combo = QComboBox(parent)
        combo.setEditable(True)
        combo.addItems(options)
        combo.setCurrentIndex(-1)
        combo.setInsertPolicy(QComboBox.InsertPolicy.InsertAtBottom)
        if combo.lineEdit():
            combo.lineEdit().setPlaceholderText(placeholder)
        combo.setStyleSheet(self.input_style())
        return combo

    def create_dropdown_option_buttons(self, parent, combo, add_x, y, delete_x):
        add_btn = QPushButton("+", parent)
        add_btn.setGeometry(add_x, y, 38, 42)
        add_btn.setToolTip("Add the typed value to this dropdown")
        add_btn.setStyleSheet(self.secondary_btn_style())
        add_btn.clicked.connect(lambda: self.add_dropdown_option(combo))

        delete_btn = QPushButton("Del", parent)
        delete_btn.setGeometry(delete_x, y, 50, 42)
        delete_btn.setToolTip("Delete the selected option from this dropdown")
        delete_btn.setStyleSheet(self.secondary_btn_style())
        delete_btn.clicked.connect(lambda: self.delete_dropdown_option(combo))

        self.dropdown_option_buttons.extend([add_btn, delete_btn])
        return add_btn, delete_btn

    def add_dropdown_option(self, combo):
        option_key = combo.property("option_key")
        text = combo.currentText().strip()
        if not text:
            text, ok = QInputDialog.getText(self, "Add dropdown option", "Option name:")
            text = text.strip()
            if not ok or not text:
                return
        if combo.findText(text, Qt.MatchFlag.MatchFixedString) < 0:
            combo.addItem(text)
        combo.setCurrentText(text)
        if option_key:
            self.dropdown_options[option_key] = self.normalize_option_list(
                [combo.itemText(i) for i in range(combo.count())]
            )
            self.save_dropdown_options()
        self.refresh_token_list()
        self.update_preview()

    def delete_dropdown_option(self, combo):
        option_key = combo.property("option_key")
        text = combo.currentText().strip()
        idx = combo.findText(text, Qt.MatchFlag.MatchFixedString)
        if idx < 0:
            self.show_error("Delete option", "Select an existing dropdown option to delete.")
            return
        if combo.count() <= 1:
            self.show_error("Delete option", "At least one option must remain in the dropdown.")
            return
        answer = QMessageBox.question(
            self,
            "Delete dropdown option",
            f"Delete '{text}' from this dropdown?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        combo.removeItem(idx)
        combo.setCurrentIndex(-1)
        combo.setEditText("")
        if option_key:
            self.dropdown_options[option_key] = self.normalize_option_list(
                [combo.itemText(i) for i in range(combo.count())]
            )
            self.save_dropdown_options()
        self.refresh_token_list()
        self.update_preview()

    def field_text(self, widget) -> str:
        if isinstance(widget, QComboBox):
            return widget.currentText().strip()
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        return ""

    def set_field_text(self, widget, value):
        value = str(value or "")
        if isinstance(widget, QComboBox):
            idx = widget.findText(value, Qt.MatchFlag.MatchFixedString)
            if idx >= 0:
                widget.setCurrentIndex(idx)
            else:
                widget.setEditText(value)
            return
        widget.setText(value)

    def clear_field_text(self, widget):
        if isinstance(widget, QComboBox):
            widget.setCurrentIndex(-1)
            widget.setEditText("")
            return
        widget.clear()

    # ---------------------------- window helpers ----------------------------

    def available_geometry_for_window(self):
        center = self.frameGeometry().center()
        screen = QGuiApplication.screenAt(center)
        if screen is None:
            screen = QGuiApplication.screenAt(QCursor.pos())
        if screen is None and self.windowHandle() is not None:
            screen = self.windowHandle().screen()
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        return screen.availableGeometry()

    def fit_to_screen(self):
        available = self.available_geometry_for_window()
        width = min(available.width(), max(self.minimumWidth(), int(available.width() * 0.98)))
        height = min(available.height(), max(self.minimumHeight(), int(available.height() * 0.97)))
        x = available.x() + (available.width() - width) // 2
        y = available.y() + (available.height() - height) // 2
        self.setGeometry(x, y, width, height)
        self.is_custom_maximized = False

    # ---------------------------- build ui ----------------------------

    def build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)

        self.container = QFrame()
        self.container.setStyleSheet("""
            background-color: #f3f7fb;
            border-radius: 8px;
            color: #0f172a;
        """)
        root.addWidget(self.container)

        # Sidebar
        self.sidebar = QFrame(self.container)
        self.sidebar.setGeometry(12, 12, 245, 896)
        self.apply_frame_style(self.sidebar, "background-color: #ffffff; border-radius: 10px; border: 1px solid #d8e1ea;")

        self.logo = QLabel(self.sidebar)
        self.logo.setGeometry(10, 12, 224, 98)
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if self.logo_path.exists():
            self.logo.setPixmap(
                QPixmap(str(self.logo_path)).scaled(
                    216, 92,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
            )
        else:
            self.logo.setText(APP_NAME)
            self.logo.setStyleSheet("font-size: 22px; font-weight: 800; color: #0f766e;")

        self.user_card = QFrame(self.sidebar)
        self.user_card.setGeometry(18, 115, 208, 88)
        self.apply_frame_style(self.user_card, "background-color: #f8fafc; border-radius: 8px; border: 1px solid #d8e1ea;")

        self.user_avatar = QLabel(self.user_card)
        self.user_avatar.setGeometry(12, 20, 48, 48)
        self.user_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.user_avatar.setText((self.current_user.get("username") or "U")[0].upper())
        self.user_avatar.setStyleSheet("""
            QLabel {
                background-color: #0f766e;
                color: white;
                border-radius: 24px;
                font-size: 18px;
                font-weight: 700;
            }
        """)

        self.user_name = QLabel(self.user_card)
        self.user_name.setGeometry(72, 18, 120, 22)
        self.user_name.setText(self.current_user.get("username", "admin"))
        self.user_name.setStyleSheet("font-size: 16px; font-weight: 800; color: #0f172a;")

        self.user_role = QLabel(self.user_card)
        self.user_role.setGeometry(72, 45, 120, 18)
        self.user_role.setText(self.role_label())
        self.user_role.setStyleSheet("font-size: 12px; color: #64748b;")

        nav_buttons = [
            ("Overview", 190),
            ("Resident Records", 235),
            ("Approvals", 280),
            ("Resident Audit", 325),
            ("Device Pairing", 370),
            ("LCD Schedule", 415),
            ("Verification", 460),
            ("IT Control Center", 505),
            ("Logs Admin", 550),
        ]

        self.btn_menu_overview = QPushButton(nav_buttons[0][0], self.sidebar)
        self.btn_menu_overview.setGeometry(18, nav_buttons[0][1], 208, 42)

        self.btn_menu_dashboard = QPushButton(nav_buttons[1][0], self.sidebar)
        self.btn_menu_dashboard.setGeometry(18, nav_buttons[1][1], 208, 42)

        self.btn_menu_approvals = QPushButton(nav_buttons[2][0], self.sidebar)
        self.btn_menu_approvals.setGeometry(18, nav_buttons[2][1], 208, 42)

        self.btn_menu_resident_audit = QPushButton(nav_buttons[3][0], self.sidebar)
        self.btn_menu_resident_audit.setGeometry(18, nav_buttons[3][1], 208, 42)

        self.btn_menu_pairing = QPushButton(nav_buttons[4][0], self.sidebar)
        self.btn_menu_pairing.setGeometry(18, nav_buttons[4][1], 208, 42)

        self.btn_menu_updates = QPushButton(nav_buttons[5][0], self.sidebar)
        self.btn_menu_updates.setGeometry(18, nav_buttons[5][1], 208, 42)

        self.btn_menu_verification = QPushButton(nav_buttons[6][0], self.sidebar)
        self.btn_menu_verification.setGeometry(18, nav_buttons[6][1], 208, 42)

        self.btn_menu_it_health = QPushButton(nav_buttons[7][0], self.sidebar)
        self.btn_menu_it_health.setGeometry(18, nav_buttons[7][1], 208, 42)

        self.btn_menu_logs = QPushButton(nav_buttons[8][0], self.sidebar)
        self.btn_menu_logs.setGeometry(18, nav_buttons[8][1], 208, 42)

        self.nav_buttons = [
            self.btn_menu_overview,
            self.btn_menu_dashboard,
            self.btn_menu_approvals,
            self.btn_menu_resident_audit,
            self.btn_menu_pairing,
            self.btn_menu_updates,
            self.btn_menu_verification,
            self.btn_menu_it_health,
            self.btn_menu_logs,
        ]

        for b in self.nav_buttons:
            b.setIconSize(QSize(18, 18))
            b.setStyleSheet("""
                QPushButton {
                    text-align: left;
                    padding-left: 18px;
                    background-color: transparent;
                    color: #334155;
                    border: none;
                    border-radius: 8px;
                    font-size: 14px;
                    font-weight: 600;
                }
                QPushButton:hover {
                    background-color: #eef4f8;
                    color: #0f172a;
                }
            """)
        self.apply_sidebar_icons()

        self.btn_refresh_devices = QPushButton("Refresh", self.sidebar)
        self.btn_refresh_devices.setGeometry(18, 590, 208, 42)
        self.btn_refresh_devices.setStyleSheet(self.secondary_btn_style())

        self.auto_refresh = QCheckBox("Auto-refresh every 3s", self.sidebar)
        self.auto_refresh.setGeometry(24, 642, 180, 24)
        self.auto_refresh.setStyleSheet("""
            QCheckBox {
                color: #334155;
                font-size: 13px;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 15px;
                height: 15px;
                border-radius: 4px;
                border: 1px solid #94a3b8;
                background: #ffffff;
            }
            QCheckBox::indicator:checked {
                background-color: #0f766e;
                border: 1px solid #0f766e;
            }
        """)

        self.connection_badge = QLabel("Gateway: Checking", self.sidebar)
        self.connection_badge.setGeometry(18, 680, 208, 28)
        self.connection_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.connection_badge.setStyleSheet("""
            QLabel {
                background-color: #f8fafc;
                color: #334155;
                border: 1px solid #d8e1ea;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 700;
            }
        """)

        self.btn_account_profile = QPushButton("Profile", self.sidebar)
        self.btn_account_profile.setGeometry(18, 705, 208, 42)
        self.btn_account_profile.setStyleSheet(self.secondary_btn_style())

        self.btn_profile_settings = QPushButton("Settings", self.sidebar)
        self.btn_profile_settings.setGeometry(18, 755, 208, 42)
        self.btn_profile_settings.setStyleSheet(self.secondary_btn_style())

        self.btn_logout = QPushButton("Logout", self.sidebar)
        self.btn_logout.setGeometry(18, 805, 208, 42)
        self.btn_logout.setStyleSheet(self.secondary_btn_style())

        self.sidebar_version = QLabel(f"Demo v{APP_VERSION}", self.sidebar)
        self.sidebar_version.setGeometry(18, 856, 208, 22)
        self.sidebar_version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sidebar_version.setStyleSheet("font-size: 12px; color: #64748b; font-weight: 800;")

        # Title area
        self.title = QLabel(f"{APP_NAME} Control Center", self.container)
        self.title.setGeometry(280, 22, 850, 32)
        self.title.setStyleSheet("font-size: 26px; font-weight: 800; color: #0f172a;")

        self.subtitle = QLabel("Hospital-grade resident display operations, approvals, verification, and technical health", self.container)
        self.subtitle.setGeometry(280, 56, 860, 18)
        self.subtitle.setStyleSheet("font-size: 13px; color: #475569;")

        self.version_badge = QLabel(f"Demo v{APP_VERSION}", self.container)
        self.version_badge.setGeometry(1140, 56, 180, 24)
        self.version_badge.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.version_badge.setStyleSheet("font-size: 12px; color: #64748b; font-weight: 800;")

        self.base_url_edit = QLineEdit(self.container)
        self.base_url_edit.setGeometry(0, 0, 1, 1)
        self.base_url_edit.setText(DEFAULT_PI_BASE_URL)
        self.base_url_edit.setStyleSheet(self.input_style())
        self.base_url_edit.setVisible(False)

        self.min_btn = QPushButton("-", self.container)
        self.min_btn.setGeometry(1370, 24, 38, 38)
        self.min_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.min_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #475569;
                border: none;
                font-size: 18px;
                font-weight: 700;
            }
            QPushButton:hover {
                color: #0f172a;
                background-color: rgba(15, 23, 42, 0.08);
                border-radius: 8px;
            }
        """)

        self.max_btn = QPushButton("[]", self.container)
        self.max_btn.setGeometry(1415, 24, 38, 38)
        self.max_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.max_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #475569;
                border: none;
                font-size: 15px;
                font-weight: 700;
            }
            QPushButton:hover {
                color: #0f172a;
                background-color: rgba(15, 23, 42, 0.08);
                border-radius: 8px;
            }
        """)

        self.close_btn = QPushButton("X", self.container)
        self.close_btn.setGeometry(1460, 24, 38, 38)
        self.close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.close_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #475569;
                border: none;
                font-size: 18px;
                font-weight: 700;
            }
            QPushButton:hover {
                color: #0f172a;
                background-color: rgba(15, 23, 42, 0.08);
                border-radius: 8px;
            }
        """)

        # Pages
        self.pages = QStackedWidget(self.container)
        self.pages.setGeometry(280, 95, self.page_base_width, 805)
        self.pages.setStyleSheet("background: transparent;")

        self.page_overview = self.build_overview_page()
        self.page_dashboard = self.build_dashboard_page()
        self.page_approvals = self.build_approvals_page()
        self.page_resident_audit = self.build_resident_audit_page()
        self.page_pairing = self.build_pairing_page()
        self.page_updates = self.build_updates_page()
        self.page_verification = self.build_verification_page()
        self.page_it_health = self.build_it_health_page()
        self.page_logs = self.build_logs_page()

        for p in [
            self.page_overview,
            self.page_dashboard,
            self.page_approvals,
            self.page_resident_audit,
            self.page_pairing,
            self.page_updates,
            self.page_verification,
            self.page_it_health,
            self.page_logs,
        ]:
            self.pages.addWidget(p)

        self.pages.setCurrentWidget(self.page_overview)
        self.set_active_menu(self.btn_menu_overview)
        self.build_sidebar_it_control_menu()
        self.strip_text_only_label_frames()
        self.min_btn.setText("-")
        self.max_btn.setText("[]")
        self.close_btn.setText("X")
        self.position_window_controls()

    def toggle_max_restore(self):
        if self.is_custom_maximized:
            if self.normal_geometry is not None:
                self.setGeometry(self.normal_geometry)
            self.is_custom_maximized = False
        else:
            self.normal_geometry = self.geometry()
            available = self.available_geometry_for_window()
            self.setGeometry(available)
            self.is_custom_maximized = True
        self.max_btn.setText("[]")

    def position_window_controls(self):
        self.sidebar.setGeometry(12, 12, 245, max(640, self.container.height() - 24))
        sidebar_h = self.sidebar.height()

        nav_y = 190
        visible_nav = [btn for btn in self.nav_buttons if btn.isVisible()]
        nav_step = 45
        if len(visible_nav) >= 8 and sidebar_h < 780:
            nav_step = 41
        for btn in visible_nav:
            btn.setGeometry(18, nav_y, 208, 40)
            nav_y += nav_step

        controls = []
        if self.btn_refresh_devices.isVisible():
            controls.append((self.btn_refresh_devices, 42, 10))
        if self.auto_refresh.isVisible():
            controls.append((self.auto_refresh, 24, 10))
        controls.append((self.connection_badge, 28, 10))
        controls.append((self.btn_account_profile, 42, 10))
        if self.btn_profile_settings.isVisible():
            controls.append((self.btn_profile_settings, 42, 10))
        controls.append((self.btn_logout, 42, 0))
        controls.append((self.sidebar_version, 22, 0))

        controls_height = sum(height + gap for _widget, height, gap in controls)
        controls_y = max(nav_y + 14, sidebar_h - controls_height - 18)
        for widget, height, gap in controls:
            x = 24 if widget is self.auto_refresh else 18
            width = 180 if widget is self.auto_refresh else 208
            widget.setGeometry(x, controls_y, width, height)
            controls_y += height + gap

        right = self.container.width() - 48
        self.close_btn.move(right, 24)
        self.max_btn.move(right - 45, 24)
        self.min_btn.move(right - 90, 24)
        self.version_badge.setGeometry(max(280, right - 280), 56, 230, 24)
        self.base_url_edit.setVisible(False)

        available_width = max(640, self.container.width() - 302)
        pages_width = min(self.page_base_width, available_width)
        pages_x = 280 + max(0, (available_width - pages_width) // 2)
        self.pages.setGeometry(pages_x, 95, pages_width, max(500, self.container.height() - 115))

    def apply_sidebar_icons(self):
        icon_map = {
            self.btn_menu_overview: QStyle.StandardPixmap.SP_DesktopIcon,
            self.btn_menu_dashboard: QStyle.StandardPixmap.SP_FileDialogInfoView,
            self.btn_menu_approvals: QStyle.StandardPixmap.SP_DialogApplyButton,
            self.btn_menu_resident_audit: QStyle.StandardPixmap.SP_FileDialogDetailedView,
            self.btn_menu_pairing: QStyle.StandardPixmap.SP_DriveNetIcon,
            self.btn_menu_updates: QStyle.StandardPixmap.SP_MediaPlay,
            self.btn_menu_verification: QStyle.StandardPixmap.SP_DialogYesButton,
            self.btn_menu_it_health: QStyle.StandardPixmap.SP_ComputerIcon,
            self.btn_menu_logs: QStyle.StandardPixmap.SP_FileDialogContentsView,
        }
        for button, icon_key in icon_map.items():
            button.setIcon(self.style().standardIcon(icon_key))
            button.setIconSize(QSize(18, 18))

    def build_sidebar_it_control_menu(self):
        self.it_control_sidebar_menu = QMenu(self)
        self.it_control_sidebar_menu.setStyleSheet("""
            QMenu {
                background-color: #ffffff;
                color: #0f172a;
                border: 1px solid #d8e1ea;
                border-radius: 8px;
                padding: 6px;
                font-size: 13px;
                font-weight: 700;
            }
            QMenu::item {
                padding: 8px 28px 8px 10px;
                border-radius: 6px;
            }
            QMenu::item:selected {
                background-color: #eef4f8;
                color: #0f172a;
            }
        """)
        sections = [
            (0, "Dashboard", "dashboard"),
            (1, "Services", "services"),
            (2, "Devices", "devices"),
            (3, "OTA", "ota"),
            (4, "Backups", "backups"),
            (5, "Logs", "logs"),
            (6, "AI Debug", "ai"),
        ]
        for index, label, icon_key in sections:
            action = self.it_control_sidebar_menu.addAction(self.control_section_icon(icon_key), label)
            action.triggered.connect(lambda _checked=False, idx=index: self.open_it_control_section(idx))
        self.btn_menu_it_health.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is getattr(self, "btn_menu_it_health", None) and event.type() == QEvent.Type.Enter:
            self.show_it_control_sidebar_menu()
        return super().eventFilter(watched, event)

    def show_it_control_sidebar_menu(self):
        if not getattr(self, "it_control_sidebar_menu", None) or not self.btn_menu_it_health.isVisible():
            return
        pos = self.btn_menu_it_health.mapToGlobal(QPoint(0, self.btn_menu_it_health.height() + 2))
        self.it_control_sidebar_menu.popup(pos)

    def open_it_control_section(self, index):
        if self.pages.currentWidget() != self.page_it_health:
            self.switch_page(self.page_it_health, self.btn_menu_it_health)
        else:
            self.set_active_menu(self.btn_menu_it_health)
        self.set_it_control_section(index)

    def card_style(self):
        return "background-color: #ffffff; border-radius: 8px; border: 1px solid #d8e1ea;"

    def table_style(self):
        return """
            QTableWidget {
                background-color: #ffffff;
                alternate-background-color: #f8fafc;
                color: #0f172a;
                border: 1px solid #d8e1ea;
                border-radius: 8px;
                gridline-color: #e2e8f0;
                font-size: 12px;
                selection-background-color: #ccfbf1;
                selection-color: #0f172a;
            }
            QHeaderView::section {
                background-color: #eef4f8;
                color: #334155;
                padding: 8px;
                border: none;
                font-weight: 700;
            }
            QTableWidget::item {
                padding: 6px;
            }
        """

    def apply_frame_style(self, frame: QFrame, css: str):
        if not frame.objectName():
            frame.setObjectName(f"frame_{id(frame)}")
        replacements = {
            "#0a0a0a": "#f3f7fb",
            "#0b1117": "#f3f7fb",
            "#0f1720": "#ffffff",
            "#101010": "#ffffff",
            "#101820": "#ffffff",
            "#111111": "#f8fafc",
            "#121212": "#ffffff",
            "#13202a": "#f8fafc",
            "#17212b": "#eef4f8",
            "#1a1a1a": "#eef4f8",
            "#1f1f1f": "#d8e1ea",
            "#242424": "#d8e1ea",
            "#253241": "#d8e1ea",
            "#262626": "#d8e1ea",
            "#273447": "#cbd5e1",
            "#2b3a48": "#cbd5e1",
            "#304050": "#cbd5e1",
            "#efefef": "#ffffff",
            "#f8fafc": "#ffffff",
            "#0a1831": "#e0f2fe",
            "#09283a": "#e0f2fe",
            "#20457b": "#7dd3fc",
            "#1f5f7a": "#7dd3fc",
        }
        for old, new in replacements.items():
            css = css.replace(old, new)
        css = re.sub(r"border-radius:\s*(?:1[0-9]|2[0-9])px", "border-radius: 8px", css)
        frame.setStyleSheet(f"QFrame#{frame.objectName()} {{{css}}}")

    def normalize_light_label_style(self, css: str) -> str:
        replacements = {
            "color: white": "color: #0f172a",
            "color:white": "color: #0f172a",
            "color: #f8fafc": "color: #0f172a",
            "color: #eef2f7": "color: #0f172a",
            "color: #edf2f7": "color: #0f172a",
            "color: #d9e2ec": "color: #334155",
            "color: #d7e0ea": "color: #334155",
            "color: #dedede": "color: #334155",
            "color: #d8d8d8": "color: #334155",
            "color: #d7d7d7": "color: #334155",
            "color: #cfcfcf": "color: #475569",
            "color: #c9d5df": "color: #334155",
            "color: #b8c1cc": "color: #475569",
            "color: #aeb7c2": "color: #64748b",
            "color: #a8a8a8": "color: #64748b",
            "color: #a7a7a7": "color: #64748b",
            "color: #9fb2c3": "color: #64748b",
            "color: #d7e3f1": "color: #475569",
        }
        for old, new in replacements.items():
            css = css.replace(old, new)
        return css

    def strip_text_only_label_frames(self):
        # Render text labels as plain text (no visible container box).
        protected_labels = {
            getattr(self, "user_avatar", None),
            getattr(self, "lcd_empty_state", None),
            getattr(self, "upd_lcd_empty_state", None),
        }
        for label in self.findChildren(QLabel):
            if label in protected_labels:
                continue
            pixmap = label.pixmap()
            if pixmap is not None and not pixmap.isNull():
                continue

            style = label.styleSheet() or ""
            label.setFrameStyle(0)
            clean = re.sub(r"(?i)\bbackground(?:-color)?\s*:\s*[^;]+;?", "", style)
            clean = re.sub(r"(?i)\bborder\s*:\s*[^;]+;?", "", clean)
            clean = self.normalize_light_label_style(clean)
            clean = clean.strip().rstrip(";")
            if clean:
                label.setStyleSheet(f"{clean}; background: transparent; border: none;")
            else:
                label.setStyleSheet("background: transparent; border: none;")

    def wrap_scroll_page(self, content: QWidget, min_height: int):
        content.setMinimumSize(self.page_base_width, min_height)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
            }
            QScrollBar:vertical {
                background: #eef4f8;
                width: 10px;
                border-radius: 5px;
                margin: 6px 2px 6px 2px;
            }
            QScrollBar::handle:vertical {
                background: #cbd5e1;
                border-radius: 5px;
                min-height: 28px;
            }
            QScrollBar:horizontal {
                background: #eef4f8;
                height: 10px;
                border-radius: 5px;
                margin: 2px 6px 2px 6px;
            }
            QScrollBar::handle:horizontal {
                background: #cbd5e1;
                border-radius: 5px;
                min-width: 28px;
            }
            QScrollBar::add-line, QScrollBar::sub-line {
                width: 0px;
                height: 0px;
            }
        """)
        scroll.setWidget(content)
        return scroll

    # ---------------------------- dashboard page ----------------------------

    def build_overview_page(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")

        hero = QFrame(page)
        hero.setGeometry(0, 0, 1218, 145)
        self.apply_frame_style(hero, "background-color: #101010; border-radius: 22px; border: 1px solid #273447;")

        title = QLabel("Clinical Operations Dashboard", hero)
        title.setGeometry(24, 22, 420, 32)
        title.setStyleSheet("font-size: 26px; font-weight: 800; color: white;")

        subtitle = QLabel("Resident data, approval workflow, display verification, and technical readiness for site rollout.", hero)
        subtitle.setGeometry(24, 58, 780, 24)
        subtitle.setStyleSheet("font-size: 13px; color: #b8c1cc;")

        self.btn_overview_new_resident = QPushButton("Open Resident Records", hero)
        self.btn_overview_new_resident.setGeometry(24, 92, 190, 42)
        self.btn_overview_new_resident.setStyleSheet(self.primary_btn_style())

        self.btn_overview_pairing = QPushButton("Go to Pairing", hero)
        self.btn_overview_pairing.setGeometry(228, 92, 150, 42)
        self.btn_overview_pairing.setStyleSheet(self.secondary_btn_style())

        self.overview_status = QLabel("Gateway and local database status will appear here.", hero)
        self.overview_status.setObjectName("overview_status_text")
        self.overview_status.setGeometry(820, 28, 360, 82)
        self.overview_status.setWordWrap(True)
        self.overview_status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.overview_status.setFrameStyle(0)
        self.overview_status.setStyleSheet("""
            QLabel#overview_status_text {
                font-size: 13px;
                color: #d8d8d8;
                background: transparent;
                border: none;
                padding: 0px;
            }
        """)

        self.summary_labels = {}
        cards = [
            ("active_residents", "Active residents", 0, 165),
            ("pending_requests", "Pending approvals", 248, 165),
            ("verification_mismatches", "Display mismatches", 496, 165),
            ("online_devices", "Connected now", 744, 165),
            ("failed_updates", "Failed updates", 992, 165),
        ]
        for key, label, x, y in cards:
            card = QFrame(page)
            card.setGeometry(x, y, 226, 116)
            self.apply_frame_style(card, self.card_style())
            small = QLabel(label, card)
            small.setGeometry(18, 18, 170, 22)
            small.setStyleSheet("font-size: 12px; color: #aeb7c2; font-weight: 700;")
            value = QLabel("0", card)
            value.setGeometry(18, 48, 170, 44)
            value.setStyleSheet("font-size: 34px; color: white; font-weight: 800;")
            self.summary_labels[key] = value

        workflow = QFrame(page)
        workflow.setGeometry(0, 305, 402, 470)
        self.apply_frame_style(workflow, self.card_style())
        workflow_title = QLabel("Workflow Guide", workflow)
        workflow_title.setGeometry(22, 20, 180, 24)
        workflow_title.setStyleSheet("font-size: 18px; color: white; font-weight: 800;")
        steps = [
            "1. Staff submits observation or source-document note.",
            "2. Admin reviews, edits, and approves resident data.",
            "3. Approved save prepares the display payload and audit entry.",
            "4. Verifier confirms software record against the e-paper screen.",
            "5. IT monitors gateway, Raspberry Pi, and device health separately.",
        ]
        for i, step in enumerate(steps):
            lbl = QLabel(step, workflow)
            lbl.setGeometry(24, 68 + i * 54, 340, 34)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("font-size: 13px; color: #dedede;")

        device_panel = QFrame(page)
        device_panel.setGeometry(425, 305, 793, 470)
        self.apply_frame_style(device_panel, self.card_style())
        device_title = QLabel("Device Status Snapshot", device_panel)
        device_title.setGeometry(22, 18, 260, 24)
        device_title.setStyleSheet("font-size: 18px; color: white; font-weight: 800;")
        self.overview_device_table = QTableWidget(device_panel)
        self.overview_device_table.setGeometry(18, 58, 756, 390)
        self.overview_device_table.setColumnCount(5)
        self.overview_device_table.setHorizontalHeaderLabels(["Device ID", "Status", "Battery", "Assigned Resident", "Last Seen"])
        self.overview_device_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.overview_device_table.verticalHeader().setVisible(False)
        self.overview_device_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.overview_device_table.setStyleSheet(self.table_style())

        return self.wrap_scroll_page(page, 820)

    def build_dashboard_page(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")

        self.residents_panel = QFrame(page)
        self.residents_panel.setGeometry(0, 0, 330, 1110)
        self.apply_frame_style(self.residents_panel, "background-color: #121212; border-radius: 22px; border: 1px solid #1f1f1f;")

        title = QLabel("Residents", self.residents_panel)
        title.setGeometry(20, 18, 120, 24)
        title.setStyleSheet("font-size: 18px; font-weight: 700;")

        self.search_resident = QLineEdit(self.residents_panel)
        self.search_resident.setGeometry(18, 55, 294, 40)
        self.search_resident.setPlaceholderText("Search name, UID, room...")
        self.search_resident.setStyleSheet(self.input_style())

        self.resident_list = QListWidget(self.residents_panel)
        self.resident_list.setGeometry(18, 110, 294, 982)
        self.resident_list.setStyleSheet("""
            QListWidget {
                background-color: transparent;
                color: #0f172a;
                border: none;
                outline: none;
                font-size: 14px;
            }
            QListWidget::item {
                background-color: #ffffff;
                border: 1px solid #d8e1ea;
                border-radius: 8px;
                padding: 12px;
                margin-bottom: 8px;
            }
            QListWidget::item:hover {
                background-color: #eef4f8;
                border: 1px solid #cbd5e1;
            }
            QListWidget::item:selected {
                background-color: #ccfbf1;
                color: #0f172a;
                border: 1px solid #0f766e;
            }
        """)

        self.form_panel = QFrame(page)
        self.form_panel.setGeometry(345, 0, 420, 1110)
        self.apply_frame_style(self.form_panel, "background-color: #121212; border-radius: 22px; border: 1px solid #1f1f1f;")

        self.form_heading = QLabel("Resident Information", self.form_panel)
        self.form_heading.setGeometry(22, 18, 180, 24)
        self.form_heading.setStyleSheet("font-size: 18px; font-weight: 700;")

        self.lbl_uid = QLabel("Resident UID", self.form_panel)
        self.lbl_uid.setGeometry(22, 58, 90, 18)
        self.lbl_uid.setStyleSheet(self.label_style())

        self.txt_uid = QLineEdit(self.form_panel)
        self.txt_uid.setGeometry(22, 80, 180, 42)
        self.txt_uid.setReadOnly(True)
        self.txt_uid.setStyleSheet(self.input_style())

        self.chk_active = QCheckBox("Resident enabled", self.form_panel)
        self.chk_active.setGeometry(230, 88, 140, 24)
        self.chk_active.setChecked(True)
        self.chk_active.setStyleSheet("""
            QCheckBox {
                color: #334155;
                font-size: 13px;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 15px;
                height: 15px;
                border-radius: 4px;
                border: 1px solid #94a3b8;
                background: #ffffff;
            }
            QCheckBox::indicator:checked {
                background-color: #0f766e;
                border: 1px solid #0f766e;
            }
        """)

        self.lbl_name = QLabel("Full Name", self.form_panel)
        self.lbl_name.setGeometry(22, 130, 90, 18)
        self.lbl_name.setStyleSheet(self.label_style())

        self.txt_name = QLineEdit(self.form_panel)
        self.txt_name.setGeometry(22, 152, 376, 42)
        self.txt_name.setStyleSheet(self.input_style())

        self.lbl_room = QLabel("Room", self.form_panel)
        self.lbl_room.setGeometry(22, 205, 50, 18)
        self.lbl_room.setStyleSheet(self.label_style())

        self.txt_room = QLineEdit(self.form_panel)
        self.txt_room.setGeometry(22, 227, 180, 42)
        self.txt_room.setStyleSheet(self.input_style())

        self.cmb_alert = QComboBox(self.form_panel)
        self.cmb_alert.setGeometry(-1000, -1000, 1, 1)
        self.cmb_alert.addItems(["Stable", "Needs Attention", "Fall Risk", "Emergency"])
        self.cmb_alert.setStyleSheet(self.input_style())
        self.cmb_alert.hide()

        self.lbl_diet = QLabel("Diet", self.form_panel)
        self.lbl_diet.setGeometry(22, 280, 120, 18)
        self.lbl_diet.setStyleSheet(self.label_style())

        self.txt_diet = self.editable_dropdown(
            self.form_panel,
            self.dropdown_options["diet"],
            "Select diet or type custom",
        )
        self.txt_diet.setProperty("option_key", "diet")
        self.txt_diet.setGeometry(22, 302, 266, 42)
        self.btn_add_diet_option, self.btn_delete_diet_option = self.create_dropdown_option_buttons(self.form_panel, self.txt_diet, 298, 302, 348)

        self.lbl_allergies = QLabel("Texture", self.form_panel)
        self.lbl_allergies.setGeometry(22, 355, 140, 18)
        self.lbl_allergies.setStyleSheet(self.label_style())

        self.txt_allergies = self.editable_dropdown(
            self.form_panel,
            self.dropdown_options["texture"],
            "Select texture or type custom",
        )
        self.txt_allergies.setProperty("option_key", "texture")
        self.txt_allergies.setGeometry(22, 377, 266, 42)
        self.btn_add_texture_option, self.btn_delete_texture_option = self.create_dropdown_option_buttons(self.form_panel, self.txt_allergies, 298, 377, 348)

        self.lbl_schedule = QLabel("Fluids", self.form_panel)
        self.lbl_schedule.setGeometry(22, 430, 80, 18)
        self.lbl_schedule.setStyleSheet(self.label_style())

        self.txt_schedule = self.editable_dropdown(
            self.form_panel,
            self.dropdown_options["fluids"],
            "Select fluids or type custom",
        )
        self.txt_schedule.setProperty("option_key", "fluids")
        self.txt_schedule.setGeometry(22, 452, 266, 42)
        self.btn_add_fluids_option, self.btn_delete_fluids_option = self.create_dropdown_option_buttons(self.form_panel, self.txt_schedule, 298, 452, 348)

        self.lbl_note = QLabel("Note", self.form_panel)
        self.lbl_note.setGeometry(22, 505, 60, 18)
        self.lbl_note.setStyleSheet(self.label_style())

        self.txt_note = QTextEdit(self.form_panel)
        self.txt_note.setGeometry(22, 527, 376, 58)
        self.txt_note.setStyleSheet(self.input_style())

        self.lbl_drinks = QLabel("Drinks", self.form_panel)
        self.lbl_drinks.setGeometry(22, 595, 60, 18)
        self.lbl_drinks.setStyleSheet(self.label_style())

        self.txt_drinks = QLineEdit(self.form_panel)
        self.txt_drinks.setGeometry(22, 617, 376, 42)
        self.txt_drinks.setStyleSheet(self.input_style())

        self.lbl_source = QLabel("Source document", self.form_panel)
        self.lbl_source.setGeometry(22, 670, 120, 18)
        self.lbl_source.setStyleSheet(self.label_style())

        self.btn_attach_source = QPushButton("Attach Document", self.form_panel)
        self.btn_attach_source.setGeometry(22, 692, 150, 36)
        self.btn_attach_source.setStyleSheet(self.secondary_btn_style())

        self.source_doc_label = QLabel("No source document attached", self.form_panel)
        self.source_doc_label.setGeometry(182, 692, 216, 36)
        self.source_doc_label.setWordWrap(True)
        self.source_doc_label.setStyleSheet("font-size: 11px; color: #a7a7a7;")

        self.lbl_resident_photo = QLabel("Resident photo for LCD", self.form_panel)
        self.lbl_resident_photo.setGeometry(22, 734, 180, 18)
        self.lbl_resident_photo.setStyleSheet(self.label_style())

        self.btn_attach_resident_photo = QPushButton("Attach Photo", self.form_panel)
        self.btn_attach_resident_photo.setGeometry(22, 756, 128, 36)
        self.btn_attach_resident_photo.setStyleSheet(self.secondary_btn_style())

        self.btn_clear_resident_photo = QPushButton("Clear Photo", self.form_panel)
        self.btn_clear_resident_photo.setGeometry(158, 756, 110, 36)
        self.btn_clear_resident_photo.setStyleSheet(self.secondary_btn_style())

        self.resident_photo_label = QLabel("No resident photo attached", self.form_panel)
        self.resident_photo_label.setGeometry(278, 756, 120, 36)
        self.resident_photo_label.setWordWrap(True)
        self.resident_photo_label.setStyleSheet("font-size: 11px; color: #a7a7a7;")

        self.chk_safety_review = QCheckBox("Needs safety review", self.form_panel)
        self.chk_safety_review.setGeometry(22, 812, 160, 24)
        self.chk_safety_review.setStyleSheet(self.chk_active.styleSheet())

        self.btn_new_resident = QPushButton("New Resident", self.form_panel)
        self.btn_new_resident.setGeometry(22, 848, 120, 42)
        self.btn_new_resident.setStyleSheet(self.secondary_btn_style())

        self.btn_save_resident = QPushButton("Save Resident", self.form_panel)
        self.btn_save_resident.setGeometry(152, 848, 120, 42)
        self.btn_save_resident.setStyleSheet(self.primary_btn_style())

        self.btn_clear_fields = QPushButton("Clear Form", self.form_panel)
        self.btn_clear_fields.setGeometry(282, 848, 116, 42)
        self.btn_clear_fields.setStyleSheet(self.secondary_btn_style())

        self.btn_delete_resident = QPushButton("Delete Resident", self.form_panel)
        self.btn_delete_resident.setGeometry(22, 898, 376, 38)
        self.btn_delete_resident.setStyleSheet("""
            QPushButton {
                background-color: #fff1f2;
                color: #b91c1c;
                border: 1px solid #fecdd3;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton:hover {
                background-color: #ffe4e6;
            }
        """)

        review_label = QLabel("Staff review note", self.form_panel)
        review_label.setGeometry(22, 950, 150, 18)
        review_label.setStyleSheet(self.label_style())

        self.nurse_review_comment = QTextEdit(self.form_panel)
        self.nurse_review_comment.setGeometry(22, 974, 376, 68)
        self.nurse_review_comment.setPlaceholderText("Write what needs review, the source checked, or the observation to verify.")
        self.nurse_review_comment.setStyleSheet(self.input_style())

        self.btn_submit_review_request = QPushButton("Submit for Admin Review", self.form_panel)
        self.btn_submit_review_request.setGeometry(22, 1054, 376, 38)
        self.btn_submit_review_request.setStyleSheet(self.primary_btn_style())

        self.preview_panel = QFrame(page)
        self.preview_panel.setGeometry(780, 0, 438, 805)
        self.apply_frame_style(self.preview_panel, "background-color: #121212; border-radius: 22px; border: 1px solid #1f1f1f;")

        self.preview_heading = QLabel("Live Preview", self.preview_panel)
        self.preview_heading.setGeometry(22, 18, 120, 24)
        self.preview_heading.setStyleSheet("font-size: 18px; font-weight: 700;")

        self.btn_go_pairing_after_save = QPushButton("Go to Pairing", self.preview_panel)
        self.btn_go_pairing_after_save.setGeometry(285, 16, 130, 34)
        self.btn_go_pairing_after_save.setStyleSheet(self.secondary_btn_style())

        self.epaper_card = QFrame(self.preview_panel)
        self.epaper_card.setGeometry(22, 60, 394, 210)
        self.apply_frame_style(self.epaper_card, "background-color: #efefef; border-radius: 18px;")

        ep_title = QLabel("E-Paper Preview", self.epaper_card)
        ep_title.setGeometry(16, 12, 120, 18)
        ep_title.setStyleSheet("color: #111111; font-size: 13px; font-weight: 700;")

        self.ep_name = QLabel("Resident Name", self.epaper_card)
        self.ep_name.setGeometry(16, 40, 240, 28)
        self.ep_name.setStyleSheet("color: #111111; font-size: 22px; font-weight: 700;")

        self.ep_room = QLabel("Room ---", self.epaper_card)
        self.ep_room.setGeometry(16, 72, 180, 22)
        self.ep_room.setStyleSheet("color: #111111; font-size: 14px;")

        self.ep_diet = QLabel("Diet: ---", self.epaper_card)
        self.ep_diet.setGeometry(16, 98, 300, 22)
        self.ep_diet.setStyleSheet("color: #111111; font-size: 14px;")

        self.ep_allergies = QLabel("Texture: ---", self.epaper_card)
        self.ep_allergies.setGeometry(16, 124, 350, 22)
        self.ep_allergies.setStyleSheet("color: #111111; font-size: 14px;")

        self.ep_fluids = QLabel("Fluids: ---", self.epaper_card)
        self.ep_fluids.setGeometry(16, 148, 350, 22)
        self.ep_fluids.setStyleSheet("color: #111111; font-size: 14px;")

        self.ep_note = QLabel("Note: ---", self.epaper_card)
        self.ep_note.setGeometry(16, 172, 350, 30)
        self.ep_note.setWordWrap(True)
        self.ep_note.setStyleSheet("color: #111111; font-size: 13px;")

        self.lcd_card = QFrame(self.preview_panel)
        self.lcd_card.setGeometry(22, 288, 394, 210)
        self.apply_frame_style(self.lcd_card, "background-color: #0a1831; border-radius: 18px; border: 2px solid #20457b;")

        lcd_title = QLabel("LCD Preview", self.lcd_card)
        lcd_title.setGeometry(16, 12, 100, 18)
        lcd_title.setStyleSheet("color: white; font-size: 13px; font-weight: 700;")

        self.lcd_image = QLabel(self.lcd_card)
        self.lcd_image.setGeometry(20, 40, 354, 120)
        self.lcd_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lcd_image.setStyleSheet("""
            QLabel {
                background-color: #bae6fd;
                border-radius: 8px;
            }
        """)
        self.lcd_image.hide()

        self.lcd_empty_state = QLabel("Add a resident image to preview the LCD display.", self.lcd_card)
        self.lcd_empty_state.setGeometry(28, 54, 338, 112)
        self.lcd_empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lcd_empty_state.setWordWrap(True)
        self.lcd_empty_state.setStyleSheet("""
            QLabel {
                background-color: #f8fafc;
                color: #0f766e;
                border: 1px dashed #7dd3fc;
                border-radius: 8px;
                padding: 16px;
                font-size: 13px;
                font-weight: 700;
            }
        """)

        self.lcd_name = QLabel("Resident Name", self.lcd_card)
        self.lcd_name.setGeometry(20, 42, 354, 28)
        self.lcd_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lcd_name.setStyleSheet("color: white; font-size: 22px; font-weight: 700;")

        self.lcd_room = QLabel("Room ---", self.lcd_card)
        self.lcd_room.setGeometry(20, 72, 354, 22)
        self.lcd_room.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lcd_room.setStyleSheet("color: #d7e3f1; font-size: 14px;")

        self.lcd_alert_banner = QLabel("STABLE", self.lcd_card)
        self.lcd_alert_banner.setGeometry(92, 104, 210, 36)
        self.lcd_alert_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lcd_alert_banner.setStyleSheet("""
            QLabel {
                background-color: #146c2e;
                color: white;
                border-radius: 8px;
                font-size: 16px;
                font-weight: 700;
            }
        """)

        self.lcd_note = QLabel("No note", self.lcd_card)
        self.lcd_note.setGeometry(20, 148, 354, 42)
        self.lcd_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lcd_note.setWordWrap(True)
        self.lcd_note.setStyleSheet("color: #eef2f7; font-size: 13px;")

        self.overview_panel = QFrame(self.preview_panel)
        self.overview_panel.setGeometry(22, 520, 394, 260)
        self.apply_frame_style(self.overview_panel, "background-color: #1a1a1a; border-radius: 16px; border: 1px solid #262626;")

        overview_title = QLabel("Overall Dashboard", self.overview_panel)
        overview_title.setGeometry(16, 14, 180, 22)
        overview_title.setStyleSheet("font-size: 16px; font-weight: 700; color: white;")

        self.record_summary_labels = {}
        summary_items = [
            ("active_residents", "Active residents", 52),
            ("online_devices", "Online devices", 90),
            ("pending_requests", "Pending approvals", 128),
            ("verification_mismatches", "Display mismatches", 166),
            ("failed_updates", "Failed updates", 204),
        ]
        for key, title_text, y in summary_items:
            label = QLabel(f"{title_text}: 0", self.overview_panel)
            label.setGeometry(18, y, 250, 24)
            label.setStyleSheet("font-size: 13px; color: #dedede;")
            self.record_summary_labels[key] = label

        self.record_summary_labels["database_mode"] = QLabel("Data store: checking", self.overview_panel)
        self.record_summary_labels["database_mode"].setGeometry(210, 14, 160, 22)
        self.record_summary_labels["database_mode"].setAlignment(Qt.AlignmentFlag.AlignRight)
        self.record_summary_labels["database_mode"].setStyleSheet("font-size: 12px; color: #2dd4bf;")

        return self.wrap_scroll_page(page, 1160)

    # ---------------------------- approvals page ----------------------------

    def build_approvals_page(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")

        left = QFrame(page)
        left.setGeometry(0, 0, 420, 805)
        self.apply_frame_style(left, self.card_style())

        title = QLabel("Resident Review Queue", left)
        title.setGeometry(22, 18, 260, 24)
        title.setStyleSheet("font-size: 18px; font-weight: 800; color: white;")

        self.approval_table = QTableWidget(left)
        self.approval_table.setGeometry(18, 58, 384, 640)
        self.approval_table.setColumnCount(5)
        self.approval_table.setHorizontalHeaderLabels(["Status", "Resident", "Room", "By", "Date"])
        self.approval_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.approval_table.verticalHeader().setVisible(False)
        self.approval_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.approval_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.approval_table.setStyleSheet(self.table_style())

        self.btn_refresh_approvals = QPushButton("Refresh Queue", left)
        self.btn_refresh_approvals.setGeometry(18, 718, 180, 42)
        self.btn_refresh_approvals.setStyleSheet(self.secondary_btn_style())

        self.approval_count_label = QLabel("Pending: 0", left)
        self.approval_count_label.setGeometry(210, 728, 160, 22)
        self.approval_count_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.approval_count_label.setStyleSheet("font-size: 13px; color: #2dd4bf; font-weight: 700;")

        right = QFrame(page)
        right.setGeometry(440, 0, 778, 805)
        self.apply_frame_style(right, self.card_style())

        detail_title = QLabel("Review Detail", right)
        detail_title.setGeometry(22, 18, 180, 24)
        detail_title.setStyleSheet("font-size: 18px; font-weight: 800; color: white;")

        self.approval_detail = QTextEdit(right)
        self.approval_detail.setGeometry(22, 58, 734, 420)
        self.approval_detail.setReadOnly(True)
        self.approval_detail.setStyleSheet(self.input_style())

        note_label = QLabel("Admin decision note", right)
        note_label.setGeometry(22, 498, 220, 22)
        note_label.setStyleSheet(self.label_style())

        self.approval_review_note = QTextEdit(right)
        self.approval_review_note.setGeometry(22, 526, 734, 110)
        self.approval_review_note.setPlaceholderText("Document verification outcome, correction made, or reason for rejection.")
        self.approval_review_note.setStyleSheet(self.input_style())

        self.btn_approve_request = QPushButton("Approve and Record", right)
        self.btn_approve_request.setGeometry(22, 662, 210, 44)
        self.btn_approve_request.setStyleSheet(self.primary_btn_style())

        self.btn_reject_request = QPushButton("Reject Request", right)
        self.btn_reject_request.setGeometry(246, 662, 170, 44)
        self.btn_reject_request.setStyleSheet(self.secondary_btn_style())

        self.approval_guidance = QLabel(
            "Approve only after checking the source document or resident chart. Approved changes are recorded for audit review.",
            right
        )
        self.approval_guidance.setGeometry(22, 724, 700, 40)
        self.approval_guidance.setWordWrap(True)
        self.approval_guidance.setStyleSheet("font-size: 13px; color: #b8c1cc;")

        return self.wrap_scroll_page(page, 860)

    # ---------------------------- resident audit page ----------------------------

    def build_resident_audit_page(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")

        left = QFrame(page)
        left.setGeometry(0, 0, 700, 805)
        self.apply_frame_style(left, self.card_style())

        title = QLabel("Resident Change Audit", left)
        title.setGeometry(22, 18, 260, 24)
        title.setStyleSheet("font-size: 18px; font-weight: 800; color: white;")

        hint = QLabel("Previous information, current information, source document, and reviewer notes.")
        hint.setGeometry(22, 50, 620, 22)
        hint.setStyleSheet("font-size: 12px; color: #9fb2c3;")

        self.resident_audit_table = QTableWidget(left)
        self.resident_audit_table.setGeometry(18, 88, 664, 690)
        self.resident_audit_table.setColumnCount(6)
        self.resident_audit_table.setHorizontalHeaderLabels(["Date/Time", "Action", "Resident UID", "Changed By", "Document", "Summary"])
        self.resident_audit_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.resident_audit_table.verticalHeader().setVisible(False)
        self.resident_audit_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.resident_audit_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.resident_audit_table.setStyleSheet(self.table_style())

        right = QFrame(page)
        right.setGeometry(730, 0, 488, 805)
        self.apply_frame_style(right, self.card_style())

        detail_title = QLabel("Before / After Review", right)
        detail_title.setGeometry(22, 18, 260, 24)
        detail_title.setStyleSheet("font-size: 18px; font-weight: 800; color: white;")

        self.resident_audit_detail = QTextEdit(right)
        self.resident_audit_detail.setGeometry(22, 58, 444, 610)
        self.resident_audit_detail.setReadOnly(True)
        self.resident_audit_detail.setStyleSheet(self.input_style())

        self.btn_open_audit_document = QPushButton("Open Source Document", right)
        self.btn_open_audit_document.setGeometry(22, 690, 205, 44)
        self.btn_open_audit_document.setStyleSheet(self.primary_btn_style())

        self.btn_refresh_audit = QPushButton("Refresh Audit", right)
        self.btn_refresh_audit.setGeometry(242, 690, 160, 44)
        self.btn_refresh_audit.setStyleSheet(self.secondary_btn_style())

        note = QLabel(
            "Admin and staff see resident-information history only. Technical send/device logs remain in IT Admin.",
            right,
        )
        note.setGeometry(22, 748, 430, 36)
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 12px; color: #9fb2c3;")

        return self.wrap_scroll_page(page, 860)

    # ---------------------------- verification page ----------------------------

    def build_verification_page(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")

        left = QFrame(page)
        left.setGeometry(0, 0, 330, 805)
        self.apply_frame_style(left, self.card_style())

        title = QLabel("Display Verification", left)
        title.setGeometry(20, 18, 220, 24)
        title.setStyleSheet("font-size: 18px; font-weight: 800; color: white;")

        self.verify_resident_list = QListWidget(left)
        self.verify_resident_list.setGeometry(18, 58, 294, 660)
        self.verify_resident_list.setStyleSheet(self.resident_list.styleSheet() if hasattr(self, "resident_list") else "")

        self.btn_refresh_verification = QPushButton("Refresh Residents", left)
        self.btn_refresh_verification.setGeometry(18, 738, 294, 42)
        self.btn_refresh_verification.setStyleSheet(self.secondary_btn_style())

        center = QFrame(page)
        center.setGeometry(350, 0, 430, 805)
        self.apply_frame_style(center, self.card_style())

        detail_title = QLabel("Software Record", center)
        detail_title.setGeometry(22, 18, 180, 24)
        detail_title.setStyleSheet("font-size: 18px; font-weight: 800; color: white;")

        self.verify_detail = QTextEdit(center)
        self.verify_detail.setGeometry(22, 58, 386, 520)
        self.verify_detail.setReadOnly(True)
        self.verify_detail.setStyleSheet(self.input_style())

        self.verify_note = QTextEdit(center)
        self.verify_note.setGeometry(22, 606, 386, 84)
        self.verify_note.setPlaceholderText("Optional note for mismatch, unreadable display, or device issue.")
        self.verify_note.setStyleSheet(self.input_style())

        self.btn_mark_verified = QPushButton("Display Matches", center)
        self.btn_mark_verified.setGeometry(22, 714, 180, 44)
        self.btn_mark_verified.setStyleSheet(self.primary_btn_style())

        self.btn_mark_mismatch = QPushButton("Report Mismatch", center)
        self.btn_mark_mismatch.setGeometry(214, 714, 180, 44)
        self.btn_mark_mismatch.setStyleSheet(self.secondary_btn_style())

        right = QFrame(page)
        right.setGeometry(800, 0, 418, 805)
        self.apply_frame_style(right, self.card_style())

        history_title = QLabel("Verification History", right)
        history_title.setGeometry(22, 18, 220, 24)
        history_title.setStyleSheet("font-size: 18px; font-weight: 800; color: white;")

        self.verification_table = QTableWidget(right)
        self.verification_table.setGeometry(18, 58, 382, 720)
        self.verification_table.setColumnCount(4)
        self.verification_table.setHorizontalHeaderLabels(["Status", "Resident", "Device", "Checked By"])
        self.verification_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.verification_table.verticalHeader().setVisible(False)
        self.verification_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.verification_table.setStyleSheet(self.table_style())

        return self.wrap_scroll_page(page, 860)

    # ---------------------------- IT control center ----------------------------

    def build_it_health_page(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")

        header = QFrame(page)
        header.setGeometry(0, 0, 1218, 118)
        self.apply_frame_style(header, self.card_style())

        title = QLabel("IT Control Center", header)
        title.setGeometry(24, 18, 360, 32)
        title.setStyleSheet("font-size: 24px; font-weight: 800; color: #0f172a;")

        subtitle = QLabel("Use the IT Control Center menu on the left to open dashboard, services, devices, logs, and diagnostics.", header)
        subtitle.setGeometry(24, 56, 760, 22)
        subtitle.setStyleSheet("font-size: 13px; color: #475569;")

        self.control_header_status = QLabel("Demo Mode - No Raspberry Pi Connected", header)
        self.control_header_status.setGeometry(820, 30, 360, 34)
        self.control_header_status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.control_header_status.setStyleSheet("font-size: 13px; color: #64748b; font-weight: 700;")

        section_label = QLabel("Control Section", header)
        section_label.setGeometry(24, 84, 130, 20)
        section_label.setStyleSheet(self.label_style())
        section_label.setVisible(False)

        self.it_control_section_combo = QComboBox(header)
        self.it_control_section_combo.setGeometry(160, 78, 300, 34)
        self.it_control_section_combo.setVisible(False)
        self.it_control_section_combo.setStyleSheet("""
            QComboBox {
                background-color: #ffffff;
                color: #0f172a;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 6px 12px;
                font-size: 13px;
                font-weight: 700;
            }
            QComboBox:focus {
                border: 1px solid #0f766e;
            }
            QComboBox::drop-down {
                border: none;
                width: 34px;
            }
        """)

        self.it_control_stack = QStackedWidget(page)
        self.it_control_stack.setGeometry(0, 145, 1218, 895)
        self.it_control_stack.setStyleSheet("background: transparent;")

        sections = [
            ("Dashboard", "dashboard", self.build_control_dashboard_section()),
            ("Services", "services", self.build_control_services_section()),
            ("Devices", "devices", self.build_control_devices_section()),
            ("OTA", "ota", self.build_control_ota_section()),
            ("Backups", "backups", self.build_control_backups_section()),
            ("Logs", "logs", self.build_control_logs_section()),
            ("AI Debug", "ai", self.build_control_ai_section()),
        ]
        for label, icon_key, section in sections:
            self.it_control_stack.addWidget(section)
            self.it_control_section_combo.addItem(self.control_section_icon(icon_key), label)
        self.it_control_section_combo.currentIndexChanged.connect(self.set_it_control_section)

        self.it_control_stack.setCurrentIndex(0)
        self.load_control_dashboard_placeholder()
        return self.wrap_scroll_page(page, 1080)

    def control_section_icon(self, key):
        icon_map = {
            "dashboard": QStyle.StandardPixmap.SP_ComputerIcon,
            "network": QStyle.StandardPixmap.SP_DriveNetIcon,
            "services": QStyle.StandardPixmap.SP_BrowserReload,
            "devices": QStyle.StandardPixmap.SP_FileDialogDetailedView,
            "ota": QStyle.StandardPixmap.SP_ArrowUp,
            "backups": QStyle.StandardPixmap.SP_DriveHDIcon,
            "logs": QStyle.StandardPixmap.SP_FileDialogContentsView,
            "ai": QStyle.StandardPixmap.SP_MessageBoxInformation,
        }
        return self.style().standardIcon(icon_map.get(key, QStyle.StandardPixmap.SP_FileIcon))

    def set_it_control_section(self, index):
        if not hasattr(self, "it_control_stack"):
            return
        if index < 0:
            return
        self.it_control_stack.setCurrentIndex(index)
        if hasattr(self, "it_control_section_combo") and self.it_control_section_combo.currentIndex() != index:
            self.it_control_section_combo.blockSignals(True)
            self.it_control_section_combo.setCurrentIndex(index)
            self.it_control_section_combo.blockSignals(False)
        if index == 0:
            self.refresh_control_dashboard()
        elif index == 1:
            self.load_control_services()
        elif index == 2:
            self.load_battery_alert_settings(timeout=3.0)
            self.load_control_devices()
        elif index == 5:
            self.load_it_audit_logs()

    def build_control_dashboard_section(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        title = QLabel("Raspberry Pi Dashboard", page)
        title.setGeometry(0, 0, 360, 28)
        title.setStyleSheet("font-size: 20px; font-weight: 800; color: #0f172a;")

        self.control_dashboard_labels = {}
        cards = [
            ("service", "Control Service Status", 0, 48),
            ("hostname", "Hostname", 245, 48),
            ("lan_ip", "LAN IP", 490, 48),
            ("tailscale_ip", "Tailscale IP", 735, 48),
            ("cpu", "CPU Usage", 0, 178),
            ("memory", "Memory Usage", 245, 178),
            ("disk", "Disk Usage", 490, 178),
            ("operation", "Operation Manager", 735, 178),
            ("refreshed", "Last Refresh", 0, 308),
        ]
        for key, label, x, y in cards:
            card = QFrame(page)
            card.setGeometry(x, y, 220, 102)
            self.apply_frame_style(card, self.card_style())
            small = QLabel(label, card)
            small.setGeometry(16, 14, 185, 18)
            small.setStyleSheet("font-size: 12px; color: #64748b; font-weight: 800;")
            value = QLabel("Pending backend support", card)
            value.setGeometry(16, 42, 188, 42)
            value.setWordWrap(True)
            value.setStyleSheet("font-size: 15px; color: #0f172a; font-weight: 800;")
            self.control_dashboard_labels[key] = value

        note = QLabel("Desktop software communicates only with the Raspberry Pi Control Service. Operation Manager and ESP32 modules remain behind the Pi.", page)
        note.setGeometry(245, 318, 700, 44)
        note.setWordWrap(True)
        note.setStyleSheet("font-size: 13px; color: #475569;")
        return page

    def build_control_network_section(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        title = QLabel("Raspberry Pi Connection", page)
        title.setGeometry(0, 0, 360, 28)
        title.setStyleSheet("font-size: 20px; font-weight: 800; color: #0f172a;")

        profile_panel = QFrame(page)
        profile_panel.setGeometry(0, 48, 978, 330)
        self.apply_frame_style(profile_panel, self.card_style())

        profile_label = QLabel("Connection Profile", profile_panel)
        profile_label.setGeometry(22, 20, 180, 20)
        profile_label.setStyleSheet(self.label_style())
        self.control_profile_combo = QComboBox(profile_panel)
        self.control_profile_combo.setGeometry(22, 48, 300, 42)
        self.control_profile_combo.setStyleSheet(self.input_style())
        self.control_profile_combo.currentIndexChanged.connect(lambda _index: self.on_control_profile_selected())

        self.btn_new_control_profile = QPushButton("New Profile", profile_panel)
        self.btn_new_control_profile.setGeometry(342, 48, 125, 42)
        self.btn_new_control_profile.setStyleSheet(self.secondary_btn_style())
        self.btn_new_control_profile.clicked.connect(self.new_control_profile)

        name_label = QLabel("Profile Name", profile_panel)
        name_label.setGeometry(22, 110, 140, 20)
        name_label.setStyleSheet(self.label_style())
        self.control_profile_name = QLineEdit(profile_panel)
        self.control_profile_name.setGeometry(22, 138, 210, 42)
        self.control_profile_name.setStyleSheet(self.input_style())

        host_label = QLabel("Control Service Host", profile_panel)
        host_label.setGeometry(250, 110, 180, 20)
        host_label.setStyleSheet(self.label_style())
        self.control_host = QLineEdit(profile_panel)
        self.control_host.setGeometry(250, 138, 240, 42)
        self.control_host.setPlaceholderText("whisperwood-pi.local or LAN/Tailscale IP")
        self.control_host.setStyleSheet(self.input_style())
        self.control_host.textChanged.connect(lambda _text: self.update_control_url_preview())

        port_label = QLabel("Port", profile_panel)
        port_label.setGeometry(510, 110, 80, 20)
        port_label.setStyleSheet(self.label_style())
        self.control_port = QLineEdit(profile_panel)
        self.control_port.setGeometry(510, 138, 100, 42)
        self.control_port.setText("7000")
        self.control_port.setStyleSheet(self.input_style())
        self.control_port.textChanged.connect(lambda _text: self.update_control_url_preview())

        key_label = QLabel("Control Service API Key", profile_panel)
        key_label.setGeometry(630, 110, 190, 20)
        key_label.setStyleSheet(self.label_style())
        self.control_api_key = QLineEdit(profile_panel)
        self.control_api_key.setGeometry(630, 138, 250, 42)
        self.control_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.control_api_key.setStyleSheet(self.input_style())
        self.control_api_key.textChanged.connect(lambda _text: self.update_control_url_preview())

        self.btn_copy_control_api_key = QPushButton("Copy API Key", profile_panel)
        self.btn_copy_control_api_key.setGeometry(630, 190, 150, 38)
        self.btn_copy_control_api_key.setStyleSheet(self.secondary_btn_style())
        self.btn_copy_control_api_key.clicked.connect(self.copy_control_api_key)

        self.control_masked_key = QLabel("API key: Not configured", profile_panel)
        self.control_masked_key.setGeometry(790, 198, 170, 20)
        self.control_masked_key.setStyleSheet("font-size: 12px; color: #64748b;")

        desc_label = QLabel("Description", profile_panel)
        desc_label.setGeometry(22, 195, 120, 20)
        desc_label.setStyleSheet(self.label_style())
        self.control_profile_description = QLineEdit(profile_panel)
        self.control_profile_description.setGeometry(22, 222, 468, 42)
        self.control_profile_description.setStyleSheet(self.input_style())

        self.btn_save_control_profile = QPushButton("Save Profile", profile_panel)
        self.btn_save_control_profile.setGeometry(510, 222, 150, 42)
        self.btn_save_control_profile.setStyleSheet(self.primary_btn_style())
        self.btn_save_control_profile.clicked.connect(self.save_control_profile)

        self.btn_test_control_connection = QPushButton("Test Connection", profile_panel)
        self.btn_test_control_connection.setGeometry(678, 222, 170, 42)
        self.btn_test_control_connection.setStyleSheet(self.secondary_btn_style())
        self.btn_test_control_connection.clicked.connect(self.test_control_connection)

        self.control_url_preview = QLabel("Control Service URL: Pending profile configuration", profile_panel)
        self.control_url_preview.setGeometry(22, 282, 460, 22)
        self.control_url_preview.setStyleSheet("font-size: 12px; color: #475569;")

        self.control_test_status = QLabel("Connection Status: Not tested", profile_panel)
        self.control_test_status.setGeometry(510, 282, 390, 22)
        self.control_test_status.setStyleSheet("font-size: 12px; color: #64748b; font-weight: 700;")

        status_panel = QFrame(page)
        status_panel.setGeometry(0, 405, 978, 260)
        self.apply_frame_style(status_panel, self.card_style())
        self.control_network_labels = {}
        rows = [
            ("profile", "Connection Profile", 22, 22),
            ("host", "Configured Host", 22, 70),
            ("port", "Configured Port", 22, 118),
            ("url", "Control Service URL", 22, 166),
            ("hostname", "Hostname", 500, 22),
            ("lan_ip", "LAN IP", 500, 70),
            ("tailscale_ip", "Tailscale IP", 500, 118),
            ("status", "Connection Status", 500, 166),
        ]
        for key, label, x, y in rows:
            small = QLabel(label, status_panel)
            small.setGeometry(x, y, 170, 18)
            small.setStyleSheet(self.label_style())
            value = QLabel("Pending backend support", status_panel)
            value.setGeometry(x, y + 22, 420, 22)
            value.setStyleSheet("font-size: 13px; color: #0f172a; font-weight: 700;")
            self.control_network_labels[key] = value
        return page

    def build_control_services_section(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        title = QLabel("Services", page)
        title.setGeometry(0, 0, 260, 28)
        title.setStyleSheet("font-size: 20px; font-weight: 800; color: #0f172a;")

        panel = QFrame(page)
        panel.setGeometry(0, 48, 978, 260)
        self.apply_frame_style(panel, self.card_style())
        self.control_service_labels = {}
        for key, label, y in [
            ("control", "Control Service Status", 22),
            ("operation", "Operation Manager Status", 72),
            ("version", "Version", 122),
            ("uptime", "Uptime", 172),
            ("last_restart", "Last Restart", 222),
        ]:
            small = QLabel(label, panel)
            small.setGeometry(22, y, 220, 18)
            small.setStyleSheet(self.label_style())
            value = QLabel("Pending backend support", panel)
            value.setGeometry(250, y - 4, 360, 26)
            value.setStyleSheet("font-size: 14px; color: #0f172a; font-weight: 800;")
            self.control_service_labels[key] = value

        self.btn_restart_operation = QPushButton("Restart Operation Manager", panel)
        self.btn_restart_operation.setGeometry(665, 28, 230, 44)
        self.btn_restart_operation.setStyleSheet(self.primary_btn_style())
        self.btn_restart_operation.clicked.connect(self.restart_operation_manager)

        self.btn_update_operation = QPushButton("Update Operation Manager", panel)
        self.btn_update_operation.setGeometry(665, 92, 230, 44)
        self.btn_update_operation.setStyleSheet(self.secondary_btn_style())
        self.btn_update_operation.setEnabled(False)
        self.btn_update_operation.setToolTip("Available after backend implementation.")

        self.btn_rollback_operation = QPushButton("Rollback Operation Manager", panel)
        self.btn_rollback_operation.setGeometry(665, 156, 230, 44)
        self.btn_rollback_operation.setStyleSheet(self.secondary_btn_style())
        self.btn_rollback_operation.setEnabled(False)
        self.btn_rollback_operation.setToolTip("Available after backend implementation.")

        recovery = QFrame(page)
        recovery.setGeometry(0, 335, 978, 255)
        self.apply_frame_style(recovery, self.card_style())
        recovery_title = QLabel("IT Account Recovery", recovery)
        recovery_title.setGeometry(22, 18, 260, 24)
        recovery_title.setStyleSheet("font-size: 18px; font-weight: 800; color: #0f172a;")

        recovery_hint = QLabel("Issue temporary passwords only for active users. The user is forced to change it after login.", recovery)
        recovery_hint.setGeometry(22, 50, 720, 22)
        recovery_hint.setStyleSheet("font-size: 12px; color: #64748b;")

        self.it_recovery_user = QComboBox(recovery)
        self.it_recovery_user.setGeometry(22, 92, 330, 44)
        self.it_recovery_user.setStyleSheet(self.input_style())

        self.it_temp_password = QLineEdit(recovery)
        self.it_temp_password.setGeometry(370, 92, 260, 44)
        self.it_temp_password.setPlaceholderText("Generated temporary password")
        self.it_temp_password.setReadOnly(True)
        self.it_temp_password.setStyleSheet(self.input_style())

        self.btn_issue_temp_password = QPushButton("Generate Temporary Password", recovery)
        self.btn_issue_temp_password.setGeometry(650, 92, 230, 44)
        self.btn_issue_temp_password.setStyleSheet(self.primary_btn_style())

        self.btn_copy_temp_password = QPushButton("Copy Password", recovery)
        self.btn_copy_temp_password.setGeometry(650, 150, 150, 40)
        self.btn_copy_temp_password.setStyleSheet(self.secondary_btn_style())

        self.it_recovery_status = QLabel("", recovery)
        self.it_recovery_status.setGeometry(22, 150, 600, 24)
        self.it_recovery_status.setStyleSheet("font-size: 12px; color: #475569;")

        self.it_2fa_note = QLabel(
            "Future production recovery: IT admin account recovery should require the configured two-step authenticator before a temporary password can be issued.",
            recovery,
        )
        self.it_2fa_note.setGeometry(22, 196, 900, 36)
        self.it_2fa_note.setWordWrap(True)
        self.it_2fa_note.setStyleSheet("font-size: 12px; color: #64748b;")
        return page

    def build_control_devices_section(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        title = QLabel("Devices", page)
        title.setGeometry(0, 0, 260, 28)
        title.setStyleSheet("font-size: 20px; font-weight: 800; color: #0f172a;")

        self.control_device_summary = {}
        for key, label, x in [
            ("total", "Total Devices", 0),
            ("online", "Online Devices", 195),
            ("offline", "Offline Devices", 390),
            ("low_battery", "Low Battery", 585),
            ("firmware", "Firmware Status", 780),
        ]:
            card = QFrame(page)
            card.setGeometry(x, 48, 175, 94)
            self.apply_frame_style(card, self.card_style())
            small = QLabel(label, card)
            small.setGeometry(14, 12, 145, 18)
            small.setStyleSheet("font-size: 12px; color: #64748b; font-weight: 800;")
            value = QLabel("0", card)
            value.setGeometry(14, 38, 145, 34)
            value.setStyleSheet("font-size: 24px; color: #0f172a; font-weight: 800;")
            self.control_device_summary[key] = value

        message = QLabel("ESP32 registry is refreshed from the Operation Manager in real time.", page)
        message.setGeometry(0, 166, 600, 26)
        message.setStyleSheet("font-size: 13px; color: #475569; font-weight: 700;")

        self.it_device_table = QTableWidget(page)
        self.it_device_table.setGeometry(0, 205, 955, 250)
        self.it_device_table.setColumnCount(8)
        self.it_device_table.setHorizontalHeaderLabels(["Device", "Online", "IP", "Port", "FW", "Battery", "Power", "Last Seen"])
        self.it_device_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.it_device_table.verticalHeader().setVisible(False)
        self.it_device_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.it_device_table.setStyleSheet(self.table_style())

        battery_panel = QFrame(page)
        battery_panel.setGeometry(0, 475, 955, 138)
        self.apply_frame_style(battery_panel, self.card_style())

        battery_title = QLabel("Battery Alert Policy", battery_panel)
        battery_title.setGeometry(22, 16, 230, 24)
        battery_title.setStyleSheet("font-size: 18px; font-weight: 800; color: #0f172a;")

        self.chk_battery_alert_enabled = QCheckBox("Enable popups", battery_panel)
        self.chk_battery_alert_enabled.setGeometry(22, 52, 145, 26)
        self.chk_battery_alert_enabled.setStyleSheet(self.checkbox_style())

        low_label = QLabel("Low at", battery_panel)
        low_label.setGeometry(178, 48, 75, 18)
        low_label.setStyleSheet(self.label_style())
        self.spin_battery_low = QSpinBox(battery_panel)
        self.spin_battery_low.setGeometry(178, 70, 92, 38)
        self.spin_battery_low.setRange(1, 100)
        self.spin_battery_low.setSuffix("%")
        self.spin_battery_low.setStyleSheet(self.input_style())

        critical_label = QLabel("Critical at", battery_panel)
        critical_label.setGeometry(288, 48, 95, 18)
        critical_label.setStyleSheet(self.label_style())
        self.spin_battery_critical = QSpinBox(battery_panel)
        self.spin_battery_critical.setGeometry(288, 70, 92, 38)
        self.spin_battery_critical.setRange(1, 100)
        self.spin_battery_critical.setSuffix("%")
        self.spin_battery_critical.setStyleSheet(self.input_style())

        cooldown_label = QLabel("Popup cooldown", battery_panel)
        cooldown_label.setGeometry(398, 48, 130, 18)
        cooldown_label.setStyleSheet(self.label_style())
        self.spin_battery_cooldown = QSpinBox(battery_panel)
        self.spin_battery_cooldown.setGeometry(398, 70, 122, 38)
        self.spin_battery_cooldown.setRange(1, 1440)
        self.spin_battery_cooldown.setSuffix(" min")
        self.spin_battery_cooldown.setStyleSheet(self.input_style())

        role_label = QLabel("Notify", battery_panel)
        role_label.setGeometry(540, 48, 70, 18)
        role_label.setStyleSheet(self.label_style())
        self.chk_battery_role_it = QCheckBox("IT Admin", battery_panel)
        self.chk_battery_role_it.setGeometry(540, 70, 95, 26)
        self.chk_battery_role_admin = QCheckBox("Admin", battery_panel)
        self.chk_battery_role_admin.setGeometry(642, 70, 82, 26)
        self.chk_battery_role_staff = QCheckBox("Staff", battery_panel)
        self.chk_battery_role_staff.setGeometry(724, 70, 75, 26)
        self.chk_battery_role_verifier = QCheckBox("Verifier", battery_panel)
        self.chk_battery_role_verifier.setGeometry(800, 70, 86, 26)
        for checkbox in [
            self.chk_battery_role_it,
            self.chk_battery_role_admin,
            self.chk_battery_role_staff,
            self.chk_battery_role_verifier,
        ]:
            checkbox.setStyleSheet(self.checkbox_style())

        self.btn_save_battery_alerts = QPushButton("Save Policy", battery_panel)
        self.btn_save_battery_alerts.setGeometry(808, 18, 124, 38)
        self.btn_save_battery_alerts.setStyleSheet(self.primary_btn_style())
        self.btn_save_battery_alerts.clicked.connect(self.save_battery_alert_settings_from_ui)

        self.battery_alert_status = QLabel("Battery popups use the latest ESP32 status sent through the Raspberry Pi.", battery_panel)
        self.battery_alert_status.setGeometry(22, 108, 900, 20)
        self.battery_alert_status.setStyleSheet("font-size: 12px; color: #64748b; font-weight: 700;")

        wifi_panel = QFrame(page)
        wifi_panel.setGeometry(0, 635, 955, 250)
        self.apply_frame_style(wifi_panel, self.card_style())

        wifi_title = QLabel("ESP32 WiFi Provisioning", wifi_panel)
        wifi_title.setGeometry(22, 18, 260, 24)
        wifi_title.setStyleSheet("font-size: 18px; font-weight: 800; color: #0f172a;")

        wifi_hint = QLabel("Plug the ESP32 into this laptop by USB, choose the COM port, scan/select WiFi, then save credentials to the device.", wifi_panel)
        wifi_hint.setGeometry(22, 48, 850, 22)
        wifi_hint.setStyleSheet("font-size: 12px; color: #64748b;")

        port_label = QLabel("USB / COM Port", wifi_panel)
        port_label.setGeometry(22, 84, 140, 18)
        port_label.setStyleSheet(self.label_style())
        self.esp32_serial_port = QComboBox(wifi_panel)
        self.esp32_serial_port.setGeometry(22, 108, 250, 40)
        self.esp32_serial_port.setStyleSheet(self.input_style())

        self.btn_refresh_esp32_ports = QPushButton("Refresh Ports", wifi_panel)
        self.btn_refresh_esp32_ports.setGeometry(288, 108, 132, 40)
        self.btn_refresh_esp32_ports.setStyleSheet(self.secondary_btn_style())
        self.btn_refresh_esp32_ports.clicked.connect(self.refresh_esp32_serial_ports)

        self.btn_scan_esp32_wifi = QPushButton("Scan WiFi", wifi_panel)
        self.btn_scan_esp32_wifi.setGeometry(435, 108, 112, 40)
        self.btn_scan_esp32_wifi.setStyleSheet(self.secondary_btn_style())
        self.btn_scan_esp32_wifi.clicked.connect(self.scan_esp32_wifi_networks)

        ssid_label = QLabel("WiFi Network", wifi_panel)
        ssid_label.setGeometry(22, 160, 140, 18)
        ssid_label.setStyleSheet(self.label_style())
        self.esp32_wifi_ssid = QComboBox(wifi_panel)
        self.esp32_wifi_ssid.setEditable(True)
        self.esp32_wifi_ssid.setGeometry(22, 184, 250, 40)
        self.esp32_wifi_ssid.setStyleSheet(self.input_style())

        pass_label = QLabel("WiFi Password", wifi_panel)
        pass_label.setGeometry(288, 160, 140, 18)
        pass_label.setStyleSheet(self.label_style())
        self.esp32_wifi_password = QLineEdit(wifi_panel)
        self.esp32_wifi_password.setGeometry(288, 184, 210, 40)
        self.esp32_wifi_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.esp32_wifi_password.setStyleSheet(self.input_style())

        host_label = QLabel("Pi Host", wifi_panel)
        host_label.setGeometry(514, 160, 120, 18)
        host_label.setStyleSheet(self.label_style())
        self.esp32_pi_host = QLineEdit(wifi_panel)
        self.esp32_pi_host.setGeometry(514, 184, 180, 40)
        self.esp32_pi_host.setPlaceholderText("172.20.0.240")
        self.esp32_pi_host.setStyleSheet(self.input_style())

        port2_label = QLabel("TCP Port", wifi_panel)
        port2_label.setGeometry(710, 160, 100, 18)
        port2_label.setStyleSheet(self.label_style())
        self.esp32_pi_port = QLineEdit(wifi_panel)
        self.esp32_pi_port.setGeometry(710, 184, 80, 40)
        self.esp32_pi_port.setText("5000")
        self.esp32_pi_port.setStyleSheet(self.input_style())

        self.btn_apply_esp32_wifi = QPushButton("Save To ESP32", wifi_panel)
        self.btn_apply_esp32_wifi.setGeometry(806, 184, 126, 40)
        self.btn_apply_esp32_wifi.setStyleSheet(self.primary_btn_style())
        self.btn_apply_esp32_wifi.clicked.connect(self.provision_esp32_wifi)

        self.esp32_wifi_status = QLabel("Connect ESP32 over USB, then refresh ports.", wifi_panel)
        self.esp32_wifi_status.setGeometry(570, 112, 360, 28)
        self.esp32_wifi_status.setStyleSheet("font-size: 12px; color: #475569; font-weight: 700;")
        return page

    def build_control_ota_section(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        title = QLabel("OTA", page)
        title.setGeometry(0, 0, 260, 28)
        title.setStyleSheet("font-size: 20px; font-weight: 800; color: #0f172a;")
        message = QLabel("OTA Framework Reserved For Future ESP32 Firmware Updates", page)
        message.setGeometry(0, 48, 620, 24)
        message.setStyleSheet("font-size: 13px; color: #475569; font-weight: 700;")

        upload = QPushButton("Upload Firmware", page)
        upload.setGeometry(0, 92, 170, 44)
        upload.setStyleSheet(self.secondary_btn_style())
        upload.setEnabled(False)
        upload.setToolTip("Available after backend implementation.")

        release = QPushButton("Release Firmware", page)
        release.setGeometry(190, 92, 170, 44)
        release.setStyleSheet(self.secondary_btn_style())
        release.setEnabled(False)
        release.setToolTip("Available after backend implementation.")

        table = QTableWidget(page)
        table.setGeometry(0, 165, 955, 430)
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["Version", "Target", "Released By", "Status"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setStyleSheet(self.table_style())
        return page

    def build_control_backups_section(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        title = QLabel("Backups", page)
        title.setGeometry(0, 0, 260, 28)
        title.setStyleSheet("font-size: 20px; font-weight: 800; color: #0f172a;")
        message = QLabel("Backup API Pending Backend Implementation", page)
        message.setGeometry(0, 48, 520, 24)
        message.setStyleSheet("font-size: 13px; color: #475569; font-weight: 700;")

        create_btn = QPushButton("Create Backup", page)
        create_btn.setGeometry(0, 92, 170, 44)
        create_btn.setStyleSheet(self.secondary_btn_style())
        create_btn.setEnabled(False)
        create_btn.setToolTip("Available after backend implementation.")

        restore_btn = QPushButton("Restore Backup", page)
        restore_btn.setGeometry(190, 92, 170, 44)
        restore_btn.setStyleSheet(self.secondary_btn_style())
        restore_btn.setEnabled(False)
        restore_btn.setToolTip("Available after backend implementation.")

        table = QTableWidget(page)
        table.setGeometry(0, 165, 955, 430)
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["Created", "Type", "Size", "Created By", "Status"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setStyleSheet(self.table_style())
        return page

    def build_control_logs_section(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        title = QLabel("Logs", page)
        title.setGeometry(0, 0, 260, 28)
        title.setStyleSheet("font-size: 20px; font-weight: 800; color: #0f172a;")

        message = QLabel("System Logs, Operation Logs, Device Logs, and Update Logs: Log Endpoint Pending Backend Implementation", page)
        message.setGeometry(0, 48, 830, 24)
        message.setStyleSheet("font-size: 13px; color: #475569; font-weight: 700;")

        self.btn_refresh_it_audit = QPushButton("Refresh Local IT Audit", page)
        self.btn_refresh_it_audit.setGeometry(0, 88, 190, 42)
        self.btn_refresh_it_audit.setStyleSheet(self.secondary_btn_style())
        self.btn_refresh_it_audit.clicked.connect(self.load_it_audit_logs)

        self.it_audit_table = QTableWidget(page)
        self.it_audit_table.setGeometry(0, 150, 955, 520)
        self.it_audit_table.setColumnCount(6)
        self.it_audit_table.setHorizontalHeaderLabels(["Date/Time", "User", "Action", "Target", "Result", "Message"])
        self.it_audit_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.it_audit_table.verticalHeader().setVisible(False)
        self.it_audit_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.it_audit_table.setStyleSheet(self.table_style())
        return page

    def build_control_ai_section(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        title = QLabel("AI Debug", page)
        title.setGeometry(0, 0, 260, 28)
        title.setStyleSheet("font-size: 20px; font-weight: 800; color: #0f172a;")

        self.btn_generate_debug_brief = QPushButton("Collect Diagnostics", page)
        self.btn_generate_debug_brief.setGeometry(0, 48, 190, 44)
        self.btn_generate_debug_brief.setStyleSheet(self.primary_btn_style())

        self.btn_copy_debug_brief = QPushButton("Copy Diagnostics", page)
        self.btn_copy_debug_brief.setGeometry(210, 48, 170, 44)
        self.btn_copy_debug_brief.setStyleSheet(self.secondary_btn_style())

        warning = QLabel("AI may recommend actions only. Human approval is required for restart, rollback, update, or delete operations.", page)
        warning.setGeometry(400, 55, 540, 28)
        warning.setStyleSheet("font-size: 12px; color: #b45309; font-weight: 800;")

        diag_label = QLabel("Diagnostics Data", page)
        diag_label.setGeometry(0, 115, 180, 20)
        diag_label.setStyleSheet(self.label_style())
        self.ai_diag_input = QTextEdit(page)
        self.ai_diag_input.setGeometry(0, 142, 460, 260)
        self.ai_diag_input.setStyleSheet(self.input_style())

        summary_label = QLabel("AI Summary", page)
        summary_label.setGeometry(492, 115, 180, 20)
        summary_label.setStyleSheet(self.label_style())
        self.ai_summary_text = QTextEdit(page)
        self.ai_summary_text.setGeometry(492, 142, 460, 120)
        self.ai_summary_text.setReadOnly(True)
        self.ai_summary_text.setStyleSheet(self.input_style())
        self.debug_brief = self.ai_summary_text

        cause_label = QLabel("Likely Cause", page)
        cause_label.setGeometry(492, 280, 180, 20)
        cause_label.setStyleSheet(self.label_style())
        self.ai_cause_text = QTextEdit(page)
        self.ai_cause_text.setGeometry(492, 307, 460, 120)
        self.ai_cause_text.setReadOnly(True)
        self.ai_cause_text.setStyleSheet(self.input_style())

        fixes_label = QLabel("Recommended Fixes", page)
        fixes_label.setGeometry(0, 430, 180, 20)
        fixes_label.setStyleSheet(self.label_style())
        self.ai_fixes_text = QTextEdit(page)
        self.ai_fixes_text.setGeometry(0, 457, 952, 180)
        self.ai_fixes_text.setReadOnly(True)
        self.ai_fixes_text.setStyleSheet(self.input_style())
        return page

    # ---------------------------- pairing page ----------------------------

    def build_pairing_page(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")

        self.pair_left = QFrame(page)
        self.pair_left.setGeometry(0, 0, 590, 805)
        self.apply_frame_style(self.pair_left, "background-color: #121212; border-radius: 22px; border: 1px solid #1f1f1f;")

        title = QLabel("Resident Pairing / Unpairing", self.pair_left)
        title.setGeometry(22, 18, 240, 24)
        title.setStyleSheet("font-size: 18px; font-weight: 700;")

        self.pair_resident_list = QListWidget(self.pair_left)
        self.pair_resident_list.setGeometry(20, 60, 260, 500)

        self.available_devices_list = QListWidget(self.pair_left)
        self.available_devices_list.setGeometry(310, 60, 260, 500)

        for lw in [self.pair_resident_list, self.available_devices_list]:
            lw.setStyleSheet("""
                QListWidget {
                    background-color: #ffffff;
                    color: #0f172a;
                    border: 1px solid #d8e1ea;
                    border-radius: 8px;
                    padding: 6px;
                }
                QListWidget::item {
                    padding: 10px;
                    margin-bottom: 4px;
                    border-radius: 8px;
                    border: 1px solid transparent;
                }
                QListWidget::item:hover {
                    background-color: #eef4f8;
                    border: 1px solid #cbd5e1;
                }
                QListWidget::item:selected {
                    background-color: #ccfbf1;
                    color: #0f172a;
                    border: 1px solid #0f766e;
                }
            """)

        lbl_avail = QLabel("Available / Known Devices", self.pair_left)
        lbl_avail.setGeometry(310, 30, 180, 20)
        lbl_avail.setStyleSheet("font-size: 13px; color: #cfcfcf;")

        self.btn_pair_selected = QPushButton("Pair Resident to Device", self.pair_left)
        self.btn_pair_selected.setGeometry(310, 572, 260, 44)
        self.btn_pair_selected.setStyleSheet(self.primary_btn_style())

        self.btn_unpair_selected = QPushButton("Unpair Selected Device", self.pair_left)
        self.btn_unpair_selected.setGeometry(310, 624, 260, 44)
        self.btn_unpair_selected.setStyleSheet(self.secondary_btn_style())

        self.pair_info = QLabel("Select a resident and a device to pair.")
        self.pair_info.setParent(self.pair_left)
        self.pair_info.setGeometry(310, 676, 260, 64)
        self.pair_info.setWordWrap(True)
        self.pair_info.setStyleSheet("font-size: 12px; color: #a8a8a8;")

        self.pair_right = QFrame(page)
        self.pair_right.setGeometry(610, 0, 608, 805)
        self.apply_frame_style(self.pair_right, "background-color: #121212; border-radius: 22px; border: 1px solid #1f1f1f;")

        self.pair_table = QTableWidget(self.pair_right)
        self.pair_table.setGeometry(18, 18, 572, 768)
        self.pair_table.setColumnCount(5)
        self.pair_table.setHorizontalHeaderLabels(["Device ID", "Resident", "Resident UID", "Online", "Battery"])
        self.pair_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.pair_table.setStyleSheet(self.table_style())
        self.pair_table.verticalHeader().setVisible(False)
        self.pair_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        return self.wrap_scroll_page(page, 860)

    # ---------------------------- updates page ----------------------------

    def build_updates_page(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")

        self.upd_left = QFrame(page)
        self.upd_left.setGeometry(0, 0, 540, 805)
        self.apply_frame_style(self.upd_left, "background-color: #121212; border-radius: 22px; border: 1px solid #1f1f1f;")

        title = QLabel("LCD Schedule", self.upd_left)
        title.setGeometry(22, 18, 180, 24)
        title.setStyleSheet("font-size: 18px; font-weight: 700;")

        self.upd_target = QComboBox(self.upd_left)
        self.upd_target.setGeometry(22, 58, 490, 42)
        self.upd_target.setStyleSheet(self.input_style())

        self.hl_type = QComboBox(self.upd_left)
        self.hl_type.setGeometry(22, 118, 220, 38)
        self.hl_type.addItems(["Word highlight (VALUE)", "Section highlight (SECTION)"])
        self.hl_type.setStyleSheet(self.input_style())

        self.hl_section = QComboBox(self.upd_left)
        self.hl_section.setGeometry(252, 118, 100, 38)
        self.hl_section.addItems(SECTIONS)
        self.hl_section.setStyleSheet(self.input_style())

        self.hl_bg = QComboBox(self.upd_left)
        self.hl_bg.setGeometry(362, 118, 70, 38)
        self.hl_bg.addItems(PALETTE)
        self.hl_bg.setStyleSheet(self.input_style())

        self.hl_fg = QComboBox(self.upd_left)
        self.hl_fg.setGeometry(442, 118, 70, 38)
        self.hl_fg.addItems(PALETTE)
        self.hl_fg.setStyleSheet(self.input_style())

        lbl_tokens = QLabel("Pick exact screen words only", self.upd_left)
        lbl_tokens.setGeometry(22, 168, 160, 18)
        lbl_tokens.setStyleSheet(self.label_style())

        self.token_list = QListWidget(self.upd_left)
        self.token_list.setGeometry(22, 192, 320, 120)
        self.token_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.token_list.setStyleSheet("""
            QListWidget {
                background-color: #ffffff;
                color: #0f172a;
                border: 1px solid #d8e1ea;
                border-radius: 8px;
                padding: 6px;
            }
            QListWidget::item:selected {
                background-color: #ccfbf1;
                color: #0f172a;
            }
        """)

        self.rules_list = QListWidget(self.upd_left)
        self.rules_list.setGeometry(22, 340, 320, 120)
        self.rules_list.setStyleSheet("""
            QListWidget {
                background-color: #ffffff;
                color: #0f172a;
                border: 1px solid #d8e1ea;
                border-radius: 8px;
                padding: 6px;
            }
            QListWidget::item:selected {
                background-color: #ccfbf1;
                color: #0f172a;
            }
        """)

        self.btn_add_highlight = QPushButton("Add Highlight", self.upd_left)
        self.btn_add_highlight.setGeometry(360, 192, 152, 38)
        self.btn_add_highlight.setStyleSheet(self.secondary_btn_style())

        self.btn_remove_highlight = QPushButton("Remove Selected", self.upd_left)
        self.btn_remove_highlight.setGeometry(360, 238, 152, 38)
        self.btn_remove_highlight.setStyleSheet(self.secondary_btn_style())

        self.btn_clear_highlights = QPushButton("Clear Highlights", self.upd_left)
        self.btn_clear_highlights.setGeometry(360, 284, 152, 38)
        self.btn_clear_highlights.setStyleSheet(self.secondary_btn_style())

        self.btn_preview = QPushButton("Preview Update", self.upd_left)
        self.btn_preview.setGeometry(360, 340, 152, 42)
        self.btn_preview.setStyleSheet(self.primary_btn_style())

        self.btn_send_text = QPushButton("Send Text Update", self.upd_left)
        self.btn_send_text.setGeometry(360, 392, 152, 42)
        self.btn_send_text.setStyleSheet(self.primary_btn_style())

        self.btn_choose_image = QPushButton("Choose LCD Image", self.upd_left)
        self.btn_choose_image.setGeometry(22, 488, 150, 42)
        self.btn_choose_image.setStyleSheet(self.secondary_btn_style())
        self.btn_choose_image.hide()

        self.btn_send_image = QPushButton("Send Photo Only", self.upd_left)
        self.btn_send_image.setGeometry(360, 444, 152, 42)
        self.btn_send_image.setStyleSheet(self.secondary_btn_style())

        self.btn_clear_image = QPushButton("Clear Image", self.upd_left)
        self.btn_clear_image.setGeometry(312, 488, 120, 42)
        self.btn_clear_image.setStyleSheet(self.secondary_btn_style())
        self.btn_clear_image.hide()

        self.image_path_label = QLabel("Resident photo is attached in Resident Records. Use Send Photo Only after the e-paper text finishes.", self.upd_left)
        self.image_path_label.setGeometry(22, 506, 490, 44)
        self.image_path_label.setWordWrap(True)
        self.image_path_label.setStyleSheet("font-size: 12px; color: #a7a7a7;")

        manual_title = QLabel("Manual LCD Control", self.upd_left)
        manual_title.setGeometry(22, 566, 180, 22)
        manual_title.setStyleSheet("font-size: 15px; font-weight: 800; color: white;")

        self.btn_lcd_on = QPushButton("Turn LCD ON", self.upd_left)
        self.btn_lcd_on.setGeometry(22, 596, 150, 40)
        self.btn_lcd_on.setStyleSheet(self.primary_btn_style())

        self.btn_lcd_off = QPushButton("Turn LCD OFF", self.upd_left)
        self.btn_lcd_off.setGeometry(184, 596, 150, 40)
        self.btn_lcd_off.setStyleSheet(self.secondary_btn_style())

        self.chk_sleep_no_image = QCheckBox("Keep LCD asleep if no image exists", self.upd_left)
        self.chk_sleep_no_image.setGeometry(22, 644, 300, 24)
        self.chk_sleep_no_image.setStyleSheet(self.chk_active.styleSheet())

        self.upd_right = QFrame(page)
        self.upd_right.setGeometry(560, 0, 658, 805)
        self.apply_frame_style(self.upd_right, "background-color: #121212; border-radius: 22px; border: 1px solid #1f1f1f;")

        self.upd_epaper_card = QFrame(self.upd_right)
        self.upd_epaper_card.setGeometry(22, 22, 614, 220)
        self.apply_frame_style(self.upd_epaper_card, "background-color: #efefef; border-radius: 18px;")

        ep_title = QLabel("E-Paper Preview", self.upd_epaper_card)
        ep_title.setGeometry(18, 14, 120, 18)
        ep_title.setStyleSheet("color: #111111; font-size: 13px; font-weight: 700;")

        self.upd_ep_name = QLabel("Resident Name", self.upd_epaper_card)
        self.upd_ep_name.setGeometry(18, 44, 320, 30)
        self.upd_ep_name.setStyleSheet("color: #111111; font-size: 22px; font-weight: 700;")

        self.upd_ep_room = QLabel("Room ---", self.upd_epaper_card)
        self.upd_ep_room.setGeometry(18, 80, 250, 24)
        self.upd_ep_room.setStyleSheet("color: #111111; font-size: 14px;")

        self.upd_ep_diet = QLabel("Diet: ---", self.upd_epaper_card)
        self.upd_ep_diet.setGeometry(18, 112, 400, 24)
        self.upd_ep_diet.setStyleSheet("color: #111111; font-size: 14px;")

        self.upd_ep_allergies = QLabel("Texture: ---", self.upd_epaper_card)
        self.upd_ep_allergies.setGeometry(18, 144, 500, 24)
        self.upd_ep_allergies.setStyleSheet("color: #111111; font-size: 14px;")

        self.upd_ep_fluids = QLabel("Fluids: ---", self.upd_epaper_card)
        self.upd_ep_fluids.setGeometry(18, 172, 500, 24)
        self.upd_ep_fluids.setStyleSheet("color: #111111; font-size: 14px;")

        self.upd_ep_note = QLabel("Note: ---", self.upd_epaper_card)
        self.upd_ep_note.setGeometry(18, 196, 560, 24)
        self.upd_ep_note.setWordWrap(True)
        self.upd_ep_note.setStyleSheet("color: #111111; font-size: 13px;")

        self.upd_lcd_card = QFrame(self.upd_right)
        self.upd_lcd_card.setGeometry(22, 252, 614, 210)
        self.apply_frame_style(self.upd_lcd_card, "background-color: #0a1831; border-radius: 18px; border: 2px solid #20457b;")

        lcd_title = QLabel("LCD Preview", self.upd_lcd_card)
        lcd_title.setGeometry(18, 14, 120, 18)
        lcd_title.setStyleSheet("color: white; font-size: 13px; font-weight: 700;")

        self.upd_lcd_image = QLabel(self.upd_lcd_card)
        self.upd_lcd_image.setGeometry(20, 44, 574, 136)
        self.upd_lcd_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.upd_lcd_image.setStyleSheet("""
            QLabel {
                background-color: #bae6fd;
                border-radius: 8px;
            }
        """)
        self.upd_lcd_image.hide()

        self.upd_lcd_empty_state = QLabel("No LCD image selected. Choose a resident image to preview the exact screen artwork.", self.upd_lcd_card)
        self.upd_lcd_empty_state.setGeometry(38, 58, 538, 112)
        self.upd_lcd_empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.upd_lcd_empty_state.setWordWrap(True)
        self.upd_lcd_empty_state.setStyleSheet("""
            QLabel {
                background-color: #f8fafc;
                color: #0f766e;
                border: 1px dashed #7dd3fc;
                border-radius: 8px;
                padding: 18px;
                font-size: 14px;
                font-weight: 700;
            }
        """)

        self.upd_lcd_name = QLabel("Resident Name", self.upd_lcd_card)
        self.upd_lcd_name.setGeometry(20, 48, 574, 30)
        self.upd_lcd_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.upd_lcd_name.setStyleSheet("color: white; font-size: 28px; font-weight: 700;")

        self.upd_lcd_room = QLabel("Room ---", self.upd_lcd_card)
        self.upd_lcd_room.setGeometry(20, 78, 574, 22)
        self.upd_lcd_room.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.upd_lcd_room.setStyleSheet("color: #d7e3f1; font-size: 15px;")

        self.upd_lcd_alert = QLabel("STABLE", self.upd_lcd_card)
        self.upd_lcd_alert.setGeometry(202, 104, 210, 34)
        self.upd_lcd_alert.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.upd_lcd_alert.setStyleSheet("""
            QLabel {
                background-color: #146c2e;
                color: white;
                border-radius: 8px;
                font-size: 18px;
                font-weight: 700;
            }
        """)

        self.upd_lcd_note = QLabel("No note", self.upd_lcd_card)
        self.upd_lcd_note.setGeometry(20, 144, 574, 46)
        self.upd_lcd_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.upd_lcd_note.setWordWrap(True)
        self.upd_lcd_note.setStyleSheet("color: #eef2f7; font-size: 14px;")

        self.schedule_panel = QFrame(self.upd_right)
        self.schedule_panel.setGeometry(22, 472, 614, 196)
        self.apply_frame_style(self.schedule_panel, self.card_style())

        schedule_title = QLabel("Global LCD Schedule", self.schedule_panel)
        schedule_title.setGeometry(18, 12, 220, 24)
        schedule_title.setStyleSheet("font-size: 16px; color: white; font-weight: 800;")

        self.schedule_resident = QComboBox(self.schedule_panel)
        self.schedule_resident.setGeometry(18, 46, 290, 38)
        self.schedule_resident.setStyleSheet(self.input_style())
        self.schedule_resident.addItem("All LCD devices", "all")
        self.schedule_resident.setEnabled(False)

        self.chk_schedule_enabled = QCheckBox("Enabled", self.schedule_panel)
        self.chk_schedule_enabled.setGeometry(320, 53, 84, 24)
        self.chk_schedule_enabled.setStyleSheet(self.chk_active.styleSheet())

        on_label = QLabel("ON", self.schedule_panel)
        on_label.setGeometry(414, 24, 30, 18)
        on_label.setStyleSheet(self.label_style())

        self.schedule_on = QTimeEdit(self.schedule_panel)
        self.schedule_on.setGeometry(414, 46, 80, 38)
        self.schedule_on.setDisplayFormat("HH:mm")
        self.schedule_on.setTime(QTime(7, 0))
        self.schedule_on.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.schedule_on.setStyleSheet(self.input_style())

        off_label = QLabel("OFF", self.schedule_panel)
        off_label.setGeometry(504, 24, 36, 18)
        off_label.setStyleSheet(self.label_style())

        self.schedule_off = QTimeEdit(self.schedule_panel)
        self.schedule_off.setGeometry(504, 46, 80, 38)
        self.schedule_off.setDisplayFormat("HH:mm")
        self.schedule_off.setTime(QTime(20, 0))
        self.schedule_off.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.schedule_off.setStyleSheet(self.input_style())

        self.btn_save_schedule = QPushButton("Save Schedule to All LCDs", self.schedule_panel)
        self.btn_save_schedule.setGeometry(18, 102, 190, 40)
        self.btn_save_schedule.setStyleSheet(self.primary_btn_style())

        self.schedule_table = QTableWidget(self.schedule_panel)
        self.schedule_table.setGeometry(225, 96, 370, 88)
        self.schedule_table.setColumnCount(4)
        self.schedule_table.setHorizontalHeaderLabels(["Device", "Status", "Rule", "Times"])
        self.schedule_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.schedule_table.verticalHeader().setVisible(False)
        self.schedule_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.schedule_table.setStyleSheet(self.table_style())

        return self.wrap_scroll_page(page, 820)

    # ---------------------------- logs page ----------------------------

    def build_logs_page(self):
        page = QWidget()
        page.setStyleSheet("background: transparent;")

        self.logs_panel = QFrame(page)
        self.logs_panel.setGeometry(0, 0, 1218, 805)
        self.apply_frame_style(self.logs_panel, "background-color: #121212; border-radius: 22px; border: 1px solid #1f1f1f;")

        lbl = QLabel("Activity Logs", self.logs_panel)
        lbl.setGeometry(22, 18, 160, 24)
        lbl.setStyleSheet("font-size: 18px; font-weight: 700;")

        self.logs_table = QTableWidget(self.logs_panel)
        self.logs_table.setGeometry(18, 100, 1180, 686)
        self.logs_table.setColumnCount(7)
        self.logs_table.setHorizontalHeaderLabels(["Date/Time", "Action", "Resident UID", "Device", "Pushed By", "Success", "Message"])
        self.logs_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.logs_table.setStyleSheet(self.table_style())
        self.logs_table.verticalHeader().setVisible(False)
        self.logs_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.logs_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        self.btn_view_log = QPushButton("View Full Log", self.logs_panel)
        self.btn_view_log.setGeometry(930, 18, 130, 42)
        self.btn_view_log.setStyleSheet(self.secondary_btn_style())

        self.btn_export_logs_pdf = QPushButton("Export Logs PDF", self.logs_panel)
        self.btn_export_logs_pdf.setGeometry(1070, 18, 128, 42)
        self.btn_export_logs_pdf.setStyleSheet(self.primary_btn_style())

        hint = QLabel("Double-click a row to view the full log.", self.logs_panel)
        hint.setGeometry(22, 62, 360, 24)
        hint.setStyleSheet("font-size: 12px; color: #a7a7a7;")

        return self.wrap_scroll_page(page, 860)

    # ---------------------------- events ----------------------------

    def bind_events(self):
        self.close_btn.clicked.connect(self.close)
        self.min_btn.clicked.connect(self.showMinimized)
        self.max_btn.clicked.connect(self.toggle_max_restore)

        self.btn_refresh_devices.clicked.connect(self.refresh_all)
        self.auto_refresh.stateChanged.connect(self.toggle_auto_refresh)

        self.btn_menu_overview.clicked.connect(lambda: self.switch_page(self.page_overview, self.btn_menu_overview))
        self.btn_menu_dashboard.clicked.connect(lambda: self.switch_page(self.page_dashboard, self.btn_menu_dashboard))
        self.btn_menu_approvals.clicked.connect(lambda: self.switch_page(self.page_approvals, self.btn_menu_approvals))
        self.btn_menu_resident_audit.clicked.connect(lambda: self.switch_page(self.page_resident_audit, self.btn_menu_resident_audit))
        self.btn_menu_pairing.clicked.connect(lambda: self.switch_page(self.page_pairing, self.btn_menu_pairing))
        self.btn_menu_updates.clicked.connect(lambda: self.switch_page(self.page_updates, self.btn_menu_updates))
        self.btn_menu_verification.clicked.connect(lambda: self.switch_page(self.page_verification, self.btn_menu_verification))
        self.btn_menu_it_health.clicked.connect(self.show_it_control_sidebar_menu)
        self.btn_menu_logs.clicked.connect(lambda: self.switch_page(self.page_logs, self.btn_menu_logs))
        self.btn_account_profile.clicked.connect(self.show_account_profile)
        self.btn_profile_settings.clicked.connect(self.show_profile_settings)
        self.btn_logout.clicked.connect(self.handle_logout)
        self.btn_overview_new_resident.clicked.connect(lambda: self.switch_page(self.page_dashboard, self.btn_menu_dashboard))
        self.btn_overview_pairing.clicked.connect(lambda: self.switch_page(self.page_pairing, self.btn_menu_pairing))

        self.btn_new_resident.clicked.connect(self.new_resident)
        self.btn_save_resident.clicked.connect(self.save_resident)
        self.btn_clear_fields.clicked.connect(self.clear_form)
        self.btn_delete_resident.clicked.connect(self.delete_selected_resident)
        self.btn_go_pairing_after_save.clicked.connect(lambda: self.switch_page(self.page_pairing, self.btn_menu_pairing))
        self.btn_attach_source.clicked.connect(self.attach_source_document)
        self.btn_attach_resident_photo.clicked.connect(self.attach_resident_photo)
        self.btn_clear_resident_photo.clicked.connect(self.clear_lcd_image)
        self.btn_submit_review_request.clicked.connect(self.submit_resident_review_request)

        self.search_resident.textChanged.connect(self.filter_residents)
        self.resident_list.itemClicked.connect(self.on_resident_selected)

        self.pair_resident_list.itemClicked.connect(self.on_pair_resident_selected)
        self.available_devices_list.itemClicked.connect(self.on_pair_device_selected)
        self.btn_pair_selected.clicked.connect(self.pair_selected_from_menu)
        self.btn_unpair_selected.clicked.connect(self.unpair_selected_from_menu)

        self.hl_bg.currentTextChanged.connect(self.apply_auto_fg)
        self.hl_type.currentTextChanged.connect(self.on_hl_type_changed)
        self.hl_section.currentTextChanged.connect(self.refresh_token_list)

        self.btn_add_highlight.clicked.connect(self.add_highlight)
        self.btn_remove_highlight.clicked.connect(self.remove_selected_highlight)
        self.btn_clear_highlights.clicked.connect(self.clear_highlights)

        self.btn_preview.clicked.connect(self.update_preview)
        self.btn_send_text.clicked.connect(self.send_text_update)
        self.btn_choose_image.clicked.connect(self.choose_image)
        self.btn_send_image.clicked.connect(self.send_image)
        self.btn_clear_image.clicked.connect(self.clear_lcd_image)
        self.upd_target.currentIndexChanged.connect(lambda _index: self.on_update_target_changed())
        self.btn_lcd_on.clicked.connect(lambda: self.send_lcd_command("on"))
        self.btn_lcd_off.clicked.connect(lambda: self.send_lcd_command("off"))
        self.btn_save_schedule.clicked.connect(self.save_lcd_schedule)
        self.btn_refresh_approvals.clicked.connect(self.load_approvals)
        self.approval_table.cellClicked.connect(lambda row, _col: self.show_approval_detail(row))
        self.btn_approve_request.clicked.connect(lambda: self.review_selected_request("APPROVED"))
        self.btn_reject_request.clicked.connect(lambda: self.review_selected_request("REJECTED"))
        self.resident_audit_table.cellClicked.connect(lambda row, _col: self.show_resident_audit_detail(row))
        self.resident_audit_table.cellDoubleClicked.connect(lambda row, _col: self.show_resident_audit_detail(row))
        self.btn_refresh_audit.clicked.connect(self.load_resident_audit)
        self.btn_open_audit_document.clicked.connect(self.open_selected_audit_document)
        self.verify_resident_list.itemClicked.connect(self.on_verify_resident_selected)
        self.btn_refresh_verification.clicked.connect(self.load_verification_page)
        self.btn_mark_verified.clicked.connect(lambda: self.record_verification("MATCH"))
        self.btn_mark_mismatch.clicked.connect(lambda: self.record_verification("MISMATCH"))
        self.btn_generate_debug_brief.clicked.connect(self.generate_debug_brief)
        self.btn_copy_debug_brief.clicked.connect(self.copy_debug_brief)
        self.btn_issue_temp_password.clicked.connect(self.issue_temporary_password)
        self.btn_copy_temp_password.clicked.connect(self.copy_temporary_password)
        self.logs_table.cellDoubleClicked.connect(lambda row, _col: self.show_log_detail(row))
        self.btn_view_log.clicked.connect(self.show_selected_log_detail)
        self.btn_export_logs_pdf.clicked.connect(self.export_logs_pdf)

        for w in [self.txt_name, self.txt_room, self.txt_drinks]:
            w.textChanged.connect(self.refresh_token_list)
            w.textChanged.connect(lambda _text: self.update_preview())
        for combo in [self.txt_diet, self.txt_allergies, self.txt_schedule]:
            combo.currentTextChanged.connect(lambda _text: self.refresh_token_list())
            combo.currentTextChanged.connect(lambda _text: self.update_preview())

        self.txt_note.textChanged.connect(self.refresh_token_list)
        self.txt_note.textChanged.connect(self.update_preview)

    # ---------------------------- page switching ----------------------------

    def set_active_menu(self, active_btn):
        for btn in self.nav_buttons:
            if btn == active_btn:
                btn.setStyleSheet("""
                    QPushButton {
                        text-align: left;
                        padding-left: 18px;
                        background-color: #0f766e;
                        color: white;
                        border: none;
                        border-radius: 8px;
                        font-size: 14px;
                        font-weight: 700;
                    }
                """)
            else:
                btn.setStyleSheet("""
                    QPushButton {
                        text-align: left;
                        padding-left: 18px;
                        background-color: transparent;
                        color: #334155;
                        border: none;
                        border-radius: 8px;
                        font-size: 14px;
                        font-weight: 600;
                    }
                    QPushButton:hover {
                        background-color: #eef4f8;
                        color: #0f172a;
                    }
                """)

    def switch_page(self, page, btn):
        self.pages.setCurrentWidget(page)
        self.set_active_menu(btn)
        if page == self.page_overview:
            self.refresh_dashboard_summary()
        elif page == self.page_pairing:
            self.load_pairing_views()
        elif page == self.page_approvals:
            self.load_approvals()
        elif page == self.page_resident_audit:
            self.load_resident_audit()
        elif page == self.page_updates:
            self.load_update_targets()
            self.load_schedule_view()
            self.on_update_target_changed()
            self.update_preview()
        elif page == self.page_verification:
            self.load_verification_page()
        elif page == self.page_it_health:
            self.load_it_health()
        elif page == self.page_logs:
            self.load_recent_logs()

    # ---------------------------- helpers ----------------------------

    def base_url(self):
        if self.server_mode:
            profile = self.db.get_active_control_profile()
            host = profile.get("host") or ""
            port = profile.get("port") or 7000
            return f"http://{host}:{port}".rstrip("/")
        return self.base_url_edit.text().strip().rstrip("/")

    def selected_device_id(self):
        return self.upd_target.currentData()

    def schedule_on_time(self):
        return self.schedule_on.time().toString("HH:mm")

    def schedule_off_time(self):
        return self.schedule_off.time().toString("HH:mm")

    def current_resident_uid(self):
        return self.txt_uid.text().strip() or None

    def display_path_label(self, path, empty_text):
        if not path:
            return empty_text
        return os.path.basename(path) if os.path.isfile(str(path)) else str(path)

    def sync_resident_photo_labels(self):
        form_text = self.display_path_label(self.selected_image_path, "No resident photo attached")
        schedule_text = self.display_path_label(
            self.selected_image_path,
            "No resident photo attached. Add one in Resident Records, then Save.",
        )
        if hasattr(self, "resident_photo_label"):
            self.resident_photo_label.setText(form_text)
        if hasattr(self, "image_path_label"):
            if self.selected_image_path:
                self.image_path_label.setText(f"Resident photo saved with record: {schedule_text}. Use Send Photo Only when the e-paper is idle.")
            else:
                self.image_path_label.setText(schedule_text)

    def set_resident_photo_path(self, path):
        self.selected_image_path = path or None
        self.sync_resident_photo_labels()
        self.update_lcd_image_preview()

    def collect_resident_payload(self):
        texture = self.field_text(self.txt_allergies)
        fluids = self.field_text(self.txt_schedule)
        return {
            "resident_uid": self.txt_uid.text().strip(),
            "full_name": self.txt_name.text().strip(),
            "room": self.txt_room.text().strip(),
            "status_alert": "Stable",
            "diet": self.field_text(self.txt_diet),
            "texture": texture,
            "allergies": texture,
            "note": self.txt_note.toPlainText().strip(),
            "drinks": self.txt_drinks.text().strip(),
            "fluids": fluids,
            "schedule": fluids,
            "source_document": self.selected_source_document,
            "safety_review_note": "Pending safety review" if self.chk_safety_review.isChecked() else "",
            "needs_safety_review": self.chk_safety_review.isChecked(),
            "lcd_image_path": self.selected_image_path,
            "lcd_schedule_enabled": False,
            "lcd_on_time": None,
            "lcd_off_time": None,
            "sleep_if_no_image": False,
            "active": self.chk_active.isChecked(),
        }

    def resident_audit_snapshot(self, row_or_payload):
        if not row_or_payload:
            return None
        texture = row_or_payload.get("texture") or row_or_payload.get("allergies")
        fluids = row_or_payload.get("fluids") or row_or_payload.get("schedule")
        return {
            "resident_uid": row_or_payload.get("resident_uid"),
            "full_name": row_or_payload.get("full_name"),
            "room": row_or_payload.get("room"),
            "diet": row_or_payload.get("diet"),
            "texture": texture,
            "note": row_or_payload.get("note"),
            "drinks": row_or_payload.get("drinks"),
            "fluids": fluids,
            "source_document": row_or_payload.get("source_document"),
            "lcd_image_path": row_or_payload.get("lcd_image_path"),
            "needs_safety_review": bool(row_or_payload.get("needs_safety_review", False)),
            "active": bool(row_or_payload.get("active", True)),
        }

    def resident_field_label(self, key):
        labels = {
            "resident_uid": "Resident UID",
            "full_name": "Full Name",
            "room": "Room",
            "diet": "Diet",
            "texture": "Texture",
            "allergies": "Texture",
            "note": "Note",
            "drinks": "Drinks",
            "fluids": "Fluids",
            "schedule": "Fluids",
            "source_document": "Source Document",
            "lcd_image_path": "Resident Photo",
            "needs_safety_review": "Needs Safety Review",
            "active": "Active",
        }
        return labels.get(str(key), str(key).replace("_", " ").title())

    def resident_field_value(self, value):
        if value is None:
            return ""
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value if str(item).strip())
        if isinstance(value, dict):
            nested = self.format_resident_snapshot(value)
            if nested:
                return "\n" + "\n".join(f"  {line}" if line else "" for line in nested.splitlines())
            return self.pretty_json(value)
        return str(value)

    def format_resident_snapshot(self, snapshot):
        if not snapshot:
            return ""
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except Exception:
                return snapshot
        if not isinstance(snapshot, dict):
            return self.pretty_json(snapshot)
        snapshot = dict(snapshot)
        if "texture" not in snapshot and "allergies" in snapshot:
            snapshot["texture"] = snapshot.get("allergies")
        if "fluids" not in snapshot and "schedule" in snapshot:
            snapshot["fluids"] = snapshot.get("schedule")

        ordered_keys = [
            "resident_uid",
            "full_name",
            "room",
            "diet",
            "texture",
            "fluids",
            "drinks",
            "note",
            "source_document",
            "lcd_image_path",
            "needs_safety_review",
            "active",
        ]
        lines = []
        seen = set()
        for key in ordered_keys:
            if key not in snapshot:
                continue
            seen.add(key)
            value = self.resident_field_value(snapshot.get(key))
            if value != "":
                lines.append(f"{self.resident_field_label(key)}: {value}")
        for key, raw_value in snapshot.items():
            if key in seen:
                continue
            if key == "allergies" and "texture" in snapshot:
                continue
            if key == "schedule" and "fluids" in snapshot:
                continue
            value = self.resident_field_value(raw_value)
            if value != "":
                lines.append(f"{self.resident_field_label(key)}: {value}")
        return "\n".join(lines)

    def show_error(self, title, text):
        QMessageBox.critical(self, title, friendly_error_message(str(text or "")))

    def show_info(self, title, text):
        QMessageBox.information(self, title, text)

    def begin_button_busy(self, button, busy_text):
        if button is None:
            return None
        state = {
            "button": button,
            "text": button.text(),
            "enabled": button.isEnabled(),
        }
        button.setText(busy_text)
        button.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        return state

    def end_button_busy(self, state):
        if not state:
            return
        button = state.get("button")
        if button is not None:
            button.setText(state.get("text") or button.text())
            button.setEnabled(bool(state.get("enabled", True)))
        try:
            QApplication.restoreOverrideCursor()
        except Exception:
            pass
        QApplication.processEvents()

    def result_error_message(self, result, fallback="The request could not be completed."):
        status_code = result.get("status_code") if isinstance(result, dict) else None
        body = result.get("body") if isinstance(result, dict) else None
        message = fallback
        if isinstance(body, dict):
            value = body.get("message") or body.get("error") or body.get("err") or body.get("detail")
            if isinstance(value, dict):
                value = value.get("message") or value.get("error") or value.get("err") or value.get("line")
            if value:
                message = str(value)
        elif body:
            message = str(body)
        return friendly_error_message(message, status_code=status_code, data=body)

    def set_gateway_state(self, online: bool):
        self.gateway_online = bool(online)
        self.apply_write_lock()

    def set_control_gateway_state(self, result=None):
        result = result or {}
        profile = self.current_control_profile()
        has_api_key = bool(profile.get("api_key"))
        self.control_service_online = bool(result.get("ok") and has_api_key)
        if self.server_mode:
            self.gateway_online = self.control_service_online
        if not profile.get("host"):
            text = "Gateway: Demo Mode"
            color = "#64748b"
            background = "#f8fafc"
            border = "#d8e1ea"
            tooltip = "Configure the Raspberry Pi Control Service in IT Control Center."
        elif result.get("ok") and not has_api_key:
            text = "Gateway: Missing Key"
            color = "#b45309"
            background = "#fffbeb"
            border = "#fcd34d"
            tooltip = "Control Service health is reachable, but protected requests require an API key."
        elif self.control_service_online:
            text = "Gateway: Connected"
            color = "#047857"
            background = "#ecfdf5"
            border = "#6ee7b7"
            tooltip = f"Connected to {profile.get('profile_name') or 'Control Service'}."
        else:
            text = "Gateway: Offline"
            color = "#b91c1c"
            background = "#fff1f2"
            border = "#fecdd3"
            tooltip = result.get("error") or "Control Service Offline or Unreachable."
        self.connection_badge.setText(text)
        self.connection_badge.setToolTip(tooltip)
        self.connection_badge.setStyleSheet(f"""
            QLabel {{
                background-color: {background};
                color: {color};
                border: 1px solid {border};
                border-radius: 8px;
                font-size: 13px;
                font-weight: 700;
            }}
        """)
        self.refresh_dashboard_summary()
        self.apply_write_lock()

    def refresh_control_connection_status(self):
        if not hasattr(self, "connection_badge"):
            return
        client = self.control_client(timeout=0.8)
        health = client.health()
        result = health
        self.control_last_results["health"] = health
        if health.get("ok") and client.api_key:
            network = client.network_status()
            self.control_last_results["network"] = network
            if not network.get("ok"):
                result = network
        self.set_control_gateway_state(result)
        self.update_control_header(result)
        self.update_control_network_labels(result)
        if hasattr(self, "control_dashboard_labels"):
            self.control_dashboard_labels["service"].setText(self.control_status_text(result))
            self.control_dashboard_labels["refreshed"].setText(time.strftime("%Y-%m-%d %H:%M:%S"))
        self.load_control_services()

    def refresh_all(self):
        busy = self.begin_button_busy(getattr(self, "btn_refresh_devices", None), "Refreshing...")
        try:
            self.refresh_control_connection_status()
            self.refresh_devices()
        finally:
            self.end_button_busy(busy)

    def apply_role_permissions(self):
        access = {
            self.btn_menu_overview: True,
            self.btn_menu_dashboard: self.can_view_residents(),
            self.btn_menu_approvals: self.is_nurse_admin(),
            self.btn_menu_resident_audit: self.can_view_residents(),
            self.btn_menu_pairing: self.can_manage_devices() and not self.is_it_admin(),
            self.btn_menu_updates: self.can_manage_devices() and not self.is_it_admin(),
            self.btn_menu_verification: self.is_nurse_admin() or self.is_verifier(),
            self.btn_menu_it_health: self.can_view_technical(),
            self.btn_menu_logs: self.is_it_admin(),
        }
        for btn, allowed in access.items():
            btn.setVisible(bool(allowed))

        self.base_url_edit.setVisible(False)
        self.base_url_edit.setEnabled(False)
        self.btn_refresh_devices.setVisible(self.can_view_technical() or self.is_nurse_admin())
        self.auto_refresh.setVisible(self.can_view_technical() or self.is_nurse_admin())
        self.btn_profile_settings.setVisible(self.is_it_admin())
        self.position_window_controls()

        field_widgets = [
            self.txt_name, self.txt_room, self.cmb_alert, self.txt_diet,
            self.txt_allergies, self.txt_note, self.txt_drinks, self.txt_schedule,
            self.chk_active, self.chk_safety_review, self.btn_attach_source,
            self.btn_attach_resident_photo, self.btn_clear_resident_photo,
            self.btn_choose_image, self.btn_clear_image,
        ]
        for widget in field_widgets:
            widget.setEnabled(self.can_edit_residents())
        for widget in getattr(self, "dropdown_option_buttons", []):
            widget.setEnabled(self.can_edit_residents())

        if hasattr(self, "nurse_review_comment"):
            self.nurse_review_comment.setEnabled(self.is_nurse() or self.is_nurse_admin())
        if hasattr(self, "btn_submit_review_request"):
            self.btn_submit_review_request.setVisible(self.is_nurse())

        if self.is_it_admin() and self.pages.currentWidget() != self.page_it_health:
            self.switch_page(self.page_it_health, self.btn_menu_it_health)
        elif not self.can_view_residents() and self.pages.currentWidget() == self.page_dashboard:
            self.switch_page(self.page_overview, self.btn_menu_overview)
        elif self.pages.currentWidget() == self.page_logs and not self.is_it_admin():
            self.switch_page(self.page_resident_audit, self.btn_menu_resident_audit)

    def apply_write_lock(self):
        resident_write = self.can_edit_residents()
        device_write = self.gateway_online and self.can_manage_devices() and not self.is_it_admin()
        settings = {
            "btn_new_resident": resident_write,
            "btn_save_resident": resident_write,
            "btn_clear_fields": resident_write or self.is_nurse(),
            "btn_delete_resident": resident_write,
            "btn_go_pairing_after_save": resident_write,
            "btn_pair_selected": device_write,
            "btn_unpair_selected": device_write,
            "btn_send_text": device_write,
            "btn_lcd_on": device_write,
            "btn_lcd_off": device_write,
            "btn_save_schedule": device_write,
            "btn_approve_request": self.is_nurse_admin(),
            "btn_reject_request": self.is_nurse_admin(),
            "btn_mark_verified": self.is_nurse_admin() or self.is_verifier(),
            "btn_mark_mismatch": self.is_nurse_admin() or self.is_verifier(),
        }
        for btn_name, enabled in settings.items():
            btn = getattr(self, btn_name, None)
            if btn is not None:
                btn.setEnabled(bool(enabled))
        if hasattr(self, "close_btn"):
            self.position_window_controls()

    def require_network_for_write(self, action_name: str) -> bool:
        if self.server_mode and self.control_service_online:
            return True
        if not self.server_mode and self.gateway_online:
            return True
        self.show_error(
            "Facility Network Required",
            f"{action_name} needs the Raspberry Pi Control Service. "
            "Connect this computer to the dedicated facility network, then try again. "
            "If you are already connected, ask IT to check the Raspberry Pi and Control Service."
        )
        return False

    def safe_get_devices(self) -> List[Dict[str, Any]]:
        try:
            try:
                return self.db.get_devices(suppress_errors=True)
            except TypeError:
                return self.db.get_devices()
        except Exception:
            return []

    def attach_source_document(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Attach source document",
            "",
            "Documents (*.pdf *.doc *.docx *.txt *.png *.jpg *.jpeg);;All Files (*)"
        )
        if not path:
            return
        self.selected_source_document = path
        self.source_doc_label.setText(os.path.basename(path))

    # ---------------------------- resident management ----------------------------

    def new_resident(self):
        self.selected_resident_id = None
        self.clear_form()
        self.txt_uid.setText(generate_resident_uid())
        self.chk_active.setChecked(True)
        self.update_preview()

    def clear_form(self):
        self.txt_uid.clear()
        self.txt_name.clear()
        self.txt_room.clear()
        self.clear_field_text(self.txt_diet)
        self.clear_field_text(self.txt_allergies)
        self.txt_note.clear()
        self.txt_drinks.clear()
        self.clear_field_text(self.txt_schedule)

        self.chk_active.setChecked(True)
        self.chk_safety_review.setChecked(False)
        self.cmb_alert.setCurrentIndex(0)

        self.selected_image_path = None
        self.sync_resident_photo_labels()
        self.selected_source_document = None
        self.source_doc_label.setText("No source document attached")

        self.rules.clear()
        self.rules_list.clear()
        self.token_list.clear()

        self.update_lcd_image_preview()

    def load_residents(self):
        current_main_id = self.selected_resident_id
        current_pair_id = self.selected_pair_resident_id
        self.resident_list.clear()
        self.pair_resident_list.clear()

        for r in self.db.get_residents():
            label = f"{r['full_name']} | {r.get('room') or 'No room'} | {r['resident_uid']}"
            if r.get("paired_device_id"):
                status = "online" if r.get("paired_device_online") else "offline"
                label += f" | {status}: {r['paired_device_id']}"

            for lw in [self.resident_list, self.pair_resident_list]:
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, r["id"])
                lw.addItem(item)

            if current_main_id is not None and r["id"] == current_main_id:
                self.resident_list.setCurrentRow(self.resident_list.count() - 1)
            if current_pair_id is not None and r["id"] == current_pair_id:
                self.pair_resident_list.setCurrentRow(self.pair_resident_list.count() - 1)

        if current_pair_id is not None:
            row = self.db.get_resident(current_pair_id)
            if row:
                self.pair_info.setText(f"Selected resident:\n{row['full_name']} ({row['resident_uid']})")

    def filter_residents(self):
        query = self.search_resident.text().strip().lower()
        for i in range(self.resident_list.count()):
            item = self.resident_list.item(i)
            item.setHidden(query not in item.text().lower())

    def refresh_dashboard_summary(self):
        summary = self.db.get_dashboard_summary()
        titles = {
            "active_residents": "Active residents",
            "online_devices": "Online devices",
            "pending_requests": "Pending approvals",
            "verification_mismatches": "Display mismatches",
            "failed_updates": "Failed updates",
        }
        for key, title in titles.items():
            if hasattr(self, "record_summary_labels") and key in self.record_summary_labels:
                self.record_summary_labels[key].setText(f"{title}: {summary.get(key, 0)}")
        if hasattr(self, "record_summary_labels") and "database_mode" in self.record_summary_labels:
            if self.server_mode:
                mode = "Server Mode Connected" if self.control_service_online else "Server Mode Offline"
            else:
                mode = "Offline Demo Mode"
            self.record_summary_labels["database_mode"].setText(f"Data store: {mode}")

        overview_values = {
            "active_residents": summary.get("active_residents", 0),
            "pending_requests": summary.get("pending_requests", 0),
            "verification_mismatches": summary.get("verification_mismatches", 0),
            "online_devices": summary.get("online_devices", 0),
            "failed_updates": summary.get("failed_updates", 0),
        }
        for key, value in overview_values.items():
            if hasattr(self, "summary_labels") and key in self.summary_labels:
                self.summary_labels[key].setText(str(value))
        if hasattr(self, "overview_status"):
            if self.server_mode:
                mode = "Server Mode Connected" if self.control_service_online else "Server Mode Offline"
            else:
                mode = "Offline Demo Mode"
            gateway_text = self.connection_badge.text().replace("Gateway: ", "")
            self.overview_status.setText(f"Data store: {mode}\nGateway: {gateway_text}\nAuto-refresh: {'on' if self.auto_refresh.isChecked() else 'off'}")
        if hasattr(self, "overview_device_table"):
            self.load_overview_devices()

    def load_overview_devices(self):
        devices = self.safe_get_devices()
        self.overview_device_table.setRowCount(len(devices))
        for r, d in enumerate(devices):
            values = [
                d.get("device_id") or "",
                "Online" if d.get("is_online") else "Offline",
                self.battery_display_text(d, compact=True),
                ("Assigned" if d.get("resident_name") else "Unassigned") if self.is_it_admin() else (d.get("resident_name") or "Unassigned"),
                str(d.get("last_seen_s") or ""),
            ]
            for c, value in enumerate(values):
                self.overview_device_table.setItem(r, c, QTableWidgetItem(str(value)))

    def load_schedule_view(self):
        if not hasattr(self, "schedule_resident"):
            return
        self.schedule_resident.blockSignals(True)
        self.schedule_resident.clear()
        devices = self.safe_get_devices()
        self.schedule_resident.addItem(f"All LCD devices ({len(devices)})", "all")
        self.schedule_resident.setCurrentIndex(0)
        self.schedule_resident.blockSignals(False)

        self.chk_schedule_enabled.setChecked(bool(self.global_schedule_enabled))
        self.chk_sleep_no_image.setChecked(bool(self.global_schedule_sleep_if_no_image))
        self.schedule_on.setTime(QTime.fromString(self.global_schedule_on, "HH:mm"))
        self.schedule_off.setTime(QTime.fromString(self.global_schedule_off, "HH:mm"))

        self.schedule_table.setRowCount(len(devices))
        enabled_text = "Enabled" if self.global_schedule_enabled else "Off"
        rule_text = "Sleep if no image" if self.global_schedule_sleep_if_no_image else "No forced sleep"
        time_text = f"{enabled_text}: {self.global_schedule_on} - {self.global_schedule_off}"
        for r, row in enumerate(devices):
            values = [
                row.get("device_id") or "",
                "Online" if row.get("is_online") else "Offline",
                rule_text,
                time_text,
            ]
            for c, value in enumerate(values):
                self.schedule_table.setItem(r, c, QTableWidgetItem(str(value)))

    def load_approvals(self):
        if not hasattr(self, "approval_table"):
            return
        requests = self.db.get_change_requests(limit=100)
        self.approval_table.setRowCount(len(requests))
        pending = 0
        for r, request in enumerate(requests):
            if request.get("status") == "PENDING":
                pending += 1
            values = [
                request.get("status") or "",
                request.get("full_name") or request.get("resident_uid") or "Unknown",
                request.get("room") or "",
                request.get("requested_by_username") or "",
                self.db.format_timestamp(request.get("created_at")),
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, request.get("id"))
                self.approval_table.setItem(r, c, item)
        self.approval_count_label.setText(f"Pending: {pending}")
        if len(requests) == 0:
            self.selected_review_request_id = None
            self.approval_detail.setPlainText("No resident review requests.")

    def show_approval_detail(self, row):
        item = self.approval_table.item(row, 0)
        if item is None:
            return
        request_id = item.data(Qt.ItemDataRole.UserRole)
        self.selected_review_request_id = request_id
        request = None
        for candidate in self.db.get_change_requests(limit=200):
            if candidate.get("id") == request_id:
                request = candidate
                break
        if not request:
            self.approval_detail.setPlainText("Request no longer exists.")
            return

        payload = request.get("proposed_payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {"raw_payload": payload}

        fields = [
            f"Status: {request.get('status')}",
            f"Resident: {request.get('full_name') or request.get('resident_uid')}",
            f"Room: {request.get('room') or ''}",
            f"Submitted By: {request.get('requested_by_username') or ''}",
            f"Submitted At: {self.db.format_timestamp(request.get('created_at'))}",
            "",
            "Staff Note:",
            request.get("comment") or "",
            "",
            "Resident Snapshot:",
        ]
        snapshot = self.format_resident_snapshot(payload)
        fields.append(snapshot or "No resident information in request.")
        if request.get("review_note"):
            fields.extend(["", "Admin Decision:", request.get("review_note")])
        self.approval_detail.setPlainText("\n".join(fields))

    def review_selected_request(self, status):
        if not self.is_nurse_admin():
            self.show_error("Permission Required", "Only admins can review resident requests.")
            return
        if self.selected_review_request_id is None:
            self.show_error("No request", "Select a request from the queue.")
            return
        note = self.approval_review_note.toPlainText().strip()
        if not note:
            self.show_error("Decision note required", "Add a short note before closing the request.")
            return
        try:
            request = None
            for candidate in self.db.get_change_requests(limit=200):
                if candidate.get("id") == self.selected_review_request_id:
                    request = candidate
                    break
            request_payload = {}
            before_snapshot = None
            if request:
                request_payload = request.get("proposed_payload") or {}
                if isinstance(request_payload, str):
                    try:
                        request_payload = json.loads(request_payload)
                    except Exception:
                        request_payload = {"raw_payload": request_payload}
                if request.get("resident_id"):
                    before_snapshot = self.resident_audit_snapshot(self.db.get_resident(request.get("resident_id")))
            self.db.update_change_request_status(
                self.selected_review_request_id,
                status,
                self.current_user.get("id"),
                self.current_user.get("username"),
                note,
            )
            self.db.log_update(
                "resident_review_decision",
                None,
                None,
                None,
                self.current_user.get("id"),
                self.current_user.get("username"),
                {
                    "request_id": self.selected_review_request_id,
                    "status": status,
                    "note": note,
                    "before": before_snapshot,
                    "after": self.resident_audit_snapshot(request_payload),
                },
                {"reviewed": True},
                True,
                f"Resident review request {status.lower()}",
            )
            self.approval_review_note.clear()
            self.selected_review_request_id = None
            self.load_approvals()
            self.load_resident_audit()
            self.load_recent_logs()
            self.refresh_dashboard_summary()
            self.show_info("Review recorded", f"Request marked {status.lower()}.")
        except Exception as e:
            self.show_error("Review failed", str(e))

    def load_resident_audit(self):
        if not hasattr(self, "resident_audit_table"):
            return
        rows = self.db.get_resident_audit_logs(limit=200)
        self.resident_audit_logs = rows
        self.resident_audit_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            document_path = self.audit_document_path(row)
            values = [
                self.db.format_timestamp(row.get("created_at")),
                self.audit_action_label(row.get("action_type")),
                row.get("resident_uid") or "",
                row.get("pushed_by_username") or "",
                "Available" if document_path else "Not attached",
                row.get("message") or "",
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, r)
                self.resident_audit_table.setItem(r, c, item)
        if rows:
            self.show_resident_audit_detail(0)
        else:
            self.selected_audit_log = None
            self.resident_audit_detail.setPlainText("No resident information changes recorded yet.")
            self.btn_open_audit_document.setEnabled(False)

    def audit_action_label(self, action):
        labels = {
            "resident_create": "Created",
            "resident_update": "Updated",
            "resident_delete": "Deleted",
            "resident_review_request": "Review Requested",
            "resident_review_decision": "Review Decision",
        }
        return labels.get(action or "", action or "")

    def audit_payloads(self, row):
        payload = row.get("payload_json") or {}
        response = row.get("response_json") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {"raw": payload}
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except Exception:
                response = {"raw": response}

        before = payload.get("before") if isinstance(payload, dict) else None
        after = payload.get("after") if isinstance(payload, dict) else None
        if before is None and isinstance(response, dict):
            before = response.get("before")
        if after is None and isinstance(response, dict):
            after = response.get("after")
        if before is None and row.get("action_type") == "resident_delete" and isinstance(payload, dict):
            before = payload.get("resident") or payload
        if after is None and row.get("action_type") in {"resident_create", "resident_update"} and isinstance(payload, dict):
            after = payload if "full_name" in payload else payload.get("resident")
        return payload, response, before, after

    def audit_document_path(self, row):
        payload, response, before, after = self.audit_payloads(row)
        candidates = [
            after,
            before,
            payload if isinstance(payload, dict) else None,
            response if isinstance(response, dict) else None,
        ]
        for candidate in candidates:
            if isinstance(candidate, dict):
                path = candidate.get("source_document")
                if path:
                    return path
        return row.get("current_source_document")

    def show_resident_audit_detail(self, row):
        if row < 0 or row >= len(getattr(self, "resident_audit_logs", [])):
            return
        audit = self.resident_audit_logs[row]
        self.selected_audit_log = audit
        payload, response, before, after = self.audit_payloads(audit)
        document_path = self.audit_document_path(audit)
        lines = [
            f"Date/Time: {self.db.format_timestamp(audit.get('created_at'))}",
            f"Action: {self.audit_action_label(audit.get('action_type'))}",
            f"Resident UID: {audit.get('resident_uid') or ''}",
            f"Changed By: {audit.get('pushed_by_username') or ''}",
            f"Source Document: {document_path or 'Not attached'}",
            f"Message: {audit.get('message') or ''}",
            "",
            "Previous Information:",
            self.format_resident_snapshot(before) or "No previous snapshot recorded.",
            "",
            "Present / Proposed Information:",
            self.format_resident_snapshot(after) or "No present snapshot recorded.",
        ]
        if audit.get("action_type") in {"resident_review_request", "resident_review_decision"}:
            lines.extend(["", "Review Payload:", self.format_resident_snapshot(payload) or self.pretty_json(payload)])
            if response:
                lines.extend(["", "Review Result:", self.format_resident_snapshot(response) or self.pretty_json(response)])
        self.resident_audit_detail.setPlainText("\n".join(lines))
        self.btn_open_audit_document.setEnabled(bool(document_path))

    def open_selected_audit_document(self):
        if not self.selected_audit_log:
            self.show_error("No audit selected", "Select a resident audit entry first.")
            return
        document_path = self.audit_document_path(self.selected_audit_log)
        if not document_path:
            self.show_error("No document", "No source document is attached to this audit entry.")
            return
        if str(document_path).lower().startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(str(document_path)))
            return
        if not os.path.exists(document_path):
            self.show_error("Document not found", f"The source document was not found:\n{document_path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(document_path))

    def load_verification_page(self):
        if not hasattr(self, "verify_resident_list"):
            return
        current_id = self.selected_verification_resident_id
        self.verify_resident_list.clear()
        for resident in self.db.get_residents():
            label = f"{resident.get('full_name')} | {resident.get('room') or 'No room'} | {resident.get('resident_uid')}"
            if resident.get("paired_device_id"):
                label += f" | {resident.get('paired_device_id')}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, resident.get("id"))
            self.verify_resident_list.addItem(item)
            if current_id is not None and resident.get("id") == current_id:
                self.verify_resident_list.setCurrentRow(self.verify_resident_list.count() - 1)
        self.load_verification_history()

    def on_verify_resident_selected(self, item):
        resident_id = item.data(Qt.ItemDataRole.UserRole)
        self.selected_verification_resident_id = resident_id
        row = self.db.get_resident(resident_id)
        if not row:
            self.verify_detail.setPlainText("Resident record was not found.")
            return
        fields = [
            f"Resident UID: {row.get('resident_uid') or ''}",
            f"Name: {row.get('full_name') or ''}",
            f"Room: {row.get('room') or ''}",
            f"Paired Device: {row.get('paired_device_id') or 'Unpaired'}",
            f"Device Status: {'Online' if row.get('paired_device_online') else 'Offline'}",
            "",
            f"Diet: {row.get('diet') or ''}",
            f"Texture: {row.get('texture') or row.get('allergies') or ''}",
            f"Drinks: {row.get('drinks') or ''}",
            f"Fluids: {row.get('fluids') or row.get('schedule') or ''}",
            "",
            "Display Note:",
            row.get("note") or "",
        ]
        self.verify_detail.setPlainText("\n".join(fields))

    def record_verification(self, status):
        if not (self.is_verifier() or self.is_nurse_admin()):
            self.show_error("Permission Required", "Only display verifiers or admins can record verification.")
            return
        if self.selected_verification_resident_id is None:
            self.show_error("No resident", "Select a resident to verify.")
            return
        row = self.db.get_resident(self.selected_verification_resident_id)
        if not row:
            self.show_error("Not found", "Resident record was not found.")
            return
        note = self.verify_note.toPlainText().strip()
        try:
            self.db.create_verification_check(
                self.selected_verification_resident_id,
                row.get("resident_uid"),
                row.get("paired_device_id"),
                status,
                note,
                self.current_user.get("id"),
                self.current_user.get("username"),
            )
            self.db.log_update(
                "display_verification",
                self.selected_verification_resident_id,
                row.get("resident_uid"),
                row.get("paired_device_id"),
                self.current_user.get("id"),
                self.current_user.get("username"),
                {"status": status, "note": note},
                {"recorded": True},
                status == "MATCH",
                "Display verification recorded",
            )
            self.verify_note.clear()
            self.load_verification_history()
            self.load_recent_logs()
            self.refresh_dashboard_summary()
            self.show_info("Verification recorded", "Display verification was recorded.")
        except Exception as e:
            self.show_error("Verification failed", str(e))

    def load_verification_history(self):
        if not hasattr(self, "verification_table"):
            return
        rows = self.db.get_verification_checks(limit=100)
        self.verification_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [
                row.get("status") or "",
                row.get("full_name") or row.get("resident_uid") or "",
                row.get("device_id") or "",
                row.get("checked_by_username") or "",
            ]
            for c, value in enumerate(values):
                self.verification_table.setItem(r, c, QTableWidgetItem(str(value)))

    def current_control_profile(self):
        if hasattr(self, "control_profile_combo"):
            row = self.control_profile_combo.currentData()
            if row:
                return row
        return self.db.get_active_control_profile() or {}

    def control_client(self, timeout=2.0):
        profile = self.current_control_profile()
        return ControlServiceClient(
            profile.get("host") or "",
            profile.get("port") or 7000,
            profile.get("api_key") or "",
            timeout=timeout,
        )

    def load_local_battery_alert_settings(self) -> Dict[str, Any]:
        try:
            settings = self.settings.load().get("battery_alert_settings") or {}
        except Exception:
            settings = {}
        return self.normalize_battery_alert_settings(settings)

    def save_local_battery_alert_settings(self, settings: Dict[str, Any]):
        data = self.settings.load()
        data["battery_alert_settings"] = self.normalize_battery_alert_settings(settings)
        self.settings.save(data)

    def load_battery_alert_settings(self, quiet: bool = True, timeout: float = 1.0):
        settings = self.load_local_battery_alert_settings()
        if self.server_mode:
            result = self.control_client(timeout=timeout).get_battery_alert_settings()
            if result.get("ok"):
                payload = result.get("data") or {}
                settings = self.normalize_battery_alert_settings(payload.get("settings") or payload)
                self.save_local_battery_alert_settings(settings)
            elif not quiet and hasattr(self, "battery_alert_status"):
                self.battery_alert_status.setText(f"Using local policy. {result.get('error') or 'Pi setting not available.'}")
                self.battery_alert_status.setStyleSheet("font-size: 12px; color: #b45309; font-weight: 800;")
        self.battery_alert_settings = settings
        self.apply_battery_alert_settings_to_ui()
        return settings

    def apply_battery_alert_settings_to_ui(self):
        if not hasattr(self, "chk_battery_alert_enabled"):
            return
        settings = self.normalize_battery_alert_settings(self.battery_alert_settings)
        self.chk_battery_alert_enabled.setChecked(bool(settings.get("enabled")))
        self.spin_battery_low.setValue(int(settings.get("low_threshold", 20)))
        self.spin_battery_critical.setValue(int(settings.get("critical_threshold", 10)))
        self.spin_battery_cooldown.setValue(int(settings.get("popup_cooldown_minutes", 30)))
        roles = set(settings.get("recipient_roles") or [])
        self.chk_battery_role_it.setChecked("IT_ADMIN" in roles)
        self.chk_battery_role_admin.setChecked("NURSE_ADMIN" in roles)
        self.chk_battery_role_staff.setChecked("NURSE" in roles)
        self.chk_battery_role_verifier.setChecked("VERIFIER" in roles)

    def battery_alert_settings_from_ui(self) -> Dict[str, Any]:
        roles = []
        for checkbox, role in [
            (self.chk_battery_role_it, "IT_ADMIN"),
            (self.chk_battery_role_admin, "NURSE_ADMIN"),
            (self.chk_battery_role_staff, "NURSE"),
            (self.chk_battery_role_verifier, "VERIFIER"),
        ]:
            if checkbox.isChecked():
                roles.append(role)
        return self.normalize_battery_alert_settings({
            "enabled": self.chk_battery_alert_enabled.isChecked(),
            "low_threshold": self.spin_battery_low.value(),
            "critical_threshold": self.spin_battery_critical.value(),
            "popup_cooldown_minutes": self.spin_battery_cooldown.value(),
            "recipient_roles": roles or ["IT_ADMIN"],
        })

    def save_battery_alert_settings_from_ui(self):
        if not self.is_it_admin():
            self.show_error("Permission Required", "Only IT admins can change battery alert policy.")
            return
        settings = self.battery_alert_settings_from_ui()
        busy = self.begin_button_busy(getattr(self, "btn_save_battery_alerts", None), "Saving...")
        try:
            result = self.control_client(timeout=5.0).save_battery_alert_settings(settings) if self.server_mode else {"ok": False}
            if result.get("ok"):
                payload = result.get("data") or {}
                settings = self.normalize_battery_alert_settings(payload.get("settings") or settings)
                message = "Battery alert policy saved to the Raspberry Pi."
                state_color = "#047857"
            else:
                message = result.get("error") or "Battery alert policy saved locally. Connect to the Raspberry Pi to share it."
                state_color = "#b45309"
            self.battery_alert_settings = settings
            self.save_local_battery_alert_settings(settings)
            self.apply_battery_alert_settings_to_ui()
            if hasattr(self, "battery_alert_status"):
                self.battery_alert_status.setText(message)
                self.battery_alert_status.setStyleSheet(f"font-size: 12px; color: {state_color}; font-weight: 800;")
            try:
                self.db.log_it_audit(
                    self.current_user.get("username"),
                    "Battery Alert Policy",
                    "devices",
                    "Success" if result.get("ok") else "Local",
                    message,
                )
            except Exception:
                pass
        finally:
            self.end_button_busy(busy)

    def battery_alert_level(self, device: Dict[str, Any], settings: Dict[str, Any]) -> Optional[str]:
        try:
            percent = int(device.get("battery_level"))
        except Exception:
            percent = None
        if percent is None and not self.truthy(device.get("battery_low")):
            return None
        if percent is not None and percent <= int(settings.get("critical_threshold", 10)):
            return "critical"
        if self.truthy(device.get("battery_low")) or (percent is not None and percent <= int(settings.get("low_threshold", 20))):
            return "low"
        return None

    def check_battery_alerts(self, devices: List[Dict[str, Any]]):
        settings = self.normalize_battery_alert_settings(self.battery_alert_settings)
        if not settings.get("enabled", True) or not self.role_allows_battery_popup():
            return
        cooldown_s = int(settings.get("popup_cooldown_minutes", 30)) * 60
        now_ts = time.time()
        due = []
        for device in devices:
            if not device.get("is_online"):
                continue
            level = self.battery_alert_level(device, settings)
            if not level:
                continue
            device_id = str(device.get("device_id") or device.get("id") or "unknown")
            key = f"{device_id}:{level}"
            last = self.battery_alert_last_popup.get(key, 0)
            if now_ts - last < cooldown_s:
                continue
            self.battery_alert_last_popup[key] = now_ts
            resident = device.get("resident_name") or device.get("paired_resident_name") or "Unassigned"
            due.append((level, device_id, resident, self.battery_display_text(device), self.power_state_text(device)))
        if not due:
            return
        critical = [row for row in due if row[0] == "critical"]
        title = "Critical Battery Alert" if critical else "Low Battery Alert"
        lines = []
        for level, device_id, resident, battery, power in due[:8]:
            lines.append(f"{device_id} | {resident} | {battery} | {power}")
        if len(due) > 8:
            lines.append(f"...and {len(due) - 8} more device(s).")
        QMessageBox.warning(
            self,
            title,
            "One or more connected smart labels need battery attention.\n\n" + "\n".join(lines),
        )

    def control_client_from_form(self, timeout=2.0):
        if not hasattr(self, "control_host"):
            return self.control_client(timeout=timeout)
        return ControlServiceClient(
            self.control_host.text().strip(),
            self.control_port.text().strip(),
            self.control_api_key.text(),
            timeout=timeout,
        )

    def control_profile_from_form(self):
        if not hasattr(self, "control_host"):
            return self.current_control_profile()
        return {
            "profile_name": self.control_profile_name.text().strip() or "Unsaved profile",
            "host": self.control_host.text().strip(),
            "port": self.control_port.text().strip() or 7000,
            "api_key": self.control_api_key.text(),
            "description": self.control_profile_description.text().strip(),
        }

    def control_value(self, data, *keys, default="Pending backend support"):
        current = data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current.get(key)
        if current in (None, ""):
            return default
        return str(current)

    def control_any_value(self, data, *keys, default="Pending backend support"):
        for key in keys:
            value = self.control_value(data, key, default=None)
            if value not in (None, ""):
                return value
        return default

    def control_percent_value(self, data, *keys, default="Pending backend support"):
        for key in keys:
            if not isinstance(data, dict) or key not in data:
                continue
            value = data.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, str) and value.strip().endswith("%"):
                return value.strip()
            try:
                return f"{float(value):.1f}%"
            except (TypeError, ValueError):
                return str(value)
        return default

    def control_ip_value(self, data, *keys, default="Pending backend support"):
        for key in keys:
            if not isinstance(data, dict) or key not in data:
                continue
            value = data.get(key)
            if isinstance(value, list):
                values = [str(item).strip() for item in value if str(item).strip()]
                if values:
                    return ", ".join(values)
            if value not in (None, ""):
                return str(value)
        return default

    def control_status_text(self, result):
        if result.get("ok"):
            return "Connected"
        return friendly_error_message(result.get("error") or "Control Service Offline or Unreachable")

    def load_it_health(self):
        if not hasattr(self, "it_control_stack"):
            return
        if self.it_control_stack.currentIndex() == 0:
            self.refresh_control_dashboard()
            return
        if hasattr(self, "control_profile_combo"):
            self.load_control_profiles()
        self.load_control_devices()
        self.load_control_services()
        self.load_it_recovery_users()
        self.load_it_audit_logs()

    def load_control_dashboard_placeholder(self):
        if not hasattr(self, "control_dashboard_labels"):
            return
        profile = self.current_control_profile()
        if not profile.get("host"):
            self.control_dashboard_labels["service"].setText("Demo Mode - No Raspberry Pi Connected")
        else:
            self.control_dashboard_labels["service"].setText("Not refreshed")
        for key in ["hostname", "lan_ip", "tailscale_ip", "cpu", "memory", "disk", "operation"]:
            self.control_dashboard_labels[key].setText("Pending backend support")
        self.control_dashboard_labels["refreshed"].setText("Not refreshed")
        self.update_control_header()

    def load_control_profiles(self):
        if not hasattr(self, "control_profile_combo"):
            return
        current_id = None
        current = self.control_profile_combo.currentData()
        if current:
            current_id = current.get("id")

        profiles = self.db.list_control_profiles()
        self.control_profile_combo.blockSignals(True)
        self.control_profile_combo.clear()
        selected_index = 0
        for idx, profile in enumerate(profiles):
            suffix = " (active)" if profile.get("is_active") else ""
            self.control_profile_combo.addItem(f"{profile.get('profile_name')}{suffix}", profile)
            if current_id and profile.get("id") == current_id:
                selected_index = idx
            elif not current_id and profile.get("is_active"):
                selected_index = idx
        if profiles:
            self.control_profile_combo.setCurrentIndex(selected_index)
        self.control_profile_combo.blockSignals(False)
        self.on_control_profile_selected()

    def on_control_profile_selected(self):
        if not hasattr(self, "control_profile_combo"):
            return
        profile = self.control_profile_combo.currentData() or self.db.get_active_control_profile() or {}
        if hasattr(self, "control_profile_name"):
            self.control_profile_name.setText(profile.get("profile_name") or "")
            self.control_host.setText(profile.get("host") or "")
            self.control_port.setText(str(profile.get("port") or 7000))
            self.control_api_key.setText(profile.get("api_key") or "")
            self.control_profile_description.setText(profile.get("description") or "")
        self.update_control_url_preview()
        self.update_control_network_labels()

    def new_control_profile(self):
        self.control_profile_combo.setCurrentIndex(-1)
        self.control_profile_name.setText("New Pi Profile")
        self.control_host.clear()
        self.control_port.setText("7000")
        self.control_api_key.clear()
        self.control_profile_description.clear()
        self.update_control_url_preview()

    def update_control_url_preview(self):
        if not hasattr(self, "control_url_preview"):
            return
        host = self.control_host.text().strip() if hasattr(self, "control_host") else ""
        port = self.control_port.text().strip() if hasattr(self, "control_port") else "7000"
        if host and port:
            url = f"http://{host}:{port}"
        else:
            url = "Pending profile configuration"
        self.control_url_preview.setText(f"Control Service URL: {url}")
        if hasattr(self, "control_masked_key"):
            try:
                port_value = int(port or 7000)
            except ValueError:
                port_value = 7000
            client = ControlServiceClient(host, port_value, self.control_api_key.text() if hasattr(self, "control_api_key") else "")
            self.control_masked_key.setText(f"API key: {client.masked_api_key()}")

    def update_control_network_labels(self, result=None, profile=None):
        if not hasattr(self, "control_network_labels"):
            return
        profile = profile or self.current_control_profile()
        host = profile.get("host") or ""
        port = profile.get("port") or 7000
        url = f"http://{host}:{port}" if host else "Pending profile configuration"
        self.control_network_labels["profile"].setText(profile.get("profile_name") or "No profile")
        self.control_network_labels["host"].setText(host or "Not configured")
        self.control_network_labels["port"].setText(str(port))
        self.control_network_labels["url"].setText(url)
        self.control_network_labels["status"].setText(self.control_status_text(result) if result else "Not tested")

        network_data = {}
        health_data = {}
        if result and result.get("ok"):
            health_data = result.get("data") or {}
        if self.control_last_results.get("network", {}).get("ok"):
            network_data = self.control_last_results["network"].get("data") or {}
        self.control_network_labels["hostname"].setText(
            self.control_value(health_data, "hostname", default=self.control_value(network_data, "hostname"))
        )
        self.control_network_labels["lan_ip"].setText(
            self.control_ip_value(network_data, "lan_ips", "lan_ip", "ip")
        )
        self.control_network_labels["tailscale_ip"].setText(
            self.control_value(network_data, "tailscale_ip")
        )

    def save_control_profile(self):
        profile = self.control_profile_combo.currentData() if hasattr(self, "control_profile_combo") else None
        profile_id = profile.get("id") if profile else None
        try:
            saved_id = self.db.save_control_profile(
                profile_id,
                self.control_profile_name.text(),
                self.control_host.text(),
                self.control_port.text(),
                self.control_api_key.text(),
                self.control_profile_description.text(),
                True,
            )
            self.db.log_it_audit(
                self.current_user.get("username"),
                "Control Profile Save",
                self.control_profile_name.text().strip(),
                "Success",
                "Raspberry Pi Control Service profile saved.",
            )
            self.load_control_profiles()
            for idx in range(self.control_profile_combo.count()):
                row = self.control_profile_combo.itemData(idx)
                if row and row.get("id") == saved_id:
                    self.control_profile_combo.setCurrentIndex(idx)
                    break
            self.show_info("Saved", "Raspberry Pi connection profile saved.")
        except Exception as e:
            self.db.log_it_audit(
                self.current_user.get("username"),
                "Control Profile Save",
                self.control_profile_name.text().strip(),
                "Failed",
                str(e),
            )
            self.show_error("Save failed", str(e))

    def copy_control_api_key(self):
        key = self.control_api_key.text() if hasattr(self, "control_api_key") else ""
        if not key:
            self.show_error("No API key", "No Control Service API key is configured for this profile.")
            return
        QGuiApplication.clipboard().setText(key)
        self.show_info("Copied", "API key copied.")

    def test_control_connection(self):
        busy = self.begin_button_busy(getattr(self, "btn_test_control_connection", None), "Testing...")
        client = self.control_client_from_form()
        try:
            result = client.health()
            self.control_last_results["health"] = result
            if result.get("ok"):
                data = result.get("data") or {}
                hostname = data.get("hostname") or "unknown host"
                version = data.get("version") or "version pending"
                self.control_test_status.setText(f"Connection Status: Connected | {hostname} | {version}")
                self.control_test_status.setStyleSheet("font-size: 12px; color: #047857; font-weight: 800;")
                audit_result = "Success"
                message = f"Connected to Control Service at {client.base_url}"
            else:
                self.control_test_status.setText(f"Connection Status: {self.control_status_text(result)}")
                self.control_test_status.setStyleSheet("font-size: 12px; color: #b91c1c; font-weight: 800;")
                audit_result = "Failed"
                message = self.control_status_text(result)
            tested_profile = self.control_profile_from_form()
            self.update_control_network_labels(result, tested_profile)
            self.update_control_header(result, tested_profile)
            self.db.log_it_audit(self.current_user.get("username"), "Connection Test", client.base_url, audit_result, message)
            self.load_it_audit_logs()
        finally:
            self.end_button_busy(busy)

    def update_control_header(self, health_result=None, profile=None):
        if not hasattr(self, "control_header_status"):
            return
        profile = profile or self.current_control_profile()
        if not profile.get("host"):
            self.control_header_status.setText("Demo Mode - No Raspberry Pi Connected")
            self.control_header_status.setStyleSheet("font-size: 13px; color: #64748b; font-weight: 800;")
            return
        if health_result and health_result.get("ok"):
            self.control_header_status.setText(f"Control Service: Connected ({profile.get('profile_name')})")
            self.control_header_status.setStyleSheet("font-size: 13px; color: #047857; font-weight: 800;")
        elif health_result:
            self.control_header_status.setText("Control Service Offline or Unreachable")
            self.control_header_status.setStyleSheet("font-size: 13px; color: #b91c1c; font-weight: 800;")
        else:
            self.control_header_status.setText(f"Control Profile: {profile.get('profile_name')}")
            self.control_header_status.setStyleSheet("font-size: 13px; color: #475569; font-weight: 800;")

    def refresh_control_dashboard(self):
        if not hasattr(self, "control_dashboard_labels"):
            return
        client = self.control_client()
        health = client.health()
        if health.get("ok"):
            results = {
                "health": health,
                "system": client.system_status(),
                "network": client.network_status(),
                "tailscale": client.tailscale_status(),
                "operation": client.operation_status(),
            }
        else:
            skipped = {
                "ok": False,
                "error": health.get("error") or "Control Service Offline or Unreachable",
                "data": {},
            }
            results = {
                "health": health,
                "system": skipped,
                "network": skipped,
                "tailscale": skipped,
                "operation": skipped,
            }
        self.control_last_results.update(results)

        health = results["health"]
        system = results["system"].get("data") if results["system"].get("ok") else {}
        network = results["network"].get("data") if results["network"].get("ok") else {}
        tailscale = results["tailscale"].get("data") if results["tailscale"].get("ok") else {}
        operation = results["operation"].get("data") if results["operation"].get("ok") else {}

        self.control_dashboard_labels["service"].setText(self.control_status_text(health))
        self.control_dashboard_labels["hostname"].setText(
            self.control_value(health.get("data") or {}, "hostname", default=self.control_value(network, "hostname"))
        )
        self.control_dashboard_labels["lan_ip"].setText(self.control_ip_value(network, "lan_ips", "lan_ip", "ip"))
        self.control_dashboard_labels["tailscale_ip"].setText(self.control_value(tailscale, "ip", default=self.control_value(network, "tailscale_ip")))
        self.control_dashboard_labels["cpu"].setText(self.control_percent_value(system, "cpu_percent", "cpu_usage", "cpu"))
        self.control_dashboard_labels["memory"].setText(self.control_percent_value(system, "memory_percent", "memory_usage", "memory"))
        self.control_dashboard_labels["disk"].setText(self.control_percent_value(system, "disk_percent", "disk_usage", "disk"))
        self.control_dashboard_labels["operation"].setText(self.control_value(operation, "status", default=self.control_status_text(results["operation"])))
        self.control_dashboard_labels["refreshed"].setText(time.strftime("%Y-%m-%d %H:%M:%S"))
        self.update_control_network_labels(health)
        self.update_control_header(health)
        self.load_control_services()

    def load_control_services(self):
        if not hasattr(self, "control_service_labels"):
            return
        health = self.control_last_results.get("health") or {"ok": False, "error": "Not refreshed"}
        operation = self.control_last_results.get("operation") or {"ok": False, "error": "Not refreshed"}

        self.control_service_labels["control"].setText(self.control_status_text(health))
        self.control_service_labels["operation"].setText(
            self.control_value(operation.get("data") or {}, "status", default=self.control_status_text(operation))
        )
        self.control_service_labels["version"].setText(self.control_value(health.get("data") or {}, "version"))
        self.control_service_labels["uptime"].setText(self.control_value(health.get("data") or {}, "uptime"))
        self.control_service_labels["last_restart"].setText(
            self.control_value(
                health.get("data") or {},
                "last_restart",
                default=self.control_value(operation.get("data") or {}, "last_restart", default="Not reported"),
            )
        )

    def load_control_devices(self):
        if not hasattr(self, "it_device_table"):
            return
        devices = self.safe_get_devices()
        offline = sum(1 for d in devices if not d.get("is_online"))
        settings = self.normalize_battery_alert_settings(self.battery_alert_settings)
        low_threshold = int(settings.get("low_threshold", 20))
        low_battery = 0
        for d in devices:
            try:
                battery = int(d.get("battery_level"))
            except Exception:
                battery = None
            if (battery is not None and battery <= low_threshold) or self.truthy(d.get("battery_low")):
                low_battery += 1
        if hasattr(self, "control_device_summary"):
            self.control_device_summary["total"].setText(str(len(devices)))
            self.control_device_summary["online"].setText(str(len(devices) - offline))
            self.control_device_summary["offline"].setText(str(offline))
            self.control_device_summary["low_battery"].setText(str(low_battery))
            versions = sorted({str(d.get("fw") or d.get("firmware") or "").strip() for d in devices if str(d.get("fw") or d.get("firmware") or "").strip()})
            self.control_device_summary["firmware"].setText(", ".join(versions) if versions else "Not reported")

        self.it_device_table.setRowCount(len(devices))
        for r, d in enumerate(devices):
            values = [
                d.get("device_id") or "",
                "Online" if d.get("is_online") else "Offline",
                d.get("ip") or "",
                str(d.get("port") or ""),
                d.get("fw") or "",
                self.battery_display_text(d),
                self.power_state_text(d),
                str(d.get("last_seen_s") or ""),
            ]
            for c, value in enumerate(values):
                self.it_device_table.setItem(r, c, QTableWidgetItem(str(value)))
        if hasattr(self, "esp32_pi_host") and not self.esp32_pi_host.text().strip():
            profile = self.current_control_profile()
            self.esp32_pi_host.setText(profile.get("host") or "")
        self.load_it_recovery_users()

    def refresh_esp32_serial_ports(self):
        if not hasattr(self, "esp32_serial_port"):
            return
        busy = self.begin_button_busy(getattr(self, "btn_refresh_esp32_ports", None), "Refreshing...")
        self.set_esp32_wifi_status("Refreshing USB serial ports...", "pending")
        try:
            current = self.esp32_serial_port.currentData()
            self.esp32_serial_port.clear()
            try:
                from serial.tools import list_ports
            except Exception:
                message = "USB serial support is missing from this installation. Install/update the desktop app, then try again."
                self.set_esp32_wifi_status(message, "error")
                self.show_error("USB Ports", message)
                return
            ports = list(list_ports.comports())
            for port in ports:
                label = f"{port.device} - {port.description}"
                self.esp32_serial_port.addItem(label, port.device)
            if current:
                for idx in range(self.esp32_serial_port.count()):
                    if self.esp32_serial_port.itemData(idx) == current:
                        self.esp32_serial_port.setCurrentIndex(idx)
                        break
            count = self.esp32_serial_port.count()
            self.set_esp32_wifi_status(
                f"{count} USB serial port(s) found." if count else "No USB serial ports found. Plug in the ESP32, wait a few seconds, then click Refresh Ports again.",
                "ok" if count else "error",
            )
        finally:
            self.end_button_busy(busy)

    def set_esp32_wifi_status(self, message, state="info"):
        if not hasattr(self, "esp32_wifi_status"):
            return
        colors = {
            "ok": "#047857",
            "error": "#b91c1c",
            "pending": "#b45309",
            "info": "#475569",
        }
        color = colors.get(state, colors["info"])
        self.esp32_wifi_status.setText(message)
        self.esp32_wifi_status.setStyleSheet(f"font-size: 12px; color: {color}; font-weight: 800;")
        QApplication.processEvents()

    def set_esp32_provisioning_busy(self, busy):
        for name in ("btn_refresh_esp32_ports", "btn_scan_esp32_wifi", "btn_apply_esp32_wifi"):
            button = getattr(self, name, None)
            if button:
                button.setEnabled(not busy)
        QApplication.processEvents()

    def _open_esp32_serial(self):
        port = self.esp32_serial_port.currentData() if hasattr(self, "esp32_serial_port") else None
        if not port:
            raise RuntimeError("Select the ESP32 USB / COM port first.")
        try:
            import serial
        except Exception as exc:
            raise RuntimeError("pyserial is not installed. Install/update the desktop app before provisioning ESP32 WiFi.") from exc
        try:
            ser = serial.Serial(port=port, baudrate=115200, timeout=0.45, write_timeout=4)
        except Exception as exc:
            detail = str(exc)
            lowered = detail.lower()
            if "access is denied" in lowered or "permission" in lowered or "permissionerror" in lowered:
                raise RuntimeError(
                    f"{port} is busy or Windows denied access. Close Arduino Serial Monitor, "
                    "Arduino Serial Plotter, PuTTY, Thonny, or any other app using the ESP32 USB port, "
                    "then click Refresh Ports and try again."
                ) from exc
            raise RuntimeError(f"Could not open {port}. Unplug/replug the ESP32, refresh ports, and try again. Details: {detail}") from exc
        try:
            ser.setDTR(False)
            ser.setRTS(False)
        except Exception:
            pass
        return ser

    def _wait_for_esp32_usb_ready(self, ser, timeout_s=8):
        lines = []
        start = time.time()
        while time.time() - start < timeout_s:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode(errors="ignore").strip()
            if not line:
                continue
            lines.append(line)
            if line.startswith(("WWREADY", "WWCFG", "WWERR")):
                break
        return lines

    def _write_esp32_command(self, ser, command):
        data = command.encode("utf-8")
        last_error = None
        for _ in range(3):
            try:
                written = ser.write(data)
                ser.flush()
                if written == len(data):
                    return
                last_error = RuntimeError(f"Only wrote {written}/{len(data)} bytes to ESP32.")
            except Exception as exc:
                last_error = exc
            time.sleep(1.2)
        raise RuntimeError(
            "Windows opened the USB serial port, but the ESP32 did not accept the command. "
            "Flash the latest provisioning firmware or select the correct ESP32 COM port."
        ) from last_error

    def _read_esp32_lines(self, ser, end_markers, timeout_s=12):
        lines = []
        start = time.time()
        markers = tuple(end_markers)
        while time.time() - start < timeout_s:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode(errors="ignore").strip()
            if not line:
                continue
            lines.append(line)
            if markers and any(line.startswith(marker) for marker in markers):
                break
        return lines

    def _esp32_serial_transaction(self, command, end_markers, timeout_s=12, ready_timeout_s=8):
        with self._open_esp32_serial() as ser:
            boot_lines = self._wait_for_esp32_usb_ready(ser, timeout_s=ready_timeout_s)
            ser.reset_input_buffer()
            self._write_esp32_command(ser, command)
            response_lines = self._read_esp32_lines(ser, end_markers, timeout_s=timeout_s)
        return boot_lines + response_lines

    def _serial_value(self, value):
        from urllib.parse import quote
        return quote(str(value or "").strip(), safe="")

    def scan_esp32_wifi_networks(self):
        busy = self.begin_button_busy(getattr(self, "btn_scan_esp32_wifi", None), "Scanning...")
        self.set_esp32_provisioning_busy(True)
        self.set_esp32_wifi_status("Scanning WiFi from ESP32 over USB... keep the board plugged in.", "pending")
        try:
            lines = self._esp32_serial_transaction("WWSCAN\n", ("WWEND", "WWERR"), timeout_s=22, ready_timeout_s=8)
            err_line = next((line for line in lines if line.startswith("WWERR")), "")
            if err_line:
                raise RuntimeError(f"The ESP32 returned an error during WiFi scan. {err_line}")
            networks = []
            for line in lines:
                if not line.startswith("WWSSID "):
                    continue
                marker = " ssid="
                rssi_marker = " rssi="
                ssid = ""
                if marker in line and rssi_marker in line:
                    ssid = line.split(marker, 1)[1].split(rssi_marker, 1)[0].strip()
                if ssid and ssid not in networks:
                    networks.append(ssid)
            self.esp32_wifi_ssid.clear()
            for ssid in networks:
                self.esp32_wifi_ssid.addItem(ssid)
            for idx in range(self.esp32_wifi_ssid.count()):
                if self.esp32_wifi_ssid.itemText(idx).strip().lower() == "bell458":
                    self.esp32_wifi_ssid.setCurrentIndex(idx)
                    break
            detail = " Last: " + lines[-1] if lines else ""
            self.set_esp32_wifi_status(
                f"Found {len(networks)} WiFi network(s).{detail}" if networks else "No WiFi networks returned by ESP32. Confirm the latest ESP32 firmware is flashed.",
                "ok" if networks else "error",
            )
            if not networks:
                self.show_error(
                    "WiFi Scan",
                    "The ESP32 did not return any WiFi networks. Keep it plugged in, confirm the latest ESP32 firmware is flashed, then try Scan WiFi again."
                )
        except Exception as exc:
            message = friendly_error_message(str(exc))
            self.set_esp32_wifi_status(f"WiFi scan failed. {message}", "error")
            self.show_error("WiFi Scan Failed", message)
        finally:
            self.set_esp32_provisioning_busy(False)
            self.end_button_busy(busy)

    def provision_esp32_wifi(self):
        ssid = self.esp32_wifi_ssid.currentText().strip() if hasattr(self, "esp32_wifi_ssid") else ""
        password = self.esp32_wifi_password.text() if hasattr(self, "esp32_wifi_password") else ""
        pi_host = self.esp32_pi_host.text().strip() if hasattr(self, "esp32_pi_host") else ""
        pi_port = self.esp32_pi_port.text().strip() if hasattr(self, "esp32_pi_port") else "5000"
        if not ssid:
            self.show_error("Missing WiFi", "Enter or select the WiFi network name.")
            return
        if not pi_host:
            self.show_error("Missing Pi Host", "Enter the Raspberry Pi LAN IP or hostname for the ESP32 to connect to.")
            return
        try:
            port_num = int(pi_port)
            if port_num < 1 or port_num > 65535:
                raise ValueError
        except Exception:
            self.show_error("Invalid Port", "ESP32 TCP port must be a number from 1 to 65535.")
            return
        command = (
            f"WWSET ssid={self._serial_value(ssid)} "
            f"pass={self._serial_value(password)} "
            f"pi={self._serial_value(pi_host)} "
            f"port={port_num}\n"
        )
        busy = self.begin_button_busy(getattr(self, "btn_apply_esp32_wifi", None), "Saving...")
        self.set_esp32_provisioning_busy(True)
        self.set_esp32_wifi_status(f"Saving WiFi '{ssid}' to ESP32 and waiting for acknowledgement...", "pending")
        try:
            lines = self._esp32_serial_transaction(command, ("WWOK", "WWERR"), timeout_s=16, ready_timeout_s=8)
            ok = any(line.startswith("WWOK") for line in lines)
            detail = " | ".join(lines[-4:]) if lines else "No response from ESP32."
            if ok:
                self.set_esp32_wifi_status(f"WiFi saved to ESP32. Reconnecting to {ssid} and Pi {pi_host}:{port_num}.", "ok")
                self.db.log_it_audit(self.current_user.get("username"), "ESP32 WiFi Provision", ssid, "Success", f"Pi host {pi_host}:{port_num}")
                self.show_info("ESP32 WiFi", "WiFi settings saved to the ESP32. It will reconnect using the new network without needing a restart.")
            else:
                message = f"The ESP32 did not confirm the WiFi save. Last response: {detail}"
                self.set_esp32_wifi_status(message, "error")
                self.show_error("ESP32 WiFi", message)
        except Exception as exc:
            message = friendly_error_message(str(exc))
            self.set_esp32_wifi_status(f"Provision failed. {message}", "error")
            self.db.log_it_audit(self.current_user.get("username"), "ESP32 WiFi Provision", ssid, "Failed", message)
            self.show_error("ESP32 WiFi Failed", message)
        finally:
            self.set_esp32_provisioning_busy(False)
            self.end_button_busy(busy)

    def restart_operation_manager(self):
        busy = self.begin_button_busy(getattr(self, "btn_restart_operation", None), "Restarting...")
        client = self.control_client()
        try:
            result = client.restart_operation()
            audit_result = "Success" if result.get("ok") else "Failed"
            message = "Operation Manager restart requested." if result.get("ok") else self.control_status_text(result)
            self.db.log_it_audit(self.current_user.get("username"), "Restart Operation Manager", client.base_url, audit_result, message)
            self.load_it_audit_logs()
            self.control_last_results["operation"] = client.operation_status()
            self.load_control_services()
            if result.get("ok"):
                self.show_info("Restart requested", "Operation Manager restart request was sent to the Raspberry Pi Control Service.")
            else:
                self.show_error("Restart failed", message)
        finally:
            self.end_button_busy(busy)

    def load_it_audit_logs(self):
        if not hasattr(self, "it_audit_table"):
            return
        rows = self.db.get_it_audit_logs(limit=100)
        self.it_audit_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = [
                self.db.format_timestamp(row.get("created_at")),
                row.get("username") or "",
                row.get("action") or "",
                row.get("target") or "",
                row.get("result") or "",
                row.get("message") or "",
            ]
            for c, value in enumerate(values):
                self.it_audit_table.setItem(r, c, QTableWidgetItem(str(value)))

    def load_it_recovery_users(self):
        if not hasattr(self, "it_recovery_user"):
            return
        current_id = self.it_recovery_user.currentData()
        self.it_recovery_user.blockSignals(True)
        self.it_recovery_user.clear()
        auth = AuthService()
        try:
            users = auth.list_users()
        finally:
            auth.close()
        users = [user for user in users if user.get("username")]
        for user in users:
            active = bool(user.get("active"))
            must_change = "must change" if user.get("password_must_change") else "password ok"
            label = f"{user.get('username')} | {self.role_label(user.get('role'))} | {'active' if active else 'inactive'} | {must_change}"
            self.it_recovery_user.addItem(label, user)
            if current_id and user.get("id") == current_id.get("id"):
                self.it_recovery_user.setCurrentIndex(self.it_recovery_user.count() - 1)
        self.it_recovery_user.blockSignals(False)

    def generate_temporary_password(self):
        alphabet = string.ascii_letters + string.digits
        return "Wv-" + "".join(secrets.choice(alphabet) for _ in range(13))

    def issue_temporary_password(self):
        if not self.is_it_admin():
            self.show_error("Permission Required", "Only IT admins can issue temporary passwords.")
            return
        user = self.it_recovery_user.currentData()
        if not user:
            self.show_error("No user", "Select an active user account.")
            return
        temporary_password = self.generate_temporary_password()
        auth = AuthService()
        try:
            temporary_password = auth.set_temporary_password(user.get("id"), temporary_password, user.get("username"))
        except Exception as e:
            self.it_recovery_status.setStyleSheet("font-size: 12px; color: #b91c1c;")
            self.it_recovery_status.setText(str(e))
            return
        finally:
            auth.close()
        self.it_temp_password.setText(temporary_password)
        self.it_recovery_status.setStyleSheet("font-size: 12px; color: #047857;")
        self.it_recovery_status.setText(f"Temporary password issued for {user.get('username')}. User must change it after login.")
        self.db.log_update(
            "temporary_password_issued",
            None,
            None,
            None,
            self.current_user.get("id"),
            self.current_user.get("username"),
            {"target_user": user.get("username"), "target_role": user.get("role")},
            {"password_must_change": True},
            True,
            "IT admin issued temporary password",
        )
        self.db.log_it_audit(
            self.current_user.get("username"),
            "Temporary Password Issued",
            user.get("username"),
            "Success",
            "Temporary password issued; user must change it after login.",
        )
        self.load_it_recovery_users()
        self.load_it_audit_logs()

    def copy_temporary_password(self):
        value = self.it_temp_password.text().strip()
        if not value:
            self.show_error("No password", "Generate a temporary password first.")
            return
        QGuiApplication.clipboard().setText(value)
        self.show_info("Copied", "Temporary password copied.")

    def generate_debug_brief(self):
        self.refresh_control_dashboard()
        profile = self.current_control_profile()
        devices = self.safe_get_devices()
        audit_logs = self.db.get_it_audit_logs(limit=10)
        offline = [d for d in devices if not d.get("is_online")]
        health = self.control_last_results.get("health", {})
        operation = self.control_last_results.get("operation", {})

        diagnostics = {
            "application": APP_NAME,
            "user_role": self.role_label(),
            "control_profile": {
                "name": profile.get("profile_name"),
                "host": profile.get("host"),
                "port": profile.get("port"),
                "api_key": "configured" if profile.get("api_key") else "missing",
            },
            "control_service": self.control_last_results,
            "legacy_gateway_url": self.base_url(),
            "legacy_gateway_state": "connected" if self.gateway_online else "offline",
            "database_mode": self.db.backend or "unknown",
            "known_devices": len(devices),
            "offline_devices": len(offline),
            "recent_it_audit": audit_logs,
        }
        if hasattr(self, "ai_diag_input"):
            self.ai_diag_input.setPlainText(json.dumps(diagnostics, indent=2, default=str))

        if not profile.get("host"):
            summary = "Demo Mode - no Raspberry Pi Control Service host is configured."
            cause = "The active connection profile does not have a host."
            fixes = [
                "Confirm this deployment build includes the site Raspberry Pi address.",
                "Confirm the Control Service port is 7000.",
                "Ask IT to verify the Control Service is listening on the configured host and port.",
            ]
        elif not profile.get("api_key"):
            summary = "Control Service profile is missing an API key."
            cause = "Protected requests require X-Whisperwood-Key."
            fixes = [
                "Confirm this deployment build includes the Control Service API key.",
                "Ask IT to verify the deployed API key matches the Control Service configuration.",
            ]
        elif health.get("ok"):
            op_status = self.control_value(operation.get("data") or {}, "status", default="operation status pending")
            summary = f"Control Service is reachable. Operation Manager: {op_status}."
            cause = "No connection fault detected from the currently implemented Control Service endpoints."
            fixes = [
                "Review Services for Operation Manager state.",
                "Use Restart Operation Manager only when a human approves the action.",
                "Check Devices once Operation Manager ESP32 registry integration is available.",
            ]
        else:
            summary = "Control Service Offline or Unreachable."
            cause = health.get("error") or "The Pi Control Service did not respond successfully."
            fixes = [
                "Verify the desktop and Raspberry Pi are on the same facility network.",
                "Confirm the configured host resolves to the Raspberry Pi.",
                "Confirm port 7000 is reachable and the Control Service is running.",
                "Use Tailscale only for remote support, diagnostics, maintenance, or recovery.",
            ]

        self.ai_summary_text.setPlainText(summary)
        self.ai_cause_text.setPlainText(cause)
        self.ai_fixes_text.setPlainText("\n".join(f"- {fix}" for fix in fixes))
        self.db.log_it_audit(
            self.current_user.get("username"),
            "Collect Diagnostics",
            profile.get("profile_name") or "No profile",
            "Success",
            "AI debug diagnostics collected. Recommendations only; no automatic service action performed.",
        )
        self.load_it_audit_logs()

    def copy_debug_brief(self):
        if hasattr(self, "ai_diag_input"):
            text = "\n\n".join([
                "Diagnostics Data:",
                self.ai_diag_input.toPlainText(),
                "AI Summary:",
                self.ai_summary_text.toPlainText(),
                "Likely Cause:",
                self.ai_cause_text.toPlainText(),
                "Recommended Fixes:",
                self.ai_fixes_text.toPlainText(),
            ])
        else:
            text = self.debug_brief.toPlainText()
        QGuiApplication.clipboard().setText(text)
        self.show_info("Copied", "Diagnostics copied.")

    def handle_logout(self):
        answer = QMessageBox.question(
            self,
            "Logout",
            "Logout and return to the login screen?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.logout_requested.emit()

    def show_required_password_change(self):
        changed = self.show_change_password_dialog(force=True)
        if not changed:
            self.show_info("Password change required", "A temporary password must be changed before using the dashboard.")
            self.logout_requested.emit()

    def show_force_password_change_warning(self):
        self.show_info(
            "Temporary password",
            "The Control Service marked this account for password change. Create a new password before continuing.",
        )

    def show_change_password_dialog(self, force=False):
        dialog = QDialog(self)
        dialog.setWindowTitle("Change Password")
        dialog.resize(460, 300)
        dialog.setStyleSheet("QDialog { background-color: #f3f7fb; color: #0f172a; }")
        layout = QVBoxLayout(dialog)

        title = QLabel("Change your password", dialog)
        title.setStyleSheet("font-size: 18px; font-weight: 800; color: #0f172a;")
        layout.addWidget(title)

        guidance_text = "You signed in with a temporary password. Create a new password before continuing." if force else "Update the password for your signed-in account."
        guidance = QLabel(guidance_text, dialog)
        guidance.setWordWrap(True)
        guidance.setStyleSheet("font-size: 12px; color: #64748b;")
        layout.addWidget(guidance)

        current_password = QLineEdit(dialog)
        current_password.setPlaceholderText("Current password")
        current_password.setEchoMode(QLineEdit.EchoMode.Password)
        current_password.setStyleSheet(self.input_style())
        layout.addWidget(current_password)

        new_password = QLineEdit(dialog)
        new_password.setPlaceholderText("New password, at least 8 characters")
        new_password.setEchoMode(QLineEdit.EchoMode.Password)
        new_password.setStyleSheet(self.input_style())
        layout.addWidget(new_password)

        confirm_password = QLineEdit(dialog)
        confirm_password.setPlaceholderText("Confirm new password")
        confirm_password.setEchoMode(QLineEdit.EchoMode.Password)
        confirm_password.setStyleSheet(self.input_style())
        layout.addWidget(confirm_password)

        status = QLabel("", dialog)
        status.setWordWrap(True)
        status.setStyleSheet("font-size: 12px; color: #b91c1c;")
        layout.addWidget(status)

        buttons = QHBoxLayout()
        save_btn = QPushButton("Save Password", dialog)
        save_btn.setStyleSheet(self.primary_btn_style())
        buttons.addWidget(save_btn)
        cancel_btn = QPushButton("Logout" if force else "Cancel", dialog)
        cancel_btn.setStyleSheet(self.secondary_btn_style())
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

        changed = {"ok": False}

        def save_password():
            if new_password.text() != confirm_password.text():
                status.setText("New password and confirmation do not match.")
                return
            auth = AuthService()
            try:
                auth.change_password(
                    self.current_user.get("id"),
                    current_password.text(),
                    new_password.text(),
                    self.current_user.get("username"),
                )
            except Exception as e:
                status.setText(str(e))
                return
            finally:
                auth.close()
            self.current_user["password_must_change"] = False
            changed["ok"] = True
            dialog.accept()

        save_btn.clicked.connect(save_password)
        cancel_btn.clicked.connect(dialog.reject)
        dialog.exec()
        return changed["ok"]

    def show_account_profile(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Profile")
        dialog.resize(520, 360)
        dialog.setStyleSheet("QDialog { background-color: #f3f7fb; color: #0f172a; }")
        layout = QVBoxLayout(dialog)

        title = QLabel("Profile", dialog)
        title.setStyleSheet("font-size: 18px; font-weight: 800; color: #0f172a;")
        layout.addWidget(title)

        guidance = QLabel(
            "Manage your own account password. Username changes require a Control Service username-change endpoint before they can be safely enabled.",
            dialog,
        )
        guidance.setWordWrap(True)
        guidance.setStyleSheet("font-size: 12px; color: #64748b;")
        layout.addWidget(guidance)

        username_edit = QLineEdit(dialog)
        username_edit.setText(self.current_user.get("username") or "")
        username_edit.setPlaceholderText("Username")
        username_edit.setReadOnly(True)
        username_edit.setStyleSheet(self.input_style())
        layout.addWidget(username_edit)

        role_label = QLabel(f"Role: {self.role_label()}", dialog)
        role_label.setStyleSheet("font-size: 13px; color: #334155;")
        layout.addWidget(role_label)

        username_note = QLabel("Username editing is disabled until backend username-change support is available.", dialog)
        username_note.setWordWrap(True)
        username_note.setStyleSheet("font-size: 12px; color: #b45309;")
        layout.addWidget(username_note)

        buttons = QHBoxLayout()
        change_password_btn = QPushButton("Change Password", dialog)
        change_password_btn.setStyleSheet(self.primary_btn_style())
        close_btn = QPushButton("Close", dialog)
        close_btn.setStyleSheet(self.secondary_btn_style())
        buttons.addWidget(change_password_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        change_password_btn.clicked.connect(lambda: self.show_change_password_dialog(force=False))
        close_btn.clicked.connect(dialog.accept)
        dialog.exec()

    def show_profile_settings(self):
        if not self.is_it_admin():
            self.show_error("Permission Required", "Only IT admins can open Settings.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Settings")
        dialog.resize(880, 680)
        dialog.setStyleSheet("QDialog { background-color: #f3f7fb; color: #0f172a; }")
        layout = QVBoxLayout(dialog)

        header = QLabel(
            f"Signed in as {self.current_user.get('username', 'admin')} | {self.role_label()}\n"
            "IT settings manage system users and your own password."
        )
        header.setWordWrap(True)
        header.setStyleSheet("font-size: 13px; color: #334155; background: transparent; border: none;")
        layout.addWidget(header)

        password_panel = QFrame(dialog)
        password_panel.setStyleSheet("QFrame { background-color: #ffffff; border: 1px solid #d8e1ea; border-radius: 8px; }")
        password_layout = QHBoxLayout(password_panel)
        password_layout.setContentsMargins(12, 12, 12, 12)
        password_text = QLabel("Account security: change your password here. Temporary password recovery is handled from this IT area.", password_panel)
        password_text.setWordWrap(True)
        password_text.setStyleSheet("font-size: 12px; color: #334155;")
        password_layout.addWidget(password_text)
        change_password_btn = QPushButton("Change My Password", password_panel)
        change_password_btn.setStyleSheet(self.primary_btn_style())
        password_layout.addWidget(change_password_btn)
        layout.addWidget(password_panel)

        users_table = QTableWidget(dialog)
        users_table.setColumnCount(5)
        users_table.setHorizontalHeaderLabels(["Username", "Role", "Active", "Must Change Password", "Created"])
        users_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        users_table.verticalHeader().setVisible(False)
        users_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        users_table.setStyleSheet(self.table_style())
        layout.addWidget(users_table)

        status_panel = QFrame(dialog)
        status_panel.setStyleSheet("QFrame { background-color: #ffffff; border: 1px solid #d8e1ea; border-radius: 8px; }")
        status_layout = QHBoxLayout(status_panel)
        status_layout.setContentsMargins(12, 12, 12, 12)
        status_text = QLabel("Select a user to activate or deactivate access.", status_panel)
        status_text.setWordWrap(True)
        status_text.setStyleSheet("font-size: 12px; color: #334155;")
        status_layout.addWidget(status_text)
        activate_btn = QPushButton("Activate User", status_panel)
        activate_btn.setStyleSheet(self.secondary_btn_style())
        deactivate_btn = QPushButton("Deactivate User", status_panel)
        deactivate_btn.setStyleSheet(self.secondary_btn_style())
        status_layout.addWidget(activate_btn)
        status_layout.addWidget(deactivate_btn)
        layout.addWidget(status_panel)

        def load_users():
            auth = AuthService()
            try:
                rows = auth.list_users()
            finally:
                auth.close()
            rows = [row for row in rows if row.get("username")]
            users_table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                values = [
                    row.get("username") or "",
                    self.role_label(row.get("role")),
                    "Yes" if row.get("active") else "No",
                    "Yes" if row.get("password_must_change") else "No",
                    self.db.format_timestamp(row.get("created_at")),
                ]
                for c, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    item.setData(Qt.ItemDataRole.UserRole, row)
                    users_table.setItem(r, c, item)

        load_users()

        def selected_settings_user():
            item = users_table.item(users_table.currentRow(), 0)
            return item.data(Qt.ItemDataRole.UserRole) if item else None

        def set_selected_user_status(active):
            user = selected_settings_user()
            if not user:
                self.show_error("No user selected", "Select a user account first.")
                return
            username = user.get("username")
            if username == self.current_user.get("username") and not active:
                self.show_error("Not allowed", "You cannot deactivate the account you are currently using.")
                return
            if not active:
                answer = QMessageBox.question(
                    self,
                    "Deactivate user",
                    f"Deactivate {username}? This removes login access but keeps audit history.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            auth = AuthService()
            try:
                auth.set_user_status(username, active)
            except Exception as e:
                self.show_error("User status failed", str(e))
            finally:
                auth.close()
            load_users()

        create_panel = QFrame(dialog)
        create_panel.setStyleSheet("QFrame { background-color: #ffffff; border: 1px solid #d8e1ea; border-radius: 8px; }")
        create_layout = QHBoxLayout(create_panel)
        create_layout.setContentsMargins(12, 12, 12, 12)

        username_edit = QLineEdit(create_panel)
        username_edit.setPlaceholderText("New username")
        username_edit.setStyleSheet(self.input_style())
        create_layout.addWidget(username_edit)

        password_edit = QLineEdit(create_panel)
        password_edit.setPlaceholderText("Temporary password")
        password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        password_edit.setStyleSheet(self.input_style())
        create_layout.addWidget(password_edit)

        role_combo = QComboBox(create_panel)
        for role in ["NURSE_ADMIN", "IT_ADMIN", "NURSE", "VERIFIER"]:
            role_combo.addItem(self.role_label(role), role)
        role_combo.setStyleSheet(self.input_style())
        create_layout.addWidget(role_combo)

        create_btn = QPushButton("Create User", create_panel)
        create_btn.setStyleSheet(self.primary_btn_style())
        create_layout.addWidget(create_btn)
        layout.addWidget(create_panel)

        def create_user():
            if not self.is_it_admin():
                self.show_error("Permission Required", "Only IT admins can create users.")
                return
            auth = AuthService()
            try:
                auth.create_user(username_edit.text(), password_edit.text(), role_combo.currentData())
            except Exception as e:
                self.show_error("Create user failed", str(e))
            finally:
                auth.close()
            username_edit.clear()
            password_edit.clear()
            load_users()

        create_btn.clicked.connect(create_user)
        deactivate_btn.setText("Delete / Deactivate User")
        create_panel.setVisible(self.is_it_admin())
        status_panel.setVisible(self.is_it_admin())
        activate_btn.clicked.connect(lambda: set_selected_user_status(True))
        deactivate_btn.clicked.connect(lambda: set_selected_user_status(False))
        change_password_btn.clicked.connect(lambda: self.show_change_password_dialog(force=False))

        close_btn = QPushButton("Close", dialog)
        close_btn.setStyleSheet(self.primary_btn_style())
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)
        dialog.exec()

    def apply_resident_row_to_form(self, row):
        if not row:
            return
        self.selected_resident_id = row.get("id")
        self.txt_uid.setText(row["resident_uid"] or "")
        self.txt_name.setText(row["full_name"] or "")
        self.txt_room.setText(row.get("room") or "")
        status_alert = row.get("status_alert") or row.get("status") or "Stable"
        alert_index = self.cmb_alert.findText(str(status_alert), Qt.MatchFlag.MatchFixedString)
        if alert_index < 0:
            alert_index = self.cmb_alert.findText(str(status_alert).title(), Qt.MatchFlag.MatchFixedString)
        self.cmb_alert.setCurrentIndex(alert_index if alert_index >= 0 else 0)
        self.set_field_text(self.txt_diet, row.get("diet") or "")
        self.set_field_text(self.txt_allergies, row.get("texture") or row.get("allergies") or "")
        self.txt_note.setPlainText(row.get("note") or "")
        self.txt_drinks.setText(row.get("drinks") or "")
        self.set_field_text(self.txt_schedule, row.get("fluids") or row.get("schedule") or "")
        self.selected_source_document = row.get("source_document") or None
        self.source_doc_label.setText(os.path.basename(self.selected_source_document) if self.selected_source_document else "No source document attached")
        self.chk_safety_review.setChecked(bool(row.get("needs_safety_review", False)))
        self.set_resident_photo_path(row.get("lcd_image_path") or None)
        self.chk_active.setChecked(bool(row.get("active", True)))

        self.update_preview()

    def on_resident_selected(self, item):
        resident_id = item.data(Qt.ItemDataRole.UserRole)
        row = self.db.get_resident(resident_id)
        if not row:
            return

        self.apply_resident_row_to_form(row)
        self.load_update_targets()
        if row.get("paired_device_id"):
            idx = self.upd_target.findData(row.get("paired_device_id"))
            if idx >= 0:
                self.upd_target.setCurrentIndex(idx)

    def on_pair_resident_selected(self, item):
        resident_id = item.data(Qt.ItemDataRole.UserRole)
        self.selected_pair_resident_id = resident_id
        row = self.db.get_resident(resident_id)
        if row:
            self.pair_info.setText(f"Selected resident:\n{row['full_name']} ({row['resident_uid']})")

    def on_pair_device_selected(self, item):
        self.selected_pair_device_id = item.data(Qt.ItemDataRole.UserRole)

    def save_resident(self):
        if not self.can_edit_residents():
            self.show_error("Permission Required", "Only an admin can save approved resident information.")
            return
        payload = self.collect_resident_payload()

        if not payload["resident_uid"]:
            payload["resident_uid"] = generate_resident_uid()
            self.txt_uid.setText(payload["resident_uid"])

        if not payload["full_name"]:
            self.show_error("Missing name", "Resident name is required.")
            return

        busy = self.begin_button_busy(getattr(self, "btn_save_resident", None), "Saving...")
        try:
            before_snapshot = None
            if self.selected_resident_id is None:
                self.selected_resident_id = self.db.create_resident(payload)
                action = "resident_create"
                message = "Resident created successfully"
            else:
                before_snapshot = self.resident_audit_snapshot(self.db.get_resident(self.selected_resident_id))
                self.db.update_resident(self.selected_resident_id, payload)
                action = "resident_update"
                message = "Resident updated successfully"

            after_snapshot = self.resident_audit_snapshot(payload)
            self.db.log_update(
                action,
                self.selected_resident_id,
                payload["resident_uid"],
                None,
                self.current_user.get("id"),
                self.current_user.get("username"),
                {"before": before_snapshot, "after": after_snapshot},
                {"saved": True, "source_document": payload.get("source_document")},
                True,
                message
            )

            self.load_residents()
            self.load_recent_logs()
            self.load_pairing_views()
            self.load_verification_page()
            self.load_approvals()
            self.load_resident_audit()
            self.refresh_dashboard_summary()
            queued = self.send_saved_resident_if_paired()
            if queued:
                self.show_info("Saved", f"{message}\n\nText update started in the background. Send the resident photo separately from LCD Schedule.")
            else:
                self.show_info("Saved", message)

        except Exception as e:
            self.show_error("Save failed", str(e))
        finally:
            self.end_button_busy(busy)

    def submit_resident_review_request(self):
        if not self.is_nurse():
            self.show_error("Permission Required", "Only staff users submit resident review requests from this view.")
            return
        if self.selected_resident_id is None:
            self.show_error("No resident", "Select a resident before sending a review note.")
            return
        comment = self.nurse_review_comment.toPlainText().strip()
        if not comment:
            self.show_error("Missing review note", "Write the observation or correction request before sending.")
            return

        row = self.db.get_resident(self.selected_resident_id)
        if not row:
            self.show_error("Not found", "Resident record was not found.")
            return

        payload = self.collect_resident_payload()
        busy = self.begin_button_busy(getattr(self, "btn_submit_review_request", None), "Submitting...")
        try:
            self.db.create_change_request(
                self.selected_resident_id,
                row.get("resident_uid"),
                payload,
                comment,
                self.current_user.get("id"),
                self.current_user.get("username"),
            )
            self.db.log_update(
                "resident_review_request",
                self.selected_resident_id,
                row.get("resident_uid"),
                row.get("paired_device_id"),
                self.current_user.get("id"),
                self.current_user.get("username"),
                {
                    "comment": comment,
                    "before": self.resident_audit_snapshot(row),
                    "after": self.resident_audit_snapshot(payload),
                },
                {"status": "PENDING"},
                True,
                "Resident review request submitted",
            )
            self.nurse_review_comment.clear()
            self.load_approvals()
            self.load_resident_audit()
            self.load_recent_logs()
            self.refresh_dashboard_summary()
            self.show_info("Submitted", "Review request sent to the admin queue.")
        except Exception as e:
            self.show_error("Submit failed", str(e))
        finally:
            self.end_button_busy(busy)

    def delete_selected_resident(self):
        if not self.can_edit_residents():
            self.show_error("Permission Required", "Only an admin can delete resident information.")
            return
        if self.selected_resident_id is None:
            self.show_error("No resident", "Select a resident to delete.")
            return

        row = self.db.get_resident(self.selected_resident_id)
        if not row:
            self.show_error("Not found", "Resident record was not found.")
            return

        answer = QMessageBox.question(
            self,
            "Delete resident",
            f"Delete {row.get('full_name', 'this resident')} permanently?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        resident_id = self.selected_resident_id
        resident_uid = row.get("resident_uid")
        busy = self.begin_button_busy(getattr(self, "btn_delete_resident", None), "Deleting...")
        try:
            self.db.delete_resident(resident_id)
            self.db.log_update(
                "resident_delete",
                None,
                resident_uid,
                row.get("paired_device_id"),
                self.current_user.get("id"),
                self.current_user.get("username"),
                {
                    "resident_id": resident_id,
                    "resident_uid": resident_uid,
                    "before": self.resident_audit_snapshot(row),
                },
                {"deleted": True, "after": None},
                True,
                f"Resident {row.get('full_name', resident_uid)} deleted",
            )
            self.selected_resident_id = None
            self.selected_pair_resident_id = None
            self.new_resident()
            self.load_residents()
            self.load_pairing_views()
            self.load_schedule_view()
            self.load_verification_page()
            self.load_approvals()
            self.load_resident_audit()
            self.load_recent_logs()
            self.refresh_dashboard_summary()
            self.show_info("Deleted", "Resident deleted successfully.")
        except Exception as e:
            self.show_error("Delete failed", str(e))
        finally:
            self.end_button_busy(busy)

    def find_paired_device_id_for_resident(self, row):
        row = dict(row or {})
        direct = row.get("paired_device_id") or row.get("device_id")
        if direct:
            return str(direct)

        resident_id = row.get("id") or self.selected_resident_id
        resident_uid = row.get("resident_uid")
        resident_name = row.get("full_name")
        for device in self.safe_get_devices():
            device_id = device.get("device_id") or device.get("id")
            if not device_id:
                continue
            paired_id = device.get("paired_resident_id") or device.get("resident_id")
            if resident_id is not None and str(paired_id or "") == str(resident_id):
                return str(device_id)
            if resident_uid and str(device.get("resident_uid") or "") == str(resident_uid):
                return str(device_id)
            if resident_name and str(device.get("resident_name") or "") == str(resident_name):
                return str(device_id)
        return None

    def build_gateway_payload_from_row(self, row, device_id):
        row = dict(row or {})
        payload = {
            "id": device_id,
            "name": row.get("full_name") or "",
            "room": row.get("room") or "",
            "note": row.get("note") or "",
            "drinks": row.get("drinks") or "",
        }
        diet = row.get("diet")
        if diet:
            payload["diet"] = [x.strip() for x in str(diet).split(",") if x.strip()]
        texture = row.get("texture") or row.get("allergies")
        if texture:
            texture_items = [x.strip() for x in str(texture).split(",") if x.strip()]
            payload["texture"] = texture_items
            payload["allergies"] = texture_items
        fluids = row.get("fluids") or row.get("schedule")
        if fluids:
            payload["fluids"] = str(fluids)
            payload["schedule"] = str(fluids)
        return payload

    def send_saved_resident_if_paired(self):
        if not self.gateway_online:
            return False
        row = self.db.get_resident(self.selected_resident_id)
        device_id = self.find_paired_device_id_for_resident(row)
        if not device_id:
            return False

        payload = self.build_gateway_payload_from_row(row, device_id)
        self.queue_resident_display_sequence(
            row,
            device_id,
            payload,
            "Saved resident text sent to paired device",
            "Auto-send",
            image_path=None,
            action_type="auto_send_after_save",
            notify_on_failure=True,
            include_image=False,
        )
        return True

    # ---------------------------- devices / pairing ----------------------------

    def refresh_devices(self):
        try:
            devices = self.gateway.get_devices(self.base_url())
            self.db.upsert_devices(devices)
            self.set_gateway_state(True)
            self.check_battery_alerts(self.safe_get_devices())
        except Exception as e:
            self.set_gateway_state(False)
            try:
                self.db.log_update(
                    "device_refresh",
                    self.selected_resident_id,
                    self.current_resident_uid(),
                    None,
                    self.current_user.get("id"),
                    self.current_user.get("username"),
                    {"base_url": self.base_url()},
                    {"error": str(e)},
                    False,
                    f"Failed to refresh devices: {e}"
                )
            except Exception:
                pass
            self.load_recent_logs()
            self.refresh_dashboard_summary()
            self.load_schedule_view()
            self.load_it_health()
            return

        self.load_update_targets()
        self.load_pairing_views()
        self.load_residents()
        self.load_recent_logs()
        self.refresh_dashboard_summary()
        self.load_schedule_view()
        self.load_it_health()

    def load_update_targets(self):
        current_device = self.selected_device_id()
        self.upd_target.blockSignals(True)
        self.upd_target.clear()
        for d in self.safe_get_devices():
            label = str(d["device_id"])
            if d.get("resident_name"):
                label += f" | {d['resident_name']}"
            self.upd_target.addItem(label, d["device_id"])
        if current_device is not None:
            idx = self.upd_target.findData(current_device)
            if idx >= 0:
                self.upd_target.setCurrentIndex(idx)
        self.upd_target.blockSignals(False)

    def resident_for_device(self, device_id):
        if not device_id:
            return None
        devices = self.safe_get_devices()
        device = next((d for d in devices if str(d.get("device_id")) == str(device_id)), None)
        if not device:
            return None

        resident_id = device.get("paired_resident_id") or device.get("resident_id")
        if resident_id:
            row = self.db.get_resident(resident_id)
            if row:
                return row

        residents = self.db.get_residents()
        resident_uid = device.get("resident_uid")
        resident_name = device.get("resident_name")
        return next(
            (
                row for row in residents
                if str(row.get("paired_device_id") or "") == str(device_id)
                or (resident_uid and str(row.get("resident_uid") or "") == str(resident_uid))
                or (resident_name and str(row.get("full_name") or "") == str(resident_name))
            ),
            None,
        )

    def on_update_target_changed(self):
        if not hasattr(self, "upd_target"):
            return
        row = self.resident_for_device(self.selected_device_id())
        if row:
            self.apply_resident_row_to_form(row)
        else:
            self.sync_resident_photo_labels()
            self.update_lcd_image_preview()

    def load_pairing_views(self):
        resident_id = self.selected_pair_resident_id
        device_id = self.selected_pair_device_id
        if self.pair_resident_list.currentItem():
            resident_id = self.pair_resident_list.currentItem().data(Qt.ItemDataRole.UserRole)
        if self.available_devices_list.currentItem():
            device_id = self.available_devices_list.currentItem().data(Qt.ItemDataRole.UserRole)

        self.available_devices_list.clear()
        devices = self.safe_get_devices()

        self.pair_table.setRowCount(len(devices))
        for r, d in enumerate(devices):
            vals = [
                d["device_id"],
                d.get("resident_name") or "Unpaired",
                d.get("resident_uid") or "-",
                "Online" if d.get("is_online") else "Offline",
                self.battery_display_text(d, compact=True)
            ]
            for c, val in enumerate(vals):
                self.pair_table.setItem(r, c, QTableWidgetItem(val))

            icon = "online" if d["is_online"] else "offline"
            status = "paired" if d.get("resident_name") else "available"
            item = QListWidgetItem(f"{d['device_id']} | {icon} | {status}")
            item.setData(Qt.ItemDataRole.UserRole, d["device_id"])
            self.available_devices_list.addItem(item)
            if device_id is not None and d["device_id"] == device_id:
                self.available_devices_list.setCurrentRow(self.available_devices_list.count() - 1)

        self.selected_pair_resident_id = resident_id
        self.selected_pair_device_id = device_id

    def pair_selected_from_menu(self):
        if not self.require_network_for_write("Pairing resident to device"):
            return
        resident_item = self.pair_resident_list.currentItem()
        device_item = self.available_devices_list.currentItem()

        resident_id = resident_item.data(Qt.ItemDataRole.UserRole) if resident_item else self.selected_pair_resident_id
        device_id = device_item.data(Qt.ItemDataRole.UserRole) if device_item else self.selected_pair_device_id

        if resident_id is None:
            self.show_error("No resident", "Select a resident first.")
            return

        if not device_id:
            self.show_error("No device", "Select a device first.")
            return

        row = self.db.get_resident(resident_id)
        if not row:
            self.show_error("Not found", "Resident record was not found.")
            return

        busy = self.begin_button_busy(getattr(self, "btn_pair_selected", None), "Pairing...")
        try:
            self.selected_pair_resident_id = resident_id
            self.selected_pair_device_id = device_id
            self.db.pair_resident_to_device(resident_id, device_id)
            self.db.log_update(
                "pair_device",
                resident_id,
                row["resident_uid"],
                device_id,
                self.current_user.get("id"),
                self.current_user.get("username"),
                {"device_id": device_id},
                {"paired": True},
                True,
                f"{row['full_name']} paired to {device_id}"
            )
            self.push_resident_row_to_device(row, device_id, "auto_send_after_pair")
            self.refresh_devices()
            self.show_info("Paired", f"{row['full_name']} paired to {device_id}.\n\nText update started in the background. Send the resident photo separately from LCD Schedule.")
        except Exception as e:
            self.show_error("Pair failed", str(e))
        finally:
            self.end_button_busy(busy)

    def push_resident_row_to_device(self, row, device_id, action_type):
        if not self.gateway_online:
            return
        payload = self.build_gateway_payload_from_row(row, device_id)
        self.queue_resident_display_sequence(
            row,
            device_id,
            payload,
            "Latest resident text pushed after pairing",
            "Auto-push",
            image_path=None,
            action_type=action_type,
            notify_on_failure=True,
            include_image=False,
        )

    def queue_resident_display_sequence(
        self,
        row,
        device_id,
        payload,
        success_message,
        label,
        image_path=None,
        action_type="auto_send",
        notify_on_failure=False,
        include_image=True,
    ):
        task = {
            "row": dict(row or {}),
            "device_id": device_id,
            "payload": dict(payload or {}),
            "success_message": success_message,
            "label": label,
            "image_path": image_path,
            "action_type": action_type,
            "notify_on_failure": notify_on_failure,
            "include_image": include_image,
            "user_id": self.current_user.get("id"),
            "username": self.current_user.get("username"),
            "server_mode": self.server_mode,
            "base_url": self.base_url(),
        }
        threading.Thread(target=self._resident_display_worker, args=(task,), daemon=True).start()

    def _resident_display_worker(self, task):
        try:
            gateway = ServerGatewayClient() if task.get("server_mode") else GatewayClient()
            success, message, response = self.send_resident_display_sequence(
                task.get("row"),
                task.get("device_id"),
                task.get("payload"),
                task.get("success_message"),
                task.get("label"),
                image_path=task.get("image_path"),
                include_image=task.get("include_image", True),
                gateway=gateway,
                base_url=task.get("base_url"),
            )
            task.update({"success": success, "message": message, "response": response})
        except Exception as exc:
            task.update({
                "success": False,
                "message": f"{task.get('label') or 'Display update'} could not finish. {friendly_error_message(str(exc))}",
                "response": {"error": str(exc)},
            })
        self.resident_display_finished.emit(task)

    def on_resident_display_finished(self, task):
        self.end_button_busy(task.get("busy_state"))
        row = task.get("row") or {}
        payload = task.get("payload") or {}
        self.db.log_update(
            task.get("action_type") or "auto_send",
            row.get("id") or self.selected_resident_id,
            row.get("resident_uid") or self.current_resident_uid(),
            task.get("device_id"),
            task.get("user_id"),
            task.get("username"),
            payload,
            task.get("response"),
            bool(task.get("success")),
            task.get("message") or "",
        )
        self.load_recent_logs()
        self.refresh_devices()
        if task.get("notify_on_success") and task.get("success"):
            self.show_info(task.get("success_title") or "Success", task.get("message") or "The request completed successfully.")
        if task.get("notify_on_failure") and not task.get("success"):
            self.show_error(task.get("failure_title") or "Display update", task.get("message") or "The resident was saved, but the display update failed.")

    def send_resident_display_sequence(self, row, device_id, payload, success_message, label, image_path=None, include_image=True, gateway=None, base_url=None):
        try:
            gateway = gateway or self.gateway
            target_base_url = base_url if base_url is not None else self.base_url()
            if not include_image:
                text_result = gateway.send_text(target_base_url, payload)
                text_success = text_result["status_code"] == 200
                response = {"text": text_result["body"], "image": {"ok": True, "skipped": True, "reason": "Photo send is manual"}}
                message = success_message if text_success else self.result_error_message(text_result, f"{label} text failed.")
                return text_success, message, response

            if self.server_mode and hasattr(gateway, "send_resident_display") and row and row.get("id"):
                result = gateway.send_resident_display(target_base_url, row.get("id"), device_id)
                body = result.get("body") or {}
                success = (
                    result["status_code"] == 200
                    and not (isinstance(body, dict) and body.get("ok") is False)
                    and not (isinstance(body, dict) and body.get("partial") is True)
                )
                message = success_message if success else self.result_error_message(result, f"{label} failed.")
                if success and isinstance(body, dict):
                    image = body.get("image") or {}
                    if isinstance(image, dict) and image.get("ok") is False:
                        detail = image.get("message") or image.get("detail") or "Resident photo could not be sent."
                        message = f"{success_message}; {friendly_error_message(str(detail))}"
                elif isinstance(body, dict) and body.get("message"):
                    message = friendly_error_message(str(body.get("message")))
                return success, message, result["body"]

            response = {}
            image_success = True
            if image_path and os.path.isfile(str(image_path)):
                image_result = gateway.send_image(target_base_url, device_id, str(image_path))
                response["image"] = image_result["body"]
                image_success = image_result["status_code"] == 200
                if not image_success:
                    message = self.result_error_message(image_result, f"{label} photo failed. E-paper text will still be attempted.")
                else:
                    message = f"{success_message}; resident photo sent"
            else:
                response["image"] = {"ok": True, "skipped": True, "reason": "No resident photo available"}

            text_result = gateway.send_text(target_base_url, payload)
            text_success = text_result["status_code"] == 200
            response["text"] = text_result["body"]
            success = text_success and image_success
            if text_success and image_success:
                message = f"{success_message}; e-paper text sent after photo step"
            elif not text_success:
                message = self.result_error_message(text_result, f"{label} text failed after the photo step.")
            return success, message, response
        except Exception as e:
            return False, f"{label} could not finish. {friendly_error_message(str(e))}", {"error": str(e)}

    def unpair_selected_from_menu(self):
        if not self.require_network_for_write("Unpairing device"):
            return
        device_item = self.available_devices_list.currentItem()
        device_id = device_item.data(Qt.ItemDataRole.UserRole) if device_item else self.selected_pair_device_id
        if not device_id:
            self.show_error("No device", "Select a device first.")
            return
        busy = self.begin_button_busy(getattr(self, "btn_unpair_selected", None), "Unpairing...")
        try:
            self.selected_pair_device_id = device_id
            self.db.unpair_device(device_id)
            self.db.log_update(
                "unpair_device",
                None,
                None,
                device_id,
                self.current_user.get("id"),
                self.current_user.get("username"),
                {"device_id": device_id},
                {"unpaired": True},
                True,
                f"Device {device_id} unpaired"
            )
            self.refresh_devices()
            self.show_info("Unpaired", f"{device_id} was unpaired.")
        except Exception as e:
            self.show_error("Unpair failed", str(e))
        finally:
            self.end_button_busy(busy)

    # ---------------------------- highlights ----------------------------

    def section_text(self, section):
        section = section.upper()
        if section == "NAME":
            return self.txt_name.text().strip()
        if section == "ROOM":
            return self.txt_room.text().strip()
        if section == "DIET":
            return self.field_text(self.txt_diet)
        if section in {"TEXTURE", "ALLERGIES"}:
            return self.field_text(self.txt_allergies)
        if section == "FLUIDS":
            return self.field_text(self.txt_schedule)
        if section == "NOTE":
            return self.txt_note.toPlainText().strip()
        if section == "DRINKS":
            return self.txt_drinks.text().strip()
        return ""

    def extract_tokens(self, section):
        text = self.section_text(section)
        if not text:
            return []

        raw = re.split(r"[,\s;/]+", text)
        seen = set()
        out = []

        for w in raw:
            w = w.strip()
            if not w:
                continue
            key = w.lower()
            if key not in seen:
                seen.add(key)
                out.append(w.upper())

        return out

    def refresh_token_list(self):
        self.token_list.clear()
        for t in self.extract_tokens(self.hl_section.currentText()):
            self.token_list.addItem(QListWidgetItem(t))

    def apply_auto_fg(self):
        fg = auto_fg_for_bg(self.hl_bg.currentText())
        idx = self.hl_fg.findText(fg)
        if idx >= 0:
            self.hl_fg.setCurrentIndex(idx)

    def on_hl_type_changed(self):
        self.token_list.setEnabled("SECTION" not in self.hl_type.currentText())

    def rule_exists(self, rule):
        return any(
            r.type == rule.type and
            r.section == rule.section and
            r.value == rule.value and
            r.bg == rule.bg and
            r.fg == rule.fg
            for r in self.rules
        )

    def add_highlight(self):
        section = self.hl_section.currentText()
        bg = self.hl_bg.currentText()
        fg = self.hl_fg.currentText()

        if "SECTION" in self.hl_type.currentText():
            rule = HighlightRule("section", section, None, bg, fg)
            if not self.rule_exists(rule):
                self.rules.append(rule)
                self.rules_list.addItem(rule.label())
            return

        selected = self.token_list.selectedItems()
        if not selected:
            self.show_error("No words selected", "Select at least one word from the token list.")
            return

        for item in selected:
            rule = HighlightRule("value", section, item.text().strip().upper(), bg, fg)
            if not self.rule_exists(rule):
                self.rules.append(rule)
                self.rules_list.addItem(rule.label())

    def remove_selected_highlight(self):
        row = self.rules_list.currentRow()
        if row < 0:
            return
        self.rules_list.takeItem(row)
        self.rules.pop(row)

    def clear_highlights(self):
        self.rules.clear()
        self.rules_list.clear()

    # ---------------------------- preview ----------------------------

    def update_lcd_image_preview(self):
        if self.selected_image_path and os.path.isfile(self.selected_image_path):
            pix = QPixmap(self.selected_image_path)

            if not pix.isNull():
                big_pix = pix.scaled(
                    self.upd_lcd_image.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
                self.upd_lcd_image.setPixmap(big_pix)
                self.upd_lcd_image.show()
                if hasattr(self, "upd_lcd_empty_state"):
                    self.upd_lcd_empty_state.hide()

                small_pix = pix.scaled(
                    self.lcd_image.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
                self.lcd_image.setPixmap(small_pix)
                self.lcd_image.show()
                if hasattr(self, "lcd_empty_state"):
                    self.lcd_empty_state.hide()

                self.upd_lcd_name.hide()
                self.upd_lcd_room.hide()
                self.upd_lcd_alert.hide()
                self.upd_lcd_note.hide()

                self.lcd_name.hide()
                self.lcd_room.hide()
                self.lcd_alert_banner.hide()
                self.lcd_note.hide()
                return

        self.upd_lcd_image.hide()
        self.lcd_image.hide()
        if hasattr(self, "upd_lcd_empty_state"):
            self.upd_lcd_empty_state.show()
        if hasattr(self, "lcd_empty_state"):
            self.lcd_empty_state.show()

        self.upd_lcd_name.hide()
        self.upd_lcd_room.hide()
        self.upd_lcd_alert.hide()
        self.upd_lcd_note.hide()

        self.lcd_name.hide()
        self.lcd_room.hide()
        self.lcd_alert_banner.hide()
        self.lcd_note.hide()

    def update_preview(self):
        name = self.txt_name.text().strip() or "Resident Name"
        room = self.txt_room.text().strip() or "---"
        diet = self.field_text(self.txt_diet) or "---"
        texture = self.field_text(self.txt_allergies) or "---"
        fluids = self.field_text(self.txt_schedule) or "---"
        note = self.txt_note.toPlainText().strip() or "---"

        self.ep_name.setText(name)
        self.ep_room.setText(f"Room {room}")
        self.ep_diet.setText(f"Diet: {diet}")
        self.ep_allergies.setText(f"Texture: {texture}")
        self.ep_fluids.setText(f"Fluids: {fluids}")
        self.ep_note.setText(f"Note: {note[:80]}")

        self.lcd_name.setText(name)
        self.lcd_room.setText(f"Room {room}")
        self.lcd_note.setText(note[:90])

        self.upd_ep_name.setText(name)
        self.upd_ep_room.setText(f"Room {room}")
        self.upd_ep_diet.setText(f"Diet: {diet}")
        self.upd_ep_allergies.setText(f"Texture: {texture}")
        self.upd_ep_fluids.setText(f"Fluids: {fluids}")
        self.upd_ep_note.setText(f"Note: {note[:120]}")

        self.upd_lcd_name.setText(name)
        self.upd_lcd_room.setText(f"Room {room}")
        self.upd_lcd_note.setText(note[:120])

        self.update_lcd_image_preview()

    # ---------------------------- gateway payload / send ----------------------------

    def build_gateway_payload(self, device_id):
        payload = {"id": device_id}

        name = self.txt_name.text().strip()
        room = self.txt_room.text().strip()
        note = self.txt_note.toPlainText().strip()
        drinks = self.txt_drinks.text().strip()
        schedule = self.field_text(self.txt_schedule)
        texture = self.field_text(self.txt_allergies)

        if name:
            payload["name"] = name
        if room:
            payload["room"] = room
        if note:
            payload["note"] = note
        if drinks:
            payload["drinks"] = drinks
        if schedule:
            payload["fluids"] = schedule
            payload["schedule"] = schedule

        diet = self.field_text(self.txt_diet)

        if diet:
            payload["diet"] = [x.strip() for x in diet.split(",") if x.strip()]

        if texture:
            texture_items = [x.strip() for x in texture.split(",") if x.strip()]
            payload["texture"] = texture_items
            payload["allergies"] = texture_items

        if self.rules:
            payload["highlights"] = [r.to_json() for r in self.rules]

        return payload

    def send_text_update(self):
        if not self.require_network_for_write("Sending text update"):
            return
        if not self.selected_resident_id:
            self.show_error("No resident", "Select or save a resident first.")
            return

        device_id = self.selected_device_id()
        if not device_id:
            self.show_error("No device", "Please select a device.")
            return

        payload = self.build_gateway_payload(device_id)

        busy = self.begin_button_busy(getattr(self, "btn_send_text", None), "Sending...")
        try:
            result = self.gateway.send_text(self.base_url(), payload)
            success = result["status_code"] == 200
            message = "Text update sent successfully" if success else self.result_error_message(result, "Text update failed.")

            self.db.log_update(
                "send_text",
                self.selected_resident_id,
                self.current_resident_uid(),
                device_id,
                self.current_user.get("id"),
                self.current_user.get("username"),
                payload,
                result["body"],
                success,
                message
            )

            self.load_recent_logs()
            self.refresh_devices()

            if success:
                self.show_info("Success", message)
            else:
                self.show_error("Send failed", message)

        except Exception as e:
            self.db.log_update(
                "send_text",
                self.selected_resident_id,
                self.current_resident_uid(),
                device_id,
                self.current_user.get("id"),
                self.current_user.get("username"),
                payload,
                {"error": str(e)},
                False,
                str(e)
            )
            self.load_recent_logs()
            self.show_error("Network Error", str(e))
        finally:
            self.end_button_busy(busy)

    def send_lcd_command(self, command, device_id=None):
        if not self.require_network_for_write("Sending LCD command"):
            return
        device_id = device_id or self.selected_device_id()
        if not device_id:
            self.show_error("No device", "Please select a paired device first.")
            return
        payload = {"device_id": device_id, "command": command}
        button = self.btn_lcd_on if command == "on" else self.btn_lcd_off
        busy = self.begin_button_busy(button, f"LCD {command.upper()}...")
        try:
            result = self.gateway.send_lcd_command(self.base_url(), device_id, command)
            success = result["status_code"] == 200
            response = result["body"]
            message = f"LCD {command.upper()} command sent" if success else self.result_error_message(result, "LCD command failed.")
        except Exception as e:
            success = False
            response = {"error": str(e)}
            message = f"LCD command could not be completed. {friendly_error_message(str(e))}"
        finally:
            self.end_button_busy(busy)
        self.db.log_update(
            "lcd_command",
            self.selected_resident_id,
            self.current_resident_uid(),
            device_id,
            self.current_user.get("id"),
            self.current_user.get("username"),
            payload,
            response,
            success,
            message
        )
        self.load_recent_logs()
        if success:
            self.show_info("LCD Command", message)
        else:
            self.show_error("LCD Command", message)

    def save_lcd_schedule(self):
        if not self.require_network_for_write("Saving LCD schedule"):
            return
        devices = [d for d in self.safe_get_devices() if d.get("device_id")]
        if not devices:
            self.show_error("No devices", "No LCD devices are available to apply the schedule.")
            return

        self.global_schedule_enabled = self.chk_schedule_enabled.isChecked()
        self.global_schedule_on = self.schedule_on_time()
        self.global_schedule_off = self.schedule_off_time()
        self.global_schedule_sleep_if_no_image = self.chk_sleep_no_image.isChecked()

        payload = {
            "resident_uid": "GLOBAL",
            "resident_id": None,
            "device_id": "all",
            "enabled": self.global_schedule_enabled,
            "lcd_on_time": self.global_schedule_on,
            "lcd_off_time": self.global_schedule_off,
            "sleep_if_no_image": self.global_schedule_sleep_if_no_image,
            "has_image": False,
            "device_ids": [d.get("device_id") for d in devices],
        }
        busy = self.begin_button_busy(getattr(self, "btn_save_schedule", None), "Saving...")
        try:
            result = self.gateway.save_schedule(self.base_url(), payload)
            success = result["status_code"] == 200
            responses = [{"device_id": "all", "status_code": result["status_code"], "body": result["body"]}]
            message = (
                f"Global LCD schedule saved for {len(devices)} device(s)."
                if success
                else self.result_error_message(result, "Global LCD schedule failed.")
            )
        except Exception as e:
            success = False
            responses = [{"device_id": "all", "error": str(e)}]
            message = f"Global LCD schedule could not be saved. {friendly_error_message(str(e))}"
        finally:
            self.end_button_busy(busy)

        self.db.log_update(
            "save_schedule",
            None,
            "GLOBAL",
            "ALL",
            self.current_user.get("id"),
            self.current_user.get("username"),
            {
                "scope": "all_lcd_devices",
                "enabled": self.global_schedule_enabled,
                "lcd_on_time": self.global_schedule_on,
                "lcd_off_time": self.global_schedule_off,
                "sleep_if_no_image": self.global_schedule_sleep_if_no_image,
                "device_ids": [d.get("device_id") for d in devices],
            },
            responses,
            success,
            message,
        )
        self.load_schedule_view()
        self.refresh_dashboard_summary()
        self.load_recent_logs()
        if success:
            self.show_info("Schedule", message)
        else:
            self.show_error("Schedule", message)

    def choose_image(self):
        self.attach_resident_photo()

    def attach_resident_photo(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Attach resident photo for LCD",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.webp)"
        )
        if path:
            self.set_resident_photo_path(path)

    def clear_lcd_image(self):
        self.set_resident_photo_path(None)

    def send_image(self):
        if not self.require_network_for_write("Sending LCD image"):
            return
        if not self.selected_resident_id:
            self.show_error("No resident", "Select or save a resident first.")
            return

        device_id = self.selected_device_id()
        if not device_id:
            self.show_error("No device", "Please select a device.")
            return

        if not self.selected_image_path or not os.path.isfile(self.selected_image_path):
            self.show_error("No image", "Choose a valid image first.")
            return

        payload = {"device_id": device_id, "image_path": self.selected_image_path}

        busy = self.begin_button_busy(getattr(self, "btn_send_image", None), "Sending...")
        row = self.db.get_resident(self.selected_resident_id) or {}
        task = {
            "row": dict(row or {}),
            "device_id": device_id,
            "payload": payload,
            "image_path": self.selected_image_path,
            "action_type": "send_image",
            "label": "LCD photo",
            "busy_state": busy,
            "notify_on_success": True,
            "notify_on_failure": True,
            "success_title": "LCD Photo",
            "failure_title": "LCD Photo",
            "user_id": self.current_user.get("id"),
            "username": self.current_user.get("username"),
            "server_mode": self.server_mode,
            "base_url": self.base_url(),
        }
        threading.Thread(target=self._resident_photo_worker, args=(task,), daemon=True).start()

    def _resident_photo_worker(self, task):
        try:
            gateway = ServerGatewayClient() if task.get("server_mode") else GatewayClient()
            result = gateway.send_image(task.get("base_url"), task.get("device_id"), task.get("image_path"))
            body = result.get("body") or {}
            success = result.get("status_code") == 200 and not (isinstance(body, dict) and body.get("ok") is False)
            message = "Resident photo sent to LCD successfully." if success else self.result_error_message(result, "LCD photo send failed.")
            task.update({"success": success, "message": message, "response": body})
        except Exception as exc:
            task.update({
                "success": False,
                "message": f"LCD photo could not finish. {friendly_error_message(str(exc))}",
                "response": {"error": str(exc)},
            })
        self.resident_display_finished.emit(task)

    # ---------------------------- logs ----------------------------

    def load_recent_logs(self):
        rows = self.db.get_recent_logs(limit=50)
        self.logs_table.setRowCount(len(rows))

        for r, row in enumerate(rows):
            created = self.db.format_timestamp(row.get("created_at"))
            values = [
                created,
                row.get("action_type") or "",
                row.get("resident_uid") or "",
                row.get("device_id") or "",
                row.get("pushed_by_username") or "",
                "Yes" if row.get("success") else "No",
                row.get("message") or "",
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, row.get("id"))
                self.logs_table.setItem(r, c, item)

    def selected_log_id(self):
        row = self.logs_table.currentRow()
        if row < 0:
            return None
        item = self.logs_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def show_selected_log_detail(self):
        row = self.logs_table.currentRow()
        if row < 0:
            self.show_error("No log selected", "Select a log row first.")
            return
        self.show_log_detail(row)

    def show_log_detail(self, row):
        item = self.logs_table.item(row, 0)
        log_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not log_id:
            return
        log = self.db.get_log(log_id)
        if not log:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Full Activity Log")
        dialog.resize(780, 620)
        layout = QVBoxLayout(dialog)

        body = QTextEdit(dialog)
        body.setReadOnly(True)
        body.setStyleSheet(self.input_style())
        body.setPlainText(self.format_log_detail(log))
        layout.addWidget(body)

        buttons = QHBoxLayout()
        export_btn = QPushButton("Export This Log PDF", dialog)
        export_btn.setStyleSheet(self.primary_btn_style())
        close_btn = QPushButton("Close", dialog)
        close_btn.setStyleSheet(self.secondary_btn_style())
        buttons.addWidget(export_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        export_btn.clicked.connect(lambda: self.export_log_pdf(log))
        close_btn.clicked.connect(dialog.accept)
        dialog.exec()

    def format_log_detail(self, log):
        return "\n".join([
            f"Date/Time: {self.db.format_timestamp(log.get('created_at'))}",
            f"Action: {log.get('action_type') or ''}",
            f"Resident UID: {log.get('resident_uid') or ''}",
            f"Device: {log.get('device_id') or ''}",
            f"Pushed By: {log.get('pushed_by_username') or ''}",
            f"Success: {'Yes' if log.get('success') else 'No'}",
            f"Message: {log.get('message') or ''}",
            "",
            "Payload:",
            self.pretty_json(log.get("payload_json")),
            "",
            "Response:",
            self.pretty_json(log.get("response_json")),
        ])

    def pretty_json(self, value):
        if value is None:
            return ""
        if not isinstance(value, str):
            return json.dumps(value, indent=2, default=str)
        try:
            return json.dumps(json.loads(value), indent=2)
        except Exception:
            return value

    def export_log_pdf(self, log):
        path, _ = QFileDialog.getSaveFileName(self, "Export log PDF", "activity-log.pdf", "PDF Files (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        self.write_pdf(path, self.format_log_detail(log))
        self.show_info("Exported", f"Log PDF saved to {path}")

    def export_logs_pdf(self):
        rows = self.db.get_recent_logs(limit=200)
        path, _ = QFileDialog.getSaveFileName(self, "Export logs PDF", "activity-logs.pdf", "PDF Files (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        text = "\n\n".join(self.format_log_detail(row) for row in rows)
        self.write_pdf(path, text or "No logs available.")
        self.show_info("Exported", f"Logs PDF saved to {path}")

    def write_pdf(self, path, text):
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
        printer.setOutputFileName(path)
        doc = QTextDocument()
        doc.setPlainText(text)
        doc.print(printer)

    # ---------------------------- timers / window events ----------------------------

    def toggle_auto_refresh(self):
        if self.auto_refresh.isChecked():
            self.timer.start()
        else:
            self.timer.stop()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self.drag_pos is not None and not self.is_custom_maximized:
            self.move(event.globalPosition().toPoint() - self.drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_pos = None
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "close_btn"):
            self.position_window_controls()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "close_btn"):
            self.position_window_controls()
            QTimer.singleShot(0, self.position_window_controls)

    def closeEvent(self, event):
        try:
            self.timer.stop()
        except Exception:
            pass
        try:
            self.control_status_timer.stop()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        event.accept()
