from PySide6.QtWidgets import QVBoxLayout

from src.config import config
from src.ui.dialogs.wizard.setups.base import SetupPage, clear_layout, hint_label


class VeyonSetupPage(SetupPage):
    sid = "local_veyon"

    def __init__(self, parent=None, veyon_service=None):
        super().__init__(parent)
        self.veyon_service = veyon_service
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        hint = hint_label(
            "Установите Veyon на эту машину, создайте пару ключей и настройте WebAPI.\n"
            "Публичный ключ будет развёрнут на удалённых хостах на шаге «Удалённые»."
        )
        layout.addWidget(hint)
        self._container = QVBoxLayout()
        self._container.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._container, 1)
        self._widget = None
        self._settings = None

    def set_snapshot(self, app):
        from copy import copy, deepcopy

        self._settings = copy(config)
        self._settings.app = deepcopy(app)
        self._settings.save = lambda: None

    @property
    def widget(self):
        return self._widget

    def on_enter(self, ctx) -> None:
        self.populate()

    def populate(self):
        """Наполнить шаг Veyon виджетом настройки."""
        from src.ui.dialogs.common.veyon_settings import VeyonConfigWidget
        from src.utils.system.subprocess_env import is_running_elevated

        clear_layout(self._container)
        if self._widget is None:
            self._widget = VeyonConfigWidget(
                self, elevated=is_running_elevated(), settings=self._settings
            )
        self._container.addWidget(self._widget, 1)

    def collect(self, ctx) -> None:
        if self._widget is not None:
            self._widget.save()
            source = self._settings.app.veyon
            names = (
                "key_name",
                "webapi_url",
                "feature_transport",
                "webapi_connection_limit",
            )
            ctx.veyon.settings = {key: getattr(source, key) for key in names}
            ctx.veyon.settings["server_port"] = source.server_port
            ctx.veyon.key_name = self._widget._key_combo.currentText().strip()

    def validate(self, ctx) -> tuple:
        import re
        from urllib.parse import urlparse

        if ctx.veyon.key_name and not re.fullmatch(
            r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}", ctx.veyon.key_name
        ):
            return False, "Недопустимое имя ключа Veyon."
        address = ctx.veyon.settings.get("webapi_url", "")
        if address and (
            urlparse(address).scheme not in ("http", "https")
            or not urlparse(address).hostname
        ):
            return False, "Укажите HTTP/HTTPS адрес Veyon WebAPI."
        return True, ""

    def is_configured(self) -> bool:
        if self._widget is None:
            return False
        try:
            return bool(self._widget.is_configured())
        except Exception:
            return False

    def summary_html(self, ctx) -> str:
        if self._widget is not None:
            try:
                if self._widget.is_configured():
                    return "настроен (ключи есть)"
                return "ключи отсутствуют"
            except Exception:
                return "не удалось проверить"
        return "не настроен"

    def wait_threads(self, timeout_ms: int) -> bool:
        """Дождаться завершения фоновых потоков виджета Veyon.

        Все это run()-потоки (QThread.run переопределён) — quit() им ничего
        не делает, ждём только через wait(). Возвращает True, если все
        потоки завершились за timeout_ms.
        """
        ok = True
        if self._widget is not None:
            threads = list(getattr(self._widget, "_background_threads", ()))
            for attr in (
                "_loader",
                "_status_checker",
                "_diag_runner",
                "_service_op_thread",
                "_pkg_thread",
                # _op_thread — _ElevatedOpThread (UAC/pkexec-диалог может
                # держать поток до ~120с; раньше не чистился вовсе).
                "_op_thread",
            ):
                t = getattr(self._widget, attr, None)
                if t is not None and t not in threads:
                    threads.append(t)
            for t in threads:
                if t is not None and hasattr(t, "isRunning") and t.isRunning():
                    try:
                        if not t.wait(timeout_ms):
                            ok = False
                    except Exception:
                        ok = False
        return ok

    def request_stop(self):
        if self._widget is not None:
            self._widget.stop_background()
