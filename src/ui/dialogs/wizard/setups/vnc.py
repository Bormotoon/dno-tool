"""Локальный шаг VNC мастера настройки (sid="local_vnc").

Выделено из src/ui/dialogs/tools/setup_wizard.py без изменения текстов
и логики: hint, VncConfigWidget(lock_server=True), fallback параметров
из config, предложение установки krdc/krfb на машину оператора,
применение локального пароля krfb, summary и save.
"""

import shutil
import sys

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QMessageBox, QVBoxLayout

from src.config import config
from src.config.settings import DEFAULT_VNC_SERVER
from src.logger import logger
from src.ui.dialogs.common.vnc_settings import (
    PASSWORD_REQUIRED,
    VncConfigWidget,
    server_label,
)
from src.ui.dialogs.wizard.core.context import VncState
from src.ui.dialogs.wizard.setups.base import SetupPage, clear_layout, hint_label


class LocalKrfbInstallWorker(QThread):
    """Потоковая установка krdc/krfb на машину оператора (dnf/apt-get)."""

    status = Signal(str)
    done = Signal(bool)

    def __init__(self, pkgs: list):
        super().__init__()
        self._pkgs = pkgs

    def run(self):
        from src.workers.setup.preparation import install_local_krfb_tools

        try:
            ok, error = install_local_krfb_tools(self._pkgs, self.status.emit)
            if not ok:
                self.status.emit(error or "Не удалось установить локальные пакеты")
            self.done.emit(ok)
        except Exception:
            self.done.emit(False)


class VncSetupPage(SetupPage):
    """Страница локального VNC: параметры x11vnc/kmsvnc/krfb."""

    sid = "local_vnc"

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(
            hint_label(
                "Укажите порт и пароль для управления экранами удалённых компьютеров.\n\n"
                "Позже их можно изменить в «Настройки VNC». Чтобы применить "
                "новый пароль на компьютерах, повторите установку на шаге «Удалённые»."
            )
        )
        self._container = QVBoxLayout()
        self._container.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._container, 1)
        self._widget = None
        self._krfb_worker = None
        self._krfb_done_cb = None
        self._guard = None

    def set_guard(self, guard) -> None:
        """Запомнить guard shell для блокировки навигации на время установки."""
        self._guard = guard

    def on_enter(self, ctx) -> None:
        clear_layout(self._container)
        if self._widget is None:
            self._widget = VncConfigWidget(self, lock_server=True)
        self._widget.set_server(ctx.vnc_server)
        self._widget.show_server_banner(self._server_banner_text(ctx.vnc_server))
        self._container.addWidget(self._widget, 1)

    @staticmethod
    def _server_banner_text(server: str) -> str:
        """Что будет установлено (вместо неактивного селектора сервера)."""
        return (
            f"Выбран {server_label(server)}. Изменить вариант можно на шаге «Начало»."
        )

    def collect(self, ctx) -> None:
        if self._widget is not None:
            server, port, pwd = self._widget.get_vnc_params()
            ctx.vnc = VncState(server, port, pwd)

    def get_vnc_params(self, ctx) -> tuple:
        """Вернуть (server, port, password): виджет, иначе fallback из config."""
        if self._widget is not None:
            return self._widget.get_vnc_params()
        vnc_server = self.selected_server(ctx)
        vnc_password = config.app.veyon.vnc_password or ""
        vnc_port = config.app.veyon.vnc_port or 5900
        return (vnc_server, vnc_port, vnc_password)

    def selected_server(self, ctx) -> str:
        """Выбранный VNC-сервер: ключ из SERVER_LABELS."""
        if self._widget is not None:
            return self._widget.current_server() or DEFAULT_VNC_SERVER
        return ctx.vnc_server or DEFAULT_VNC_SERVER

    def ensure_local_krfb_tools(self, log_fn, on_done) -> bool:
        """Предложить установку krdc+krfb на машину оператора (Linux).

        Returns:
            True - можно продолжать установку немедленно; False - идёт
            потоковая локальная установка, продолжение в on_done(ok).
        """
        if sys.platform == "win32":
            # Windows: krdc/krfb недоступны, браузерного вьювера достаточно.
            return True

        missing = []
        if shutil.which("krdc") is None:
            missing.append("krdc")
        if shutil.which("kwriteconfig5") is None:
            missing.append("krfb")
        if not missing:
            return True  # всё уже установлено

        pkg_list = " и ".join(missing)
        answer = QMessageBox.question(
            self,
            "Локальная машина",
            f"На этом компьютере не установлен {pkg_list}.\n\n"
            "krdc нужен для доступа к экранам хостов из интерфейса,\n"
            "krfb - для общего доступа к экрану этого компьютера.\n\n"
            "Установить пакеты сейчас?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            log_fn(
                f"[локальная машина] установка {pkg_list} отложена - "
                "клиентский доступ будет доступен только в браузере"
            )
            return True

        self._krfb_worker = LocalKrfbInstallWorker(missing)
        self._krfb_worker.status.connect(log_fn)
        self._krfb_done_cb = on_done
        self._krfb_worker.done.connect(self._on_local_krfb_done)
        self._krfb_worker.start()

        if self._guard is not None:
            try:
                self._guard.register_thread("vnc_local_krfb", self._krfb_worker, 60000)
            except Exception:
                pass
            try:
                self._guard.set_busy("vnc_local_krfb", True)
            except Exception:
                pass
        log_fn(f"[локальная машина] установка {', '.join(missing)} запущена")
        return False

    def _on_local_krfb_done(self, ok: bool) -> None:
        """Завершение локальной установки krdc/krfb: разблокировать guard."""
        if self._krfb_worker is not None:
            try:
                self._krfb_worker.wait(2000)
            except Exception:
                pass
            self._krfb_worker = None
        if self._guard is not None:
            try:
                self._guard.set_busy("vnc_local_krfb", False)
            except Exception:
                pass
        cb = self._krfb_done_cb
        self._krfb_done_cb = None
        if cb is not None:
            cb(ok)

    def apply_local_krfb_password(self, password: str) -> tuple:
        """Применить пароль krfb в home оператора. Returns (ok, msg)."""
        from src.services.remote.krfb_service import KRFBService

        ok, msg = KRFBService.apply_password_local(password)
        if not ok:
            logger.warning(f"SetupWizard: local krfb password: {msg}")
        return ok, msg

    def krfb_active(self) -> bool:
        """True, пока идёт потоковая установка krdc/krfb."""
        try:
            return self._krfb_worker is not None and self._krfb_worker.isRunning()
        except Exception:
            return False

    def cleanup(self) -> None:
        if self._widget is not None:
            try:
                self._widget.cleanup()
            except Exception:
                pass
        if self._guard is not None:
            try:
                unregister = getattr(self._guard, "unregister_thread", None)
                if callable(unregister):
                    if not self.krfb_active():
                        unregister("vnc_local_krfb")
            except Exception:
                pass

    def validate(self, ctx) -> tuple:
        if any(char in ctx.vnc.password for char in ("\0", "\n", "\r")):
            return False, "Пароль VNC должен быть одной строкой без нулевых байтов."
        if ctx.vnc_server in PASSWORD_REQUIRED and not ctx.vnc.password:
            return (
                False,
                f"Для {server_label(ctx.vnc_server)} задайте пароль. "
                "Пустой пароль разрешён только для x11vnc.",
            )
        return True, ""

    def summary_html(self, ctx) -> str:
        if self._widget is not None:
            server, port, pwd = self._widget.get_vnc_params()
        else:
            server = self.selected_server(ctx)
            port = config.app.veyon.vnc_port or 5900
            # Пароль общий на все серверы (ключ veyon.vnc_password).
            pwd = config.app.veyon.vnc_password or ""
        return f"{server_label(server)}, порт {port}" + (
            ", с паролем" if pwd else ", без пароля"
        )
