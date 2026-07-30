import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from config import APP_NAME, APP_VERSION, ASSETS_DIR
from ui.splash_screen import SplashScreen
from ui.login_window import LoginWindow
from ui.dashboard_window import DashboardWindow


def configure_windows_identity():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        app_id = f"EnhancedLiving.WhisperwoodDemo.{APP_VERSION}"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def configure_app_icon(app: QApplication):
    icon_path = Path(ASSETS_DIR) / "enhanced_living_whisperwood_icon.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))


class AppController:
    def __init__(self):
        self.splash = SplashScreen()
        self.login = None
        self.dashboard = None

        self.splash.finished.connect(self.show_login)
        self._create_login_window()

    def _create_login_window(self):
        if self.login is not None:
            try:
                self.login.deleteLater()
            except Exception:
                pass
        self.login = LoginWindow()
        self.login.login_success.connect(self.show_dashboard)

    def start(self):
        self.splash.show()

    def show_login(self):
        if self.dashboard is not None:
            try:
                self.dashboard.close()
                self.dashboard.deleteLater()
            except Exception:
                pass
            self.dashboard = None
        if self.login is None:
            self._create_login_window()
        self.login.prepare_for_show(clear_username=False)
        self.login.show()

    def show_dashboard(self, user: dict):
        if self.dashboard is not None:
            try:
                self.dashboard.close()
                self.dashboard.deleteLater()
            except Exception:
                pass
        self.dashboard = DashboardWindow(current_user=user)
        self.dashboard.logout_requested.connect(self.show_login)
        self.dashboard.show()
        if self.login is not None:
            try:
                self.login.close()
                self.login.deleteLater()
            except Exception:
                pass
            self.login = None


def main():
    configure_windows_identity()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    configure_app_icon(app)
    controller = AppController()
    controller.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
