"""Настройки Veyon - вкладки: Установка, Ключи, WebAPI, Диагностика.

Экспортирует:
- VeyonConfigWidget — композитный виджет для встраивания в мастер настройки.
- VeyonSettingsDialog — тонкая обёртка-диалог над VeyonConfigWidget.
"""

import os
import sys
import subprocess
import re

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QSpinBox,
    QLabel,
    QPushButton,
    QDialogButtonBox,
    QHBoxLayout,
    QComboBox,
    QCheckBox,
    QInputDialog,
    QFileDialog,
    QMessageBox,
    QGridLayout,
    QSlider,
    QTabWidget,
    QWidget,
    QPlainTextEdit,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
)
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt, QTimer, QThread, Signal

from src.config import config
from src.logger import logger
from src.services.remote.veyon_service import VeyonService
from src.ui.icons import icon_data_uri
from src.utils.system.subprocess_env import build_clean_env


# ================================================================
#  Фоновые потоки для асинхронной загрузки данных Veyon CLI
# ================================================================


class _VeyonLoader(QThread):
    """Загружает данные Veyon CLI в фоновом потоке — без замораживания UI."""

    loaded = Signal(dict)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self._service = service

    def run(self):
        result = {}
        try:
            result["keys_detailed"] = self._service.list_auth_keys_detailed()
        except Exception:
            result["keys_detailed"] = []
        try:
            result["keys"] = self._service.list_auth_keys()
        except Exception:
            result["keys"] = []
        try:
            result["webapi_enabled"] = self._service.get_webapi_enabled()
        except Exception:
            result["webapi_enabled"] = False
        try:
            result["port"] = self._service.get_webapi_port()
        except Exception:
            result["port"] = 11080
        try:
            result["conn_limit"] = self._service.get_webapi_connection_limit()
        except Exception:
            result["conn_limit"] = 32
        try:
            result["service_status"] = self._service.get_service_status()
        except Exception:
            result["service_status"] = "unknown"
        try:
            result["auth_method"] = self._service.get_auth_method()
        except Exception:
            result["auth_method"] = None
        self.loaded.emit(result)


class _StatusChecker(QThread):
    """Проверяет статус сервиса в фоновом потоке."""

    status_ready = Signal(str)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self._service = service

    def run(self):
        try:
            status = self._service.get_service_status()
        except Exception:
            status = "unknown"
        self.status_ready.emit(status)


class _ServiceOperationThread(QThread):
    """Выполняет операцию с сервисом (start/stop/restart) в фоновом потоке."""

    operation_done = Signal(bool, str)  # (success, operation_name)

    def __init__(self, service, operation, parent=None):
        super().__init__(parent)
        self._service = service
        self._operation = operation

    def run(self):
        try:
            if self._operation == "start":
                ok = self._service.start_service()
            elif self._operation == "stop":
                ok = self._service.stop_service()
            elif self._operation == "restart":
                ok = self._service.restart_service()
            else:
                ok = False
        except Exception:
            ok = False
        self.operation_done.emit(ok, self._operation)


class _ElevatedOpThread(QThread):
    """Фоновое выполнение операции, требующей элевации.

    Синхронные операции с ключами/конфигом Veyon при not-elevated запуске
    показывают системный диалог авторизации (UAC / pkexec), который
    блокирует вызывающий поток до ответа пользователя. Выполнение в
    QThread не замораживает GUI; результат возвращается сигналом.
    """

    done = Signal(bool, object)  # (success, callback_result)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        ok = False
        result = None
        try:
            ok = bool(self._fn())
        except Exception:
            ok = False
        self.done.emit(ok, result)


class _PackageCheckThread(QThread):
    """Проверить установленные пакеты через rpm вне GUI-потока."""

    output_ready = Signal(str)

    def __init__(self, packages: list[str], parent=None):
        super().__init__(parent)
        self._packages = packages

    def run(self):
        for pkg in self._packages:
            if self.isInterruptionRequested():
                return
            try:
                result = subprocess.run(
                    ["rpm", "-q", pkg],
                    capture_output=True,
                    timeout=10,
                    env=build_clean_env(),
                )
                try:
                    stdout = result.stdout.decode("utf-8", errors="replace").strip()
                except Exception:
                    stdout = str(result.stdout).strip()
                if result.returncode == 0:
                    self.output_ready.emit(f"  ✓ {stdout}")
                else:
                    self.output_ready.emit(f"  ✗ {pkg} — не установлен")
            except Exception as e:
                self.output_ready.emit(f"  ✗ {pkg} — ошибка проверки: {e}")


class _PackageOperationThread(QThread):
    """Запускает dnf install/remove в фоновом потоке с построчным выводом.

    При not-elevated запуске команда оборачивается в pkexec (появится
    polkit-диалог авторизации). Стриминговый вывод при этом недоступен
    (pkexec не даёт пайпов) — вывод показывается после завершения.
    """

    output_ready = Signal(str)
    finished_signal = Signal(int)  # return code

    def __init__(self, cmd, parent=None):
        super().__init__(parent)
        self._cmd = cmd

    def run(self):
        from src.utils.system.subprocess_env import is_running_elevated, run_elevated

        # ── Not elevated (Linux): pkexec, вывод после завершения ──
        if sys.platform != "win32" and not is_running_elevated():
            self._run_pkexec()
            return

        try:
            proc = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=build_clean_env(),
            )
            for raw_line in proc.stdout:
                try:
                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    line = str(raw_line).rstrip()
                self.output_ready.emit(line)
            proc.wait(timeout=180)
            self.finished_signal.emit(proc.returncode)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.output_ready.emit("\n[Превышено время ожидания (180 сек)]")
            self.finished_signal.emit(-1)
        except FileNotFoundError:
            self.output_ready.emit(
                "Ошибка: команда не найдена. Возможно, dnf не установлен."
            )
            self.finished_signal.emit(-1)
        except Exception as e:
            self.output_ready.emit(f"Ошибка: {e}")
            self.finished_signal.emit(-1)

    def _run_pkexec(self):
        """Linux not-root: установка пакетов через pkexec (polkit-диалог)."""
        from src.utils.system.subprocess_env import run_elevated

        self.output_ready.emit("[Требуются права администратора — подтвердите запрос]")
        ok, err = run_elevated(self._cmd, timeout=900)
        if ok:
            self.output_ready.emit("Команда выполнена успешно.")
            self.finished_signal.emit(0)
        else:
            self.output_ready.emit(f"Ошибка: {err or 'операция не выполнена'}")
            self.finished_signal.emit(-1)


class _DiagnosticsRunner(QThread):
    """Запускает диагностику в фоновом потоке."""

    diagnostics_ready = Signal(str)

    def __init__(self, service, webapi_url, port, key_name, parent=None):
        super().__init__(parent)
        self._service = service
        self._webapi_url = webapi_url
        self._port = port
        self._key_name = key_name
        # Data-URI иконок готовятся в GUI-потоке (байты zip-темы + кэш);
        # пустой URI = <img> не вставляем, остаётся читаемый текст.
        self._icon_ok = icon_data_uri("result_success")
        self._icon_err = icon_data_uri("result_failure")

    @staticmethod
    def _img(uri: str) -> str:
        return f'<img src="{uri}" width="14" height="14">' if uri else ""

    def run(self):
        icon_ok = self._img(self._icon_ok)
        icon_err = self._img(self._icon_err)
        lines = []

        # 1. CLI
        try:
            cli_path = self._service._cli_path
            cli_found = os.path.isfile(cli_path)
            if cli_found:
                found_path = cli_path if os.path.isfile(cli_path) else None
                lines.append(f"{icon_ok} <b>veyon-cli:</b> найден — {found_path}")
            else:
                lines.append(
                    f"{icon_err} <b>veyon-cli:</b> не найден в PATH и стандартных директориях"
                )
        except Exception as e:
            lines.append(f"{icon_err} <b>veyon-cli:</b> ошибка проверки — {e}")

        # 2. Ключ
        try:
            if self._service and self._key_name:
                key_file = self._service._find_key_file()
                if key_file:
                    lines.append(f"{icon_ok} <b>Ключ:</b> найден — {key_file}")
                else:
                    lines.append(
                        f"{icon_err} <b>Ключ:</b> '{self._key_name}' не найден"
                    )
            elif self._service:
                lines.append(f"{icon_err} <b>Ключ:</b> не выбран")
            else:
                lines.append(f"{icon_err} <b>Ключ:</b> VeyonService недоступен")
        except Exception as e:
            lines.append(f"{icon_err} <b>Ключ:</b> ошибка проверки — {e}")

        # 3. WebAPI
        try:
            import requests

            url = self._webapi_url or config.app.veyon.webapi_url
            r = requests.get(url + "hoststate/localhost", timeout=3)
            if r.status_code == 200:
                lines.append(f"{icon_ok} <b>WebAPI:</b> доступен (порт {self._port})")
            else:
                lines.append(f"{icon_err} <b>WebAPI:</b> отвечает код {r.status_code}")
        except Exception:
            lines.append(
                f"{icon_err} <b>WebAPI:</b> не доступен — проверьте, запущен ли Veyon Service"
            )

        # 4. Сервис
        try:
            if self._service:
                status = self._service.get_service_status()
                if status == "running":
                    lines.append(f"{icon_ok} <b>Сервис:</b> запущен")
                elif status == "stopped":
                    lines.append(f"{icon_err} <b>Сервис:</b> остановлен")
                else:
                    lines.append(f"{icon_err} <b>Сервис:</b> статус неизвестен")
        except Exception as e:
            lines.append(f"{icon_err} <b>Сервис:</b> ошибка проверки — {e}")

        self.diagnostics_ready.emit("<br>".join(lines))


# ================================================================
#  Композитный виджет
# ================================================================


class VeyonConfigWidget(QWidget):
    """Композитный виджет настройки Veyon для встраивания в диалоги и мастер."""

    def __init__(self, parent=None, elevated: bool = True, settings=None):
        super().__init__(parent)
        self._config = settings or config
        self._draft_mode = settings is not None
        self._loader = None
        self._status_checker = None
        self._diag_runner = None
        self._service_op_thread = None
        self._pkg_thread = None
        self._background_threads = []
        self._loading = False
        self._elevated = elevated
        self._setup_ui()
        self._load_local_values()
        self._webapi_enabled_chk.toggled.connect(self._on_webapi_enabled_changed)
        self._start_loader()

    # ================================================================
    #  UI
    # ================================================================

    def show_webapi_section(self) -> None:
        """Show the WebAPI tab and focus its connection limit control.

        Public navigation entry for other settings windows (thumbnail
        resources link here instead of duplicating the WebAPI limit).
        """
        self._tabs.setCurrentWidget(self._webapi_tab)
        slider = getattr(self, "_conn_limit_slider", None)
        if slider is not None:
            slider.setFocus()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._tabs = QTabWidget()
        self._webapi_tab = self._build_webapi_tab()
        self._tabs.addTab(self._build_install_tab(), "Установка")
        self._tabs.addTab(self._build_keys_tab(), "Ключи")
        self._tabs.addTab(self._webapi_tab, "WebAPI")
        self._tabs.addTab(self._build_diagnostics_tab(), "Диагностика")
        self._set_veyon_tabs_enabled(False)
        layout.addWidget(self._tabs)

        bottom_row = QHBoxLayout()

        self._load_label = QLabel("Загрузка данных Veyon…")
        bottom_row.addWidget(self._load_label)

        self._load_progress = QProgressBar()
        self._load_progress.setRange(0, 0)
        self._load_progress.setTextVisible(False)
        self._load_progress.setFixedHeight(16)
        self._load_progress.setMaximumWidth(220)
        bottom_row.addWidget(self._load_progress)
        bottom_row.addStretch()

        layout.addLayout(bottom_row)

    def _set_veyon_tabs_enabled(self, enabled: bool) -> None:
        """Enable integration controls once Veyon data has loaded."""
        self._tabs.setEnabled(enabled)

    # --- Elevation state ---

    # --- Вкладка: WebAPI ---

    def _build_webapi_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setSpacing(10)

        webapi_group = QGroupBox("WebAPI сервер")
        webapi_layout = QGridLayout(webapi_group)

        self._webapi_enabled_chk = QCheckBox("Включен")
        self._webapi_enabled_chk.setToolTip(
            "Включить/выключить WebAPI сервер Veyon (требует прав администратора)"
        )
        webapi_layout.addWidget(self._webapi_enabled_chk, 0, 0)

        port_label = QLabel("&Порт:")
        port_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        webapi_layout.addWidget(port_label, 0, 1)
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(11080)
        self._port_spin.setToolTip("Порт WebAPI сервера Veyon (по умолчанию 11080)")
        port_label.setBuddy(self._port_spin)
        webapi_layout.addWidget(self._port_spin, 0, 2)

        self._port_edit_btn = QPushButton("Применить")
        self._port_edit_btn.setToolTip(
            "Применить новый порт (требует прав администратора и перезапуска сервиса)"
        )
        self._port_edit_btn.clicked.connect(self._on_apply_port)
        webapi_layout.addWidget(self._port_edit_btn, 0, 3)

        # Лимит одновременных WebAPI-соединений (WebAPI/ConnectionLimit).
        # Степень двойки: 32 (default) / 64 / 128 / 256 / 512 / 1024. Определяет
        # сколько хостов могут одновременно получать миниатюры через WebAPI.
        # Persistent per-host клиенты dno-tool требуют 1 соединение на хост,
        # поэтому лимит должен быть >= числа одновременно онлайн хостов с Veyon.
        # QSlider с целочисленной шкалой 5..10 → 2^value (32..1024).
        conn_limit_label = QLabel("Лимит &соединений:")
        conn_limit_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        webapi_layout.addWidget(conn_limit_label, 2, 1)
        conn_limit_row = QHBoxLayout()
        self._conn_limit_slider = QSlider(Qt.Orientation.Horizontal)
        self._conn_limit_slider.setMinimum(5)  # 2^5 = 32
        self._conn_limit_slider.setMaximum(10)  # 2^10 = 1024
        self._conn_limit_slider.setSingleStep(1)
        self._conn_limit_slider.setPageStep(1)
        self._conn_limit_slider.setToolTip(
            "Максимальное число одновременных WebAPI-соединений (степень 2).\n"
            "Должно быть >= числа одновременно онлайн хостов с Veyon.\n"
            "По умолчанию 32. Требует прав администратора и перезапуска сервиса."
        )
        self._conn_limit_value_label = QLabel("32")
        self._conn_limit_value_label.setMinimumWidth(40)
        self._conn_limit_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._conn_limit_slider.valueChanged.connect(self._on_conn_limit_slider_changed)
        conn_limit_row.addWidget(self._conn_limit_slider, 1)
        conn_limit_row.addWidget(self._conn_limit_value_label)
        conn_limit_label.setBuddy(self._conn_limit_slider)
        webapi_layout.addLayout(conn_limit_row, 2, 2)

        self._conn_limit_btn = QPushButton("Применить")
        self._conn_limit_btn.setToolTip(
            "Применить лимит соединений (требует прав администратора и "
            "перезапуска сервиса)"
        )
        self._conn_limit_btn.clicked.connect(self._on_apply_conn_limit)
        webapi_layout.addWidget(self._conn_limit_btn, 2, 3)

        url_label = QLabel("&URL:")
        webapi_layout.addWidget(url_label, 1, 0, 1, 2)
        self._webapi_url_edit = QLineEdit()
        self._webapi_url_edit.setToolTip(
            "URL WebAPI сервера Veyon. Изменяется автоматически при смене порта."
        )
        url_label.setBuddy(self._webapi_url_edit)
        self._port_spin.valueChanged.connect(self._on_port_changed)
        webapi_layout.addWidget(self._webapi_url_edit, 1, 2, 1, 2)

        tab_layout.addWidget(webapi_group)

        # ── Управление фичами: транспорт WebAPI vs veyon-cli ──
        feature_group = QGroupBox("Управление фичами")
        feature_layout = QGridLayout(feature_group)

        transport_label = QLabel("&Транспорт:")
        transport_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        feature_layout.addWidget(transport_label, 0, 0)

        self._feature_transport_combo = QComboBox()
        self._feature_transport_combo.addItem(
            "Через WebAPI (быстрее, использует соединения миниатюр)", "webapi"
        )
        self._feature_transport_combo.addItem(
            "Через командную строку Veyon (совместимость)", "cli"
        )
        self._feature_transport_combo.setToolTip(
            "Управление командами Veyon — через WebAPI или командную строку.\n\n"
            "WebAPI: команды отправляются через уже открытые соединения миниатюр — "
            "это быстрее и не запускает дополнительные процессы. "
            "Если миниатюры выключены или хост недоступен, "
            "автоматически используется командная строка Veyon.\n\n"
            "Командная строка: для каждой команды запускается отдельный процесс Veyon. "
            "Работает независимо от состояния миниатюр."
        )
        transport_label.setBuddy(self._feature_transport_combo)
        feature_layout.addWidget(self._feature_transport_combo, 0, 1)
        self._feature_transport_combo.currentIndexChanged.connect(
            self._on_feature_transport_changed
        )

        tab_layout.addWidget(feature_group)

        svc_group = QGroupBox("Veyon Service")
        svc_layout = QGridLayout(svc_group)

        svc_layout.addWidget(QLabel("Состояние:"), 0, 0)
        self._service_status_label = QLabel("Загрузка...")
        self._service_status_label.setStyleSheet(
            "font-weight: bold; color: gray; padding: 4px;"
        )
        self._service_status_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        svc_layout.addWidget(
            self._service_status_label, 0, 1, alignment=Qt.AlignmentFlag.AlignLeft
        )

        svc_btn_row = QHBoxLayout()
        self._service_btn = QPushButton("Запустить")
        self._service_btn.clicked.connect(self._on_service_button)
        svc_btn_row.addWidget(self._service_btn)

        self._stop_btn = QPushButton("Остановить")
        self._stop_btn.clicked.connect(self._on_stop_service)
        svc_btn_row.addWidget(self._stop_btn)

        self._restart_btn = QPushButton("Перезапустить")
        self._restart_btn.clicked.connect(self._on_restart_service)
        svc_btn_row.addWidget(self._restart_btn)

        svc_layout.addLayout(svc_btn_row, 1, 0, 1, 2)

        reg_row = QHBoxLayout()
        self._register_btn = QPushButton("В автозапуск")
        self._register_btn.setToolTip(
            "Зарегистрировать Veyon Service для автозапуска при загрузке ОС"
        )
        self._register_btn.clicked.connect(self._on_register_service)
        reg_row.addWidget(self._register_btn)

        self._unregister_btn = QPushButton("Убрать из автозапуска")
        self._unregister_btn.setToolTip("Отменить автозапуск Veyon Service")
        self._unregister_btn.clicked.connect(self._on_unregister_service)
        reg_row.addWidget(self._unregister_btn)

        svc_layout.addLayout(reg_row, 2, 0, 1, 2)

        tab_layout.addWidget(svc_group)
        tab_layout.addStretch()
        return tab

    def _on_port_changed(self, port: int):
        self._webapi_url_edit.setText(f"http://localhost:{port}/api/v1/")

    def _on_apply_port(self):
        port = self._port_spin.value()
        service = self._get_veyon_service()
        if not service:
            return
        self._port_edit_btn.setEnabled(False)
        self._port_edit_btn.setText("Применение…")
        self._run_elevated_op(
            lambda: service.set_webapi_port(port),
            on_done=lambda ok: self._on_apply_port_done(ok, port),
        )

    def _on_apply_port_done(self, ok: bool, port: int):
        """Завершение применения порта (GUI-поток)."""
        self._port_edit_btn.setEnabled(True)
        self._port_edit_btn.setText("Применить")
        service = self._get_veyon_service()
        if not ok:
            self._show_operation_error("изменение порта WebAPI")
            if service:
                actual = service.get_webapi_port()
                self._port_spin.blockSignals(True)
                self._port_spin.setValue(actual)
                self._port_spin.blockSignals(False)
                self._on_port_changed(actual)
            return
        self._on_port_changed(port)
        reply = QMessageBox.question(
            self,
            "Перезапуск сервиса",
            "Для применения порта требуется перезапустить "
            "Veyon Service. Перезапустить сейчас?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            QTimer.singleShot(500, self._do_restart_service)

    @staticmethod
    def _conn_limit_value_from_slider(v: int) -> int:
        """Преобразовать значение ползунка (5..10) в лимит (32..1024)."""
        return 2**v

    @staticmethod
    def _conn_limit_slider_from_value(limit: int) -> int:
        """Преобразовать лимит в значение ползунка (ближайшая степень 2)."""
        import math

        if limit <= 0:
            return 5
        return max(5, min(10, int(math.log2(limit))))

    def _on_conn_limit_slider_changed(self, v: int):
        """Обновить подпись значения при движении ползунка."""
        self._conn_limit_value_label.setText(str(self._conn_limit_value_from_slider(v)))

    def _on_apply_conn_limit(self):
        """Применить лимит соединений WebAPI (с элевацией + рестарт)."""
        limit = self._conn_limit_value_from_slider(self._conn_limit_slider.value())
        service = self._get_veyon_service()
        if not service:
            return
        self._conn_limit_btn.setEnabled(False)
        self._conn_limit_btn.setText("Применение…")
        self._run_elevated_op(
            lambda: service.set_webapi_connection_limit(limit),
            on_done=lambda ok: self._on_apply_conn_limit_done(ok, limit),
        )

    def _on_apply_conn_limit_done(self, ok: bool, limit: int):
        """Завершение применения лимита соединений (GUI-поток)."""
        self._conn_limit_btn.setEnabled(True)
        self._conn_limit_btn.setText("Применить")
        service = self._get_veyon_service()
        if not ok:
            self._show_operation_error("изменение лимита соединений WebAPI")
            if service:
                cur = service.get_webapi_connection_limit()
                self._conn_limit_slider.blockSignals(True)
                self._conn_limit_slider.setValue(
                    self._conn_limit_slider_from_value(cur)
                )
                self._conn_limit_value_label.setText(str(cur))
                self._conn_limit_slider.blockSignals(False)
            return
        # Сохраняем в settings.json для монитора пулов (мгновенное чтение без subprocess)
        try:
            self._config.app.veyon.webapi_connection_limit = limit
            self._config.save()
        except Exception:
            pass
        reply = QMessageBox.question(
            self,
            "Перезапуск сервиса",
            "Для применения лимита соединений требуется перезапустить "
            "Veyon Service. Перезапустить сейчас?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            QTimer.singleShot(500, self._do_restart_service)

    def _start_background_thread(self, thread):
        """Удерживать каждый поток до фактического finished, включая повторы."""
        if getattr(self, "_shutting_down", False):
            return
        self._background_threads.append(thread)

        def release():
            if thread in self._background_threads:
                self._background_threads.remove(thread)

        thread.finished.connect(release)
        thread.start()

    def stop_background(self):
        self._shutting_down = True
        for thread in tuple(self._background_threads):
            thread.requestInterruption()
            thread.quit()

    def _run_elevated_op(self, fn, on_done=None):
        """Выполнить операцию с элевацией в фоновом потоке.

        Args:
            fn: callable без аргументов, выполняется в QThread.
            on_done: callable(ok: bool) — вызывается в GUI-потоке.
        """
        self._op_thread = _ElevatedOpThread(fn, self)
        if on_done is not None:
            self._op_thread.done.connect(lambda ok, _r: on_done(ok))
        self._start_background_thread(self._op_thread)

    def _on_feature_transport_changed(self, _idx: int):
        """Сохранить выбор транспорта фичей (webapi | cli) в settings.json."""
        transport = self._feature_transport_combo.currentData()
        try:
            self._config.app.veyon.feature_transport = transport
            self._config.save()
        except Exception:
            pass

    # --- Вкладка: Ключи ---

    def _build_keys_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setSpacing(10)

        group = QGroupBox("Ключи аутентификации")
        group_layout = QVBoxLayout(group)

        self._keys_table = QTableWidget(0, 4)
        self._keys_table.setHorizontalHeaderLabels(
            ["Имя", "Тип", "ID пары", "Группа доступа"]
        )
        self._keys_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._keys_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self._keys_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._keys_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self._keys_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._keys_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._keys_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._keys_table.setMinimumHeight(150)
        group_layout.addWidget(self._keys_table)

        btn_layout = QHBoxLayout()

        self._create_key_btn = QPushButton("Создать пару")
        self._create_key_btn.clicked.connect(self._on_create_key)
        btn_layout.addWidget(self._create_key_btn)

        self._delete_key_btn = QPushButton("Удалить")
        self._delete_key_btn.clicked.connect(self._on_delete_key)
        btn_layout.addWidget(self._delete_key_btn)

        self._export_key_btn = QPushButton("Экспорт")
        self._export_key_btn.clicked.connect(self._on_export_key)
        btn_layout.addWidget(self._export_key_btn)

        self._import_key_btn = QPushButton("Импорт")
        self._import_key_btn.clicked.connect(self._on_import_key)
        btn_layout.addWidget(self._import_key_btn)

        self._extract_btn = QPushButton("Извлечь public")
        self._extract_btn.setToolTip("Извлечь открытый ключ из закрытого")
        self._extract_btn.clicked.connect(self._on_extract_key)
        btn_layout.addWidget(self._extract_btn)

        self._group_btn = QPushButton("Группа")
        self._group_btn.setToolTip("Изменить группу доступа для выбранного ключа")
        self._group_btn.clicked.connect(self._on_change_group)
        btn_layout.addWidget(self._group_btn)

        group_layout.addLayout(btn_layout)

        active_row = QHBoxLayout()
        active_row.addWidget(QLabel("Активный ключ:"))
        self._key_combo = QComboBox()
        self._key_combo.setMinimumWidth(150)
        self._key_combo.setToolTip(
            "Ключ, используемый для аутентификации при захвате миниатюр"
        )
        active_row.addWidget(self._key_combo)
        active_row.addSpacing(20)
        active_row.addWidget(QLabel("Метод аутентификации:"))
        self._auth_method_label = QLabel("—")
        self._auth_method_label.setStyleSheet("font-weight: bold;")
        active_row.addWidget(self._auth_method_label)
        active_row.addStretch()
        self._enable_key_auth_btn = QPushButton("Включить аутентификацию по ключу")
        self._enable_key_auth_btn.setToolTip(
            "Установить метод аутентификации по ключу (Authentication/Method=1)"
        )
        self._enable_key_auth_btn.clicked.connect(self._on_enable_key_auth)
        active_row.addWidget(self._enable_key_auth_btn)
        group_layout.addLayout(active_row)

        tab_layout.addWidget(group)
        tab_layout.addStretch()
        return tab

    # --- Вкладка: Диагностика ---

    def _build_diagnostics_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setSpacing(8)

        self._diag_label = QLabel("Нажмите «Проверить» для запуска диагностики.")
        self._diag_label.setTextFormat(Qt.RichText)
        self._diag_label.setWordWrap(True)
        self._diag_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        tab_layout.addWidget(self._diag_label)

        check_btn = QPushButton("Проверить")
        check_btn.clicked.connect(self._run_diagnostics)
        tab_layout.addWidget(check_btn, alignment=Qt.AlignLeft)

        tab_layout.addStretch()
        return tab

    # --- Вкладка: Установка ---

    def _build_install_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setSpacing(10)

        self._install_group = QGroupBox("Установка Veyon (Linux)")
        install_layout = QVBoxLayout(self._install_group)

        info_label = QLabel("Установка пакетов Veyon через менеджер пакетов dnf.")
        info_label.setWordWrap(True)
        install_layout.addWidget(info_label)

        pkg_row = QHBoxLayout()
        pkg_row.addWidget(QLabel("Пакеты:"))
        self._pkg_combo = QComboBox()
        self._pkg_combo.addItem("Veyon (все пакеты)", "veyon veyon-core")
        self._pkg_combo.addItem("Veyon Core (только ядро)", "veyon-core")
        self._pkg_combo.setToolTip("Выбор пакета для установки")
        pkg_row.addWidget(self._pkg_combo, 1)
        install_layout.addLayout(pkg_row)

        btn_row = QHBoxLayout()
        self._install_btn = QPushButton("Установить")
        self._install_btn.clicked.connect(self._on_install)
        btn_row.addWidget(self._install_btn)

        self._remove_btn = QPushButton("Удалить")
        self._remove_btn.clicked.connect(self._on_remove)
        btn_row.addWidget(self._remove_btn)

        self._check_pkg_btn = QPushButton("Проверить наличие")
        self._check_pkg_btn.clicked.connect(self._on_check_package)
        btn_row.addWidget(self._check_pkg_btn)

        install_layout.addLayout(btn_row)

        self._install_progress = QProgressBar()
        self._install_progress.setVisible(False)
        install_layout.addWidget(self._install_progress)

        self._install_log = QPlainTextEdit()
        self._install_log.setReadOnly(True)
        self._install_log.setMinimumHeight(100)
        self._install_log.setPlaceholderText("Вывод команд появится здесь...")
        install_layout.addWidget(self._install_log, 1)

        tab_layout.addWidget(self._install_group, 1)

        self._win_group = QGroupBox("Установка Veyon (Windows)")
        win_layout = QVBoxLayout(self._win_group)
        win_label = QLabel(
            "На Windows Veyon устанавливается через MSI-установщик.\n"
            "Скачайте установщик с официального сайта: "
            '<a href="https://veyon.io/download">veyon.io/download</a>\n'
            "и запустите от имени администратора."
        )
        win_label.setWordWrap(True)
        win_label.setOpenExternalLinks(True)
        win_label.setTextFormat(Qt.RichText)
        win_layout.addWidget(win_label)
        tab_layout.addWidget(self._win_group)

        if sys.platform == "win32":
            self._install_group.setVisible(False)
        else:
            self._win_group.setVisible(False)

        return tab

    # ================================================================
    #  Данные
    # ================================================================

    def _get_veyon_service(self):
        try:
            from src.di import get_container

            return get_container().resolve(VeyonService)
        except Exception as e:
            logger.error(f"VeyonSettingsDialog: cannot resolve VeyonService: {e}")
            return None

    def _show_operation_error(self, operation: str):
        """Показать ошибку операции — прав или другую."""
        service = self._get_veyon_service()
        if service and VeyonService._is_permission_error(service.last_error()):
            self._show_permission_error(operation)
        else:
            err = service.last_error() if service else ""
            QMessageBox.warning(
                self,
                "Ошибка",
                f"Не удалось выполнить: {operation}.\n{err}"
                if err
                else f"Не удалось выполнить: {operation}.",
            )

    def _show_permission_error(self, operation: str):
        """Ошибка прав — обычно пользователь отклонил запрос авторизации."""
        err = ""
        service = self._get_veyon_service()
        if service:
            err = service.last_error() or ""
        detail = f"\n\n{err}" if err else ""
        QMessageBox.warning(
            self,
            "Недостаточно прав",
            f"Не удалось выполнить: {operation}.\n\n"
            f"Операция требует прав администратора.{detail}",
        )

    def _load_local_values(self):
        """Заполнить значения из конфига (мгновенно, без subprocess)."""
        v = self._config.app.veyon
        self._webapi_url_edit.setText(v.webapi_url)

    def _start_loader(self, block_ui: bool = True):
        """Запустить фоновую загрузку данных Veyon CLI.

        Args:
            block_ui: True — блокировать вкладки и показывать индикатор (первичная загрузка).
                      False — только показать индикатор без блокировки (обновление ключей).
        """
        service = self._get_veyon_service()
        if not service:
            self._service_status_label.setText("недоступен")
            if block_ui:
                self._finish_loading()
            return
        # Проверяем наличие veyon-cli (без требования активного сервиса —
        # операции с ключами и конфигом работают через CLI локально).
        cli_found = service.is_cli_available()
        if not cli_found:
            # Без CLI никакие операции невозможны — блокируем вкладки.
            self._set_veyon_tabs_enabled(False)
            if block_ui:
                self._finish_loading()
            return
        if block_ui:
            self._loading = True
            self._set_veyon_tabs_enabled(False)
        self._load_label.setText("Загрузка данных Veyon…")
        self._load_label.setVisible(True)
        self._load_progress.setVisible(True)
        self._loader = _VeyonLoader(service, self)
        self._loader.loaded.connect(self._on_loaded)
        self._start_background_thread(self._loader)

    def _finish_loading(self):
        """Разблокировать вкладки и скрыть индикатор загрузки."""
        self._loading = False
        self._set_veyon_tabs_enabled(True)
        self._load_progress.setVisible(False)
        self._load_label.setVisible(False)

    def _on_loaded(self, data: dict):
        """Обработчик результатов фоновой загрузки — заполняет UI."""
        # Сбрасываем текст на случай, если он был изменён при reload_keys
        self._load_label.setText("Загрузка данных Veyon…")
        # Таблица ключей
        self._keys_table.setRowCount(0)
        for entry in data.get("keys_detailed", []):
            row = self._keys_table.rowCount()
            self._keys_table.insertRow(row)
            self._keys_table.setItem(row, 0, QTableWidgetItem(entry.get("name", "")))
            self._keys_table.setItem(row, 1, QTableWidgetItem(entry.get("type", "")))
            self._keys_table.setItem(row, 2, QTableWidgetItem(entry.get("pair_id", "")))
            self._keys_table.setItem(row, 3, QTableWidgetItem(entry.get("group", "")))

        # ComboBox активного ключа
        keys = data.get("keys", [])
        v = self._config.app.veyon
        self._key_combo.clear()
        self._key_combo.addItems(keys)
        idx = self._key_combo.findText(v.key_name)
        if idx >= 0:
            self._key_combo.setCurrentIndex(idx)
        elif keys:
            self._key_combo.setCurrentIndex(0)

        # WebAPI
        self._webapi_enabled_chk.blockSignals(True)
        self._webapi_enabled_chk.setChecked(data.get("webapi_enabled", False))
        self._webapi_enabled_chk.blockSignals(False)

        port = data.get("port", 11080)
        self._port_spin.blockSignals(True)
        self._port_spin.setValue(port)
        self._port_spin.blockSignals(False)

        # Лимит соединений WebAPI (ползунок 5..10 → 32..1024)
        # Приоритет: settings.json (мгновенно), fallback — service read (subprocess)
        conn_limit = getattr(self._config.app.veyon, "webapi_connection_limit", 0) or 0
        if conn_limit <= 0:
            conn_limit = data.get("conn_limit", 32)
        slider_val = self._conn_limit_slider_from_value(conn_limit)
        self._conn_limit_slider.blockSignals(True)
        self._conn_limit_slider.setValue(slider_val)
        self._conn_limit_value_label.setText(
            str(self._conn_limit_value_from_slider(slider_val))
        )
        self._conn_limit_slider.blockSignals(False)

        # Транспорт фичей (webapi | cli) — из settings.json
        transport = getattr(self._config.app.veyon, "feature_transport", "webapi") or "webapi"
        idx = 0 if transport == "webapi" else 1
        self._feature_transport_combo.blockSignals(True)
        self._feature_transport_combo.setCurrentIndex(idx)
        self._feature_transport_combo.blockSignals(False)

        # Статус сервиса
        self._apply_service_status(data.get("service_status", "unknown"))

        # Блок «Вход по ключу» — обновление статуса
        self._update_key_auth_status(keys, data.get("auth_method"))

        # Разблокировать UI и скрыть индикатор
        self._finish_loading()

    def _update_key_auth_status(self, keys: list = None, auth_method=None):
        """Обновить метку метода аутентификации."""
        if auth_method == 1:
            self._auth_method_label.setText("по ключу")
            self._auth_method_label.setStyleSheet("font-weight: bold; color: green;")
        elif auth_method == 2:
            self._auth_method_label.setText("по паролю")
            self._auth_method_label.setStyleSheet("font-weight: bold; color: #b8860b;")
        elif auth_method == 0:
            self._auth_method_label.setText("отключён")
            self._auth_method_label.setStyleSheet("font-weight: bold; color: #b8860b;")
        else:
            self._auth_method_label.setText("неизвестно")
            self._auth_method_label.setStyleSheet("font-weight: bold; color: gray;")

    # ================================================================
    #  WebAPI / Сервис
    # ================================================================

    def _apply_service_status(self, status: str):
        """Применить статус сервиса к UI (вызывается в главном потоке)."""
        # Кнопки start/stop/restart доступны всегда (элевация по требованию,
        # UAC / pkexec) — гейт только по статусу: нет смысла «Запускать»
        # запущенный сервис или «Останавливать» остановленный.
        if status == "running":
            self._service_status_label.setText("● Запущен")
            self._service_status_label.setStyleSheet(
                "font-weight: bold; color: #27ae60; padding: 4px;"
            )
            self._service_btn.setEnabled(False)
            self._stop_btn.setEnabled(True)
        elif status == "stopped":
            self._service_status_label.setText("● Остановлен")
            self._service_status_label.setStyleSheet(
                "font-weight: bold; color: #c0392b; padding: 4px;"
            )
            self._service_btn.setEnabled(True)
            self._stop_btn.setEnabled(False)
        else:
            self._service_status_label.setText(f"● {status}")
            self._service_status_label.setStyleSheet(
                "font-weight: bold; color: gray; padding: 4px;"
            )
            self._service_btn.setEnabled(True)
            self._stop_btn.setEnabled(True)

    def _update_service_status(self):
        """Асинхронно проверить статус сервиса."""
        service = self._get_veyon_service()
        if not service:
            self._service_status_label.setText("недоступен")
            return
        self._service_status_label.setText("● Проверка...")
        self._status_checker = _StatusChecker(service, self)
        self._status_checker.status_ready.connect(self._apply_service_status)
        self._start_background_thread(self._status_checker)

    def _on_service_button(self):
        service = self._get_veyon_service()
        if not service:
            return
        self._service_status_label.setText("● Запуск...")
        self._service_status_label.setStyleSheet(
            "font-weight: bold; color: gray; padding: 4px;"
        )
        self._service_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._service_op_thread = _ServiceOperationThread(service, "start", self)
        self._service_op_thread.operation_done.connect(self._on_service_op_done)
        self._start_background_thread(self._service_op_thread)

    def _on_stop_service(self):
        service = self._get_veyon_service()
        if not service:
            return
        reply = QMessageBox.question(
            self,
            "Veyon Service",
            "Остановить Veyon Service?\nWebAPI и миниатюры перестанут работать.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._service_status_label.setText("● Остановка...")
        self._service_status_label.setStyleSheet(
            "font-weight: bold; color: gray; padding: 4px;"
        )
        self._service_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._service_op_thread = _ServiceOperationThread(service, "stop", self)
        self._service_op_thread.operation_done.connect(self._on_service_op_done)
        self._start_background_thread(self._service_op_thread)

    def _on_restart_service(self):
        service = self._get_veyon_service()
        if not service:
            return
        self._service_status_label.setText("● Перезапуск...")
        self._service_status_label.setStyleSheet(
            "font-weight: bold; color: gray; padding: 4px;"
        )
        self._service_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._service_op_thread = _ServiceOperationThread(service, "restart", self)
        self._service_op_thread.operation_done.connect(self._on_service_op_done)
        self._start_background_thread(self._service_op_thread)

    def _on_service_op_done(self, success: bool, operation: str):
        """Обработчик завершения операции с сервисом."""
        if not success:
            op_names = {"start": "запуск", "stop": "остановка", "restart": "перезапуск"}
            op_text = f"{op_names.get(operation, operation)} Veyon Service"
            self._show_operation_error(op_text)
        QTimer.singleShot(3000, self._update_service_status)

    def _on_register_service(self):
        service = self._get_veyon_service()
        if not service:
            return
        self._register_btn.setEnabled(False)
        self._run_elevated_op(
            service.register_service,
            on_done=lambda ok: self._on_register_service_done(ok),
        )

    def _on_register_service_done(self, ok: bool):
        """Завершение регистрации сервиса в автозапуск (GUI-поток)."""
        self._register_btn.setEnabled(True)
        if ok:
            QMessageBox.information(
                self, "Автозапуск", "Veyon Service зарегистрирован для автозапуска."
            )
        else:
            self._show_operation_error("регистрация сервиса в автозапуск")

    def _on_unregister_service(self):
        service = self._get_veyon_service()
        if not service:
            return
        reply = QMessageBox.question(
            self,
            "Автозапуск",
            "Убрать Veyon Service из автозапуска?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._unregister_btn.setEnabled(False)
        self._run_elevated_op(
            service.unregister_service,
            on_done=lambda ok: self._on_unregister_service_done(ok),
        )

    def _on_unregister_service_done(self, ok: bool):
        """Завершение отмены автозапуска сервиса (GUI-поток)."""
        self._unregister_btn.setEnabled(True)
        if ok:
            QMessageBox.information(
                self, "Автозапуск", "Veyon Service убран из автозапуска."
            )
        else:
            self._show_operation_error("отмена автозапуска сервиса")

    def _on_webapi_enabled_changed(self):
        service = self._get_veyon_service()
        if not service:
            return
        enabled = self._webapi_enabled_chk.isChecked()
        # Блокируем повторное переключение на время операции (элевация).
        self._webapi_enabled_chk.setEnabled(False)
        self._run_elevated_op(
            lambda: service.set_webapi_enabled(enabled),
            on_done=lambda ok: self._on_webapi_enabled_done(ok, enabled),
        )

    def _on_webapi_enabled_done(self, ok: bool, enabled: bool):
        """Завершение включения/отключения WebAPI (GUI-поток)."""
        service = self._get_veyon_service()
        self._webapi_enabled_chk.setEnabled(True)
        if not ok:
            self._show_operation_error("включение/отключение WebAPI")
            if service:
                self._webapi_enabled_chk.blockSignals(True)
                self._webapi_enabled_chk.setChecked(service.get_webapi_enabled())
                self._webapi_enabled_chk.blockSignals(False)
            return
        reply = QMessageBox.question(
            self,
            "Перезапуск сервиса",
            "Для применения изменений WebAPI требуется перезапустить Veyon Service. Перезапустить сейчас?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            QTimer.singleShot(500, self._do_restart_service)

    def _do_restart_service(self):
        service = self._get_veyon_service()
        if not service:
            return
        # restart_service() верифицирует новый PID до 25 сек — блокировать
        # GUI-поток на это время нельзя: выполняем в фоновом потоке,
        # событие состояния публикует сам VeyonService по факту успеха.
        self._service_status_label.setText("● Перезапуск...")
        self._service_status_label.setStyleSheet(
            "font-weight: bold; color: gray; padding: 4px;"
        )
        self._service_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._service_op_thread = _ServiceOperationThread(service, "restart", self)
        self._service_op_thread.operation_done.connect(self._on_service_op_done)
        self._start_background_thread(self._service_op_thread)

    # ================================================================
    #  Ключи CRUD
    # ================================================================

    def _get_selected_key_name(self) -> str:
        row = self._keys_table.currentRow()
        if row < 0:
            return ""
        item = self._keys_table.item(row, 0)
        return item.text() if item else ""

    def _get_selected_key_type(self) -> str:
        row = self._keys_table.currentRow()
        if row < 0:
            return ""
        item = self._keys_table.item(row, 1)
        return item.text() if item else ""

    def _on_enable_key_auth(self):
        """Включить метод аутентификации по ключу (Authentication/Method=1)."""
        service = self._get_veyon_service()
        if not service:
            QMessageBox.warning(self, "Veyon", "VeyonService недоступен.")
            return

        active = self._config.app.veyon.key_name or self._key_combo.currentText().strip()
        if not active:
            QMessageBox.warning(
                self,
                "Аутентификация по ключу",
                "Не выбран активный ключ. Сначала создайте или выберите пару ключей.",
            )
            return

        self._enable_key_auth_btn.setEnabled(False)
        self._enable_key_auth_btn.setText("Включение…")
        self._run_elevated_op(
            service.set_auth_method_key_file,
            on_done=lambda ok: self._on_enable_key_auth_done(ok),
        )

    def _on_enable_key_auth_done(self, ok: bool):
        """Завершение включения аутентификации по ключу (GUI-поток)."""
        self._enable_key_auth_btn.setEnabled(True)
        self._enable_key_auth_btn.setText("Включить аутентификацию по ключу")
        service = self._get_veyon_service()
        auth_method = None
        if service:
            try:
                auth_method = service.get_auth_method()
            except Exception:
                auth_method = None
        self._update_key_auth_status(None, auth_method)

        if ok and auth_method == 1:
            QMessageBox.information(
                self,
                "Аутентификация по ключу",
                "Метод аутентификации по ключу включён (Authentication/Method=1).",
            )
        else:
            self._show_operation_error("включение метода аутентификации по ключу")

    def _on_create_key(self):
        name, ok = QInputDialog.getText(
            self, "Создать пару ключей", "Имя ключа:", text="teacher"
        )
        if not ok or not name.strip():
            return
        service = self._get_veyon_service()
        if not service:
            return
        self._create_key_btn.setEnabled(False)
        self._run_elevated_op(
            lambda: service.create_auth_key(name.strip()),
            on_done=lambda ok2: self._on_key_op_done(
                ok2, f"создание ключа '{name}'", reload_after=True
            ),
        )

    def _on_key_op_done(self, ok: bool, operation: str, reload_after: bool = True):
        """Общий обработчик завершения операции с ключом (GUI-поток)."""
        for btn_name in (
            "_create_key_btn",
            "_delete_key_btn",
            "_export_key_btn",
            "_import_key_btn",
            "_extract_btn",
            "_group_btn",
        ):
            w = getattr(self, btn_name, None)
            if w is not None:
                w.setEnabled(True)
        if not ok:
            self._show_operation_error(operation)
            return
        if reload_after:
            self._reload_keys()

    def _on_delete_key(self):
        name = self._get_selected_key_name()
        key_type = self._get_selected_key_type()
        if not name:
            QMessageBox.information(self, "Удалить ключ", "Выберите ключ в таблице.")
            return
        if not key_type:
            key_type = "private"
        reply = QMessageBox.question(
            self,
            "Удалить ключ",
            f"Удалить ключ '{name}/{key_type}'?\nЭто действие нельзя отменить.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        service = self._get_veyon_service()
        if not service:
            return
        self._delete_key_btn.setEnabled(False)
        self._run_elevated_op(
            lambda: service.delete_auth_key_single(name, key_type),
            on_done=lambda ok2: self._on_key_op_done(
                ok2, f"удаление ключа '{name}/{key_type}'"
            ),
        )

    def _on_export_key(self):
        name = self._get_selected_key_name()
        key_type = self._get_selected_key_type()
        if not name or not key_type:
            QMessageBox.information(self, "Экспорт", "Выберите ключ в таблице.")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Экспорт ключа",
            f"{name}_{key_type}_key.pem",
            "PEM files (*.pem);;All files (*)",
        )
        if not file_path:
            return
        service = self._get_veyon_service()
        if not service:
            return
        self._export_key_btn.setEnabled(False)
        self._run_elevated_op(
            lambda: service.export_auth_key(name, key_type, file_path),
            on_done=lambda ok2: self._on_export_key_done(
                ok2, name, key_type, file_path
            ),
        )

    def _on_export_key_done(self, ok: bool, name: str, key_type: str, file_path: str):
        """Завершение экспорта ключа (GUI-поток)."""
        self._export_key_btn.setEnabled(True)
        if ok:
            QMessageBox.information(
                self, "Экспорт", f"Ключ {name}/{key_type} экспортирован в:\n{file_path}"
            )
        else:
            self._show_operation_error(f"экспорт ключа '{name}/{key_type}'")

    def _on_import_key(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Импорт ключа", "", "PEM files (*.pem);;All files (*)"
        )
        if not file_path:
            return

        filename = os.path.basename(file_path)
        match = re.match(r"^(.+?)_(private|public)_key\.pem$", filename, re.IGNORECASE)

        service = self._get_veyon_service()
        if not service:
            return

        if match:
            name = match.group(1)
            key_type = match.group(2).lower()
            self._import_key_btn.setEnabled(False)
            self._run_elevated_op(
                lambda: service.import_auth_key(name, key_type, file_path),
                on_done=lambda ok2: self._on_import_key_done(
                    ok2, name, key_type, file_path, show_info=True
                ),
            )
        else:
            name, ok = QInputDialog.getText(
                self, "Имя ключа", "Имя для ключа:", text="imported"
            )
            if not ok or not name.strip():
                if ok:
                    QMessageBox.warning(
                        self, "Ошибка", "Имя ключа не может быть пустым."
                    )
                return
            name = name.strip()
            key_type, ok2 = QInputDialog.getItem(
                self,
                "Тип ключа",
                "Тип импортируемого ключа:",
                ["private", "public"],
                0,
                False,
            )
            if not ok2 or not key_type:
                return
            self._import_key_btn.setEnabled(False)
            self._run_elevated_op(
                lambda: service.import_auth_key(name, key_type, file_path),
                on_done=lambda ok3: self._on_import_key_done(
                    ok3, name, key_type, file_path
                ),
            )

    def _on_import_key_done(
        self,
        ok: bool,
        name: str,
        key_type: str,
        file_path: str,
        show_info: bool = False,
    ):
        """Завершение импорта ключа (GUI-поток)."""
        self._import_key_btn.setEnabled(True)
        if not ok:
            self._show_operation_error(f"импорт ключа '{name}/{key_type}'")
            return
        self._reload_keys()
        if show_info:
            QMessageBox.information(
                self,
                "Импорт",
                f"Ключ '{name}/{key_type}' импортирован из:\n{file_path}",
            )

    def _on_extract_key(self):
        name = self._get_selected_key_name()
        if not name:
            QMessageBox.information(self, "Извлечь public", "Выберите ключ в таблице.")
            return
        service = self._get_veyon_service()
        if not service:
            return
        self._extract_btn.setEnabled(False)
        self._run_elevated_op(
            lambda: service.extract_public_key(name),
            on_done=lambda ok2: self._on_extract_key_done(ok2, name),
        )

    def _on_extract_key_done(self, ok: bool, name: str):
        """Завершение извлечения открытого ключа (GUI-поток)."""
        self._extract_btn.setEnabled(True)
        if ok:
            self._reload_keys()
            QMessageBox.information(
                self, "Извлечение", f"Открытый ключ '{name}' извлечён."
            )
        else:
            self._show_operation_error(f"извлечение открытого ключа '{name}'")

    def _on_change_group(self):
        name = self._get_selected_key_name()
        key_type = self._get_selected_key_type()
        if not name or not key_type:
            QMessageBox.information(self, "Группа доступа", "Выберите ключ в таблице.")
            return
        row = self._keys_table.currentRow()
        current_group = ""
        group_item = self._keys_table.item(row, 3)
        if group_item:
            current_group = group_item.text()

        service = self._get_veyon_service()
        if not service:
            return

        # Подгрузка списка групп (локальные + доменные) — синхронно, без кэша.
        groups = service.list_user_groups(include_domain=True)
        # Текущая группа (например «BUILTIN\Пользователи») может отличаться
        # формой от имён в списке («Пользователи») — ищем по нормализованному
        # имени, дубликаты формы не добавляем.
        norm = VeyonService._normalize_group_name
        current_idx = next(
            (i for i, g in enumerate(groups) if norm(g) == norm(current_group)), 0
        )
        if current_group and norm(current_group) not in (norm(g) for g in groups):
            groups = [current_group] + groups
            current_idx = 0

        new_group, ok = QInputDialog.getItem(
            self,
            "Группа доступа",
            f"Группа доступа для ключа '{name}/{key_type}':",
            groups,
            current_idx,
            True,  # editable=True — как в Veyon Configurator
        )
        if not ok or not new_group:
            return
        new_group = new_group.strip()
        if not new_group:
            return
        self._group_btn.setEnabled(False)
        self._run_elevated_op(
            lambda: service.set_key_access_group(name, key_type, new_group),
            on_done=lambda ok2: self._on_change_group_done(
                ok2, name, key_type, new_group
            ),
        )

    def _on_change_group_done(self, ok: bool, name: str, key_type: str, group: str):
        """Завершение смены группы доступа (GUI-поток)."""
        if not ok:
            self._on_key_op_done(
                False, f"изменение группы доступа для '{name}/{key_type}'"
            )
            return
        self._on_key_op_done(True, "", reload_after=True)
        QMessageBox.information(
            self,
            "Группа доступа",
            f"Группа доступа для ключа '{name}/{key_type}' изменена на '{group}'.\n\n"
            "Если ключ отображается как «???» или группа пустая — текущий "
            "пользователь не входит в выбранную группу: читать ключ могут "
            "только её члены и Администраторы (ограничение Veyon).",
        )

    def _reload_keys(self):
        """Перезагрузить таблицу ключей и ComboBox через фоновый поток.

        Вкладки остаются активными — показывается только индикатор загрузки.
        """
        service = self._get_veyon_service()
        if not service:
            return
        self._load_label.setText("Обновление списка ключей…")
        self._load_label.setVisible(True)
        self._load_progress.setVisible(True)
        self._loader = _VeyonLoader(service, self)
        self._loader.loaded.connect(self._on_loaded)
        self._start_background_thread(self._loader)

    # ================================================================
    #  Установка пакетов (Linux)
    # ================================================================

    def _start_package_operation(self, cmd: list, action: str):
        """Запустить операцию dnf в фоновом потоке."""
        self._install_log.clear()
        self._install_progress.setVisible(True)
        self._install_progress.setRange(0, 0)
        self._install_log.appendPlainText(f"$ {' '.join(cmd)}\n")

        self._pkg_thread = _PackageOperationThread(cmd, self)
        self._pkg_thread.output_ready.connect(self._install_log.appendPlainText)
        self._pkg_thread.finished_signal.connect(
            lambda rc: self._on_package_done(rc, action)
        )
        self._start_background_thread(self._pkg_thread)

    def _on_package_done(self, return_code: int, action: str):
        """Обработчик завершения операции dnf."""
        self._install_progress.setVisible(False)
        if return_code == 0:
            self._install_log.appendPlainText(
                f"\n{action.capitalize()} завершена успешно."
            )
            self._notify_state_changed()
        elif return_code == -1:
            self._install_log.appendPlainText(f"\n{action.capitalize()} прервана.")
        else:
            self._install_log.appendPlainText(
                f"\n{action.capitalize()} завершена с ошибкой (код {return_code})."
            )

    def _notify_state_changed(self):
        """Уведомить UI об изменении состояния локального Veyon.

        Публикует DATA_SAVED (veyon_config): MainWindow обновляет видимость
        Veyon-тулбара, кнопок и миниатюр немедленно, а сервисный кэш
        детектора установки сбрасывается (установка/удаление пакетов,
        рестарт сервиса могли изменить доступность Veyon).
        """
        service = self._get_veyon_service()
        if service:
            service.invalidate_installed_cache()
        try:
            from src.architecture import EventBus, EventType
            from src.di import get_container

            get_container().resolve(EventBus).publish_typed(
                event_type=EventType.DATA_SAVED,
                source="VeyonConfigWidget",
                data={"type": "veyon_config"},
            )
        except Exception as e:
            logger.debug(f"VeyonConfigWidget: cannot publish state change: {e}")

    def _on_install(self):
        packages = self._pkg_combo.currentData()
        if not packages:
            return
        reply = QMessageBox.question(
            self,
            "Установка",
            f"Установить пакеты: {packages}?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        cmd = ["dnf", "install", "-y"] + packages.split()
        self._start_package_operation(cmd, "установка")

    def _on_remove(self):
        packages = self._pkg_combo.currentData()
        if not packages:
            return
        reply = QMessageBox.question(
            self,
            "Удаление",
            f"Удалить пакеты: {packages}?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        cmd = ["dnf", "remove", "-y"] + packages.split()
        self._start_package_operation(cmd, "удаление")

    def _on_check_package(self):
        packages = self._pkg_combo.currentData()
        if not packages:
            return
        self._install_log.clear()
        self._install_log.appendPlainText("Проверка установленных пакетов...\n")
        self._check_pkg_btn.setEnabled(False)
        thread = _PackageCheckThread(packages.split(), self)
        thread.output_ready.connect(self._install_log.appendPlainText)
        thread.finished.connect(self._on_package_check_finished)
        self._start_background_thread(thread)

    def _on_package_check_finished(self):
        self._check_pkg_btn.setEnabled(True)

    # ================================================================
    #  Сохранение
    # ================================================================

    def save(self):
        """Сохранить настройки Veyon в конфиг и применить параметры."""
        from dataclasses import replace
        from src.config.settings import THUMBNAIL_CONFIG_LOCK

        with THUMBNAIL_CONFIG_LOCK:
            self._config.app.veyon = replace(
                self._config.app.veyon,
                webapi_url=self._webapi_url_edit.text().strip(),
                key_name=self._key_combo.currentText().strip(),
            )

        self._config.save()
        logger.info("VeyonConfigWidget: values collected")
        if self._draft_mode:
            return

        try:
            from src.di import get_container
            from src.services.configuration.config_service import ConfigService

            get_container().resolve(ConfigService).notify_thumbnail_settings_changed()
        except Exception as e:
            logger.error(f"VeyonConfigWidget: cannot update timer params: {e}")

    def _on_save(self):
        self.save()

    def is_configured(self) -> bool:
        """Возвращает True, если Veyon CLI найден, есть хотя бы один ключ и выбран активный ключ."""
        service = self._get_veyon_service()
        if not service:
            return False
        try:
            keys = service.list_auth_keys()
            if not keys:
                return False
            active = self._config.app.veyon.key_name or self._key_combo.currentText().strip()
            return bool(active and active in keys)
        except Exception:
            return False

    # ================================================================
    #  Диагностика
    # ================================================================

    def _run_diagnostics(self):
        """Запустить диагностику асинхронно."""
        service = self._get_veyon_service()
        self._diag_label.setText("Проверка...")

        webapi_url = self._webapi_url_edit.text().strip()
        port = str(self._port_spin.value())
        key_name = self._key_combo.currentText().strip()

        self._diag_runner = _DiagnosticsRunner(
            service, webapi_url, port, key_name, self
        )
        self._diag_runner.diagnostics_ready.connect(self._on_diagnostics_ready)
        self._start_background_thread(self._diag_runner)

    def _on_diagnostics_ready(self, html: str):
        """Обработчик результатов диагностики."""
        self._diag_label.setText(html)


# ================================================================
#  Диалог-обёртка
# ================================================================


class VeyonSettingsDialog(QDialog):
    """Диалог настроек Veyon — тонкая обёртка над VeyonConfigWidget."""

    def __init__(self, parent=None, elevated: bool = True):
        super().__init__(parent)
        self._closing_result = None
        self._save_on_close = False
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Настройки Veyon")
        self.setMinimumWidth(540)
        self.setMinimumHeight(420)

        layout = QVBoxLayout(self)
        self.widget = VeyonConfigWidget(self, elevated=elevated)
        layout.addWidget(self.widget)

        buttons = QDialogButtonBox()
        self._save_btn = buttons.addButton("Сохранить", QDialogButtonBox.AcceptRole)
        self._cancel_btn = buttons.addButton("Отмена", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def show_webapi_section(self) -> None:
        """Open the dialog directly on the WebAPI connection limit."""
        self.widget.show_webapi_section()

    def _on_save(self):
        if self._closing_result is not None:
            return
        self._save_on_close = True
        self._request_done(QDialog.DialogCode.Accepted)

    def _request_done(self, result):
        if self._closing_result is not None:
            if result == QDialog.DialogCode.Rejected:
                self._closing_result = result
                self._save_on_close = False
            return
        self._closing_result = result
        self.widget.stop_background()
        self._save_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)
        self._poll_done()

    def _poll_done(self):
        if any(thread.isRunning() for thread in self.widget._background_threads):
            QTimer.singleShot(50, self._poll_done)
            return
        if self._save_on_close:
            try:
                self.widget.save()
            except Exception:
                self._closing_result = None
                self._save_on_close = False
                self.widget._shutting_down = False
                self._save_btn.setEnabled(True)
                self._cancel_btn.setEnabled(True)
                QMessageBox.critical(self, "Сохранение Veyon", "Не удалось сохранить настройки Veyon.")
                return
        result = self._closing_result
        self._closing_result = None
        self._save_on_close = False
        super().done(result)

    def done(self, result):
        self._request_done(result)

    def reject(self):
        self._request_done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        event.ignore()
        self.reject()
