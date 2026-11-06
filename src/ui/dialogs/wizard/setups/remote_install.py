"""Оркестратор удаленной установки сервисов (sid="remote").

Страница выбора пакетов живет в setups/packages.py.
Выделено из src/ui/dialogs/tools/setup_wizard.py (монолит).
"""

from PySide6.QtCore import Qt, QSize, Signal, QObject, Slot
from PySide6.QtWidgets import (
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView as QHV,
    QAbstractItemView,
    QSplitter,
    QMessageBox,
    QDialog,
)

from src.config import config
from src.config.settings import DEFAULT_VNC_SERVER
from src.logger import logger
from src.ui.icons import _icon_from_key
from src.ui.widgets.device_tree import DeviceTreeView
from src.workers.setup.worker import (
    RESULT_SUCCESS,
    RESULT_ERROR,
    RESULT_IGNORE,
    RESULT_ABORT,
    RESULT_WAITING,
)

from ..core.context import InstallPlan, WizardContext
from ..core.registry import REMOTE_SERVICES, SERVICE_LABELS
from .base import SetupPage, hint_label


STATUS_ICONS = {
    RESULT_SUCCESS: "result_success",
    RESULT_ERROR: "result_failure",
    RESULT_IGNORE: "result_skip",
    RESULT_ABORT: "result_cancelled",
    RESULT_WAITING: "result_pending",
}
STATUS_PENDING_ICON = "result_pending"
STATUS_TEXTS = {
    RESULT_SUCCESS: "Успех",
    RESULT_ERROR: "Неудача",
    RESULT_IGNORE: "Пропуск",
    RESULT_ABORT: "Отмена",
    RESULT_WAITING: "Ожидает готовности",
}


class _RunResultReceiver(QObject):
    """GUI-thread receiver; worker-thread lambdas can lose queued pool results."""

    def __init__(self, page, run_id):
        super().__init__(page)
        self.page = page
        self.run_id = run_id

    @Slot(object, str, int, str)
    def result(self, device, service, code, output):
        self.page._dispatch_run(
            self.run_id, self.page._on_service_result, device, service, code, output
        )

    @Slot(str, str, str)
    def log(self, host, service, message):
        self.page._dispatch_run(
            self.run_id, self.page._on_host_log, host, service, message
        )

    @Slot(str)
    def error(self, message):
        self.page._dispatch_run(self.run_id, self.page._on_remote_error, message)

    @Slot()
    def finished(self):
        self.page._dispatch_run(self.run_id, self.page._on_remote_install_finished)


class RemoteInstallPage(SetupPage):
    """Удаленная потоковая установка сервисов на отмеченные хосты."""

    sid = "remote"

    install_state_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        hint = hint_label(
            "Отметьте хосты в дереве для установки. Ключи (SSH и Veyon) готовятся "
            "на локальных шагах и только деплоятся на хосты. Статус обновляется "
            "в таблице по мере выполнения. Двойной клик по строке - детальный лог хоста.\n"
            "Если включено sudo (на шаге SSH-ключи или в колонке Sudo у хоста), "
            "установка пакетов выполняется через sudo с паролем хоста."
        )
        layout.addWidget(hint)

        svc_row = QHBoxLayout()
        svc_label = QLabel("Сервисы:")
        svc_label.setStyleSheet("font-weight: bold;")
        svc_row.addWidget(svc_label)
        self._svc_label = QLabel()
        self._svc_label.setStyleSheet("color: gray;")
        svc_row.addWidget(self._svc_label)
        svc_row.addStretch()
        layout.addLayout(svc_row)

        top_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._remote_tree = DeviceTreeView(
            read_only=True, enable_ping=False, show_checkboxes=True, show_columns=False
        )
        self._remote_tree.setMinimumWidth(280)
        top_splitter.addWidget(self._remote_tree)

        table_wrap = QLabel() if False else None
        # Обертка таблицы + кнопки (отдельный виджет для сплиттера).
        from PySide6.QtWidgets import QWidget as _QWidget

        table_wrap = _QWidget()
        table_layout = QVBoxLayout(table_wrap)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(4)

        self._remote_table = QTableWidget(0, 1)
        self._remote_table.setHorizontalHeaderLabels(["Хост"])
        self._remote_table.verticalHeader().setVisible(False)
        self._remote_table.setIconSize(QSize(20, 20))
        self._remote_table.verticalHeader().setDefaultSectionSize(26)
        self._remote_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._remote_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._remote_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._remote_table.cellDoubleClicked.connect(self._on_table_double_click)
        table_layout.addWidget(self._remote_table, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._remote_install_btn = QPushButton("Установить")
        self._remote_install_btn.setIcon(_icon_from_key("result_executing"))
        self._remote_install_btn.setIconSize(QSize(18, 18))
        self._remote_install_btn.clicked.connect(self._on_install_clicked)
        btn_row.addWidget(self._remote_install_btn)

        self._remote_abort_btn = QPushButton("Прервать")
        self._remote_abort_btn.clicked.connect(self.abort_install)
        self._remote_abort_btn.setVisible(False)
        btn_row.addWidget(self._remote_abort_btn)

        table_layout.addLayout(btn_row)

        top_splitter.addWidget(table_wrap)
        top_splitter.setStretchFactor(1, 1)
        layout.addWidget(top_splitter, 3)

        log_label = QLabel("Общий лог:")
        log_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(log_label)

        self._remote_log = QPlainTextEdit()
        self._remote_log.setReadOnly(True)
        self._remote_log.setMaximumHeight(160)
        self._remote_log.setStyleSheet("font-family: monospace;")
        layout.addWidget(self._remote_log)

        self._service_col_map: dict = {}
        self._host_row_map: dict = {}
        self._row_host_map: dict = {}
        self._row_host_name: dict = {}
        self._host_logs: dict = {}
        self._worker = None
        self._thread = None
        self._remote_worker = None
        self._remote_thread = None
        self._install_active = False
        self._install_ran = False
        self._guard = None
        self._ctx_services = {key: True for key in REMOTE_SERVICES}
        self._pending_ctx = None
        self._last_ctx = None

    # --- связи ---

    def bind(self, guard, collect_callback, veyon_service):
        self._guard = guard
        self._collect_callback = collect_callback
        self._veyon_service = veyon_service
        self._preparation = None
        self._run_id = None
        self._abort_requested = False
        self._table_signature = None

    def set_services(self, services: dict) -> None:
        self._ctx_services = dict(services or {})

    # --- контракт SetupPage ---

    def on_enter(self, ctx: WizardContext) -> None:
        self._last_ctx = ctx
        if getattr(ctx, "services", None):
            self._ctx_services = dict(ctx.services)
        self._populate_remote(ctx)

    def collect(self, ctx: WizardContext) -> None:
        self._last_ctx = ctx
        res = ctx.install_result
        res.host_row_map = dict(self._host_row_map)
        res.row_host_map = dict(self._row_host_map)
        res.row_host_name = dict(self._row_host_name)
        res.service_col_map = dict(self._service_col_map)
        res.host_logs = {
            k: dict(v) if isinstance(v, dict) else v for k, v in self._host_logs.items()
        }
        res.ran = bool(self._install_ran)

    def summary_html(self, ctx) -> str:
        if not self._install_ran:
            return "Установка: не выполнялась"
        from collections import Counter

        counts = Counter()
        for row in range(self._remote_table.rowCount()):
            for col in self._service_col_map.values():
                item = self._remote_table.item(row, col)
                counts[item.data(Qt.ItemDataRole.UserRole) if item else "pending"] += 1
        return "Установка: " + ", ".join(
            f"{STATUS_TEXTS.get(code, 'ожидание')}: {count}"
            for code, count in counts.items()
        )

    # --- внутреннее: populate/rebuild ---

    def _active_services(self, ctx=None) -> dict:
        if ctx is not None and getattr(ctx, "services", None):
            return dict(ctx.services)
        return dict(self._ctx_services)

    def _populate_remote(self, ctx=None) -> None:
        """Наполнить страницу удаленной установки текущими хостами."""
        if self._is_remote_install_active():
            return
        current_hosts = self._get_hosts_data(ctx)

        if self._remote_tree is not None:
            previously_checked: set = set()
            if self._install_ran:
                previously_checked = set(self._host_logs.keys())
            self._remote_tree._model.load_tree_data(current_hosts)
            if previously_checked:
                self._restore_checked_iids(previously_checked)

        services = self._active_services(ctx)
        active_names = [
            SERVICE_LABELS[s] for s in REMOTE_SERVICES if services.get(s, False)
        ]
        if active_names:
            self._svc_label.setText("Будут установлены: " + ", ".join(active_names))
        else:
            self._svc_label.setText("Сервисы не выбраны")

        if not self._remote_table_matches_current(ctx):
            self._rebuild_remote_table(ctx)

    def _get_hosts_data(self, ctx=None):
        return list(ctx.hosts_data) if ctx is not None else []

    def _signature(self, ctx):
        import hashlib, json
        from dataclasses import asdict

        return hashlib.sha256(
            json.dumps(
                dict(
                    hosts=ctx.hosts_data,
                    services=ctx.services,
                    backend=ctx.vnc_server,
                    vnc=asdict(ctx.vnc),
                    ssh=asdict(ctx.ssh),
                    veyon=asdict(ctx.veyon),
                    packages=ctx.packages,
                    service_options=ctx.service_options,
                    credentials=asdict(ctx.credentials),
                ),
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()

    def _remote_table_matches_current(self, ctx=None):
        return ctx is not None and self._table_signature == self._signature(ctx)

    def _restore_checked_iids(self, iids: set) -> None:
        """Проставить чекбоксы Checked элементам дерева с данными iid."""
        root = self._remote_tree._model.invisibleRootItem()

        def walk(item):
            for i in range(item.rowCount()):
                child = item.child(i)
                dev = getattr(child, "device", None)
                if dev is not None:
                    iid = getattr(dev, "iid", None) or dev.host
                    if iid in iids and self._remote_tree.show_checkboxes:
                        child.setCheckState(Qt.CheckState.Checked)
                if child.hasChildren():
                    walk(child)

        walk(root)

    def _rebuild_remote_table(self, ctx=None) -> None:
        """Перестроить таблицу статусов по отмеченным хостам и выбранным сервисам."""
        self._table_signature = self._signature(ctx) if ctx is not None else None
        services = self._active_services(ctx)
        active_svcs = [s for s in REMOTE_SERVICES if services.get(s, False)]

        devices = []
        if self._remote_tree is not None:
            devices = self._remote_tree.get_all_checked_devices()

        self._service_col_map = {}
        cols = 1
        for svc in active_svcs:
            self._service_col_map[svc] = cols
            cols += 1

        self._remote_table.setColumnCount(cols)
        from src.ui.dialogs.common.vnc_settings import server_label

        vnc_header = f"VNC ({server_label(self._selected_vnc_server(ctx))})"
        headers = ["Хост"] + [
            vnc_header if s == "vnc" else SERVICE_LABELS[s] for s in active_svcs
        ]
        self._remote_table.setHorizontalHeaderLabels(headers)
        self._remote_table.horizontalHeader().setSectionResizeMode(
            0, QHV.ResizeMode.Stretch
        )
        for c in range(1, cols):
            self._remote_table.horizontalHeader().setSectionResizeMode(
                c, QHV.ResizeMode.ResizeToContents
            )

        self._remote_table.setRowCount(0)
        self._host_row_map = {}
        self._row_host_map = {}
        self._row_host_name = {}
        self._host_logs = {}

        for row, dev in enumerate(devices):
            self._remote_table.insertRow(row)
            host_item = QTableWidgetItem(dev.name or dev.host)
            host_item.setToolTip(dev.host)
            self._remote_table.setItem(row, 0, host_item)
            iid = getattr(dev, "iid", None) or dev.host
            self._host_row_map[iid] = row
            self._row_host_map[row] = iid
            self._row_host_name[iid] = dev.host
            for svc in active_svcs:
                col = self._service_col_map[svc]
                item = QTableWidgetItem()
                item.setIcon(_icon_from_key(STATUS_PENDING_ICON))
                item.setData(Qt.ItemDataRole.UserRole, "pending")
                item.setToolTip("Ожидание")
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._remote_table.setItem(row, col, item)
            self._host_logs[iid] = {}

    def _selected_vnc_server(self, ctx=None) -> str:
        if ctx is not None:
            return ctx.vnc_server
        return getattr(config.app.veyon, "vnc_server", DEFAULT_VNC_SERVER) or DEFAULT_VNC_SERVER

    def _is_remote_install_active(self) -> bool:
        if self._guard is not None:
            try:
                if self._guard.is_busy("remote"):
                    return True
            except Exception:
                pass
        return bool(self._install_active)

    def install_active(self) -> bool:
        return self._is_remote_install_active()

    def install_ran(self) -> bool:
        return bool(self._install_ran)

    # --- запуск ---

    def _on_install_clicked(self) -> None:
        # Кнопка - единственная точка входа: ctx берется из кэша on_enter.
        # Объект ctx у диалога один на всю жизнь мастера, всегда актуален.
        ctx = self._last_ctx
        if ctx is None:
            return
        self.start_install(ctx)

    def start_install(self, ctx: WizardContext) -> None:
        """Запустить потоковую установку сервисов на отмеченные хосты."""
        self._pending_ctx = ctx
        if self._guard is not None and self._guard.is_active():
            return
        self._worker = None
        if self._guard is not None:
            try:
                if self._guard.is_busy("remote"):
                    return
            except Exception:
                pass
        if self._install_active:
            return
        if self._remote_tree is None:
            return

        devices = self._remote_tree.get_all_checked_devices()
        if not devices:
            QMessageBox.information(
                self,
                "Установка",
                "Отметьте хосты для установки (поставьте галочки в дереве).",
            )
            return

        services = {
            svc: bool(self._active_services(ctx).get(svc, False))
            for svc in REMOTE_SERVICES
        }

        if not any(services.values()):
            QMessageBox.information(self, "Установка", "Не выбран ни один сервис.")
            return

        if not self._collect_callback():
            return
        if services.get("ssh") and (not ctx.ssh.pubkey or not ctx.ssh.private_key_path):
            QMessageBox.critical(
                self, "SSH", "Закрытый SSH-ключ отсутствует или пара ключей недоступна. Проверьте шаг SSH."
            )
            return
        from src.workers.setup.preparation import OperatorPreparationWorker
        from dataclasses import asdict
        import shutil, sys

        local_packages = []
        if services.get("vnc") and ctx.vnc_server == "krfb" and sys.platform != "win32":
            local_packages = [
                name
                for name, executable in (("krdc", "krdc"), ("krfb", "kwriteconfig5"))
                if shutil.which(executable) is None
            ]
            if (
                local_packages
                and QMessageBox.question(
                    self,
                    "Локальные пакеты",
                    "Установить на этой машине: " + ", ".join(local_packages) + "?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                local_packages = []
        plan = InstallPlan(
            devices=devices,
            services=services,
            service_options=tuple((key, tuple(values.items())) for key, values in ctx.service_options.items()),
            ssh_pubkey=ctx.ssh.pubkey,
            ssh_private_key_path=ctx.ssh.private_key_path,
            veyon_key_name=ctx.veyon.key_name,
            vnc_password=ctx.vnc.password,
            vnc_port=ctx.vnc.port,
            veyon_server_port=ctx.veyon.settings.get("server_port", 11100),
            vnc_server=ctx.vnc_server,
            extra_packages=ctx.packages,
            use_sudo_override=ctx.ssh.use_sudo
            if services.get("ssh")
            else ctx.baseline["app"].ssh.use_sudo,
            ssh_settings=tuple(asdict(ctx.baseline["app"].ssh).items()),
            default_bundle_iid=ctx.ssh.bundle_iid
            if services.get("ssh")
            else ctx.baseline["app"].credentials.default_bundle_iid,
            credential_bundles=tuple(
                (b.iid, b.login, b.password) for b in ctx.credentials.bundles
            ),
        )
        ctx.install_plan = plan
        self._run_id = plan.run_id
        self._abort_requested = False
        self._rebuild_remote_table(ctx)
        self._remote_log.clear()
        self._install_ran = True
        ctx.applied_actions.append(
            "запущена подготовка и установка на выбранных хостах"
        )
        self._preparation = OperatorPreparationWorker(
            plan, self._veyon_service, local_packages, self
        )
        self._preparation.status.connect(self._remote_log.appendPlainText)
        self._preparation.service_failed.connect(self._on_preparation_service_failed)
        self._preparation.ready.connect(self._on_plan_prepared)
        self._preparation.finished.connect(self._on_preparation_finished)
        self._guard.register_thread(
            "operator_preparation",
            self._preparation,
            16000,
            abort=self.abort_install,
        )
        self._install_active = True
        self._remote_install_btn.setVisible(False)
        self._remote_abort_btn.setVisible(True)
        self.install_state_changed.emit(True)
        self._preparation.start()

    def _on_preparation_service_failed(self, service, error):
        self._remote_log.appendPlainText(error)
        for device in self._pending_ctx.install_plan.devices:
            self._on_service_result(device, service, RESULT_ERROR, error)

    def _on_preparation_finished(self):
        if self._abort_requested and self._install_active and self._worker is None:
            self._on_remote_install_finished()

    def _on_plan_prepared(self, plan, error):
        self._install_active = False
        if self._abort_requested:
            self._finalize_pending(RESULT_ABORT, "Прервано при подготовке")
            self._on_remote_install_finished()
        elif error:
            self._remote_log.appendPlainText(error)
            self._finalize_pending(RESULT_ERROR, error)
            self._on_remote_install_finished()
        else:
            self._pending_ctx.install_plan = plan
            if any(plan.services.values()):
                self._start_worker(self._pending_ctx)
            else:
                self._on_remote_install_finished()

    def _finalize_pending(self, code, message):
        plan = self._pending_ctx.install_plan
        for device in plan.devices:
            for service, col in self._service_col_map.items():
                item = self._remote_table.item(self._host_row_map[device.iid], col)
                if item.data(Qt.ItemDataRole.UserRole) == "pending":
                    self._on_service_result(device, service, code, message)

    def _start_worker(self, ctx: WizardContext) -> None:
        from PySide6.QtCore import QThread
        from src.workers.setup.worker import (
            RemoteServiceInstallerWorker,
            RESULT_SUCCESS as _RS,
            RESULT_ERROR as _RE,
            RESULT_IGNORE as _RI,
            RESULT_ABORT as _RA,
        )

        _ = (_RS, _RE, _RI, _RA)

        plan = ctx.install_plan
        if plan is None or not getattr(plan, "devices", None):
            return

        self._worker = RemoteServiceInstallerWorker(
            devices=plan.devices,
            services=plan.services,
            ssh_public_key=plan.ssh_pubkey,
            veyon_public_key_pem=plan.veyon_pem,
            veyon_key_name=plan.veyon_key_name,
            vnc_password=plan.vnc_password,
            vnc_port=plan.vnc_port,
            veyon_server_port=plan.veyon_server_port,
            vnc_server=plan.vnc_server,
            extra_packages=list(plan.extra_packages),
            use_sudo_override=plan.use_sudo_override,
            default_bundle_iid=plan.default_bundle_iid,
            ssh_private_key_path=plan.ssh_private_key_path,
            credential_bundles=plan.credential_bundles,
            veyon_private_key=plan.veyon_private_key,
            ssh_settings=plan.ssh_settings,
            service_options=plan.service_options,
        )
        self._remote_worker = self._worker
        self._thread = QThread()
        self._remote_thread = self._thread
        self._worker.moveToThread(self._thread)

        receiver = _RunResultReceiver(self, plan.run_id)
        self._result_receiver = receiver
        self._worker.service_result.connect(receiver.result, Qt.ConnectionType.QueuedConnection)
        self._worker.host_log.connect(receiver.log, Qt.ConnectionType.QueuedConnection)
        self._worker.error.connect(receiver.error, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(receiver.finished, Qt.ConnectionType.QueuedConnection)
        self._thread.finished.connect(receiver.deleteLater)
        self._thread.started.connect(self._worker.execute)
        self._worker.finished.connect(self._thread.quit)

        if self._guard is not None:
            try:
                self._guard.register_thread(
                    "remote", self._thread, 30000, abort=self.abort_install
                )
                self._guard.set_busy("remote", True)
            except Exception as e:
                logger.error(f"RemoteInstallPage: guard register failed: {e}")
                self._finalize_pending(
                    RESULT_ERROR, "Не удалось зарегистрировать фоновую установку"
                )
                self._on_remote_install_finished()
                return

        self._install_active = True
        self._remote_install_btn.setVisible(False)
        self._remote_abort_btn.setVisible(True)
        self.install_state_changed.emit(True)

        self._remote_log.appendPlainText(
            f"=== Запуск установки на {len(plan.devices)} хост(ов) ==="
        )
        self._thread.start()

    def abort_install(self) -> None:
        self._abort_requested = True
        if self._preparation is not None and self._preparation.isRunning():
            self._preparation.requestInterruption()
        if self._worker is not None:
            self._remote_log.appendPlainText("=== Прерывание установки... ===")
            try:
                self._worker.abort()
            except Exception as e:
                logger.warning(f"RemoteInstallPage: abort failed: {e}")
        elif self._remote_worker is not None:
            self._remote_log.appendPlainText("=== Прерывание установки... ===")
            try:
                self._remote_worker.abort()
            except Exception as e:
                logger.warning(f"RemoteInstallPage: abort failed: {e}")

    # --- слоты воркера ---

    def _dispatch_run(self, run_id, callback, *args):
        if run_id == self._run_id:
            callback(*args)

    def _on_service_result(self, device, service, code, output) -> None:
        """Обновить ячейку таблицы по результату сервиса."""
        iid = getattr(device, "iid", None) or device.host
        row = self._host_row_map.get(iid)
        col = self._service_col_map.get(service)
        if row is None or col is None:
            return

        icon_key = STATUS_ICONS.get(code)
        text = STATUS_TEXTS.get(code, "")
        item = self._remote_table.item(row, col)
        if item is None:
            item = QTableWidgetItem()
            self._remote_table.setItem(row, col, item)
        if icon_key:
            item.setIcon(_icon_from_key(icon_key))
        item.setData(Qt.ItemDataRole.UserRole, code)
        item.setText("")
        item.setToolTip(f"{text}\n\n{output[-800:]}" if output else text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        self._host_logs.setdefault(iid, {})[service] = output
        if self._pending_ctx is not None:
            try:
                self.collect(self._pending_ctx)
            except Exception:
                pass

    def _on_host_log(self, host, service, message) -> None:
        """Краткое сообщение в общий лог + сохранение info в детальный лог хоста."""
        svc_label = SERVICE_LABELS.get(service, service)
        self._remote_log.appendPlainText(f"[{host}] {svc_label}: {message}")
        if service == "ssh":
            iid = None
            for key, name in self._row_host_name.items():
                if name == host:
                    iid = key
                    break
            if iid is not None and iid in self._host_logs:
                info = self._host_logs[iid].get("_info")
                if info is None:
                    info = []
                    self._host_logs[iid]["_info"] = info
                info.append(message)

    def _on_remote_error(self, msg) -> None:
        self._remote_log.appendPlainText(f"ОШИБКА: {msg}")

    def _on_remote_install_finished(self) -> None:
        """Завершение установки - разблокировать кнопки."""
        self._install_active = False
        self._remote_install_btn.setVisible(True)
        self._remote_abort_btn.setVisible(False)
        self._finalize_pending(
            RESULT_ABORT if self._abort_requested else RESULT_ERROR,
            "Прервано" if self._abort_requested else "Операция не вернула результат",
        )
        self.install_state_changed.emit(False)
        self._remote_log.appendPlainText(
            "=== Установка отменена ==="
            if self._abort_requested
            else "=== " + self.summary_html(self._pending_ctx) + " ==="
        )
        if self._thread is not None:
            self._thread.quit()
        if self._guard is not None:
            self._guard.set_busy("remote", False)

    def _on_table_double_click(self, row, col) -> None:
        """Показать детальный лог хоста по двойному клику."""
        iid = self._row_host_map.get(row)
        if iid is None or iid not in self._host_logs:
            return

        host = self._row_host_name.get(iid, "")
        logs = self._host_logs[iid]
        if not logs:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Лог установки - {host}")
        dlg.resize(720, 500)
        lay = QVBoxLayout(dlg)
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setStyleSheet("font-family: monospace;")
        info = logs.get("_info")
        if info:
            edit.appendPlainText("=== Подключение ===")
            for line in info:
                edit.appendPlainText(line)
            edit.appendPlainText("")
        for svc in REMOTE_SERVICES:
            if svc in logs:
                edit.appendPlainText(f"=== {SERVICE_LABELS[svc]} ===")
                edit.appendPlainText(logs[svc])
                edit.appendPlainText("")
        lay.addWidget(edit)
        dlg.exec()
