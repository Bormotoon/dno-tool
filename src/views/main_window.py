"""Главное окно приложения — корневой виджет UI.

MainWindow интегрирует дерево устройств, список выбранных устройств,
панель команд и таблицу результатов. Инициализирует архитектурные
компоненты (EventBus, DialogManager, WorkerBridge, ViewState) через
DI-контейнер, регистрирует фабрики диалогов, подписывается на события
и управляет жизненным циклом воркеров при закрытии."""

import os
import json
from typing import List, Dict, Optional, Any, Tuple
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QTreeView,
    QListView,
    QSplitter,
    QVBoxLayout,
    QHBoxLayout,
    QFrame,
    QPushButton,
    QToolButton,
    QAbstractItemView,
    QMenuBar,
    QFileDialog,
    QMenu,
    QInputDialog,
    QLineEdit,
    QComboBox,
    QTableWidget,
    QHeaderView,
    QMessageBox,
    QDialog,
    QSystemTrayIcon,
    QLabel,
    QSizeGrip,
)
from PySide6.QtCore import Qt, QSize, QTimer, QThread, Signal
from PySide6.QtGui import (
    QIcon,
    QStandardItemModel,
    QAction,
    QColor,
    QPainter,
    QPixmap,
    QFont,
    QFontMetrics,
    QKeySequence,
)

from src.ui.widgets import (
    DeviceTreeView,
    CustomTreeItem,
    CustomListItem,
    DeviceListView,
    ViewToolbar,
    VeyonToolbar,
    ControlToolbar,
    DeviceListToolbar,
    CustomCommandToolbar,
    CommandResultTable,
    TaskComboBox,
)
from src.ui.widgets.device_list_toolbar import (
    FLAT_MENU_TOOLBUTTON_QSS,
    FLAT_TOOLBUTTON_QSS,
)

# Каомодзи статус-строки: реестры и выбор — в src/ui/widgets/status_kaomoji.py.
from src.ui.widgets.status_kaomoji import (
    FEATURE_MOOD as _FEATURE_MOOD,
    KAOMOJI_FONT_FAMILY as _KAOMOJI_FONT_FAMILY,
    KAOMOJI_STATE_LABELS as _KAOMOJI_STATE_LABELS,
    pick_kaomoji,
)

from src.domain.models import DeviceModel
from src.domain.device_identity import device_identity, device_key
from src.domain.task_options import desktop_session_enabled

from src.ui.dialogs import (
    DeviceEditDialog,
    ConfigDialog,
    CommandResultDialog,
    BashViewerDialog,
    IPScannerDialog,
    CommandEditorDialog,
    CommandImportDialog,
    CommandConflictDialog,
    RemoteRecordingDialog,
    PolkitEditorDialog,
    VeyonSettingsDialog,
    VncSettingsDialog,
    DiagnosticsDialog,
)

from src.workers import CommandWorker, Command

from src.config import *

from PySide6.QtCore import Signal

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Signal

from src.logger import logger
from src.data import datastore

# ARCHITECTURE: Импорт архитектурных компонентов
from src.architecture import (
    EventBus,
    EventType,
    Event,
    DialogManager,
    DialogResult,
    DialogConfig,
    WorkerBridge,
    ViewState,
)

# ARCHITECTURE: Импорт сервисов и DI контейнера
from src.di import get_container
from src.services import (
    DeviceInfoService,
    DeviceService,
    CommandService,
    ConfigService,
    VeyonService,
    HostControlService,
    KRFBService,
    NodeService,
    get_host_info_service,
)
from src.services.hosts.host_info_service import TOOLTIP_MENU_FIELDS, STATIC_TOOLTIP_FIELDS
from src.workers import get_command_worker_pool
from src.ui.tray import TrayController
from src.ui.icons import set_themed_icon, make_action, _icon_from_key, qicon_from_path
from src.services.operation_errors import OperationOutcome, run_capturing_errors
from src.ui.batch_status import BatchStatusAggregator, plural_errors
from src.utils.text.text_utils import plural_ru


# ========== Статусная строка: индикатор-кружок и карта состояний ==========

# Цвета индикатора по состояниям. None = пустой кружок (idle).
STATUS_COLORS = {
    "idle": QColor("#888888"),  # grey — готов, ничего не выполняется
    "busy": QColor("#1e88e5"),  # blue — выполняется операция
    "ok": QColor("#43a047"),  # green — успех (автосброс на idle через 3 сек)
    "error": QColor("#e53935"),  # red — ошибка (автосброс на idle через 6 сек)
}


def _plural_hosts(n: int) -> str:
    """Правильное русское склонение слова «хост» по числу.

    1 → хост, 2-4 → хоста, 5-20 → хостов, 21 → хост, 22-24 → хоста и т.д.
    """
    return plural_ru(n, "хост", "хоста", "хостов")


# Ключи статических полей тултипа (для миграции сохранённого списка полей).
STATIC_TOOLTIP_FIELDS_KEYS = {key for key, _label in STATIC_TOOLTIP_FIELDS}


# Завершение: бюджет закрытия соединений и повтор вопроса после «Подождать».
_SHUTDOWN_CLOSE_BUDGET_SEC = 3.0
_SHUTDOWN_DIALOG_REPEAT_MS = 30_000


def _status_dot_icon(color: QColor, size: int = 12) -> QIcon:
    """Нарисовать круглый индикатор заданного цвета (как _state_icon в cameras.py).

    Используется в статус-строке для визуальной индикации состояния операций.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    if color is not None:
        painter.setBrush(color)
        painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pixmap)


# Человеко-читаемые имена фичей для статус-строки.
# Токен REST/UI → короткое имя (для «✅ <Имя>: <host>»).
_FEATURE_LABELS = {
    "lock": "Блокировка",
    "unlock": "Разблокировка",
    "lock_input": "Блокировка ввода",
    "unlock_input": "Разблокировка ввода",
    "restart_service": "Перезапуск Veyon",
    "logoff_webapi": "Завершение сеанса (WebAPI)",
    "login": "Вход пользователя (Veyon)",
    "login_ssh": "Вход пользователя (DBus)",
    "website": "Открытие сайта",
    "start_app": "Запуск приложения",
    "message": "Сообщение",
    "power_on": "Включение (WoL)",
    "reboot": "Перезагрузка",
    "powerdown": "Выключение",
    "logoff": "Завершение сеанса",
    "ssh_lock": "Блокировка экрана (SSH)",
    "ssh_unlock": "Разблокировка экрана (SSH)",
    "ssh_lock_input": "Блокировка ввода (SSH)",
    "ssh_unlock_input": "Разблокировка ввода (SSH)",
    "krfb_password": "Смена пароля VNC",
    "krfb_restart": "Перезапуск VNC",
}


def _node_args_for_feature(feature: str, args: tuple) -> dict:
    """Конвертация общих аргументов фичи (tuple из _prompt_veyon_args)
    в dict-args для node-API (client.veyon_feature).

    Ключи — те же, что собирает _show_veyon_feature_node для одиночного
    node-хоста; batch-режим (on_toolbar_veyon_feature) использует этот
    хелпер, чтобы спросить параметры один раз и раздать всем node-хостам.
    """
    if feature == "website":
        return {"url": args[0] if len(args) > 0 else ""}
    if feature == "start_app":
        return {
            "applications": list(args)
            if isinstance(args, (list, tuple))
            else [str(args)]
        }
    if feature == "message":
        return {"text": args[0] if len(args) > 0 else ""}
    if feature in ("login", "login_ssh"):
        return {
            "username": args[0] if len(args) > 0 else "",
            "password": args[1] if len(args) > 1 else "",
        }
    return {}


class MainWindow(QWidget):
    """Main application window with integrated architecture components"""

    command_progress = Signal(str, str)  # hostname, output
    # Маршалинг обновлений статус-строки из worker-потоков в GUI-поток.
    # (text, state, hint, mood) — см. _set_status.
    _status_update_signal = Signal(str, str, str, str)
    _node_edit_result = Signal(object, object, object, object)
    _node_delete_result = Signal(object, object, object)
    _node_vnc_result = Signal(object, str, str, object)

    def __init__(self) -> None:
        super().__init__()
        self._node_edit_result.connect(self._apply_node_device_edit_result)
        self._node_delete_result.connect(self._apply_node_device_delete_result)
        self._node_vnc_result.connect(self._apply_node_vnc_result)
        self._node_vnc_pending: set[tuple[str, str]] = set()

        # Трей и сворачивание в трей
        self._tray_controller: Optional["TrayController"] = None
        self._force_quit = False
        self._commands_download: QThread | None = None
        self._shutdown_pending = False
        self._shutdown_ready = False
        self._shutdown_coordinator = None
        self._shutdown_dialog: QMessageBox | None = None

        # Debounce сейва дерева: itemChanged штормит при каскаде чекбоксов
        # (снятие галочки с папки = десятки событий: checkstate, сброс
        # иконок, propagate вверх) и при каждой смене иконки пинга.
        # Полный export_tree_data + deepcopy datastore на каждое событие
        # фризил GUI-поток. Сейв откладывается на 250мс простоя и
        # схлопывается в один; состояние чекбоксов не персистится
        # (export его не содержит), так что задержка ни на что не влияет.
        self._tree_save_timer = QTimer(self)
        self._tree_save_timer.setSingleShot(True)
        self._tree_save_timer.setInterval(250)
        self._tree_save_timer.timeout.connect(self._flush_tree_data_changed)

        # ARCHITECTURE: Инициализация архитектурных компонентов
        self._init_architecture()

        self.init_ui()

        self.load_commands()
        self.data = self.load_initial_data()

        # ARCHITECTURE: Автозапуск мастера настройки при первом старте
        self._maybe_run_first_time_wizard()

        self.setAcceptDrops(True)

    def set_tray_controller(self, controller: "TrayController") -> None:
        """Привязать контроллер системного трея к окну."""
        self._tray_controller = controller
        controller.show_requested.connect(self._on_tray_show_requested)
        controller.cameras_requested.connect(self.show_cameras_dialog)
        controller.quit_requested.connect(self.quit_from_tray)

    def _on_tray_show_requested(self) -> None:
        """Слот 'Открыть' из меню трея: показать/скрыть окно."""
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def quit_from_tray(self) -> None:
        """Слот 'Выход' из меню трея: полностью закрыть приложение."""
        self._force_quit = True
        self.close()

    def _init_architecture(self):
        """ARCHITECTURE: Инициализация архитектурных компонентов и сервисов"""
        # Получаем DI контейнер (сервисы уже инициализированы в __main__.py)
        self.container = get_container()

        # Получаем сервисы из контейнера
        self.device_service = self.container.resolve(DeviceService)
        self.command_service = self.container.resolve(CommandService)
        self.config_service = self.container.resolve(ConfigService)
        self.veyon_service = self.container.resolve(VeyonService)
        from src.services.thumbnails.thumbnail_service import ThumbnailService

        self.thumbnail_service = self.container.resolve(ThumbnailService)
        self.host_control_service = self.container.resolve(HostControlService)
        self.krfb_service = self.container.resolve(KRFBService)
        self.node_service = self.container.resolve(NodeService)

        from src.ui.controllers.node_tree_controller import NodeTreeController

        self._node_tree_controller = NodeTreeController(self)

        # Получаем архитектурные компоненты (event_bus уже передан через конструктор)
        self.event_bus = self.container.resolve(EventBus)
        self.dialog_manager = self.container.resolve(DialogManager)
        self.worker_bridge = self.container.resolve(WorkerBridge)
        self.view_state = self.container.resolve(ViewState)

        # Подключаем PersistentStateManager для сохранения состояния между сессиями
        from src.architecture import PersistentStateManager

        if not getattr(self.view_state, "_persistence_bound", False):
            self._persistence_manager = PersistentStateManager()
            self._persistence_manager.bind(self.view_state)
            self.view_state._persistence_bound = True

        # Регистрация фабрик диалогов
        self._register_dialog_factories()

        # Подписка на события
        self._subscribe_to_events()

        # Инициализируем состояние UI
        self._init_view_state()

        logger.debug("MainWindow: Architecture components and services initialized")

    def _register_dialog_factories(self):
        """ARCHITECTURE: Регистрация фабрик для создания диалогов"""
        # Основные диалоги
        self.dialog_manager.register_factory(
            "config", lambda parent=None, **kwargs: ConfigDialog(parent)
        )
        self.dialog_manager.register_factory(
            "about", lambda parent=None, **kwargs: self._create_about_dialog(parent)
        )
        self.dialog_manager.register_factory(
            "scanner", lambda parent=None, **kwargs: self._create_scanner_dialog(parent)
        )

        # Диалоги устройств
        self.dialog_manager.register_factory(
            "device_edit",
            lambda parent=None, device=None, **kwargs: DeviceEditDialog(device, parent),
        )
        self.dialog_manager.register_factory(
            "device_info",
            lambda parent=None, device=None, **kwargs: self.container.resolve(DeviceInfoService).dialog(device, parent),
        )

        # Диалоги команд
        self.dialog_manager.register_factory(
            "command_edit", lambda parent=None, **kwargs: CommandEditorDialog(parent)
        )
        self.dialog_manager.register_factory(
            "command_info",
            lambda parent=None, command=None, **kwargs: CommandInfoDialog(
                command, parent
            ),
        )
        self.dialog_manager.register_factory(
            "command_result",
            lambda parent=None, hostname="", output="", status="", device_iid=None, **kwargs: (
                CommandResultDialog(
                    hostname, output, status, parent, device_iid=device_iid, node_iid=kwargs.get("node_iid")
                )
            ),
        )

        # Диалоги редакторов
        self.dialog_manager.register_factory(
            "bash_editor", lambda parent=None, **kwargs: BashEditorDialog(parent)
        )
        self.dialog_manager.register_factory(
            "bash_viewer",
            lambda parent=None, initial_text="", **kwargs: BashViewerDialog(
                parent, initial_text
            ),
        )

        # Диалоги SSH
        self.dialog_manager.register_factory(
            "ssh_manage",
            lambda parent=None, **kwargs: self._create_ssh_manage_dialog(parent),
        )
        self.dialog_manager.register_factory(
            "known_hosts",
            lambda parent=None, **kwargs: self._create_known_hosts_dialog(parent),
        )

        # Диалоги SFTP
        self.dialog_manager.register_factory(
            "sftp", lambda parent=None, **kwargs: SFTPDialog(parent)
        )
        self.dialog_manager.register_factory(
            "sftp_command_edit",
            lambda parent=None, **kwargs: SFTPCommandEditorDialog(parent),
        )
        self.dialog_manager.register_factory(
            "remote_file_info",
            lambda parent=None, **kwargs: RemoteFileInfoDialog(parent),
        )
        self.dialog_manager.register_factory(
            "remote_file_preview",
            lambda parent=None, **kwargs: RemoteFilePreviewDialog(parent),
        )

        # Диалоги параметров
        self.dialog_manager.register_factory(
            "params",
            lambda parent=None, params=None, **kwargs: ParamsInputDialog(
                params, parent
            ),
        )

        # Диалоги утилит
        self.dialog_manager.register_factory(
            "pinger", lambda parent=None, **kwargs: PingMonitorDialog(parent)
        )
        self.dialog_manager.register_factory(
            "ip_scanner_ports", lambda parent=None, **kwargs: PortsEditorDialog(parent)
        )
        self.dialog_manager.register_factory(
            "remote_recording",
            lambda parent=None, device=None, **kwargs: RemoteRecordingDialog(
                device, parent
            ),
        )

        logger.debug("MainWindow: Dialog factories registered")

    def _create_about_dialog(self, parent=None):
        """ARCHITECTURE: Фабричный метод для создания AboutDialog"""
        from src.ui.dialogs.common.about import AboutDialog

        return AboutDialog(parent)

    def _create_scanner_dialog(self, parent=None):
        """ARCHITECTURE: Фабричный метод для создания IPScannerDialog с подключенными сигналами"""
        dialog = getattr(self, "_scanner_dialog", None)
        if dialog is None:
            dialog = IPScannerDialog(parent or self)
            dialog.exported_device.connect(self._on_scanner_device_import)
            self._scanner_dialog = dialog
        return dialog

    def _create_known_hosts_dialog(self, parent=None):
        """ARCHITECTURE: Фабричный метод для создания KnownHostsDialog"""
        from config import LOGS_PATH
        from src.ui.dialogs.tools.known_hosts import KnownHostsDialog

        known_hosts_path = os.path.expanduser("~/.ssh/known_hosts")
        return KnownHostsDialog(known_hosts_path, parent)

    def _create_ssh_manage_dialog(self, parent=None):
        """ARCHITECTURE: Фабричный метод для создания SSHManageDialog"""
        from src.ui.dialogs.common.ssh_manage import SSHManageDialog

        return SSHManageDialog(parent)

    def _on_scanner_device_import(self, devices, folder):
        """ARCHITECTURE: Обработчик импорта устройств из сканнера"""
        # Делегируем добавление и разрешение целевой папки в DeviceTreeView
        scanner = getattr(self, "_scanner_dialog", None)
        imported_devices = []
        added = self.treeview.add_devices_at_selected_folder(
            devices, folder, deduplicate=True, added_devices=imported_devices,
            dialog_parent=scanner,
        )
        if not added:
            return
        if added and scanner is not None:
            for device in imported_devices:
                item = scanner.results_tree._hosts.get(device.host)
                if item is not None:
                    item.setCheckState(Qt.CheckState.Unchecked)
        # Сохраняем импортированные устройства в datastore
        self.treeview.data_changed.emit(self.treeview.get_tree_data())

        # Определяем имя папки для события (для обратной совместимости отчёта)
        if folder is not None:
            folder_name = folder.text() if hasattr(folder, "text") else "root"
        else:
            selected = self.treeview.selectedIndexes()
            if selected:
                item = self.treeview._model.itemFromIndex(selected[0])
                if item is not None and (
                    not hasattr(item, "device") or item.device is None
                ):
                    folder_name = item.text()
                else:
                    folder_name = "root"
            else:
                folder_name = "root"

        # Публикуем событие о добавлении устройств
        self.event_bus.publish_typed(
            EventType.DEVICE_ADDED,
            source="MainWindow",
            data={"device_count": added, "folder": folder_name},
        )

    def _subscribe_to_events(self):
        """ARCHITECTURE: Подписка на события EventBus"""
        # Подписываемся на события workers
        self.event_bus.subscribe(EventType.WORKER_STARTED, self._on_worker_event)
        self.event_bus.subscribe(EventType.WORKER_FINISHED, self._on_worker_event)
        self.event_bus.subscribe(EventType.WORKER_ERROR, self._on_worker_event)

        # Подписываемся на события команд
        self.event_bus.subscribe(EventType.COMMAND_STARTED, self._on_command_event)
        self.event_bus.subscribe(EventType.COMMAND_FINISHED, self._on_command_event)
        self.event_bus.subscribe(EventType.COMMAND_ABORTED, self._on_command_event)

        # Событие сохранения данных (обновляем видимость Veyon-элементов
        # при изменении конфига Veyon/VNC через диалоги).
        self.event_bus.subscribe(EventType.DATA_SAVED, self._on_data_saved)
        self.event_bus.subscribe(
            EventType.DATA_CHANGED, self._on_datastore_hosts_changed
        )

        # События нод (федерация): изменение списка/настроек нод → синхронизация
        # секций дерева и клиентов.
        self.event_bus.subscribe(
            EventType.NODE_CONFIG_CHANGED, self._on_node_config_changed
        )

        logger.debug("MainWindow: Subscribed to EventBus events")

    def _on_datastore_hosts_changed(self, event: Event) -> None:
        """Reflect owner API updates on Qt models before a later tree save."""
        if event.source != "DataStore" or event.data.get("type") != "hosts":
            return
        if hasattr(self, "treeview"):
            self.treeview.reconcile_local_hosts(self.device_service.get_devices_raw())
            self._refresh_veyon_ui_visibility()

    def _on_node_config_changed(self, event: Event) -> None:
        """ARCHITECTURE: Реакция на изменение конфигурации нод (добавление/удаление
        через NodeSettingsDialog, смена ключа доступа, отображения и т.п.)."""
        try:
            self._nodes_reload_sections()
        except Exception as e:
            logger.debug(f"MainWindow: _on_node_config_changed: {e}")

    def _on_data_saved(self, event: Event):
        """ARCHITECTURE: Обработчик события сохранения данных.

        Реагирует на изменения конфига Veyon/VNC — обновляет видимость
        UI-элементов в зависимости от текущего статуса Veyon. Тип ``config``
        — применяет опцию каомодзи к статус-строке немедленно (без ожидания
        следующего обновления статуса).
        """
        try:
            data = getattr(event, "data", {}) or {}
            etype = data.get("type") if isinstance(data, dict) else None
            if etype in (
                "thumbnails_config",
                "config",
                "veyon_config",
                "vnc_config",
                "wizard_finished",
            ):
                if hasattr(self, "listview"):
                    self.listview.apply_thumbnail_settings()
                if hasattr(self, "list_toolbar"):
                    self.list_toolbar.set_thumbnail_style(
                        config.app.thumbnails.style == "plain"
                    )
                self._refresh_veyon_ui_visibility()
            if etype == "config":
                self._apply_kaomoji_visibility()
        except Exception as e:
            logger.error(f"MainWindow: _on_data_saved handler error: {e}")

    def _apply_kaomoji_visibility(self) -> None:
        """Применить опцию каомодзи к статус-строке немедленно.

        Вызывается при сохранении общих настроек: выключение скрывает метку
        сразу, включение показывает нейтральный (idle) каомодзи.
        """
        label = getattr(self, "_status_kaomoji", None)
        if label is None:
            return
        if not getattr(config.app, "kaomoji_enabled", True):
            label.hide()
            return
        label.show()
        self._update_kaomoji("idle")

    def _init_view_state(self):
        """ARCHITECTURE: Инициализация состояния UI"""
        # Регистрируем состояние главного окна
        self.view_state.register("main_window", persist=True)
        # Регистрируем состояние панели списка устройств
        self.view_state.register("device_list_view", persist=True)

        # Восстанавливаем сохраненные размеры и позицию
        saved_geometry = self.view_state.get("main_window", "geometry")
        if saved_geometry:
            self.restoreGeometry(saved_geometry)

        # Привязываем splitter к состоянию
        if hasattr(self, "right_splitter"):
            self.view_state.bind_widget(
                "main_window", self.right_splitter, "sizes", "splitter_sizes"
            )

    def _save_view_state(self):
        """ARCHITECTURE: Сохранение состояния UI"""
        # Сохраняем геометрию окна
        self.view_state.set("main_window", "geometry", self.saveGeometry())

        # Сохраняем состояние splitter
        if hasattr(self, "right_splitter"):
            self.view_state.set(
                "main_window", "splitter_sizes", self.right_splitter.sizes()
            )

        # Сохраняем состояние дерева (раскрытые узлы)
        if hasattr(self, "treeview"):
            expanded_state = self._get_tree_expanded_state()
            self.view_state.set("main_window", "tree_expanded", expanded_state)
            # Режим «ноды первым уровнем» (тулбар дерева)
            self.view_state.set(
                "main_window",
                "tree_show_nodes",
                bool(getattr(self.treeview, "show_nodes", False)),
            )

        # Сохраняем состояние панели «Вид» (online_only/icon_size/mode/style).
        if hasattr(self, "list_toolbar"):
            self.view_state.set(
                "device_list_view",
                "online_only",
                self.list_toolbar._online_action.isChecked(),
            )
            self.view_state.set(
                "device_list_view", "icon_size", self.list_toolbar._slider.value()
            )
            self.view_state.set(
                "device_list_view",
                "thumbnail_mode",
                self.list_toolbar._thumb_action.isChecked(),
            )
            self.view_state.set(
                "device_list_view",
                "tooltip_info_fields",
                [
                    key
                    for key, _label in TOOLTIP_MENU_FIELDS
                    if self.list_toolbar._info_field_actions[key].isChecked()
                ],
            )
            # Видимость трёх тулбаров.
            self.view_state.set(
                "device_list_view",
                "view_toolbar_visible",
                self.view_toolbar_action.isChecked(),
            )
            self.view_state.set(
                "device_list_view",
                "veyon_toolbar_visible",
                self.veyon_toolbar_action.isChecked(),
            )
            self.view_state.set(
                "device_list_view",
                "control_toolbar_visible",
                self.control_toolbar_action.isChecked(),
            )
            self.view_state.set(
                "device_list_view",
                "result_table_visible",
                self.result_table_visible_action.isChecked(),
            )
            # Видимость пользовательских панелей (dict panel_iid -> bool).
            try:
                vis_map = {}
                for pid, act in getattr(self, "_custom_toolbar_actions", {}).items():
                    try:
                        vis_map[pid] = bool(act.isChecked())
                    except Exception:
                        continue
                if vis_map:
                    self.view_state.set(
                        "device_list_view", "custom_toolbars_visible", vis_map
                    )
            except Exception:
                pass

    def _get_tree_expanded_state(self) -> list:
        """Получить список раскрытых узлов дерева"""
        if not hasattr(self, "treeview"):
            return []

        expanded = []
        model = self.treeview._model
        root = model.invisibleRootItem()

        def collect_expanded(item, path=""):
            if item.rowCount() > 0:
                index = model.indexFromItem(item)
                if self.treeview.isExpanded(index):
                    expanded.append(path + item.text())
                for i in range(item.rowCount()):
                    child = item.child(i)
                    collect_expanded(child, path + item.text() + "/")

        collect_expanded(root)
        return expanded

    def _restore_tree_expanded_state(self):
        """Восстановить состояние раскрытых узлов дерева"""
        if not hasattr(self, "treeview"):
            return

        expanded_state = self.view_state.get("main_window", "tree_expanded", [])
        if not expanded_state:
            return

        model = self.treeview._model

        def find_item_by_path(path):
            parts = path.split("/")
            current = model.invisibleRootItem()
            for part in parts:
                found = None
                for i in range(current.rowCount()):
                    if current.child(i).text() == part:
                        found = current.child(i)
                        break
                if found:
                    current = found
                else:
                    return None
            return current

        for item_path in expanded_state:
            item = find_item_by_path(item_path)
            if item:
                index = model.indexFromItem(item)
                self.treeview.setExpanded(index, True)

    def _on_worker_event(self, event: Event):
        """ARCHITECTURE: Обработчик событий workers"""
        worker_id = event.get("worker_id", "unknown")
        logger.debug(
            f"MainWindow: Worker event {event.event_type.name} from {worker_id}"
        )

    def _on_command_event(self, event: Event):
        """ARCHITECTURE: Обработчик событий команд"""
        logger.debug(f"MainWindow: Command event {event.event_type.name}")

    # ========== ARCHITECTURE: Обработчики сигналов сервисов ==========

    def _on_show_nodes_toggled(self, checked: bool) -> None:
        """Тулбар дерева: показывать/скрыть ноды первым уровнем.

        checked=False: секции снимаются в стэш, клиенты нод останавливаются
        (опрос не тратит ресурсы). checked=True: секции возвращаются,
        клиенты перезапускаются (_nodes_reload_sections).
        """
        try:
            from src.ui.widgets.device_tree import LOCAL_NODE_IID

            self.treeview.set_show_nodes(bool(checked))
            if not checked:
                self.node_service.stop_all_clients()
            else:
                # Перезапуск клиентов + секций (set_show_nodes вернул стэш).
                self._nodes_reload_sections()
            # set_show_nodes переносит элементы с их чекбоксами; локальные
            # пинг-таймеры не требуют пересинхронизации (DeviceModel те же).
            # Категория устройств в списке обновляется ниже (путь папок
            # изменился: префикс «Эта машина»).
            for dev in self.treeview.get_all_checked_devices():
                self.update_in_listview(dev)
        except Exception as e:
            logger.debug(f"MainWindow: _on_show_nodes_toggled: {e}")

    def _on_tree_data_changed(self, new_data: list):
        """
        Обработчик сигнала изменения данных дерева.

        Args:
            new_data: Новые данные дерева устройств
        """
        # Сохраняем данные через сервис (только в памяти)
        success = self.device_service.save_devices(new_data)

        if success:
            logger.debug("MainWindow: Tree data saved to service (in-memory)")
        else:
            logger.error("MainWindow: Failed to save tree data via DeviceService")
            QMessageBox.critical(
                self,
                "Ошибка",
                "Не удалось сохранить данные устройств. Проверьте лог файл.",
            )

    # ========== КОНЕЦ ARCHITECTURE ==========

    def init_ui(self):
        """Инициализация интерфейса"""
        self.setup_menu()
        self.setup_frames()
        self.setup_tree_view()
        self.setup_command_panel()  # Command panel first
        self.setup_list_view()  # List second
        self.setup_result_table()  # Table third
        self.setup_layout()
        self.setup_models()
        # Маршалинг статусов из worker-потоков в GUI-поток.
        self._status_update_signal.connect(self._set_status)
        self.setup_connections()

    def setup_frames(self):
        """Setup main frames"""
        # Left frame
        self.tree_frame = QFrame()
        self.tree_frame.setFrameShape(QFrame.Shape.NoFrame)
        tree_layout = QVBoxLayout()
        tree_layout.setContentsMargins(2, 2, 2, 2)
        tree_layout.setSpacing(4)
        self.tree_frame.setLayout(tree_layout)

        # Right frame with command panel, list and table
        self.right_frame = QFrame()
        self.right_frame.setFrameShape(QFrame.Shape.NoFrame)
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(2, 2, 2, 2)
        right_layout.setSpacing(4)
        self.right_frame.setLayout(right_layout)

    def setup_command_panel(self):
        """Setup command execution panel"""
        command_frame = QFrame()
        command_frame.setFrameShape(QFrame.Shape.NoFrame)
        command_layout = QHBoxLayout()
        command_layout.setContentsMargins(0, 0, 0, 0)
        command_layout.setSpacing(4)
        command_frame.setLayout(command_layout)

        self.command_combo_box = TaskComboBox()
        # self.command_combo_box.setEditable(False)

        command_layout.addWidget(self.command_combo_box, 1)

        self.execute_button = QToolButton(command_frame)
        self.execute_button.setText("Выполнить")
        self.execute_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.execute_button.setAutoRaise(True)
        self.execute_button.setStyleSheet(FLAT_MENU_TOOLBUTTON_QSS)
        set_themed_icon(self.execute_button, "result_executing")
        self.execute_button.setIconSize(QSize(32, 32))
        self.execute_button.setFixedHeight(32)
        self.execute_button.setToolTip(
            "Выполнить выбранную задачу на отмеченных хостах.\n"
            "Стрелка справа - другие способы запуска, например своя команда."
        )
        # Основное действие - выбранная задача; стрелка открывает разовый запуск.
        # Меню принадлежит окну, а не кнопке: стиль кнопки на него не действует.
        self.execute_menu = QMenu(self)
        self.custom_command_action = make_action(
            "command", "Своя команда…", self.execute_menu
        )
        self.custom_command_action.setToolTip(
            "Выполнить произвольный скрипт на отмеченных хостах, "
            "не добавляя его в задачи"
        )
        self.execute_menu.setToolTipsVisible(True)
        self.execute_menu.addAction(self.custom_command_action)
        self.execute_button.setMenu(self.execute_menu)
        self.execute_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)

        self.command_info_button = QToolButton(command_frame)
        self.command_info_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.command_info_button.setAutoRaise(True)
        self.command_info_button.setStyleSheet(FLAT_TOOLBUTTON_QSS)
        set_themed_icon(self.command_info_button, "menu_info")
        self.command_info_button.setToolTip("Информация о задаче")
        self.command_info_button.setAccessibleName("Информация о задаче")
        self.command_info_button.setIconSize(QSize(32, 32))
        self.command_info_button.setFixedSize(32, 32)

        # Кнопка отмены подменяет «Выполнить» на время задания, поэтому
        # оформлена так же: плоская QToolButton с текстом рядом с иконкой.
        self.command_abort_button = QToolButton(command_frame)
        self.command_abort_button.setText("Отменить")
        self.command_abort_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.command_abort_button.setAutoRaise(True)
        self.command_abort_button.setStyleSheet(FLAT_TOOLBUTTON_QSS)
        set_themed_icon(self.command_abort_button, "menu_abort")
        self.command_abort_button.setIconSize(QSize(32, 32))
        self.command_abort_button.setFixedHeight(32)

        # Общая ширина: при смене кнопок поле выбора задачи не меняет размер.
        swap_width = max(
            self.execute_button.sizeHint().width(),
            self.command_abort_button.sizeHint().width(),
        )
        self.execute_button.setMinimumWidth(swap_width)
        self.command_abort_button.setMinimumWidth(swap_width)

        self.command_abort_button.setEnabled(False)
        self.command_abort_button.setVisible(False)

        command_layout.addWidget(self.command_info_button)
        command_layout.addWidget(self.command_abort_button)
        command_layout.addWidget(self.execute_button)
        self.right_frame.layout().addWidget(command_frame)

    def setup_menu(self):
        """Настройка главного меню"""
        self.menu_bar = QMenuBar(self)
        self.menu_bar.setNativeMenuBar(False)

        # File menu
        file_menu = self.menu_bar.addMenu("📁 Файл")

        hosts_menu = file_menu.addMenu("Хосты")

        set_themed_icon(hosts_menu.menuAction(), "menu_hosts")
        load_action = make_action("menu_import_host", "Импортировать список…", self)
        load_action.setShortcut(QKeySequence("Ctrl+O"))
        load_action.triggered.connect(self.on_menu_load_tree)
        hosts_menu.addAction(load_action)

        import_cab_action = make_action(
            "menu_import_classroom", "Импортировать из comp-cab-setup…", self
        )
        import_cab_action.triggered.connect(self.import_comp_cab_hosts)
        hosts_menu.addAction(import_cab_action)

        save_as_action = make_action("menu_export_host", "Экспортировать список…", self)
        save_as_action.triggered.connect(self.save_tree)
        hosts_menu.addAction(save_as_action)

        hosts_menu.addSeparator()

        save_action = make_action("menu_save", "Сохранить список", self)
        save_action.setShortcut(QKeySequence("Ctrl+S"))
        save_action.triggered.connect(lambda: self.save_tree(DEFAULT_HOSTS_FILE))
        hosts_menu.addAction(save_action)

        file_menu.addSeparator()
        commands_menu = file_menu.addMenu("Задачи")
        set_themed_icon(commands_menu.menuAction(), "menu_commands")
        import_commands_action = make_action(
            "menu_import", "Импортировать задачи…", self
        )
        import_commands_action.triggered.connect(self.import_commands_from_file)
        commands_menu.addAction(import_commands_action)

        import_server_action = make_action(
            "menu_import", "Импортировать задачи с сервера…", self
        )
        import_server_action.triggered.connect(self.import_commands_from_server)
        commands_menu.addAction(import_server_action)

        export_commands_action = make_action(
            "menu_export", "Экспортировать задачи…", self
        )
        export_commands_action.triggered.connect(lambda: self.save_commands_to_file())
        commands_menu.addAction(export_commands_action)

        commands_menu.addSeparator()

        save_commands_action = make_action("menu_save", "Сохранить задачи", self)
        save_commands_action.triggered.connect(
            lambda: self.save_commands_to_file(DEFAULT_COMMANDS_FILE)
        )
        commands_menu.addAction(save_commands_action)

        file_menu.addSeparator()
        exit_action = make_action("menu_exit", "Выход", self)
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.quit_from_tray)
        file_menu.addAction(exit_action)

        settings_menu = self.menu_bar.addMenu("⚙️ Настройки")

        connection_menu = settings_menu.addMenu("Подключение")

        set_themed_icon(connection_menu.menuAction(), "menu_credentials")

        credentials_action = make_action(
            "menu_credentials", "Связки учётных данных", self
        )
        credentials_action.triggered.connect(self.show_credentials_dialog)
        connection_menu.addAction(credentials_action)

        ssh_manage_action = make_action("key_exists", "Управление SSH ключами", self)
        ssh_manage_action.triggered.connect(self.show_ssh_manage_dialog)
        connection_menu.addAction(ssh_manage_action)

        thumbnails_action = make_action("toolbar_thumbnails", "Миниатюры экранов", self)
        thumbnails_action.triggered.connect(self.show_thumbnail_settings_dialog)
        settings_menu.addAction(thumbnails_action)

        self.remote_menu = settings_menu.addMenu("Удалённое управление")

        set_themed_icon(self.remote_menu.menuAction(), "menu_veyon")

        self.veyon_settings_action = make_action("menu_veyon", "Настройки Veyon", self)
        self.veyon_settings_action.triggered.connect(self.show_veyon_settings_dialog)
        self.remote_menu.addAction(self.veyon_settings_action)

        vnc_settings_action = make_action("menu_vnc", "Настройки VNC", self)
        vnc_settings_action.triggered.connect(self.show_vnc_settings_dialog)
        self.remote_menu.addAction(vnc_settings_action)

        web_server_action = make_action("menu_terminal", "Веб-сервер управления", self)
        web_server_action.triggered.connect(self.show_web_server_dialog)
        self.remote_menu.addAction(web_server_action)

        data_menu = settings_menu.addMenu("Данные")

        set_themed_icon(data_menu.menuAction(), "menu_commands")

        nodes_action = make_action("menu_nodes", "Ноды", self)
        nodes_action.triggered.connect(self.show_node_settings_dialog)
        data_menu.addAction(nodes_action)

        command_edit_action = make_action("command", "Редактировать задачи", self)
        command_edit_action.setShortcut(QKeySequence("Ctrl+E"))
        command_edit_action.triggered.connect(self.show_command_edit_dialog)
        data_menu.addAction(command_edit_action)

        settings_menu.addSeparator()

        config_action = make_action("menu_settings", "Конфигурация", self)
        config_action.triggered.connect(self.show_config_dialog)
        settings_menu.addAction(config_action)

        # Меню «Вид»
        view_menu = self.menu_bar.addMenu("👁️ Вид")

        # Подменю «Панели» — переключение видимости трёх тулбаров.
        panels_menu = view_menu.addMenu("Панели")
        set_themed_icon(panels_menu.menuAction(), "menu_list_toolbar")
        self.panels_menu = panels_menu

        self.view_toolbar_action = QAction("Вид", self)
        self.view_toolbar_action.setCheckable(True)
        self.view_toolbar_action.setChecked(True)
        self.view_toolbar_action.triggered.connect(self._toggle_view_toolbar)
        panels_menu.addAction(self.view_toolbar_action)

        self.veyon_toolbar_action = QAction("Veyon", self)
        self.veyon_toolbar_action.setCheckable(True)
        self.veyon_toolbar_action.setChecked(True)
        self.veyon_toolbar_action.triggered.connect(self._toggle_veyon_toolbar)
        panels_menu.addAction(self.veyon_toolbar_action)

        self.control_toolbar_action = QAction("Управление", self)
        self.control_toolbar_action.setCheckable(True)
        self.control_toolbar_action.setChecked(True)
        self.control_toolbar_action.triggered.connect(self._toggle_control_toolbar)
        panels_menu.addAction(self.control_toolbar_action)

        # Динамическая часть: пользовательские панели + настройка.
        self._custom_toolbar_actions: dict = {}
        self._custom_panels_menu_items: list = []
        panels_menu.addSeparator()
        self._custom_panels_sep = panels_menu.addSeparator()
        self._customize_panels_action = make_action(
            "menu_settings", "Настроить панели...", self
        )
        self._customize_panels_action.triggered.connect(
            self.show_custom_toolbars_dialog
        )
        panels_menu.addAction(self._customize_panels_action)

        view_menu.addSeparator()

        self.result_table_visible_action = make_action(
            "menu_results", "Результаты выполнения", self, toggle=True
        )
        self.result_table_visible_action.setCheckable(True)
        self.result_table_visible_action.setChecked(True)
        self.result_table_visible_action.triggered.connect(self._toggle_result_table)
        view_menu.addAction(self.result_table_visible_action)

        tools_menu = self.menu_bar.addMenu("🔧 Инструменты")
        scanner_action = make_action("menu_scanner", "Сканер хостов", self)
        scanner_action.triggered.connect(self.show_scanner_dialog)
        tools_menu.addAction(scanner_action)

        try:
            import pyqtgraph  # noqa: F401

            pinger_action = make_action("menu_ping", "Пингер", self)
            pinger_action.triggered.connect(self.show_pinger_dialog)
            tools_menu.addAction(pinger_action)
        except ImportError:
            logger.warning("pyqtgraph не установлен - пингер недоступен")

        cameras_action = make_action("menu_recording", "Камеры ЕЦХД", self)
        cameras_action.triggered.connect(self.show_cameras_dialog)
        tools_menu.addAction(cameras_action)

        topology_action = make_action("menu_topology", "Карта сети", self)
        topology_action.triggered.connect(self.show_topology_dialog)
        tools_menu.addAction(topology_action)

        video_action = make_action("menu_video_editor", "Видеоредактор", self)
        video_action.triggered.connect(self.show_video_editor_dialog)
        tools_menu.addAction(video_action)

        help_menu = self.menu_bar.addMenu("❓ Помощь")
        help_tools_menu = help_menu.addMenu("Инструменты")
        set_themed_icon(help_tools_menu.menuAction(), "menu_tools")
        wizard_action = make_action("menu_tools", "Мастер настройки", self)
        wizard_action.triggered.connect(self.show_setup_wizard)
        help_tools_menu.addAction(wizard_action)

        diag_action = make_action("menu_diagnostics", "Диагностика", self)
        diag_action.triggered.connect(self.show_diagnostics_dialog)
        help_tools_menu.addAction(diag_action)

        ffmpeg_action = make_action("menu_video_editor", "FFmpeg", self)
        ffmpeg_action.triggered.connect(self.show_ffmpeg_dialog)
        help_tools_menu.addAction(ffmpeg_action)

        help_tools_menu.addSeparator()

        monitor_action = make_action(
            "menu_monitoring",
            "Мониторинг ресурсов",
            self,
        )
        monitor_action.triggered.connect(self.show_pool_monitor)
        help_tools_menu.addAction(monitor_action)

        help_menu.addSeparator()

        about_action = make_action("menu_info", "О программе", self)
        about_action.triggered.connect(self.show_about_dialog)
        help_menu.addAction(about_action)

    def refresh_icons(self) -> None:
        """Перечитать все иконки интерфейса под текущую тему иконок.

        Вызывается при live-переключении темы иконок в настройках
        (без перезапуска): реестр виджетов (меню, тулбары, кнопки),
        дерево, список, таблица результатов, комбо задач и трей.
        Контекстные меню и transient-диалоги читают ICONS при
        создании и обновляются сами.
        """
        from src.ui.icons import refresh_themed_icons

        try:
            refresh_themed_icons()
        except Exception as e:
            logger.debug(f"MainWindow: refresh_themed_icons: {e}")
        for name in ("treeview", "listview", "result_table"):
            widget = getattr(self, name, None)
            refresh = getattr(widget, "refresh_icons", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception as e:
                    logger.debug(f"MainWindow: {name}.refresh_icons: {e}")
        # Пользовательские панели: pack/file иконки перечитать вручную.
        try:
            panels = {getattr(p, "iid", ""): p for p in self._custom_panels()}
            for pid, w in getattr(self, "custom_toolbars", {}).items():
                refresh = getattr(w, "refresh_icons", None)
                if callable(refresh) and pid in panels:
                    refresh(panels[pid])
        except Exception as e:
            logger.debug(f"MainWindow: custom toolbars refresh_icons: {e}")
        try:
            model = self.command_combo_box.model()
            refresh = getattr(model, "refresh_icons", None)
            if callable(refresh):
                refresh()
        except Exception as e:
            logger.debug(f"MainWindow: task combo refresh_icons: {e}")
        tray_refresh = getattr(self._tray_controller, "refresh_icons", None)
        if callable(tray_refresh):
            try:
                tray_refresh()
            except Exception as e:
                logger.debug(f"MainWindow: tray refresh_icons: {e}")
        # Открытая карта сети: перечитать иконки узлов под текущую тему.
        try:
            topo = getattr(self, "_topology_dialog", None)
            topo_refresh = getattr(topo, "refresh_icons", None)
            if callable(topo_refresh):
                topo_refresh()
        except Exception as e:
            logger.debug(f"MainWindow: topology refresh_icons: {e}")
        # Открытый SFTP-командер: создаётся напрямую из списка устройств
        # (device_list.open_sftp, не через dialog_manager), поэтому ищем
        # среди top-level виджетов по наличию refresh_icons.
        try:
            from PySide6.QtWidgets import QApplication

            from src.ui.dialogs.tools.sftp import SFTPDialog

            app = QApplication.instance()
            for w in app.topLevelWidgets() if app else []:
                if isinstance(w, SFTPDialog):
                    try:
                        w.refresh_icons()
                    except Exception as e:
                        logger.debug(f"MainWindow: sftp refresh_icons: {e}")
        except Exception as e:
            logger.debug(f"MainWindow: sftp refresh sweep: {e}")
        # Один отложенный сейв: ремап путей иконок персистится в hosts.json
        # (дерево заблокировало itemChanged на время обновления), полный
        # export+deepcopy - раз, через debounce 250мс.
        try:
            self._tree_save_timer.start()
        except Exception as e:
            logger.debug(f"MainWindow: refresh_icons save timer: {e}")

    def show_about_dialog(self):
        """ARCHITECTURE: Показать диалог 'О программе' через DialogManager"""
        dialog_id = self.dialog_manager.open_typed("about", modal=True, parent=self)
        if dialog_id:
            logger.debug(f"MainWindow: About dialog opened with id={dialog_id}")
        else:
            # Fallback: открываем напрямую если фабрика не сработала
            from src.ui.dialogs.common.about import AboutDialog

            dialog = AboutDialog(self)
            dialog.exec_()

    def _ask_command_import_mode(self, file_path: str) -> Optional[tuple]:
        """Диалог режима импорта с превью и разрешением конфликтов.

        Returns:
            (mode, resolution) или None при отмене.
        """
        ok, msg, preview = self.command_service.preview_import_from_file(file_path)
        if not ok:
            QMessageBox.critical(self, "Ошибка импорта", msg)
            return None
        dlg = CommandImportDialog(
            self,
            preview_stats=preview,
            local_count=self.command_service.get_tasks_count(),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        mode = dlg.selected_mode()
        resolution = None
        if mode == CommandImportDialog.MODE_MERGE:
            conflicts = self.command_service.find_import_conflicts(file_path)
            if conflicts:
                cdlg = CommandConflictDialog(self, conflicts=conflicts)
                if cdlg.exec() != QDialog.DialogCode.Accepted:
                    return None
                resolution = cdlg.resolution()
        return mode, resolution

    def import_commands_from_file(self):
        """Импорт задач из файла"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Импортировать задачи", "", "JSON Files (*.json)"
        )
        if file_path:
            asked = self._ask_command_import_mode(file_path)
            if asked is None:
                return
            mode, resolution = asked

            # ARCHITECTURE: Используем сервис для загрузки
            success, message, _stats = self.command_service.import_commands_from_file(
                file_path, mode=mode, resolution=resolution
            )

            if success:
                # Получаем данные из сервиса
                commands = self.command_service.get_all_commands()
                self.command_combo_box.clear()
                self.command_combo_box.load_tasks(commands)
                self.command_combo_box.select_first_task()

                QMessageBox.information(self, "Импорт задач", message)

                # ARCHITECTURE: Публикуем событие об изменении данных
                self.event_bus.publish_typed(
                    EventType.DATA_CHANGED,
                    source="MainWindow",
                    data={"type": "commands_imported", "count": len(commands)},
                )

                logger.info(f"MainWindow: {message}")
            else:
                logger.error(f"MainWindow: {message}")
                QMessageBox.critical(self, "Ошибка импорта", message)

    def import_commands_from_server(self):
        """Импорт команд с GitHub через Contents API: загрузка идёт вне GUI-потока."""
        if self._commands_download is not None:
            return
        from src.workers.command_download import CommandDownloadWorker
        from src.workers.qthread_registry import track_qthread

        self._set_status("⏳ Загрузка команд с сервера…", "busy")
        worker = track_qthread(CommandDownloadWorker(), "Загрузка команд")
        self._commands_download = worker
        worker.ready.connect(self._on_server_commands_downloaded)
        worker.start()

    def _on_server_commands_downloaded(self, result: dict[str, Any]) -> None:
        """Предложить режим импорта и применить загруженный набор команд."""
        self._commands_download = None
        if not result["success"]:
            self._set_status("❌ Ошибка загрузки команд", "error")
            QMessageBox.critical(self, "Ошибка", result["message"])
            return

        import tempfile

        tmp = tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w", encoding="utf-8"
        )
        json.dump(result["data"], tmp, ensure_ascii=False, indent=4)
        tmp.close()

        try:
            asked = self._ask_command_import_mode(tmp.name)
            if asked is None:
                self._set_status("Готово", "idle")
                return
            mode, resolution = asked

            success, message, _stats = self.command_service.import_commands_from_file(
                tmp.name, mode=mode, resolution=resolution
            )
        finally:
            os.unlink(tmp.name)

        if success:
            commands = self.command_service.get_all_commands()
            self.command_combo_box.clear()
            self.command_combo_box.load_tasks(commands)
            self.command_combo_box.select_first_task()
            self._set_status(f"✅ {message}", "ok")
            QMessageBox.information(self, "Импорт задач", message)

            # ARCHITECTURE: Публикуем событие об изменении данных
            self.event_bus.publish_typed(
                EventType.DATA_CHANGED,
                source="MainWindow",
                data={
                    "type": "commands_imported",
                    "count": self.command_service.get_tasks_count(),
                },
            )

            logger.info(f"MainWindow: commands imported from server")
        else:
            self._set_status(f"❌ {message}", "error")
            QMessageBox.critical(self, "Ошибка импорта", message)

    def save_commands_to_file(self, file_path: str = None):
        """Сохранение задач в файл"""
        if not file_path:
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Сохранить задачи", "", "JSON Files (*.json)"
            )
            if not file_path:
                return  # Отмена выбора файла не является ошибкой.

        # ARCHITECTURE: Используем сервис для сохранения
        success, message = self.command_service.export_commands_to_file(file_path)

        if success:
            logger.info(f"MainWindow: {message}")
            self._set_status(f"✅ {message}", "ok")
        else:
            logger.error(f"MainWindow: {message}")
            self._set_status(f"❌ {message}", "error")
            QMessageBox.critical(self, "Ошибка", message)

    def show_config_dialog(self):
        """ARCHITECTURE: Показать диалог конфигурации через DialogManager"""
        config = DialogConfig(
            dialog_class=ConfigDialog,
            parent=self,
            modal=True,
            on_result=self._on_config_dialog_result,
        )
        dialog_id = self.dialog_manager.open(config)
        logger.debug(f"MainWindow: Config dialog opened with id={dialog_id}")

    def show_web_server_dialog(self):
        """Показать диалог управления веб-сервером."""
        try:
            from src.ui.dialogs.tools.web_server import WebServerDialog
        except ImportError as e:
            logger.error(f"MainWindow: недоступен диалог веб-сервера: {e}")
            QMessageBox.warning(
                self, "Веб-сервер", f"Модуль веб-сервера недоступен:\n{e}"
            )
            return
        dlg = WebServerDialog(self)
        dlg.exec()

    def show_credentials_dialog(self):
        """Показать диалог управления связками учётных данных."""
        try:
            from src.ui.dialogs.tools.credentials import CredentialsDialog
        except ImportError as e:
            logger.error(f"MainWindow: недоступен диалог связок учётных данных: {e}")
            QMessageBox.warning(
                self, "Связки учётных данных", f"Модуль недоступен:\n{e}"
            )
            return
        dlg = CredentialsDialog(self)
        dlg.exec()

    def show_node_settings_dialog(self):
        """Показать диалог управления нодами (федерация)."""
        try:
            from src.ui.dialogs.tools.node_settings import NodeSettingsDialog
        except ImportError as e:
            logger.error(f"MainWindow: недоступен диалог нод: {e}")
            QMessageBox.warning(self, "Ноды", f"Модуль недоступен:\n{e}")
            return
        dlg = NodeSettingsDialog(self)
        dlg.exec()
        # После диалога: обновить секции нод в дереве/списке
        # (клиенты уже остановлены/запущены диалогом; перезагрузим дерево).
        self._nodes_reload_sections()

    def _node_stop_trackers(self, node_iid: str) -> None:
        """Остановить per-host таймеры (статусы + миниатюры) одной ноды."""
        if hasattr(self, "_node_tree_controller"):
            self._node_tree_controller.stop_trackers(node_iid)

    def _nodes_reload_sections(self) -> None:
        """Перечитать ноды из конфига и синхронизировать секции дерева."""
        if hasattr(self, "_node_tree_controller"):
            self._node_tree_controller.reload_sections()

    def show_veyon_settings_dialog(self):
        """Показать диалог настроек Veyon"""
        from src.utils.system.subprocess_env import is_running_elevated

        dialog = VeyonSettingsDialog(self, elevated=is_running_elevated())
        dialog.exec()
        if dialog.result() == QDialog.DialogCode.Accepted:
            logger.info("MainWindow: Veyon settings saved")
            self.event_bus.publish_typed(
                EventType.DATA_SAVED, source="MainWindow", data={"type": "veyon_config"}
            )
            # Перерисовываем миниатюры под новый стиль отображения (monitor/plain)
            try:
                self.listview.refresh_thumbnails()
            except Exception as e:
                logger.error(f"MainWindow: cannot refresh thumbnails: {e}")

    def show_vnc_settings_dialog(self):
        """Показать диалог настроек VNC"""
        dialog = VncSettingsDialog(self)
        dialog.exec()
        if dialog.result() == QDialog.DialogCode.Accepted:
            logger.info("MainWindow: VNC settings saved")
            self.event_bus.publish_typed(
                EventType.DATA_SAVED, source="MainWindow", data={"type": "vnc_config"}
            )

    def show_thumbnail_settings_dialog(self) -> None:
        from src.ui.dialogs.common.thumbnail_settings import ThumbnailSettingsDialog

        ThumbnailSettingsDialog(self, self.config_service).exec()

    def show_diagnostics_dialog(self):
        """Показать диалог диагностики SSH/Veyon/VNC."""
        dialog = DiagnosticsDialog(self)
        dialog.exec()

    def _refresh_veyon_ui_visibility(self):
        """Скрыть/показать Veyon-элементы UI в зависимости от доступности Veyon.

        Управляет видимостью:
        - Кнопки Veyon-фичей и переключатель «Миниатюры Veyon» в тулбаре списка.
        - Veyon-подменю в контекстном меню списка устройств.

        Пункт «Настройки Veyon» в меню «Удалённое управление» остаётся
        видимым всегда — через него пользователь диагностирует отсутствие
        Veyon и запускает сервис вручную.

        Состояние детектора кэшируется на стороне VeyonService (TTL 30 сек);
        здесь дополнительно сбрасываем кэш, чтобы изменения статуса сервиса
        подхватывались таймером _veyon_status_timer.
        """
        try:
            self.veyon_service.invalidate_installed_cache()
            features = self.veyon_service.is_features_available()
        except Exception as e:
            logger.error(f"MainWindow: veyon availability check failed: {e}")
            features = False

        try:
            devices = (
                self.listview.get_all_devices() if hasattr(self, "listview") else []
            )
            thumbnails = self.thumbnail_service.is_available(devices)
        except Exception as exc:
            logger.warning(f"MainWindow: thumbnail availability check failed: {exc}")
            thumbnails = False

        # Видимость тулбаров и контекстного меню — по гранулярным флагам.
        # `features` управляет Veyon-зависимыми кнопками; power-кнопки
        # (power_on/reboot/powerdown/login_ssh/logoff) всегда видны — работают
        # через SSH/UDP/DBus, не требуют Veyon.

        # Thumbnail readiness is independent of Veyon control features.
        if hasattr(self, "list_toolbar"):
            try:
                self.list_toolbar.set_thumbnails_available(thumbnails)
            except Exception as e:
                logger.error(f"MainWindow: cannot update ViewToolbar: {e}")

        # VeyonToolbar: виден = (галка пользователя) AND (Veyon доступен).
        # Галка сохраняет желание пользователя; при появлении Veyon тулбар
        # вернётся автоматически (если галка вкл). Разделители вокруг тулбара
        # обновляются синхронно в _apply_veyon_toolbar_visibility.
        if hasattr(self, "veyon_toolbar"):
            try:
                self._apply_veyon_toolbar_visibility()
            except Exception as e:
                logger.error(f"MainWindow: cannot update VeyonToolbar: {e}")

        # ControlToolbar: всегда виден (галка пользователя), не зависит от Veyon.
        # (видимость управляется _toggle_control_toolbar)

        # Пересчитать видимость контейнера (скрыть если все 3 тулбара скрыты).
        self._update_toolbars_container_visibility()

        # Контекстное меню списка: подменю «Veyon» скрывается целиком при
        # недоступном Veyon; подменю «Управление» (power_on/reboot/powerdown/
        # logoff через SSH/UDP) всегда видимо.
        if hasattr(self, "listview"):
            try:
                self.listview.set_veyon_features_available(features)
            except Exception as e:
                logger.error(
                    f"MainWindow: cannot update listview veyon visibility: {e}"
                )

        # Переходы доступности миниатюр: при остановке Veyon — очистить
        # показанные миниатюры и остановить таймеры захвата (карточки
        # показывают стандартные иконки онлайн/офлайн); при восстановлении
        # — перезапустить таймеры. Режим миниатюр (_thumbnail_mode)
        # сохраняется — пользователь не теряет включённый режим.
        prev_thumbs = getattr(self, "_prev_thumbnails_available", None)
        if prev_thumbs is not True and thumbnails is True:
            # Veyon появился — перезапустить миниатюры, если режим включён.
            if hasattr(self, "listview"):
                try:
                    self.listview.reactivate_thumbnails()
                except Exception as e:
                    logger.error(f"MainWindow: cannot reactivate thumbnails: {e}")
        # A missing backend never stops other sources or drops subscriptions.
        self._prev_thumbnails_available = thumbnails

        # Адаптивный интервал таймера статуса Veyon: при активных миниатюрах
        # проверяем чаще (10 сек) чтобы быстро обнаружить падение veyon-service
        # и остановить захват (deactivate_thumbnails) — иначе таймеры продолжают
        # слать задачи в пул, забивая его 10-сек таймаутами на упавший WebAPI.
        # Без миниатюр 60 сек достаточно (только для обновления видимости UI).
        self._adjust_veyon_status_timer_interval()

    def _adjust_veyon_status_timer_interval(self):
        """Подстроить интервал _veyon_status_timer под режим миниатюр."""
        timer = getattr(self, "_veyon_status_timer", None)
        if timer is None:
            return
        thumb_active = False
        if hasattr(self, "listview"):
            thumb_active = bool(getattr(self.listview, "_thumbnail_mode", False))
        new_interval = 10000 if thumb_active else 60000
        if timer.interval() != new_interval:
            timer.setInterval(new_interval)
            logger.debug(
                f"MainWindow: veyon status timer interval -> {new_interval}ms "
                f"(thumbnails={'on' if thumb_active else 'off'})"
            )

    def _start_veyon_status_timer(self):
        """Запустить таймер периодической проверки статуса Veyon.

        Каждые 60 сек сбрасывает кэш детектора и обновляет видимость
        UI-элементов — чтобы подхватить запуск/остановку сервиса Veyon
        в рантайме (без перезапуска приложения).
        """
        from PySide6.QtCore import QTimer

        self._veyon_status_timer = QTimer(self)
        self._veyon_status_timer.setInterval(60000)
        self._veyon_status_timer.timeout.connect(self._refresh_veyon_ui_visibility)
        self._veyon_status_timer.start()

    def show_remote_recording_dialog(self, device: DeviceModel):
        """ARCHITECTURE: Показать диалог удалённой записи"""
        if not device:
            QMessageBox.information(self, "Удалённая запись", "Устройство не выбрано.")
            return
        dialog = RemoteRecordingDialog(device, self)
        dialog.show()
        logger.debug(f"MainWindow: Remote recording dialog opened for {device.host}")

    def show_polkit_editor_dialog(self, device: DeviceModel):
        """Показать диалог редактора политик Polkit"""
        from src.ui.widgets.device_tree import LOCAL_NODE_IID

        if not device:
            QMessageBox.information(self, "Редактор политик", "Устройство не выбрано.")
            return
        node_iid = getattr(device, "node_iid", None)
        if node_iid and node_iid != LOCAL_NODE_IID:
            QMessageBox.information(
                self, "Редактор политик", "Редактор политик доступен только для локальных хостов."
            )
            return
        dialog = PolkitEditorDialog(device, self)
        dialog.exec()
        logger.debug(f"MainWindow: Polkit editor dialog closed for {device.host}")

    def show_veyon_access_dialog(self, device: DeviceModel, mode: str):
        """Запустить окно удалённого доступа (просмотр/управление).

        mode: "view"|"control" - Veyon (veyon-cli remoteaccess);
              "vnc_view"|"vnc_control" - окно VNC программы (VncViewerWindow);
              "krfb_view"|"krfb_control" - KRFB (krdc/браузер с backend=krfb);
              "krfbkr_view"|"krfbkr_control" - KRFB принудительно через krdc;
              "krfbbr_view"|"krfbbr_control" - KRFB принудительно через браузер.
        """
        from src.ui.widgets.device_tree import LOCAL_NODE_IID

        if not device:
            QMessageBox.information(self, "Удалённый доступ", "Устройство не выбрано.")
            return

        # Окно VNC программы: прямое подключение к VNC-серверу хоста.
        if mode in ("vnc_view", "vnc_control"):
            _nid = getattr(device, "node_iid", None)
            if _nid and _nid != LOCAL_NODE_IID:
                QMessageBox.information(
                    self, "VNC", "Окно VNC доступно только для локальных хостов."
                )
                return
            from src.ui.dialogs.tools.vnc_viewer import open_vnc_viewer

            open_vnc_viewer(
                device,
                "view" if mode == "vnc_view" else "control",
                getattr(self, "view_state", None),
            )
            return

        # KRFB-режимы: через KRFBService. На Windows всегда браузер; на
        # Linux krdc-варианты принудительны ("krfbkr_*"), браузерные -
        # принудительны ("krfbbr_*"), базовые ("krfb_*") - авто.
        if mode in (
            "krfb_view",
            "krfb_control",
            "krfbkr_view",
            "krfbkr_control",
            "krfbbr_view",
            "krfbbr_control",
        ):
            krfb_mode = "view" if mode.endswith("_view") else "control"
            if mode.startswith("krfbkr_"):
                viewer = "krdc"
            elif mode.startswith("krfbbr_"):
                viewer = "browser"
            else:
                viewer = None
            ok = self.krfb_service.open_viewer(device, krfb_mode, viewer=viewer)
            if not ok:
                QMessageBox.warning(
                    self,
                    "KRFB",
                    f"Не удалось открыть доступ ({krfb_mode}):\n"
                    f"{self.krfb_service.last_error() or 'ошибка'}",
                )
            return

        # Node-хост УДАЛЁННОЙ ноды: веб-вьювер ноды-владельца (редирект).
        # Локальные хосты (node_iid == LOCAL_NODE_IID при тумблере «Ноды») —
        # локальный veyon-cli.
        _nid = getattr(device, "node_iid", None)
        if _nid and _nid != LOCAL_NODE_IID:
            client = self.node_service.get_client(_nid)
            if client is None:
                QMessageBox.warning(
                    self,
                    "Veyon",
                    f"Нода {self.node_service.get_display_name(_nid)} недоступна.",
                )
                return
            self._request_node_vnc(client, _nid, device.iid, mode)
            return
        proc = self.veyon_service.start_remote_access(device.host, mode)
        if proc is None:
            mode_text = "просмотр" if mode == "view" else "управление"
            QMessageBox.warning(
                self,
                "Veyon",
                f"Не удалось запустить Veyon ({mode_text}).\n\n"
                f"Проверьте, что veyon-cli установлен и доступен в PATH.\n"
                f"Хост: {device.host}",
            )
        else:
            logger.debug(f"MainWindow: Veyon {mode} started for {device.host}")

    def _request_node_vnc(
        self, client: object, node_iid: str, device_iid: str, mode: str
    ) -> None:
        """Получить ссылку VNC в рабочем потоке, исключая повторный запрос."""
        from src.workers.network.worker_pool import get_service_worker_pool

        key = (node_iid, device_iid)
        if key in self._node_vnc_pending:
            return
        self._node_vnc_pending.add(key)

        def complete(task_id: str, result: object) -> None:
            self._node_vnc_result.emit(client, node_iid, device_iid, result)

        def failed(task_id: str, error: str) -> None:
            complete(task_id, ("", error))

        try:
            get_service_worker_pool().submit(
                lambda: client.vnc_url(device_iid, view=1 if mode == "view" else 0),
                on_complete=complete, on_error=failed,
                metadata={"type": "node_vnc_url", "node_iid": node_iid},
            )
        except RuntimeError as error:
            failed("", str(error))

    def _apply_node_vnc_result(
        self, client: object, node_iid: str, device_iid: str, result: object
    ) -> None:
        """Открыть актуальную ссылку после доставки результата в GUI."""
        self._node_vnc_pending.discard((node_iid, device_iid))
        if self._shutdown_pending or self.node_service.get_client(node_iid) is not client:
            return
        section = self.treeview.get_node_section_item(node_iid)
        if section is None or self.treeview._find_tree_item_by_iid(device_iid, section) is None:
            return
        url, error = result
        if not url:
            QMessageBox.warning(self, "Veyon", error or "VNC ноды недоступен.")
            return
        from src.utils.system.subprocess_env import open_url_in_browser

        if not open_url_in_browser(url):
            QMessageBox.warning(self, "Veyon", "Не удалось открыть браузер.")

    # Карты REST-имён фичей → (метод сервиса, нужны_аргументы).
    # Veyon-зависимые фичи (WebAPI/RFB) и host-control (SSH/systemd/UDP/DBus)
    # разделены по сервисам: VeyonService.FEATURE_MAP и HostControlService.FEATURE_MAP.
    # krfb-операции (через SSH) - в KRFBService.FEATURE_MAP.
    # Единый резолв — через _resolve_feature (возвращает сервис + entry).
    _VEYON_FEATURE_MAP = VeyonService.FEATURE_MAP
    _HOST_FEATURE_MAP = HostControlService.FEATURE_MAP
    _KRFB_FEATURE_MAP = KRFBService.FEATURE_MAP

    def _prompt_veyon_args(self, feature: str, device=None):
        """Показать диалог ввода аргументов для фичи.

        Returns:
            tuple аргументов для метода сервиса или None если отменено.
        """
        if feature in ("login", "login_ssh"):
            # Одинаковый диалог для Veyon-входа (WebAPI) и SSH/DBus-входа.
            title = (
                "Вход пользователя (SSH)"
                if feature == "login_ssh"
                else "Вход пользователя"
            )
            dialog = QDialog(self)
            dialog.setWindowTitle(title)
            dialog.setMinimumWidth(320)
            layout = QVBoxLayout(dialog)
            from PySide6.QtWidgets import QFormLayout

            form = QFormLayout()
            user_edit = QLineEdit()
            pass_edit = QLineEdit()
            pass_edit.setEchoMode(QLineEdit.Password)
            form.addRow("Имя пользователя:", user_edit)
            form.addRow("Пароль:", pass_edit)
            layout.addLayout(form)
            btn_layout = QHBoxLayout()
            ok_btn = QPushButton("OK")
            cancel_btn = QPushButton("Отмена")
            btn_layout.addWidget(ok_btn)
            btn_layout.addWidget(cancel_btn)
            layout.addLayout(btn_layout)
            ok_btn.clicked.connect(dialog.accept)
            cancel_btn.clicked.connect(dialog.reject)
            if dialog.exec() == QDialog.Accepted:
                return (user_edit.text().strip(), pass_edit.text())
            return None
        elif feature == "website":
            url, ok = QInputDialog.getText(
                self, "Открыть сайт", "URL:", text="https://"
            )
            if ok and url.strip():
                return (url.strip(),)
            return None
        elif feature == "start_app":
            cmd, ok = QInputDialog.getText(
                self, "Запустить программу", "Командная строка:", text=""
            )
            if ok and cmd.strip():
                return (cmd.strip(),)
            return None
        elif feature == "message":
            text, ok = QInputDialog.getMultiLineText(
                self, "Отправить сообщение", "Текст сообщения:", ""
            )
            if ok and text.strip():
                return (text.strip(),)
            return None
        elif feature == "power_on":
            mac = None
            if device and getattr(device, "mac_address", None):
                mac = device.mac_address
            if not mac:
                mac, ok = QInputDialog.getText(
                    self, "Включить (Wake-on-LAN)", "MAC-адрес:", text=""
                )
                if not ok or not mac.strip():
                    return None
                mac = mac.strip()
            return (mac,)
        elif feature == "krfb_password":
            pwd, ok = QInputDialog.getText(
                self,
                "Сменить пароль VNC",
                "Новый пароль (пусто = сгенерировать):",
                QLineEdit.EchoMode.Password,
            )
            if not ok:
                return None
            pwd = pwd.strip()
            if not pwd:
                pwd = self.krfb_service.generate_password()
                QMessageBox.information(
                    self,
                    "Сменить пароль VNC",
                    f"Сгенерирован пароль:\n\n{pwd}\n\n"
                    "Пароль отправляется на хост; скопируйте его сейчас.",
                )
            return (pwd,)
        return ()

    def _resolve_feature(self, feature: str):
        """Резолв token → (service, (method_name, has_args)).

        Ищет в трёх картах: VeyonService.FEATURE_MAP (Veyon-зависимые),
        HostControlService.FEATURE_MAP (SSH/systemd/UDP/DBus)
        и KRFBService.FEATURE_MAP (krfb-операции через SSH).
        Возвращает (service, entry) или (None, None) если токен неизвестен.
        """
        entry = self._VEYON_FEATURE_MAP.get(feature)
        if entry:
            return self.veyon_service, entry
        entry = self._HOST_FEATURE_MAP.get(feature)
        if entry:
            return self.host_control_service, entry
        entry = self._KRFB_FEATURE_MAP.get(feature)
        if entry:
            return self.krfb_service, entry
        return None, None

    def show_veyon_feature(self, device: DeviceModel, feature: str):
        """Применить фичу к одному устройству (в пуле потоков, с отчётом в статус-строку).

        Точечная отработка из контекстного меню — действует только на одно устройство.
        Фича может принадлежать VeyonService (WebAPI/RFB) или HostControlService
        (SSH/systemd/UDP/DBus) — резолв через _resolve_feature.

        Результат и ошибка именно этого вызова выводятся в статусную строку:
        «✅ <Имя>: <host>» / «❌ <Имя>: <host> — <детали>».
        """
        from src.ui.widgets.device_tree import LOCAL_NODE_IID

        if not device:
            return
        # Node-хост УДАЛЁННОЙ ноды: фича выполняется на ноде-владельце через
        # её API. Локальные хосты при включённом тумблере «Ноды» имеют
        # node_iid == LOCAL_NODE_IID — они идут обычным путём через сервис.
        _nid = getattr(device, "node_iid", None)
        if _nid and _nid != LOCAL_NODE_IID:
            self._show_veyon_feature_node(device, feature)
            return
        service, entry = self._resolve_feature(feature)
        if not service or not entry:
            return
        method_name, has_args = entry
        pool = get_command_worker_pool()
        label = _FEATURE_LABELS.get(feature, feature)
        mood = _FEATURE_MOOD.get(feature, "")

        def _report_single(outcome: OperationOutcome) -> None:
            """Колбэк-обёртка: обновить статус-строку по результату одной фичи."""
            if outcome:
                self._set_status(f"✅ {label}: {device.host}", "ok", mood=mood)
            else:
                self._set_status(
                    f"❌ {label}: {device.host} — {outcome.error or 'ошибка'}",
                    "error",
                    hint=outcome.detail,
                    mood=mood,
                )

        if has_args:
            args = self._prompt_veyon_args(feature, device)
            if args is None:
                return
            # login/login_ssh/website/start_app/message принимают (host, *args);
            # power_on — только (mac), без host.
            if feature in ("website", "start_app", "message", "login", "login_ssh"):

                def _run() -> bool:
                    return bool(getattr(service, method_name)(device.host, *args))

            else:

                def _run() -> bool:
                    return bool(getattr(service, method_name)(*args))

        else:

            def _run() -> bool:
                return bool(getattr(service, method_name)(device.host))

        self._set_status(f"⏳ {label}: {device.host}…", "busy", mood=mood)

        def _on_complete(task_id: str, outcome: OperationOutcome) -> None:
            _report_single(outcome)

        def _on_error(task_id: str, error: str) -> None:
            self._set_status(f"❌ {label}: {device.host} — {error}", "error", mood=mood)

        pool.submit(
            lambda: run_capturing_errors(service, _run),
            on_complete=_on_complete,
            on_error=_on_error,
            metadata={
                "host": device.host,
                "type": "veyon_feature",
                "feature": label,
                "device": device.name or device.host,
            },
        )

    def _show_veyon_feature_node(
        self,
        device: DeviceModel,
        feature: str,
        prefill_args: dict = None,
        on_complete=None,
        on_error=None,
    ) -> None:
        """Применить фичу к node-хосту через API ноды-владельца.

        Маршрутизация через NodeService → client.veyon_feature(iid, feature, args).
        ``prefill_args`` (batch из on_toolbar_veyon_feature) подставляются как
        есть — один промпт на все node-хосты вместо окна на каждый хост.
        ``on_complete(task_id, (ok, err))`` / ``on_error(task_id, str)`` —
        необязательные колбэки агрегатора batch-режима; без них используется
        встроенный per-host отчёт в статус-строку (точечный вызов).
        """
        from src.ui.widgets.device_tree import LOCAL_NODE_IID

        label = _FEATURE_LABELS.get(feature, feature)
        mood = _FEATURE_MOOD.get(feature, "")
        nid = getattr(device, "node_iid", None)
        # Защитный guard: локальные хосты (LOCAL_NODE_IID) сюда попадать не
        # должны — у них нет клиента ноды; делегируем локальному сервису.
        if not nid or nid == LOCAL_NODE_IID:
            self.show_veyon_feature(device, feature)
            return
        client = self.node_service.get_client(nid)
        if client is None:
            self._set_status(
                f"❌ {label}: нода {self.node_service.get_display_name(nid)} недоступна",
                "error",
                mood=mood,
            )
            return

        # Аргументные фичи: запрашиваем параметры (как для локальных)
        args: dict = {}
        if prefill_args is not None:
            # Batch-режим: аргументы уже собраны (общие для всех node-хостов)
            args = dict(prefill_args)
        elif feature in ("website",):
            a = self._prompt_veyon_args(feature, device)
            if a is None:
                return
            args = {"url": a[0] if isinstance(a, tuple) and a else ""}
        elif feature in ("start_app",):
            a = self._prompt_veyon_args(feature, device)
            if a is None:
                return
            # start_app: список команд
            args = {
                "applications": list(a) if isinstance(a, (list, tuple)) else [str(a)]
            }
        elif feature in ("message",):
            a = self._prompt_veyon_args(feature, device)
            if a is None:
                return
            args = {"text": a[0] if isinstance(a, tuple) and a else ""}
        elif feature in ("login", "login_ssh"):
            a = self._prompt_veyon_args(feature, device)
            if a is None:
                return
            if isinstance(a, tuple) and len(a) >= 2:
                args = {"username": a[0], "password": a[1]}
        elif feature == "power_on":
            mac = getattr(device, "mac_address", None)
            if not mac:
                self._set_status(
                    f"⚠ {label}: {device.host} — нет MAC-адреса",
                    "error",
                    mood=mood,
                )
                return
            args = {"mac": mac}

        self._set_status(f"⏳ {label}: {device.host}…", "busy", mood=mood)
        iid = device.iid

        def _run():
            try:
                return client.veyon_feature(iid, feature, args)
            except Exception as e:
                return False, str(e)

        # Batch-режим передаёт агрегирующие колбэки (общий счётчик на весь
        # батч); точечный вызов — встроенный per-host отчёт.
        def _on_complete_builtin(task_id, result):
            try:
                ok, err = result
            except Exception:
                ok, err = False, ""
            if ok:
                self._set_status(f"✅ {label}: {device.host}", "ok", mood=mood)
            else:
                self._set_status(
                    f"❌ {label}: {device.host} — {err or 'ошибка'}",
                    "error",
                    mood=mood,
                )

        def _on_error_builtin(task_id, error):
            self._set_status(f"❌ {label}: {device.host} — {error}", "error", mood=mood)

        _cb_complete = on_complete if on_complete is not None else _on_complete_builtin
        _cb_error = on_error if on_error is not None else _on_error_builtin

        pool = get_command_worker_pool()
        pool.submit(
            _run,
            on_complete=_cb_complete,
            on_error=_cb_error,
            metadata={
                "host": device.host,
                "type": "node_veyon_feature",
                "feature": label,
                "device": device.name or device.host,
            },
        )

    def on_toolbar_veyon_feature(self, feature: str):
        """Применить фичу ко всем устройствам в списке (= отмеченным в дереве).

        Список DeviceList содержит все устройства, отмеченные чекбоксами в дереве
        (по всем категориям/группам). Выделение мышью в списке игнорируется —
        тулбар работает со всем списком целиком; выделенные хосты обслуживает
        групповое ПКМ списка.
        """
        self._run_feature_on_devices(self.listview.get_all_devices(), feature)

    def _run_feature_on_devices(self, devices: list, feature: str) -> None:
        """Применить фичу к группе хостов (тулбар или групповое ПКМ).

        Результаты агрегируются: «✅ <Имя>: N/M успешно» + детали последней
        ошибки в статус-строке. Для аргументных фичей параметры спрашиваются
        один раз (общие для всех хостов), кроме ``power_on`` (MAC индивидуален).
        """
        from src.ui.widgets.device_tree import LOCAL_NODE_IID

        devices = list(devices or [])
        if not devices:
            return
        # Резолв сервиса/entry — всегда: локальные хосты при включённом
        # тумблере «Ноды» имеют node_iid == LOCAL_NODE_IID, «node_iid задан»
        # НЕ значит «все хосты удалённые». Без резолва has_args терялся →
        # аргументные фичи (website и др.) пропускали общий промпт и окно
        # ввода появлялось на каждый хост по отдельности.
        service, entry = self._resolve_feature(feature)
        if not service or not entry:
            return
        method_name, has_args = entry
        pool = get_command_worker_pool()
        label = _FEATURE_LABELS.get(feature, feature)
        total = len(devices)
        mood = _FEATURE_MOOD.get(feature, "")
        self._set_status(f"⏳ {label}: 0/{total}…", "busy", mood=mood)

        def _is_remote(item: DeviceModel) -> bool:
            nid = getattr(item, "node_iid", None)
            return bool(nid and nid != LOCAL_NODE_IID)

        # Итог считается по реально запущенным локальным задачам: хосты нод идут
        # своим агрегатором, а WoL без MAC-адреса не запускается.
        local_total = sum(
            1
            for item in devices
            if not _is_remote(item)
            and (feature != "power_on" or getattr(item, "mac_address", None))
        )
        aggregator = BatchStatusAggregator(
            local_total,
            label,
            lambda text, state, hint: self._set_status(
                text, state, hint=hint, mood=mood
            ),
        )

        def _make_runner(host: str, args: tuple = ()):
            """Создать runner-функцию для одного хоста."""

            def _call() -> bool:
                if feature in (
                    "website",
                    "start_app",
                    "message",
                    "login",
                    "login_ssh",
                ):
                    return bool(getattr(service, method_name)(host, *args))
                if feature == "power_on":
                    return bool(service.power_on(host))
                return bool(getattr(service, method_name)(host))

            def _run() -> OperationOutcome:
                return run_capturing_errors(service, _call)

            return _run

        def _on_complete(task_id: str, outcome: OperationOutcome) -> None:
            aggregator.record(bool(outcome), outcome.error, outcome.detail)

        def _on_error(task_id: str, error: str) -> None:
            aggregator.record(False, error)

        if has_args:
            # Для аргументных фичей спрашиваем параметры один раз
            # (login/login_ssh/website/start_app/message — один набор для всех).
            # power_on — специальный случай: MAC индивидуален per-device.
            # Хосты без MAC пропускаются молча (лог info), без модального промпта.
            if feature == "power_on":
                # Агрегатор нод-части (раньше per-host статусы перезаписывались,
                # батч WoL выглядел зависшим на первом IP).
                _node_on_complete, _node_on_error = self._make_node_aggregator(
                    devices, feature, label, mood
                )
                for d in devices:
                    # Node-хост удалённой ноды: power_on через ноду (MAC из
                    # данных хоста на ноде). Локальные (node_iid None или
                    # LOCAL_NODE_IID) — обычный WoL.
                    nid = getattr(d, "node_iid", None)
                    if nid and nid != LOCAL_NODE_IID:
                        self._show_veyon_feature_node(
                            d,
                            feature,
                            on_complete=_node_on_complete,
                            on_error=_node_on_error,
                        )
                        continue
                    mac = getattr(d, "mac_address", None)
                    if not mac:
                        logger.info(
                            f"MainWindow: WoL skipped for {d.host}: no MAC address"
                        )
                        continue
                    # power_on принимает MAC, не host — runner особый.
                    runner = (
                        lambda m: lambda: run_capturing_errors(
                            service, lambda: bool(service.power_on(m))
                        )
                    )(mac)
                    pool.submit(
                        runner,
                        on_complete=_on_complete,
                        on_error=_on_error,
                        metadata={
                            "host": d.host,
                            "type": "veyon_feature",
                            "feature": label,
                            "device": d.name or d.host,
                        },
                    )
                return
            args = self._prompt_veyon_args(feature)
            if args is None:
                return
            # Общие node-args — один раз для всех node-хостов (без повторных
            # промптов: _prompt_veyon_args выше уже спросил пользователя).
            node_args = _node_args_for_feature(feature, args)
            # Агрегатор статуса для node-части батча (Фикс: раньше каждый
            # node-хост перезаписывал статус-строку индивидуально, итоговая
            # сводка и ошибки терялись).
            _node_on_complete, _node_on_error = self._make_node_aggregator(
                devices, feature, label=label, mood=mood
            )

            for d in devices:
                nid = getattr(d, "node_iid", None)
                # Node-хост удалённой ноды: фича выполняется на ноде; локальные
                # (node_iid None или LOCAL_NODE_IID) — локальным сервисом.
                if nid and nid != LOCAL_NODE_IID:
                    self._show_veyon_feature_node(
                        d,
                        feature,
                        prefill_args=node_args,
                        on_complete=_node_on_complete,
                        on_error=_node_on_error,
                    )
                    continue
                runner = _make_runner(d.host, args)
                pool.submit(
                    runner,
                    on_complete=_on_complete,
                    on_error=_on_error,
                    metadata={
                        "host": d.host,
                        "type": "veyon_feature",
                        "feature": label,
                        "device": d.name or d.host,
                    },
                )
        else:
            # Агрегатор статуса для node-части батча безаргументных фичей
            # (lock/unlock/lock_input/unlock_input): раньше каждый node-хост
            # перезаписывал статус индивидуальным «⏳ Блокировка: <ip>…», батч
            # выглядел зависшим на первом хосте, итоговая сводка терялась.
            _node_on_complete, _node_on_error = self._make_node_aggregator(
                devices, feature, label, mood
            )

            for d in devices:
                nid = getattr(d, "node_iid", None)
                # Node-хост удалённой ноды: фича выполняется на ноде; локальные
                # (node_iid None или LOCAL_NODE_IID) — локальным сервисом.
                if nid and nid != LOCAL_NODE_IID:
                    self._show_veyon_feature_node(
                        d,
                        feature,
                        on_complete=_node_on_complete,
                        on_error=_node_on_error,
                    )
                    continue
                runner = _make_runner(d.host)
                pool.submit(
                    runner,
                    on_complete=_on_complete,
                    on_error=_on_error,
                    metadata={
                        "host": d.host,
                        "type": "veyon_feature",
                        "feature": label,
                        "device": d.name or d.host,
                    },
                )

    def _make_node_aggregator(
        self, devices: list, feature: str, label: str = "", mood: str = ""
    ):
        """Создать общие колбэки агрегатора для node-части батча фичей.

        Возвращает (on_complete, on_error): счётчик «⏳ {label} (ноды): N/M
        (✅x ❌y)» → итог «✅ N/M» / «⚠ N/M успешно, K ошибок — <last_err>».
        Без агрегатора каждый node-хост перезаписывал статус индивидуально —
        батч выглядел зависшим на первом хосте, сводка и ошибки терялись.
        """
        from src.ui.widgets.device_tree import LOCAL_NODE_IID

        if not label:
            label = _FEATURE_LABELS.get(feature, feature)

        node_total = sum(
            1
            for d in devices
            if getattr(d, "node_iid", None)
            and getattr(d, "node_iid", None) != LOCAL_NODE_IID
            # WoL без MAC-адреса на ноду не отправляется и итога не даёт.
            and (feature != "power_on" or getattr(d, "mac_address", None))
        )
        aggregator = BatchStatusAggregator(
            node_total,
            label,
            lambda text, state, hint: self._set_status(
                text, state, hint=hint, mood=mood
            ),
            suffix=" (ноды)",
        )

        def _node_on_complete(task_id, result):
            ok, err = False, ""
            try:
                ok, err = result
            except Exception:
                pass
            aggregator.record(bool(ok), err)

        def _node_on_error(task_id, error):
            aggregator.record(False, error)

        return _node_on_complete, _node_on_error

    def _on_config_dialog_result(self, result: DialogResult, data: Any):
        """ARCHITECTURE: Обработчик результата диалога конфигурации"""
        if result == DialogResult.ACCEPTED:
            logger.info("MainWindow: Configuration saved")
            # Публикуем событие об изменении конфигурации
            self.event_bus.publish_typed(
                EventType.DATA_SAVED, source="MainWindow", data={"type": "config"}
            )

    def import_comp_cab_hosts(self):
        """Импорт хостов из comp-cab-setup с папками по кабинетам."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        from src.services.hosts.comp_cab_import import (
            find_default_files,
            parse_comp_cab_hosts,
        )

        defaults = find_default_files()
        start = str(defaults[0]) if defaults else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Импорт из comp-cab-setup", start, "Хосты (*.json *.txt);;Все файлы (*)"
        )
        if not path:
            return
        try:
            result = parse_comp_cab_hosts(path)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Импорт", f"Не удалось прочитать файл: {error}")
            return

        current = self.treeview.get_tree_data()
        known = set()

        def collect(nodes):
            for node in nodes if isinstance(nodes, list) else [nodes]:
                if isinstance(node, dict):
                    if "host" in node and "name" in node:
                        known.add(str(node["host"]).strip().casefold())
                    else:
                        for value in node.values():
                            collect(value)

        collect(current)

        def fresh(nodes):
            out = []
            for node in nodes:
                if "host" in node and "name" in node:
                    if node["host"].casefold() not in known:
                        out.append(node)
                else:
                    for key, value in node.items():
                        kept = fresh(value)
                        if kept:
                            out.append({key: kept})
            return out

        new_nodes = fresh(result["tree"])
        added = sum(1 for _ in self._iter_hosts(new_nodes))
        if not added:
            QMessageBox.information(self, "Импорт", "Новых хостов не найдено.")
            return
        self.treeview.load_tree_data(list(current) + new_nodes)
        self.treeview.data_changed.emit(self.treeview.get_tree_data())
        text = f"Добавлено хостов: {added}"
        if result["skipped"]:
            text += f"\nПропущено: {len(result['skipped'])}"
        QMessageBox.information(self, "Импорт", text)

    @classmethod
    def _iter_hosts(cls, nodes):
        for node in nodes:
            if "host" in node and "name" in node:
                yield node
            else:
                for value in node.values():
                    yield from cls._iter_hosts(value)

    def show_scanner_dialog(self):
        """ARCHITECTURE: Показать диалог сканнера через DialogManager"""
        # Создаем диалог с подключенными сигналами
        from src.ui.dialogs.tools.ip_scanner import IPScannerDialog

        dialog = self._create_scanner_dialog(self)
        if not dialog.has_pending_workers():
            dialog._closing_after_abort = False
        dialog.setModal(False)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def setup_tree_view(self):
        """Настройка дерева устройств"""
        from src.ui.widgets.device_list_toolbar import DeviceTreeToolbar

        self.treeview = DeviceTreeView()

        # Тулбар дерева: добавить папку/хост/диапазон, сортировка,
        # сканер, развернуть/свернуть всё, показ нод первым уровнем.
        self.tree_toolbar = DeviceTreeToolbar()
        tree_bar_layout = self.tree_frame.layout()
        tree_bar_layout.addWidget(self.tree_toolbar)

        # Строка поиска под тулбаром: временный фильтр дерева (имя/адрес).
        # Debounce 200 мс — фильтр применяется после паузы ввода.
        self.tree_search_edit = QLineEdit()
        self.tree_search_edit.setPlaceholderText("Фильтр по имени или адресу…")
        self.tree_search_edit.setClearButtonEnabled(True)
        self.tree_search_edit.setFixedHeight(26)
        self.tree_search_edit.setToolTip(
            "Временный фильтр: показывать только совпадающие хосты"
        )
        self._tree_search_timer = QTimer(self)
        self._tree_search_timer.setSingleShot(True)
        self._tree_search_timer.setInterval(200)
        self._tree_search_timer.timeout.connect(
            lambda: self.treeview.apply_filter(self.tree_search_edit.text())
        )
        self.tree_search_edit.textChanged.connect(
            lambda: self._tree_search_timer.start()
        )
        tree_bar_layout.addWidget(self.tree_search_edit)

        tree_bar_layout.addWidget(self.treeview)

    def setup_list_view(self):
        """Setup device list view"""
        list_frame = QFrame()
        list_frame.setFrameShape(QFrame.Shape.NoFrame)
        list_layout = QVBoxLayout()
        list_layout.setContentsMargins(0, 0, 0, 2)
        list_layout.setSpacing(0)
        list_frame.setLayout(list_layout)

        # Контейнер тулбаров: 3 панели в ряд (Вид | Veyon | Управление).
        # Скрывается целиком (_update_toolbars_container_visibility), если
        # все три тулбара скрыты (галками пользователя ИЛИ авто-скрытием
        # Veyon-тулбара при отсутствии Veyon).
        self.toolbars_container = QFrame()
        self.toolbars_container.setFrameShape(QFrame.Shape.NoFrame)
        tb_layout = QHBoxLayout(self.toolbars_container)
        tb_layout.setContentsMargins(0, 0, 0, 0)
        tb_layout.setSpacing(8)

        # ViewToolbar — фильтр онлайн, миниатюры, размер (импортирован как
        # DeviceListToolbar для обратной совместимости; это ViewToolbar).
        self.list_toolbar = ViewToolbar()
        self.veyon_toolbar = VeyonToolbar()
        self.control_toolbar = ControlToolbar()

        # Вертикальные разделители (QFrame VLine, 1px) между тулбарами.
        # Видимость управляется в _apply_veyon_toolbar_visibility: разделитель
        # виден только если оба соседних тулбара видны.
        self._veyon_left_sep = self._make_toolbar_separator()
        self._veyon_right_sep = self._make_toolbar_separator()

        tb_layout.addWidget(self.list_toolbar)
        tb_layout.addWidget(self._veyon_left_sep)
        tb_layout.addWidget(self.veyon_toolbar)
        tb_layout.addWidget(self._veyon_right_sep)
        tb_layout.addWidget(self.control_toolbar)
        tb_layout.addStretch(1)

        list_layout.addWidget(self.toolbars_container)

        # Ряд пользовательских панелей (по одной QToolBar на панель).
        self.custom_toolbars_container = QFrame()
        self.custom_toolbars_container.setFrameShape(QFrame.Shape.NoFrame)
        custom_tb_layout = QVBoxLayout(self.custom_toolbars_container)
        custom_tb_layout.setContentsMargins(0, 2, 0, 0)
        custom_tb_layout.setSpacing(2)
        self.custom_toolbars: dict = {}
        list_layout.addWidget(self.custom_toolbars_container)

        self.listview = DeviceListView()

        list_layout.addWidget(self.listview)

        # Create splitter between list and table
        self.right_splitter = QSplitter(Qt.Orientation.Vertical)
        self.right_splitter.addWidget(list_frame)
        self.right_frame.layout().addWidget(self.right_splitter)

        # Force initial update
        self.listview.viewport().update()

    def _make_toolbar_separator(self) -> QFrame:
        """Вертикальный разделитель 1px между тулбарами (QFrame VLine)."""
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFixedWidth(1)
        sep.setStyleSheet("color: palette(mid);")
        return sep

    def _apply_veyon_toolbar_visibility(self):
        """Видимость Veyon-тулбара + разделителей вокруг него.

        Veyon-тулбар: виден = (галка пользователя) AND (Veyon-доступен).
        Разделитель слева: виден если (Veyon-тулбар виден) AND (View-галка вкл).
        Разделитель справа: виден если (Veyon-тулбар виден) AND (Control-галка вкл).

        Использует desires (галки + Veyon-доступность), не isVisible() —
        isVisible() возвращает False до show() родителя, что приводило бы
        к преждевременному скрытию разделителей при init.
        """
        try:
            veyon_available = self.veyon_service.is_features_available()
        except Exception:
            veyon_available = False
        veyon_visible = self.veyon_toolbar_action.isChecked() and veyon_available
        self.veyon_toolbar.setVisible(veyon_visible)
        self._veyon_left_sep.setVisible(
            veyon_visible and self.view_toolbar_action.isChecked()
        )
        self._veyon_right_sep.setVisible(
            veyon_visible and self.control_toolbar_action.isChecked()
        )

    def _update_toolbars_container_visibility(self):
        """Скрыть область тулбаров, если все три тулбара скрыты.

        Использует desires (галки пользователя + Veyon-доступность), а не
        isVisible() — isVisible() возвращает False до show() родителя,
        что приводило бы к преждевременному скрытию контейнера при init.

        Видимость каждого тулбара:
        - View: галка view_toolbar_action.
        - Veyon: галка veyon_toolbar_action AND Veyon-доступен.
        - Control: галка control_toolbar_action.
        Если ни один не должен быть виден — скрываем контейнер целиком.
        """
        if not hasattr(self, "toolbars_container"):
            return
        try:
            veyon_available = self.veyon_service.is_features_available()
        except Exception:
            veyon_available = False
        any_wanted = (
            self.view_toolbar_action.isChecked()
            or (self.veyon_toolbar_action.isChecked() and veyon_available)
            or self.control_toolbar_action.isChecked()
        )
        self.toolbars_container.setVisible(any_wanted)

    def _toggle_view_toolbar(self):
        """Переключить видимость панели «Вид»."""
        visible = self.view_toolbar_action.isChecked()
        self.list_toolbar.setVisible(visible)
        self.view_state.set("device_list_view", "view_toolbar_visible", visible)
        # Разделитель слева от Veyon зависит от видимости View — обновить.
        self._apply_veyon_toolbar_visibility()
        self._update_toolbars_container_visibility()

    def _toggle_veyon_toolbar(self):
        """Переключить видимость панели «Veyon».

        Галка пользователя сохраняется; фактическая видимость определяется
        в _apply_veyon_toolbar_visibility (галка AND Veyon-доступен).
        Разделители вокруг тулбара обновляются синхронно.
        """
        visible = self.veyon_toolbar_action.isChecked()
        self._apply_veyon_toolbar_visibility()
        self.view_state.set("device_list_view", "veyon_toolbar_visible", visible)
        self._update_toolbars_container_visibility()

    def _toggle_control_toolbar(self):
        """Переключить видимость панели «Управление»."""
        visible = self.control_toolbar_action.isChecked()
        self.control_toolbar.setVisible(visible)
        self.view_state.set("device_list_view", "control_toolbar_visible", visible)
        # Разделитель справа от Veyon зависит от видимости Control — обновить.
        self._apply_veyon_toolbar_visibility()
        self._update_toolbars_container_visibility()

    def _toggle_result_table(self):
        """Переключить видимость таблицы результатов выполнения команд."""
        visible = self.result_table_visible_action.isChecked()
        self.result_table_frame.setVisible(visible)
        self.view_state.set("device_list_view", "result_table_visible", visible)

    # ---- Пользовательские панели команд ----

    def _custom_panels(self) -> list:
        try:
            return list(getattr(config.app.custom_toolbar, "panels", []) or [])
        except Exception:
            return []

    def _custom_visibility_map(self) -> dict:
        try:
            m = self.view_state.get("device_list_view", "custom_toolbars_visible", {})
            return dict(m) if isinstance(m, dict) else {}
        except Exception:
            return {}

    def _is_custom_panel_visible(self, panel_iid: str) -> bool:
        return bool(self._custom_visibility_map().get(panel_iid, True))

    def _custom_task_resolver(self, task_iid: str) -> tuple:
        try:
            task = datastore.find_task_by_iid(task_iid or "")
        except Exception:
            task = None
        if task:
            return (True, str(task.get("name", "")))
        return (False, "")

    def _rebuild_custom_toolbars(self) -> None:
        """Перестроить виджеты панелей и пункты меню Вид > Панели."""
        if not hasattr(self, "custom_toolbars_container"):
            return
        layout = self.custom_toolbars_container.layout()
        vis_map = self._custom_visibility_map()
        panels = self._custom_panels()
        # Удалить виджеты исчезнувших панелей
        for pid in list(getattr(self, "custom_toolbars", {})):
            if not any(getattr(p, "iid", "") == pid for p in panels):
                w = self.custom_toolbars.pop(pid)
                try:
                    layout.removeWidget(w)
                    w.deleteLater()
                except Exception:
                    pass
        for panel in panels:
            pid = getattr(panel, "iid", "")
            w = getattr(self, "custom_toolbars", {}).get(pid)
            if w is None:
                w = CustomCommandToolbar(pid, self)
                try:
                    w.commandRequested.connect(self._on_custom_toolbar_command)
                except Exception:
                    pass
                try:
                    w.customContextMenuRequested.connect(
                        lambda _pos, _p=panel: self.show_custom_toolbars_dialog()
                    )
                except Exception:
                    pass
                self.custom_toolbars[pid] = w
                layout.addWidget(w)
            try:
                w.rebuild(panel, self._custom_task_resolver)
            except Exception as e:
                logger.debug(f"MainWindow: custom toolbar rebuild: {e}")
            wanted = bool(vis_map.get(pid, True)) and bool(
                getattr(panel, "buttons", [])
            )
            try:
                w.setVisible(wanted)
            except Exception:
                pass
        self._rebuild_custom_panels_menu()
        self._update_custom_toolbars_visibility()

    def _rebuild_custom_panels_menu(self) -> None:
        panels_menu = getattr(self, "panels_menu", None)
        if panels_menu is None:
            return
        for item in getattr(self, "_custom_panels_menu_items", []) or []:
            try:
                panels_menu.removeAction(item)
            except Exception:
                pass
        self._custom_panels_menu_items = []
        self._custom_toolbar_actions = {}
        customize = getattr(self, "_customize_panels_action", None)
        sep = getattr(self, "_custom_panels_sep", None)
        for panel in self._custom_panels():
            pid = getattr(panel, "iid", "")
            act = QAction(getattr(panel, "name", "") or "Панель", self)
            act.setCheckable(True)
            act.setChecked(self._is_custom_panel_visible(pid))
            act.triggered.connect(
                lambda _checked=False, _p=pid: self._toggle_custom_toolbar(_p)
            )
            if customize is not None:
                try:
                    panels_menu.insertAction(customize, act)
                except Exception:
                    panels_menu.addAction(act)
            else:
                panels_menu.addAction(act)
            self._custom_panels_menu_items.append(act)
            self._custom_toolbar_actions[pid] = act
        if sep is not None:
            try:
                sep.setVisible(bool(self._custom_panels()))
            except Exception:
                pass

    def _toggle_custom_toolbar(self, panel_iid: str) -> None:
        act = getattr(self, "_custom_toolbar_actions", {}).get(panel_iid)
        visible = bool(act.isChecked()) if act is not None else True
        vis_map = self._custom_visibility_map()
        vis_map[panel_iid] = visible
        self.view_state.set("device_list_view", "custom_toolbars_visible", vis_map)
        w = getattr(self, "custom_toolbars", {}).get(panel_iid)
        if w is not None:
            # Пустая панель остается скрытой даже при включенной галке.
            # Проверяем конфиг (кнопки), а не w.actions(): actions включает
            # label-виджет и сепараторы, поэтому для панели с названием
            # bool(actions) врал бы True даже без кнопок.
            try:
                panel = next(
                    (
                        p
                        for p in self._custom_panels()
                        if getattr(p, "iid", "") == panel_iid
                    ),
                    None,
                )
                if panel is not None:
                    has_buttons = bool(getattr(panel, "buttons", []))
                else:
                    name_action = getattr(w, "_name_action", None)
                    has_buttons = any(
                        not a.isSeparator() and a is not name_action
                        for a in w.actions()
                    )
            except Exception:
                has_buttons = True
            w.setVisible(visible and has_buttons)
        self._update_custom_toolbars_visibility()

    def _update_custom_toolbars_visibility(self) -> None:
        container = getattr(self, "custom_toolbars_container", None)
        if container is None:
            return
        try:
            any_visible = any(
                w.isVisibleTo(container) if hasattr(w, "isVisibleTo") else w.isVisible()
                for w in getattr(self, "custom_toolbars", {}).values()
            )
        except Exception:
            any_visible = False
        # isVisibleTo до show() врет - смотрим желание (видимость виджетов)
        if not any_visible:
            try:
                any_visible = any(
                    not w.isHidden()
                    for w in getattr(self, "custom_toolbars", {}).values()
                )
            except Exception:
                pass
        container.setVisible(bool(any_visible))

    def show_custom_toolbars_dialog(self) -> None:
        """Открыть редактор пользовательских панелей.

        Редактор сам сохраняет конфиг и перестраивает панели по Применить
        и по Ok; здесь остается пересобрать на случай отклонения диалога
        после примененных ранее изменений (Применить, затем Отмена).
        """
        try:
            from src.ui.dialogs.custom_toolbar import CustomToolbarsDialog

            dlg = CustomToolbarsDialog(self)
            dlg.exec()
            if dlg._changed_since_open():
                try:
                    get_container().resolve(ConfigService).save_config()
                except Exception:
                    try:
                        config.save()
                    except Exception:
                        pass
                self._rebuild_custom_toolbars()
        except Exception as e:
            logger.error(f"MainWindow: custom toolbars dialog: {e}")
            QMessageBox.warning(self, "Панели", f"Не удалось открыть редактор:\n{e}")

    def _on_custom_toolbar_command(self, task_iid: str) -> None:
        """Клик по кнопке панели: выполнить команду как кнопка Выполнить."""
        try:
            task = datastore.find_task_by_iid(task_iid or "")
        except Exception:
            task = None
        if not task:
            ret = QMessageBox.warning(
                self,
                "Панель",
                "Команда удалена или недоступна. Открыть редактор панелей?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ret == QMessageBox.StandardButton.Yes:
                self.show_custom_toolbars_dialog()
            return
        try:
            devices = self.listview.get_all_devices()
        except Exception:
            devices = []
        if not devices:
            QMessageBox.information(
                self, "Панель", "Нет хостов в списке для выполнения."
            )
            return
        try:
            self.execute_command(devices, task)
        except Exception as e:
            logger.error(f"MainWindow: custom toolbar execute: {e}")
            QMessageBox.warning(self, "Панель", f"Не удалось выполнить команду:\n{e}")

    def _on_toolbar_online_only(self, online_only: bool):
        self.listview.set_online_only(online_only)
        self.view_state.set("device_list_view", "online_only", online_only)

    def _on_toolbar_icon_size(self, size: int):
        self.listview.set_icon_size(size)
        self.view_state.set("device_list_view", "icon_size", size)

    def _on_toolbar_thumbnail_mode(self, enabled: bool):
        self.listview.set_thumbnail_mode(enabled)
        self.view_state.set("device_list_view", "thumbnail_mode", enabled)
        # Адаптивный интервал таймера статуса Veyon зависит от режима миниатюр.
        self._adjust_veyon_status_timer_interval()

    def _on_toolbar_thumbnail_style(self, plain: bool):
        # plain=True → одна картинка, plain=False → в рамке
        style = "plain" if plain else "monitor"
        if self.config_service.update_thumbnail_config(style=style):
            ok, message = self.config_service.save_config()
            if not ok:
                logger.warning(f"MainWindow: cannot save thumbnail style: {message}")
            self.listview.refresh_thumbnails()

    def _on_toolbar_tooltip_fields(self, fields: list):
        """Меню «Информация в тултипе хоста»: применить и сохранить выбор полей."""
        get_host_info_service().set_enabled_fields(fields)
        self.view_state.set("device_list_view", "tooltip_info_fields", fields)

    def setup_result_table(self):
        """Setup result table"""
        self.result_table_frame = QFrame()
        self.result_table_frame.setFrameShape(QFrame.Shape.NoFrame)
        table_layout = QVBoxLayout()
        table_layout.setContentsMargins(0, 2, 0, 0)
        self.result_table_frame.setLayout(table_layout)

        self.result_table = CommandResultTable()
        self.result_table.set_show_full_host_path(
            self.view_state.get("main_window", "result_show_full_host_path", False) is True
        )
        self.result_table.full_host_path_changed.connect(
            lambda full: self.view_state.set(
                "main_window", "result_show_full_host_path", full
            )
        )
        self.result_table.doubleClicked.connect(self.on_result_double_clicked)
        table_layout.addWidget(self.result_table)

        # Add table to splitter
        self.right_splitter.addWidget(self.result_table_frame)

        # Set initial sizes
        self.right_splitter.setStretchFactor(0, 1)  # List gets 1 part
        self.right_splitter.setStretchFactor(1, 1)  # Table gets 1 part

        self.right_splitter.setSizes([50, 50])  # Initial sizes parts

    def setup_layout(self):
        """Настройка компоновки главного окна"""
        # Создаем разделитель для дерева и правой части
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(4)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.insertWidget(0, self.tree_frame)
        self.splitter.insertWidget(1, self.right_frame)
        self.splitter.setStretchFactor(1, 1)

        # Устанавливаем размер дерева в четверть окна (306 из 1074)
        self.splitter.setSizes([306, 768])

        # Основной компоновщик с отступами
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(8, 0, 4, 4)
        main_layout.setSpacing(0)
        main_layout.addWidget(self.menu_bar)
        main_layout.addWidget(self.splitter, 1)
        # Статусная строка внизу окна: текст слева, индикатор + sizegrip справа.
        self._setup_status_bar(main_layout)
        self.setLayout(main_layout)

    # ========== Статусная строка ==========

    def _setup_status_bar(self, parent_layout: QVBoxLayout) -> None:
        """Создать нижнюю статусную строку главного окна.

        Структура (QFrame + QHBoxLayout):
          [QLabel текст статуса, stretch=1] [QLabel индикатор-кружок ●] [QSizeGrip]

        Текст обновляется через ``_set_status(text, state)``; индикатор
        меняет цвет (grey/orange/green/red) по ``state``. QSizeGrip в правом
        нижнем углу позволяет менять размер окна (по образцу
        ``CameraSettingsDialog.setSizeGripEnabled(True)``).
        """
        self._status_frame = QFrame()
        self._status_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self._status_frame.setFixedHeight(22)
        status_layout = QHBoxLayout(self._status_frame)
        status_layout.setContentsMargins(8, 1, 4, 1)
        status_layout.setSpacing(6)

        self._status_label = QLabel("Готово")
        self._status_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        status_layout.addWidget(self._status_label, 1)

        # Каомодзи-метка (по образцу Hermes): эмоция текущего состояния.
        # Отключается в настройках (config.app.kaomoji_enabled).
        self._status_kaomoji = QLabel()
        self._status_kaomoji.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        # Шрифт с широким покрытием CJK/box-drawing глифов: большинство
        # символов берутся из него напрямую, редкие — через штатный
        # fallback Qt (пулы в status_kaomoji.py подобраны так, что fallback
        # не доходит до растровых шрифтов → нет предупреждений DirectWrite).
        kaomoji_font = QFont(_KAOMOJI_FONT_FAMILY)
        self._status_kaomoji.setFont(kaomoji_font)
        status_layout.addWidget(self._status_kaomoji)

        self._status_indicator = QLabel()
        self._status_indicator.setFixedSize(14, 14)
        self._status_indicator.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        status_layout.addWidget(self._status_indicator)

        # Sizegrip — значок-уголок в правом нижнем углу (как в CameraSettingsDialog).
        self._status_sizegrip = QSizeGrip(self._status_frame)
        status_layout.addWidget(self._status_sizegrip)

        parent_layout.addWidget(self._status_frame)

        # Таймер автосброса ok/error → idle.
        self._status_reset_timer: Optional[QTimer] = None
        # Последний показанный каомодзи (исключает повтор подряд).
        self._last_kaomoji: str = ""
        # Исходное состояние.
        self._set_status("Готово", "idle")

    def _set_status(
        self, text: str, state: str = "idle", hint: str = "", mood: str = ""
    ) -> None:
        """Обновить статусную строку (текст + цвет индикатора).

        Потокобезопасно: если вызвано не из GUI-потока, маршалирует через
        ``QTimer.singleShot(0, ...)``. Автосброс ``ok``→``idle`` через 3 сек,
        ``error``→``idle`` через 6 сек (отдельный singleShot-таймер).

        Длинный текст обрезается по ширине ``_status_label`` (elide right,
        многоточие). Tooltip (hint) на метке:

        - если передан явный ``hint`` — он используется как tooltip
          (полный диагноз ошибки), даже если ``text`` не обрезан;
        - иначе если ``text`` обрезан — полный ``text`` идёт в tooltip;
        - иначе tooltip очищается.

        Args:
            text: текст статуса (статистика, результат операции, прогресс).
            state: один из ``idle``/``busy``/``ok``/``error``
                (см. ``STATUS_COLORS``).
            hint: опциональный подробный текст для tooltip (например,
                детальный диагноз ошибки). Пустая строка — нет hint.
            mood: опциональный mood-токен для каомодзи (``lock``/``power``/
                ``wake``/``bulk``) — перекрывает базовый пул состояния
                (см. ``status_kaomoji.STATUS_KAOMOJI_MOOD``). Пустая строка —
                базовый пул.
        """
        # Маршалинг в GUI-поток, если вызвано из worker-потока.
        if QThread.currentThread() != QApplication.instance().thread():
            self._status_update_signal.emit(text, state, hint, mood)
            return

        color = STATUS_COLORS.get(state, STATUS_COLORS["idle"])

        # Elide: обрезаем длинный текст по ширине метки.
        # Ширина берётся с запасом (в status bar могут быть и другие виджеты).
        fm = QFontMetrics(self._status_label.font())
        label_width = max(80, self._status_label.width() - 4)
        elided = fm.elidedText(text, Qt.TextElideMode.ElideRight, label_width)
        self._status_label.setText(elided)

        # Tooltip (hint): явный hint приоритетнее; иначе полный text если обрезан.
        if hint:
            self._status_label.setToolTip(hint)
        elif elided != text:
            self._status_label.setToolTip(text)
        else:
            self._status_label.setToolTip("")

        self._status_indicator.setPixmap(_status_dot_icon(color).pixmap(12, 12))

        # Каомодзи: обновляем вместе с состоянием.
        self._update_kaomoji(state, mood)

        # Автосброс ok/error → idle через задержку.
        if state in ("ok", "error"):
            delay = 3000 if state == "ok" else 6000
            # Перезапускаем таймер автосброса.
            if self._status_reset_timer is not None:
                self._status_reset_timer.stop()
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._status_reset_to_idle)
            t.start(delay)
            self._status_reset_timer = t
        elif self._status_reset_timer is not None and state == "idle":
            # Явный idle — отменяем запланированный автосброс.
            self._status_reset_timer.stop()
            self._status_reset_timer = None

    def _status_reset_to_idle(self) -> None:
        """Автосброс индикатора на idle после ok/error."""
        self._status_reset_timer = None
        # Текст оставляем — пользователь видит последний результат,
        # меняем только цвет индикатора.
        self._status_indicator.setPixmap(
            _status_dot_icon(STATUS_COLORS["idle"]).pixmap(12, 12)
        )
        # Каомодзи тоже возвращаем в нейтральное состояние.
        self._update_kaomoji("idle")

    def _update_kaomoji(self, state: str, mood: str = "") -> None:
        """Показать/скрыть каомодзи по флагу ``config.app.kaomoji_enabled``.

        При включённой опции выбирает случайный каомодзи из пула
        состояния (или mood) и показывает его в метке справа от текста;
        tooltip — название состояния. При выключенной опции метка
        скрывается (применяется сразу при следующем обновлении статуса).
        """
        label = getattr(self, "_status_kaomoji", None)
        if label is None:
            return
        if not getattr(config.app, "kaomoji_enabled", True):
            label.hide()
            return
        kaomoji = pick_kaomoji(state, mood, prev=self._last_kaomoji)
        self._last_kaomoji = kaomoji
        if not kaomoji:
            label.setText("")
            label.hide()
            return
        label.setText(kaomoji)
        label.setToolTip(_KAOMOJI_STATE_LABELS.get(state, ""))
        label.show()

    def _update_hosts_stat(self) -> None:
        """Обновить статистику отмеченных хостов в статус-строке.

        Вызывается при изменении чекбоксов в дереве и при добавлении/удалении
        устройств из listview. Не меняет индикатор — только текст.
        """
        try:
            n = len(self.listview.get_all_devices())
        except Exception:
            n = 0
        suffix = "Готово" if n == 0 else f"Отмечено: {n} {_plural_hosts(n)}"
        self._status_label.setText(suffix)

    def setup_models(self):
        """Настройка моделей данных"""

    def setup_connections(self):
        """Настройка сигналов и слотов"""

        if self.treeview is not None:
            self.treeview.clicked.connect(self.on_treeview_clicked)
            self.treeview.itemDoubleClicked.connect(self.on_treeview_double_clicked)
            self.treeview.fileDropped.connect(self.load_hosts_from_txt_file)
            # ARCHITECTURE: Подключаем сигнал изменений данных для обработки через сервис
            self.treeview.data_changed.connect(self._on_tree_data_changed)
            # Подключаем сигнал удаления устройства для синхронизации со списком
            self.treeview.device_removed.connect(self.remove_from_listview)
            # Подключаем сигнал обновления устройства (переименование/редактирование) для синхронизации со списком
            self.treeview.device_updated.connect(self.update_in_listview)
            # Ноды (федерация)
            self.treeview.node_refresh_requested.connect(self._node_refresh_tree)
            self.treeview.node_hide_requested.connect(self._node_hide_section)
            self.treeview.node_remove_requested.connect(self._node_remove_node)
            self.treeview.node_device_edit_requested.connect(self._node_device_edit)
            self.treeview.node_device_delete_requested.connect(self._node_device_delete)
            self.treeview.node_device_add_requested.connect(self._node_device_add)

        if hasattr(self, "tree_toolbar"):
            self.tree_toolbar.add_folder_requested.connect(
                self.treeview.add_folder_via_toolbar
            )
            self.tree_toolbar.add_host_requested.connect(
                self.treeview.add_host_via_toolbar
            )
            self.tree_toolbar.add_range_requested.connect(
                self.treeview.add_range_via_toolbar
            )
            self.tree_toolbar.sort_requested.connect(self.treeview.sort_via_toolbar)
            self.tree_toolbar.scanner_requested.connect(self.show_scanner_dialog)
            self.tree_toolbar.expand_all_requested.connect(
                self.treeview.expand_all_safe
            )
            self.tree_toolbar.collapse_all_requested.connect(
                self.treeview.collapse_all_safe
            )
            self.tree_toolbar.show_nodes_changed.connect(self._on_show_nodes_toggled)

        if self.treeview._model is not None:
            self.treeview._model.itemChanged.connect(self.on_tree_item_changed)
            self.treeview._model.itemDataChanged.connect(self.on_tree_ItemDataChanged)

        if self.listview is not None:
            self.listview.clicked.connect(self.on_list_item_clicked)
            self.listview.itemDoubleClicked.connect(self.on_list_item_double_clicked)
            self.listview.deviceInfoRequested.connect(self._open_device_info)
            self.listview.commandsExecuted.connect(self.on_single_host_command_executed)
            self.listview.customCommandRequested.connect(
                self.on_single_host_custom_command_requested
            )
            self.listview.remoteRecordingRequested.connect(
                self.show_remote_recording_dialog
            )
            self.listview.polkitEditorRequested.connect(self.show_polkit_editor_dialog)
            self.listview.veyonAccessRequested.connect(self.show_veyon_access_dialog)
            self.listview.veyonFeatureRequested.connect(self.show_veyon_feature)
            # Групповое ПКМ по выделенным хостам: команды и «Управление».
            self.listview.commandsExecutedForDevices.connect(
                self.on_selected_hosts_command_executed
            )
            self.listview.customCommandRequestedForDevices.connect(
                self.show_custom_command_dialog
            )
            self.listview.veyonFeatureRequestedForDevices.connect(
                self._run_feature_on_devices
            )

        if hasattr(self, "list_toolbar"):
            # ViewToolbar — фильтр онлайн, миниатюры, размер.
            self.list_toolbar.online_only_changed.connect(self._on_toolbar_online_only)
            self.list_toolbar.icon_size_changed.connect(self._on_toolbar_icon_size)
            self.list_toolbar.thumbnail_mode_changed.connect(
                self._on_toolbar_thumbnail_mode
            )
            self.list_toolbar.thumbnail_style_changed.connect(
                self._on_toolbar_thumbnail_style
            )
            self.list_toolbar.tooltip_fields_changed.connect(
                self._on_toolbar_tooltip_fields
            )

            # VeyonToolbar / ControlToolbar — фичи
            # (WebAPI/RFB и SSH/DBus/UDP; krfb-фичи - из подменю ПКМ).
            self.veyon_toolbar.veyonFeatureRequested.connect(
                self.on_toolbar_veyon_feature
            )
            self.control_toolbar.veyonFeatureRequested.connect(
                self.on_toolbar_veyon_feature
            )

            # Восстанавливаем сохранённое состояние панели «Вид»
            online_only = self.view_state.get("device_list_view", "online_only", False)
            icon_size = self.view_state.get("device_list_view", "icon_size", 64)
            thumbnail_mode = self.view_state.get(
                "device_list_view", "thumbnail_mode", False
            )

            self.list_toolbar.set_online_only(online_only)
            self.list_toolbar.set_icon_size(icon_size)
            self.list_toolbar.set_thumbnail_mode(thumbnail_mode)
            self.listview.set_online_only(online_only)
            self.listview.set_icon_size(icon_size)
            self.listview.set_thumbnail_mode(thumbnail_mode)

            # Восстанавливаем выбранные поля тултипа хоста (persist).
            tooltip_fields = self.view_state.get(
                "device_list_view", "tooltip_info_fields", None
            )
            if not tooltip_fields:
                from src.services.hosts.host_info_service import DEFAULT_FIELDS

                tooltip_fields = list(DEFAULT_FIELDS)
            else:
                # Миграция версий: раньше в меню были только живые поля, а
                # статика (Имя/Хост/Порт/Нода) показывалась всегда. Если в
                # сохранённом списке нет ни одного статического ключа — это
                # старая настройка: добавить статику (маркер сохраняется,
                # чтобы не перезаписывать осознанный выбор после миграции).
                saved_static = {
                    k for k in tooltip_fields if k in STATIC_TOOLTIP_FIELDS_KEYS
                }
                if not saved_static and not self.view_state.get(
                    "device_list_view", "tooltip_static_migrated", False
                ):
                    tooltip_fields = [k for k, _l in STATIC_TOOLTIP_FIELDS] + list(
                        tooltip_fields
                    )
                    self.view_state.set(
                        "device_list_view", "tooltip_static_migrated", True
                    )
            self.list_toolbar.set_tooltip_fields(tooltip_fields)
            get_host_info_service().set_enabled_fields(tooltip_fields)

            # Восстанавливаем стиль миниатюр (в рамке / одна картинка)
            style_plain = config.app.thumbnails.style == "plain"
            self.list_toolbar.set_thumbnail_style(style_plain)

            # Восстанавливаем видимость трёх тулбаров.
            # Миграция: если новые ключи отсутствуют, унаследовать старый
            # `toolbar_visible` для всех трёх (обратная совместимость).
            new_keys_exist = any(
                self.view_state.get("device_list_view", k, None) is not None
                for k in (
                    "view_toolbar_visible",
                    "veyon_toolbar_visible",
                    "control_toolbar_visible",
                )
            )
            if not new_keys_exist:
                old = self.view_state.get("device_list_view", "toolbar_visible", True)
                view_tb = veyon_tb = control_tb = old
            else:
                view_tb = self.view_state.get(
                    "device_list_view", "view_toolbar_visible", True
                )
                veyon_tb = self.view_state.get(
                    "device_list_view", "veyon_toolbar_visible", True
                )
                control_tb = self.view_state.get(
                    "device_list_view", "control_toolbar_visible", True
                )

            self.view_toolbar_action.setChecked(view_tb)
            self.list_toolbar.setVisible(view_tb)

            self.veyon_toolbar_action.setChecked(veyon_tb)
            # Фактическая видимость Veyon-тулбара = галка AND Veyon-доступен;
            # разделители вокруг тулбара обновляются синхронно.
            # _refresh_veyon_ui_visibility ниже (вызывается в конце метода)
            # вызовет _apply_veyon_toolbar_visibility и пересчитает контейнер.
            self._apply_veyon_toolbar_visibility()

            self.control_toolbar_action.setChecked(control_tb)
            self.control_toolbar.setVisible(control_tb)

            # Восстанавливаем видимость таблицы результатов
            result_table_visible = self.view_state.get(
                "device_list_view", "result_table_visible", True
            )
            self.result_table_visible_action.setChecked(result_table_visible)
            self.result_table_frame.setVisible(result_table_visible)

        # Пользовательские панели: построить после restore галок.
        try:
            self._rebuild_custom_toolbars()
        except Exception as e:
            logger.debug(f"MainWindow: rebuild custom toolbars: {e}")

        self.execute_button.clicked.connect(self.on_execute_clicked)
        self.custom_command_action.triggered.connect(self.on_custom_command_requested)
        self.command_info_button.clicked.connect(self.on_command_info_clicked)
        self.command_abort_button.clicked.connect(self.on_command_abort_clicked)

        # Первичная настройка видимости Veyon-элементов UI и запуск таймера
        # периодической проверки статуса Veyon-сервиса (60 сек).
        self._refresh_veyon_ui_visibility()
        self._start_veyon_status_timer()

    def closeEvent(self, event):
        """Handle main window close event"""
        if getattr(self, "_shutdown_ready", False):
            event.accept()
            QApplication.quit()
            return
        if getattr(self, "_shutdown_pending", False):
            event.ignore()
            return
        # Если трей активен и пользователь не запросил выход — сворачиваем в трей
        if (
            self._tray_controller is not None
            and self._tray_controller.is_active()
            and not self._force_quit
        ):
            event.ignore()
            self.hide()
            self._tray_controller.show_message(
                "DNO Tool",
                "Приложение свернуто в трей",
                QSystemTrayIcon.MessageIcon.Information,
                2000,
            )
            return

        if self._defer_video_editors_exit():
            event.ignore()
            return

        # Сбросить отложенный сейв дерева (debounce on_tree_item_changed),
        # иначе последнее изменение перед быстрым закрытием потеряется.
        try:
            timer = getattr(self, "_tree_save_timer", None)
            if timer is not None and timer.isActive():
                timer.stop()
                self._flush_tree_data_changed()
        except Exception:
            pass

        self._shutdown_pending = True
        event.ignore()
        self._save_view_state()
        for child in self.children():
            if isinstance(child, QTimer):
                child.stop()
        self._request_dialogs_shutdown()
        from src.services.hosts.device_info import get_device_info_service

        get_device_info_service().shutdown()
        self._cleanup_architecture()
        try:
            from src.services import NodeService

            ns = self.container.resolve_optional(NodeService)
            if ns is not None:
                ns.shutdown()
        except Exception as e:
            logger.warning(f"MainWindow: ошибка остановки NodeService: {e}")

        from src.workers.network import get_host_ping_timer_manager

        get_host_ping_timer_manager().stop_all()

        # Останавливаем per-host таймеры нод (статусы)
        try:
            from src.workers.node.status import get_node_status_manager

            get_node_status_manager().stop_all()
        except Exception as e:
            logger.debug(f"MainWindow: node status stop_all: {e}")

        # Stop GUI timers before releasing backend resources off the GUI thread.
        if hasattr(self, "listview"):
            self.listview.set_thumbnail_mode(False)

        self.listview.clear_devices()
        self.treeview.clear_devices()
        self.worker_bridge.request_stop_all()
        from src.ui.dialogs.tools.vnc_viewer import close_all_vnc_viewers

        close_all_vnc_viewers()
        from src.workers.video_editor.thumbnail_worker import shutdown_media_pools

        shutdown_media_pools(wait_ms=0)

        if (
            not getattr(self, "current_worker_id", None)
            and hasattr(self, "worker")
            and self.command_executed
            and self.worker is not None
        ):
            logger.debug("MainWindow: Aborting worker directly on close")
            try:
                self.worker.abort()
            except RuntimeError:
                logger.debug("MainWindow: worker has already been deleted")

        if self._tray_controller is not None:
            self._tray_controller.hide()

        # Останавливаем все активные сеансы записи камер ЕЦХД,
        # чтобы не осталось осиротевших процессов после выхода.
        camera_service = self._resolve_camera_service()
        if camera_service is not None:
            camera_service.request_shutdown_recordings()
        cameras_dlg = getattr(self, "_cameras_dialog", None)
        if cameras_dlg is not None:
            try:
                cameras_dlg.request_shutdown_recordings()
            except Exception as e:
                logger.warning(f"MainWindow: ошибка остановки записей камер: {e}")

        self._set_status("Завершение фоновых операций...", "busy")
        self.setEnabled(False)
        from src.architecture.shutdown import ApplicationShutdown

        from src.config import LOGS_PATH

        self._shutdown_coordinator = ApplicationShutdown(
            self._finish_background_shutdown,
            lambda: not self._pending_shutdown_owners(),
            self._complete_shutdown,
            parent=self,
            describe_pending=self._pending_shutdown_owners,
            recordings_pending=self._shutdown_recordings_pending,
            on_soft_deadline=self._show_shutdown_dialog,
            on_progress=self._on_shutdown_progress,
            stacks_dir=LOGS_PATH,
        )
        self._shutdown_coordinator.start()

    def _defer_video_editors_exit(self) -> bool:
        """Получить решение о монтаже до необратимой остановки сервисов."""
        if getattr(self, "_video_exit_pending", False):
            return True
        for editor in self.findChildren(QDialog):
            if not hasattr(editor, "close_cancelled") or getattr(
                editor, "_close_complete", False
            ):
                continue
            self._video_exit_pending = True
            if getattr(editor, "_project_io_thread", None) is not None:

                def retry_exit() -> None:
                    self._video_exit_pending = False
                    self.close()

                QTimer.singleShot(100, retry_exit)
                return True

            def resume_exit(*_args, dialog=editor) -> None:
                release_exit(dialog)
                QTimer.singleShot(0, self.close)

            def cancel_exit(*_args, dialog=editor) -> None:
                release_exit(dialog)
                self._force_quit = False

            def release_exit(dialog) -> None:
                self._video_exit_pending = False
                try:
                    dialog.finished.disconnect(resume_exit)
                    dialog.close_cancelled.disconnect(cancel_exit)
                except RuntimeError:
                    pass

            editor.finished.connect(resume_exit)
            editor.close_cancelled.connect(cancel_exit)
            editor.done(QDialog.DialogCode.Rejected)
            return True
        return False

    def _request_dialogs_shutdown(self) -> None:
        """Request cancellation on the GUI thread before stopping shared pools."""
        # Include dialogs created directly, outside DialogManager. Closing a
        # dialog can change the QObject tree, so iterate over a snapshot.
        dialogs = list(self.findChildren(QDialog))
        for dialog in dialogs:
            try:
                dialog.done(QDialog.DialogCode.Rejected)
            except RuntimeError:
                # A previous dialog may already have deleted this child.
                pass

    def _finish_background_shutdown(self) -> None:
        """Run network closure and executor waits outside the Qt GUI thread."""
        from src.architecture.shutdown import ShutdownPlan, run_shutdown_plan
        from src.ui.dialogs.tools.ip_scanner import shutdown_scanner_pools
        from src.utils.network.reachability import shutdown_reachability_prober
        from src.web import get_web_server
        from src.web.node_server import get_node_server
        from src.workers.application_pools import shutdown_application_pools
        from src.workers.network import shutdown_gui_worker_pools
        from src.workers.qthread_registry import request_tracked_qthreads_stop
        from src.workers.tracked_pool import TrackedThreadPoolExecutor

        def cancel_tracked_pools() -> None:
            # Scanner and topology own additional pools outside the shared
            # role budgets. Stop admission everywhere before waiting anywhere.
            for pool in TrackedThreadPoolExecutor.get_all_tracked_pools():
                pool.shutdown(wait=False, cancel_futures=True)

        def wait_tracked_pools() -> None:
            for pool in TrackedThreadPoolExecutor.get_all_tracked_pools():
                pool.shutdown(wait=True)

        run_shutdown_plan(
            ShutdownPlan(
                stop_admission=(
                    shutdown_gui_worker_pools,
                    lambda: shutdown_application_pools(wait=False),
                    shutdown_scanner_pools,
                    cancel_tracked_pools,
                    request_tracked_qthreads_stop,
                ),
                # Закрытие прерывает выполняющиеся операции, поэтому идёт до ожидания.
                close_resources=(
                    get_web_server().stop,
                    get_node_server().stop,
                    self.thumbnail_service.shutdown,
                    lambda: self.veyon_service.stop_all(_SHUTDOWN_CLOSE_BUDGET_SEC),
                    shutdown_reachability_prober,
                ),
                wait_for_workers=(
                    lambda: shutdown_application_pools(wait=True),
                    wait_tracked_pools,
                ),
            )
        )

    def _pending_shutdown_owners(self) -> list[str]:
        """Preserve every Qt owner until its actual operating-system thread ends."""
        pending = []
        if self.worker_bridge.has_pending_workers():
            pending.append("Команды")
        from src.services.hosts.device_info import get_device_info_service

        if get_device_info_service().has_pending_workers():
            pending.append("Сведения об устройстве")
        if self.node_service.has_pending_clients():
            pending.append("Клиенты нод")
        from src.web import get_web_server
        from src.web.node_server import get_node_server

        if get_web_server().has_pending_workers():
            pending.append("Веб-сервер")
        if get_node_server().has_pending_workers():
            pending.append("Сервер нод")
        if self._shutdown_recordings_pending():
            pending.append("Запись камер")
        from src.workers.qthread_registry import pending_qthread_labels

        pending.extend(pending_qthread_labels())
        for thread in self.findChildren(QThread):
            if thread.isRunning():
                pending.append(type(thread).__name__)
        for dialog in self.findChildren(QDialog):
            has_pending = getattr(dialog, "has_pending_workers", None)
            if callable(has_pending) and has_pending():
                pending.append(f"Окно «{dialog.windowTitle()}»")
        from src.workers.video_editor.thumbnail_worker import shutdown_media_pools

        if not shutdown_media_pools(wait_ms=0):
            pending.append("Видеоредактор")
        return pending

    def _shutdown_recordings_pending(self) -> bool:
        camera_service = self._resolve_camera_service()
        if camera_service is not None and camera_service.has_pending_recordings():
            return True
        cameras = getattr(self, "_cameras_dialog", None)
        return cameras is not None and cameras.has_pending_workers()

    def _on_shutdown_progress(self, count: int) -> None:
        self._set_status(f"Завершение: осталось операций {count}", "busy")

    def _show_shutdown_dialog(self, pending: list[str]) -> None:
        """Спросить, ждать ли дальше; окно верхнего уровня, главное уже отключено."""
        if self._shutdown_ready or not pending:
            return
        lines = "\n".join(f"• {name}" for name in pending)
        text = f"Не завершились фоновые операции:\n{lines}"
        if self._shutdown_recordings_pending():
            text += (
                "\n\nИдёт завершение записи камер. "
                "Принудительный выход может повредить файл."
            )
        box = QMessageBox(QMessageBox.Icon.Warning, "Завершение DNO Tool", text)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        wait_button = box.addButton("Подождать", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Завершить принудительно", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(wait_button)
        box.buttonClicked.connect(
            lambda button: self._on_shutdown_dialog_answer(button is wait_button)
        )
        self._shutdown_dialog = box
        box.show()

    def _on_shutdown_dialog_answer(self, wait: bool) -> None:
        self._shutdown_dialog = None
        coordinator = self._shutdown_coordinator
        if coordinator is None:
            return
        if not wait:
            coordinator.force_exit_now()
            return
        QTimer.singleShot(
            _SHUTDOWN_DIALOG_REPEAT_MS,
            lambda: self._show_shutdown_dialog(coordinator.pending()),
        )

    def _complete_shutdown(self) -> None:
        self._shutdown_ready = True
        if self._shutdown_dialog is not None:
            self._shutdown_dialog.close()
            self._shutdown_dialog = None
        self.close()

    def _cleanup_architecture(self):
        """ARCHITECTURE: Очистка архитектурных компонентов"""
        # Прерываем все активные workers
        if hasattr(self, "worker_bridge"):
            aborted = self.worker_bridge.abort_all()
            if aborted > 0:
                logger.debug(f"MainWindow: Aborted {aborted} workers")

        # Закрываем все открытые диалоги
        if hasattr(self, "dialog_manager"):
            closed = self.dialog_manager.close_all()
            if closed > 0:
                logger.debug(f"MainWindow: Closed {closed} dialogs")

        logger.debug("MainWindow: Architecture components cleaned up")

    def on_execute_clicked(self):
        """Handle execute button click"""

        devices = self.listview.get_all_devices()
        selected_data = self.command_combo_box.get_selected_task_data()

        if not selected_data:
            return

        self.execute_command(devices, selected_data)

    def on_custom_command_requested(self, *_):
        """Стрелка «Выполнить» -> «Своя команда…»: отмеченные хосты списка."""
        self.show_custom_command_dialog(self.listview.get_all_devices())

    def on_single_host_custom_command_requested(self, device: DeviceModel):
        """ПКМ по хосту -> «Своя команда…»: только этот хост."""
        self.show_custom_command_dialog([device])

    def _command_running(self) -> bool:
        """Идёт ли задание на локальных хостах или на нодах."""
        return (
            getattr(self, "worker", None) is not None
            or getattr(self, "node_worker", None) is not None
        )

    def show_custom_command_dialog(self, devices: list[DeviceModel]):
        """Окно разового скрипта; запуск идёт обычным конвейером задач."""
        if self._command_running():
            QMessageBox.information(
                self,
                "Своя команда",
                "Задача уже выполняется. Дождитесь завершения или отмените её.",
            )
            return
        from src.ui.dialogs.command.custom_command import (
            CustomCommandDialog,
            CustomCommandStore,
        )

        # Хранилище живёт с окном: последний ввод возвращается при новом открытии.
        store = getattr(self, "_custom_command_store", None)
        if store is None:
            store = self._custom_command_store = CustomCommandStore(self.view_state)
        dialog = CustomCommandDialog(
            devices,
            store,
            default_timeout=config.app.ssh.command_timeout,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        # Повторная проверка: состояние могло измениться, пока окно было открыто.
        if self._command_running():
            QMessageBox.information(
                self,
                "Своя команда",
                "Задача уже выполняется. Текст команды сохранён в окне, "
                "запустите её после завершения.",
            )
            return
        self.execute_command(devices, dialog.command().build_task())

    def on_command_info_clicked(self):
        """Handle command info button click"""

        selected_data = self.command_combo_box.get_selected_task_data()

        if not selected_data:
            return

        parameters = selected_data.get("params", [])

        if not selected_data:
            QMessageBox.warning(
                self,
                "Выбор команды",
                "Команда не выбрана.\n\nПожалуйста, выберите команду из списка.",
            )
            return

        name = selected_data.get("name", "N/A")
        description = selected_data.get("description", "Нет описания.")

        parameters = ", ".join(str(param) for param in parameters)

        commands_data = selected_data.get("commands", [])
        commands = [Command(cmd) for cmd in commands_data]

        if not commands:
            commands = []
        timeout = selected_data.get("timeout", "N/A")

        desktop_default = desktop_session_enabled(selected_data)
        steps = []
        for index, cmd in enumerate(commands, 1):
            details = f"# Шаг {index}: {cmd.commandType.upper()}\n"
            if cmd.commandType == "ssh":
                desktop = cmd.uses_desktop_session(desktop_default)
                details += (
                    "# Запуск в среде пользователя: "
                    + ("Да" if desktop else "Нет")
                    + "\n"
                )
                details += "# Фоновый запуск: " + ("Да" if cmd.background else "Нет") + "\n"
            steps.append(details + cmd.text)
        command_text = "\n\n".join(steps)
        session = (
            "Одна SSH-сессия на задачу"
            if selected_data.get("session", False)
            else "Отдельная SSH-сессия на каждый шаг"
        )
        message_text = (
            f"# Имя команды: {name}\n"
            f"# Описание: {description}\n"
            f"# Параметры: {parameters}\n"
            f"# Таймаут: {timeout} сек.\n"
            f"# Сессия: {session}\n\n"
            f"{command_text}"
        )
        dialog = BashViewerDialog(self, initial_text=message_text)
        dialog.setWindowTitle("Информация о выбранной команде")
        dialog.editor.setReadOnly(True)
        dialog.exec_()

    def on_command_abort_clicked(self):
        """Handle command abort button click"""
        logger.debug("MainWindow: Abort button clicked")

        # Без воркеров сигнал завершения не придёт: снимаем зависшее состояние.
        if not any(
            getattr(self, name, None) is not None
            for name in (
                "current_worker_id", "worker",
                "current_node_worker_id", "node_worker",
            )
        ):
            if self.command_executed:
                self.on_execute_thread_finished()
                self._set_status("Выполнение отменено", "idle", mood="bulk")
            return

        # Mark all pending results as cancelled before aborting worker
        if hasattr(self, "result_table") and self.result_table:
            self.result_table.mark_pending_as_cancelled()

        # Используем WorkerBridge для корректного прерывания worker
        if hasattr(self, "current_worker_id") and self.current_worker_id:
            logger.debug(
                f"MainWindow: Aborting worker via WorkerBridge: {self.current_worker_id}"
            )
            aborted = self.worker_bridge.abort_worker(self.current_worker_id)
            if aborted:
                logger.info(
                    f"MainWindow: Worker {self.current_worker_id} aborted successfully"
                )
            else:
                logger.warning(
                    f"MainWindow: Failed to abort worker {self.current_worker_id}"
                )
        elif hasattr(self, "worker") and self.worker:
            # Fallback: прерываем напрямую если worker_bridge не использовался
            logger.debug("MainWindow: Aborting worker directly (fallback)")
            self.worker.abort()
        else:
            logger.warning("MainWindow: No worker to abort")

        # Прерывание node-worker (ноды выполняют независимо от локальных)
        if hasattr(self, "current_node_worker_id") and self.current_node_worker_id:
            try:
                self.worker_bridge.abort_worker(self.current_node_worker_id)
            except Exception as e:
                logger.debug(f"MainWindow: abort node worker: {e}")
        elif hasattr(self, "node_worker") and self.node_worker:
            try:
                self.node_worker.abort()
            except Exception:
                pass

    def load_initial_data(self):
        """Загрузка начальных данных"""
        # Режим «ноды первым уровнем» (тулбар дерева) — до load_tree_data:
        # от него зависит, попадут ли локальные хосты в секцию «Эта машина».
        show_nodes = self.view_state.get("main_window", "tree_show_nodes", True)
        self.treeview.show_nodes = bool(show_nodes)
        if hasattr(self, "tree_toolbar"):
            self.tree_toolbar.set_show_nodes(bool(show_nodes))

        # ARCHITECTURE: Загружаем оригинальные данные с группами через сервис
        raw_data = self.device_service.get_devices_raw()
        self.treeview.load_tree_data(raw_data)
        self.treeview.setCurrentIndex(self.treeview._model.invisibleRootItem().index())

        # Восстанавливаем состояние раскрытых узлов (если не задано expandAll)
        if config.app.expand:
            self.treeview.expand_all_safe()
        else:
            self._restore_tree_expanded_state()

        # ARCHITECTURE: Подключаемся к нодам (федерация) после загрузки дерева
        self._nodes_init()

    # ========== Ноды (федерация инстансов) ==========

    def _nodes_init(self) -> None:
        """Подключиться ко всем включённым нодам и создать их секции в дереве."""
        if hasattr(self, "_node_tree_controller"):
            self._node_tree_controller.init_nodes()

    def _node_start_client(self, rec) -> None:
        """Запустить NodeClientWorker для ноды и подключить сигналы."""
        if hasattr(self, "_node_tree_controller"):
            self._node_tree_controller.start_client(rec)

    def _on_node_hosts_updated(self, node_iid: str, tree: list) -> None:
        """Дерево хостов ноды обновилось — перестроить секцию в дереве."""
        if self.sender() is not self.node_service.get_client(node_iid):
            return
        try:
            if config.app.nodes.show_in_tree:
                name = self.node_service.get_display_name(node_iid)
                self.treeview.get_or_create_node_section(node_iid, name)
                count = self.treeview.update_node_section_hosts(node_iid, tree)
                # Восстановленные чекбоксы — обновить снапшот отмеченных
                self._sync_node_checked_iids(node_iid)
                logger.debug(f"MainWindow: node {node_iid} hosts updated ({count})")
        except Exception as e:
            logger.debug(f"MainWindow: _on_node_hosts_updated {node_iid}: {e}")

    def _on_node_online(self, node_iid: str, reachable: bool) -> None:
        """Обновить связь с нодой и сразу инвалидировать её миниатюры."""
        if self.sender() is not self.node_service.get_client(node_iid):
            return
        try:
            from src.workers.node.thumbnails import get_node_thumbnail_manager

            get_node_thumbnail_manager().on_node_online(node_iid, reachable)
        except (ImportError, RuntimeError, ValueError, KeyError, OSError) as e:
            logger.debug(f"MainWindow: node thumbnail connectivity {node_iid}: {e}")
        try:
            self.treeview.set_node_reachable(node_iid, reachable)
        except Exception as e:
            logger.debug(f"MainWindow: _on_node_online {node_iid}: {e}")
        if not reachable:
            self._set_status(
                f"⚠ Нода недоступна: {self.node_service.get_display_name(node_iid)}",
                "error",
            )

    def _on_node_name_updated(self, node_iid: str, name: str) -> None:
        """Имя из hello — если пользователь не задал локальное, обновим секцию."""
        if self.sender() is not self.node_service.get_client(node_iid):
            return
        try:
            rec = self.node_service.get_node_by_iid(node_iid)
            if rec is not None and not rec.name and name:
                item = self.treeview.get_node_section_item(node_iid)
                if item is not None:
                    item.setText(name)
        except Exception:
            pass

    def _node_refresh_tree(self, node_iid: str) -> None:
        """Запрос на немедленное обновление дерева ноды."""
        client = self.node_service.get_client(node_iid)
        if client is not None:
            client.refresh_now()

    def _node_hide_section(self, node_iid: str) -> None:
        """Скрыть секцию ноды: снять «Отображение» в конфиге (persist) и убрать из дерева.

        Скрытие — локальное: autoconnect=False убирает секцию и останавливает
        клиент (NODE_CONFIG_CHANGED → _nodes_reload_sections), запись и ключ
        доступа сохраняются. Удаление секции здесь — на случай, если событие
        не пришло.
        """
        try:
            self.node_service.update_node(node_iid, autoconnect=False)
            self.treeview.remove_node_section(node_iid)
        except Exception as e:
            logger.debug(f"MainWindow: _node_hide_section {node_iid}: {e}")

    def _node_remove_node(self, node_iid: str) -> None:
        """Удалить ноду из настроек (с подтверждением)."""
        name = self.node_service.get_display_name(node_iid)
        if (
            QMessageBox.question(
                self,
                "Удаление ноды",
                f"Удалить ноду «{name}» из настроек?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.No
        ):
            return
        self.node_service.delete_node(node_iid)
        self.treeview.remove_node_section(node_iid)

    # ========== /Ноды ==========
    def save_tree(self, file_path: str = None):
        """Сохранение дерева в JSON файл с сохранением иерархии"""
        # Если путь не указан (вызов из меню "Экспортировать список"), показать диалог сохранения
        if not file_path:
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Экспортировать список хостов", "", "JSON Files (*.json)"
            )
            # Если пользователь отменил диалог
            if not file_path:
                return

        # Получаем актуальные данные из дерева
        new_data = self.treeview.get_tree_data()

        # Сохраняем данные в datastore (обновляем _hosts_data_raw)
        success = self.device_service.save_devices(new_data)

        if not success:
            logger.error("MainWindow: Failed to save devices to datastore")
            QMessageBox.critical(self, "Ошибка", "Не удалось сохранить данные")
            return

        # Сохраняем в указанный файл
        success = datastore.save_hosts_to_file(file_path)

        if success:
            logger.info(f"MainWindow: Tree saved to {file_path}")
            self._set_status(f"✅ Дерево сохранено в {file_path}", "ok")
        else:
            logger.error(f"MainWindow: Failed to save tree to {file_path}")
            self._set_status("❌ Не удалось сохранить файл", "error")
            QMessageBox.critical(self, "Ошибка", "Не удалось сохранить файл")

    def on_menu_load_tree(self):
        """Загрузка дерева из JSON файла"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Загрузить файл", "", "JSON Files (*.json)"
        )
        if file_path:
            # ARCHITECTURE: Используем сервис для загрузки
            success, message = self.device_service.import_devices_from_file(file_path)

            if success:
                # Получаем данные из сервиса
                data = self.device_service.get_devices_raw()
                self.treeview.load_tree_data(data)
                self.treeview.setCurrentIndex(
                    self.treeview._model.invisibleRootItem().index()
                )

                # Очищаем список отмеченных — старые DeviceModel больше не валидны
                # после перестроения дерева (tree items пересозданы с Unchecked).
                try:
                    self.listview.clear_devices()
                except Exception as e:
                    logger.error(
                        f"MainWindow: cannot clear listview after tree load: {e}"
                    )

                # ARCHITECTURE: Публикуем событие о загрузке данных
                self.event_bus.publish_typed(
                    EventType.DATA_LOADED,
                    source="MainWindow",
                    data={
                        "type": "hosts",
                        "file": file_path,
                        "count": self.device_service.get_devices_count(),
                    },
                )

                logger.info(f"MainWindow: {message}")
            else:
                logger.error(f"MainWindow: {message}")
                QMessageBox.critical(self, "Ошибка импорта", message)

    def on_tree_ItemDataChanged(self, item: CustomTreeItem, role: Qt.ItemDataRole):
        """
        Обработка изменения состояния чекбокса элемента
        Добавляет/удаляет устройства в список справа
        """
        if role == Qt.ItemDataRole.CheckStateRole:
            if not item.hasChildren():
                if item.checkState() == Qt.CheckState.Checked:
                    self.add_to_listview(item)
                elif item.checkState() == Qt.CheckState.Unchecked:
                    self.remove_from_listview(item.device)
            # Ноды: дифф-синк per-host статус-таймеров (покрывает одиночные
            # клики, «отметить все», каскад папок, пересборку секции).
            node_iid = getattr(item, "node_iid", None)
            if node_iid:
                self._sync_node_checked_iids(node_iid)

    def _sync_node_checked_iids(self, node_iid: str) -> None:
        """Синхронизировать статус-таймеры хостов ноды с чекбоксами дерева.

        Раньше здесь строился снапшот iid для батч-poll; теперь — дифф-синк
        NodeStatusManager (зеркало локального _sync_checked_devices_to_ping_manager,
        но через diff: нужным ref=1, лишние останавливаются — refs
        нормализуются после массовых операций и пересборок секции).
        Локальная нода (LOCAL_NODE_IID) не синкается — её хосты пингуются
        локально (HostPingTimer).
        """
        from src.ui.widgets.device_tree import (
            LOCAL_NODE_IID,
            DEVICE_NODE_DATAROLE,
        )

        if not node_iid or node_iid == LOCAL_NODE_IID:
            return
        try:
            section = self.treeview.get_node_section_item(node_iid)
            devices = {}
            if section is None:
                # Скрытая секция лежит в стэше дерева — берём оттуда
                for s in getattr(self.treeview, "_hidden_node_sections", []):
                    if s.data(DEVICE_NODE_DATAROLE) == node_iid:
                        section = s
                        break

            if section is not None:

                def _walk(it):
                    for i in range(it.rowCount()):
                        ch = it.child(i)
                        if (
                            hasattr(ch, "device")
                            and ch.device is not None
                            and ch.checkState() == Qt.CheckState.Checked
                        ):
                            devices[ch.device.iid] = ch.device
                        if ch.hasChildren():
                            _walk(ch)

                _walk(section)

            from src.workers.node.status import get_node_status_manager

            get_node_status_manager().sync_node(node_iid, devices)
        except Exception as e:
            logger.debug(f"MainWindow: _sync_node_checked_iids {node_iid}: {e}")

    def on_tree_item_changed(self, item: CustomTreeItem):
        """Обработка изменения элемента дерева (текст, иконка)"""
        parent = item.parent()
        if parent is not None:
            from src.ui.widgets.device_tree import NODE_SECTION_DATAROLE

            # Секция ноды — не папка: её иконку (зелёная/красная нода)
            # itemChanged-хук не трогает.
            if not bool(parent.data(NODE_SECTION_DATAROLE)):
                icon = _icon_from_key("folder")
                parent.setIcon(icon)

        # Сейв дебаунсится (_tree_save_timer): каскад чекбоксов и смены
        # иконок генерят десятки itemChanged подряд, а полный export +
        # deepcopy datastore на каждый фризил GUI. Иконка папки выше
        # обновлена синхронно — визуально всё сразу корректно.
        self._tree_save_timer.start()

    def _flush_tree_data_changed(self) -> None:
        """Отложенный сейв дерева после пачки itemChanged (debounce).

        Читает актуальное состояние дерева в момент срабатывания —
        все промежуточные события каскада схлопываются в один export.
        """
        try:
            treeview = getattr(self, "treeview", None)
            if treeview is None:
                return
            self.treeview.data_changed.emit(self.treeview.get_tree_data())
        except RuntimeError:
            pass  # окно уже уничтожено
        except Exception as e:
            logger.debug(f"MainWindow: _flush_tree_data_changed: {e}")

    def add_to_listview(self, tree_item: CustomTreeItem):
        """
        Добавление устройства в список справа
        tree_item - элемент дерева с данными устройства
        """
        from src.ui.widgets.device_tree import LOCAL_NODE_IID

        device = tree_item.device
        # Node-хост: device.node_iid уже проставлен секцией дерева. Если нет
        # (теоретически) — определяем по родительской цепочке (секция ноды).
        nid = getattr(device, "node_iid", None)
        if nid is None:
            node = tree_item.parent()
            while node is not None:
                maybe = getattr(node, "node_iid", None)
                if maybe and getattr(node, "is_node_section", False):
                    nid = maybe
                    break
                node = node.parent()
            if nid:
                device.node_iid = nid
        parent = tree_item.parent()
        # Build full path from root to the parent using '->' as separator.
        # Для хостов нод категория — путь папок внутри ноды (как у локальных
        # хостов путь внутри локального дерева). Удалённые ноды — с префиксом
        # «Нода: имя», локальные (LOCAL_NODE_IID) — путь от корня секции.
        if nid:
            node_prefix = (
                ""
                if nid == LOCAL_NODE_IID
                else f"Нода: {self.node_service.get_display_name(nid)}"
            )
            parts = []
            node = parent
            while node is not None and not getattr(node, "is_node_section", False):
                parts.append(node.text())
                node = node.parent()
            parts.reverse()
            inner_path = " -> ".join(parts) if parts else ""
            if node_prefix and inner_path:
                category = f"{node_prefix} -> {inner_path}"
            elif node_prefix:
                category = node_prefix
            elif inner_path:
                category = inner_path
            else:
                category = "Эта машина"
        elif parent is None:
            category = "Нет категории"
        else:
            parts = []
            node = parent
            while node is not None:
                parts.append(node.text())
                node = node.parent()
            parts.reverse()
            category = " -> ".join(parts) if parts else "Нет категории"

        new_list_item = self.listview.add_device(device, category=category)
        new_list_item.tree_item = tree_item  # Back-reference if needed
        self.listview.viewport().update()
        self._update_hosts_stat()

        # Node-хост (удалённая нода): add_device уже запустил per-host
        # статус-таймер (NodeStatusManager.start_ping) и подписал миниатюры
        # (NodeThumbnailManager.start_thumbnail, онлайн-гейт внутри) —
        # как у локальных хостов. Локальная нода — обычные локальные таймеры.

    def remove_from_listview(self, device: DeviceModel):
        """
        Удаление устройства из списка справа

        Args:
            device: Объект DeviceModel удаляемого устройства
        """
        if device:
            # По iid (первичный), fallback по host
            iid = getattr(device, "iid", None)
            if iid:
                removed = self.listview.remove_device_by_iid(iid, getattr(device, "node_iid", None))
                if removed:
                    self._update_hosts_stat()
                return
            # fallback для данных без iid
            if device.host:
                self.listview.remove_device_by_host(device.host)
                self._update_hosts_stat()

    def update_in_listview(self, device: DeviceModel):
        """
        Обновление устройства в списке справа (при переименовании/редактировании/перемещении в дереве).

        Определяет текущую категорию устройства в дереве (полный путь папок) и передаёт
        её в listview. Если категория изменилась — listview переместит элемент.

        Args:
            device: Объект DeviceModel обновлённого устройства
        """
        if not device:
            return
        new_category = "Нет категории"
        tree_item = None
        iid = getattr(device, "iid", None)
        if iid and hasattr(self, "treeview"):
            from src.ui.widgets.device_tree import LOCAL_NODE_IID

            tree_item = self.treeview._find_tree_item_by_iid(iid, node_iid=getattr(device, "node_iid", None))
            if tree_item is not None:
                # Хост ноды: путь папок внутри секции ноды (удалённые ноды —
                # с префиксом «Нода: имя»; локальная нода — просто путь).
                nid = getattr(device, "node_iid", None) or tree_item.node_iid
                if nid and not getattr(tree_item, "is_node_section", False):
                    node_prefix = (
                        ""
                        if nid == LOCAL_NODE_IID
                        else f"Нода: {self.node_service.get_display_name(nid)}"
                    )
                    parts = []
                    node = tree_item.parent()
                    while node is not None and not getattr(
                        node, "is_node_section", False
                    ):
                        parts.append(node.text())
                        node = node.parent()
                    parts.reverse()
                    inner_path = " -> ".join(parts) if parts else ""
                    if node_prefix and inner_path:
                        new_category = f"{node_prefix} -> {inner_path}"
                    elif node_prefix:
                        new_category = node_prefix
                    elif inner_path:
                        new_category = inner_path
                    else:
                        new_category = "Эта машина"
                else:
                    parent = tree_item.parent()
                    if parent is not None:
                        parts = []
                        node = parent
                        while node is not None:
                            parts.append(node.text())
                            node = node.parent()
                        parts.reverse()
                        new_category = " -> ".join(parts) if parts else "Нет категории"
        self.listview.update_or_move_device_by_iid(device, new_category, tree_item=tree_item)

    def on_treeview_clicked(self, index):
        """Обработка клика по элементу дерева"""
        pass

    def load_hosts_from_txt_file(self, files: list):
        for file in files:
            with open(file, "r", encoding="utf-8") as f:
                for line in f:
                    value = {
                        "name": line.strip(),
                        "host": line.strip(),
                    }
                    device = DeviceModel(value)
                    parent = self.treeview._model.invisibleRootItem()
                    self.treeview.add_device(parent, device)
                    # ARCHITECTURE: Используем сервис для добавления
                    self.device_service.add_device(value)

        self.treeview.setCurrentIndex(self.treeview._model.invisibleRootItem().index())
        self.treeview._model.setHorizontalHeaderLabels(["Списки"])
        self.treeview.sort_tree()
        self.treeview.data_changed.emit(self.treeview.get_tree_data())

    def on_treeview_double_clicked(self, item: CustomTreeItem):
        """
        Обработка двойного клика по элементу дерева
        Открывает диалог редактирования устройства
        """
        if item:
            device = item.device
            if device is not None:
                # Node-хост удалённой ноды: редактирование выполняется на
                # ноде-владельце (данные принадлежат ноде, локально не
                # сохраняются). Локальные хосты при тумблере «Ноды» имеют
                # node_iid == LOCAL_NODE_IID — обычный локальный путь.
                from src.ui.widgets.device_tree import LOCAL_NODE_IID

                _nid = getattr(device, "node_iid", None)
                if _nid and _nid != LOCAL_NODE_IID:
                    self._node_device_edit(_nid, device)
                    return
                self._local_device_edit(device)
                return
            else:
                self.treeview.rename_folder_action(item)

    def _node_device_edit(self, node_iid: str, device: DeviceModel) -> None:
        """Редактирование хоста на ноде (fedерация): диалог + host_crud update.

        После успеха нода сама пришлёт обновлённое дерево хостов
        (hosts_updated) — секция перестроится. Локально DeviceModel
        обновляем немедленно (иконки/список), не сохраняя в hosts.json.
        Локальные хосты (LOCAL_NODE_IID) — обычный локальный диалог
        (клиента ноды у них нет).
        """
        from src.ui.widgets.device_tree import LOCAL_NODE_IID

        if not node_iid or node_iid == LOCAL_NODE_IID:
            self._local_device_edit(device)
            return
        client = self.node_service.get_client(node_iid)
        if client is None:
            QMessageBox.warning(
                self,
                "Нода",
                f"Нода {self.node_service.get_display_name(node_iid)} недоступна.",
            )
            return
        dialog = DeviceEditDialog(device, self)
        supports_backends = self.node_service.supports_thumbnail_backends(node_iid)
        dialog.set_thumbnail_backends_supported(supports_backends)
        if not dialog.exec():
            return
        new_device = dialog.get_updated_device()
        if new_device.thumbnail_backend == "vnc" and not supports_backends:
            QMessageBox.warning(
                self, "Нода", "Для миниатюр VNC обновите программу на ноде."
            )
            return
        # Поля для обновления (кроме iid)
        data = new_device.to_dict()
        data.pop("iid", None)
        if not supports_backends:
            data.pop("thumbnail_backend", None)

        def _run():
            try:
                return client.host_crud("update", iid=device.iid, device=data)
            except Exception as e:
                return False, str(e), None

        self._set_status(f"⏳ Обновление хоста на ноде…", "busy")
        pool = get_command_worker_pool()

        def _on_complete(task_id, result):
            self._node_edit_result.emit(client, device, data, result)

        def _on_error(task_id, error):
            self._set_status(f"❌ Обновление хоста: {error}", "error")

        pool.submit(
            _run,
            on_complete=_on_complete,
            on_error=_on_error,
            metadata={
                "host": device.host,
                "type": "node_host_crud",
                "device": device.name or device.host,
            },
        )

    def _apply_node_device_edit_result(
        self, client: object, device: DeviceModel, data: dict, result: object
    ) -> None:
        """Применить редактирование в GUI только для актуального клиента."""
        if self._shutdown_pending:
            return
        node_iid = getattr(device, "node_iid", None)
        if self.node_service.get_client(node_iid) is not client:
            return
        try:
            ok, msg, _dev = result
        except (TypeError, ValueError):
            ok, msg = False, str(result)
        if not ok:
            self._set_status(f"❌ Обновление хоста: {msg or 'ошибка'}", "error")
            return
        self._set_status(f"✅ Хост обновлён на ноде: {msg}", "ok")
        device.update(data)
        section = self.treeview.get_node_section_item(node_iid)
        if section is None:
            return
        item = self.treeview._find_tree_item_by_iid(device.iid, parent=section)
        if item is not None:
            item.setText(device.name)
            item.update()
            self.treeview.device_updated.emit(device)

    def _node_device_delete(self, node_iid: str, device: DeviceModel) -> None:
        """Удаление хоста на ноде (fedерация): подтверждение + host_crud delete.

        Локальные хосты (LOCAL_NODE_IID) — обычное локальное удаление.
        """
        from src.ui.widgets.device_tree import LOCAL_NODE_IID

        if not node_iid or node_iid == LOCAL_NODE_IID:
            self._local_device_delete(device)
            return
        client = self.node_service.get_client(node_iid)
        if client is None:
            QMessageBox.warning(
                self,
                "Нода",
                f"Нода {self.node_service.get_display_name(node_iid)} недоступна.",
            )
            return
        if (
            QMessageBox.question(
                self,
                "Удаление хоста на ноде",
                f"Удалить хост «{device.name}» ({device.host}) с ноды "
                f"«{self.node_service.get_display_name(node_iid)}»?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.No
        ):
            return

        def _run():
            try:
                return client.host_crud("delete", iid=device.iid)
            except Exception as e:
                return False, str(e), None

        self._set_status("⏳ Удаление хоста на ноде…", "busy")
        pool = get_command_worker_pool()

        def _on_complete(task_id, result):
            self._node_delete_result.emit(client, device, result)

        def _on_error(task_id, error):
            self._set_status(f"❌ Удаление хоста: {error}", "error")

        pool.submit(
            _run,
            on_complete=_on_complete,
            on_error=_on_error,
            metadata={
                "host": device.host,
                "type": "node_host_crud",
                "device": device.name or device.host,
            },
        )

    def _apply_node_device_delete_result(
        self, client: object, device: DeviceModel, result: object
    ) -> None:
        """Применить удаление в GUI только для актуального клиента ноды."""
        if self._shutdown_pending:
            return
        node_iid = getattr(device, "node_iid", None)
        if self.node_service.get_client(node_iid) is not client:
            return
        try:
            ok, msg, _dev = result
        except (TypeError, ValueError):
            ok, msg = False, str(result)
        if not ok:
            self._set_status(f"❌ Удаление хоста: {msg or 'ошибка'}", "error")
            return
        self._set_status(f"✅ Хост удалён с ноды: {msg}", "ok")
        section = self.treeview.get_node_section_item(node_iid)
        if section is None:
            return
        item = self.treeview._find_tree_item_by_iid(device.iid, parent=section)
        if item is not None and item.parent() is not None:
            self.remove_from_listview(item.device)
            item.parent().removeRow(item.row())

    def _node_device_add(self, node_iid: str) -> None:
        """Добавить хост на ноду (fedерация): диалог + host_crud add.

        Устройство создаётся в hosts.json ноды-владельца (в корень её
        дерева); после успеха нода пришлёт новое дерево хостов.
        """
        client = self.node_service.get_client(node_iid)
        if client is None:
            QMessageBox.warning(
                self,
                "Нода",
                f"Нода {self.node_service.get_display_name(node_iid)} недоступна.",
            )
            return
        from src.ui.dialogs.common.common import DeviceEditDialog

        device = DeviceModel({"name": "", "host": ""})
        device.node_iid = node_iid
        dialog = DeviceEditDialog(device, self)
        supports_backends = self.node_service.supports_thumbnail_backends(node_iid)
        dialog.set_thumbnail_backends_supported(supports_backends)
        if not dialog.exec():
            return
        device = dialog.get_updated_device()
        if device.thumbnail_backend == "vnc" and not supports_backends:
            QMessageBox.warning(
                self, "Нода", "Для миниатюр VNC обновите программу на ноде."
            )
            return
        data = device.to_dict()
        data.pop("iid", None)
        if not supports_backends:
            data.pop("thumbnail_backend", None)

        def _run():
            try:
                return client.host_crud("add", device=data)
            except Exception as e:
                return False, str(e), None

        self._set_status("⏳ Добавление хоста на ноду…", "busy")
        pool = get_command_worker_pool()

        def _on_complete(task_id, result):
            try:
                ok, msg, _dev = result
            except Exception:
                ok, msg = False, str(result)
            if ok:
                self._set_status(f"✅ Хост добавлен на ноду: {msg}", "ok")
                # Обновление секции придёт от ноды (hosts_updated)
                c = self.node_service.get_client(node_iid)
                if c is not None:
                    c.refresh_now()
            else:
                self._set_status(f"❌ Добавление хоста: {msg or 'ошибка'}", "error")

        def _on_error(task_id, error):
            self._set_status(f"❌ Добавление хоста: {error}", "error")

        pool.submit(
            _run,
            on_complete=_on_complete,
            on_error=_on_error,
            metadata={
                "host": getattr(device, "host", ""),
                "type": "node_host_crud",
                "device": device.name or device.host,
            },
        )

    def _local_device_edit(self, device: DeviceModel) -> None:
        """Локальное редактирование хоста: диалог + обновление модели/дерева.

        Путь treeview.doubleClicked / ПКМ «Редактировать» для хостов без
        ноды-владельца (в т.ч. node_iid == LOCAL_NODE_IID при тумблере «Ноды»).
        """
        if device is None:
            return
        dialog = DeviceEditDialog(device, self)
        if not dialog.exec():
            return
        new_device = dialog.get_updated_device()
        device.update(new_device.to_dict())
        item = self.treeview._find_tree_item_by_iid(device.iid, node_iid=getattr(device, "node_iid", None))
        if item is not None:
            item.setText(device.name)
            item.update()
        self.treeview.device_updated.emit(device)
        new_data = self.treeview.get_tree_data()
        self.treeview.data_changed.emit(new_data)

    def _local_device_delete(self, device: DeviceModel) -> None:
        """Локальное удаление хоста: подтверждение + чистка дерева/списка."""
        if device is None:
            return
        item = self.treeview._find_tree_item_by_iid(device.iid, node_iid=getattr(device, "node_iid", None))
        if item is not None:
            self.treeview.remove_selected_items([item])

    def on_list_item_clicked(self, index):
        """Обработка клика по элементу списка"""
        # При клике просто выбираем элемент
        pass

    def on_list_item_double_clicked(self, item: CustomListItem):
        """Открыть карточку по устойчивой идентичности устройства."""
        if item and hasattr(item, "device"):
            self._open_device_info(item.device)

    def _open_device_info(self, device: DeviceModel) -> None:
        """Разрешить исходную карточку заново после обновления/сортировки списка."""
        from src.services.hosts.device_info import get_device_info_service

        item = self.treeview._find_tree_item_by_iid(
            device.iid, node_iid=getattr(device, "node_iid", None)
        )
        if item is None:
            self._set_status("Устройство удалено из списка; карточка не открыта", "error")
            return
        current = getattr(item, "device", None)
        if current is not None:
            get_device_info_service().open_dialog(current, self)

    def show_tree_context_menu(self, position):
        """Показать контекстное меню для элемента дерева"""

    def on_execute_thread_finished(self):
        if not self.command_executed:
            return
        self.command_executed = False
        self.execute_button.setEnabled(True)
        self.execute_button.setVisible(True)
        self.command_abort_button.setEnabled(False)
        self.command_abort_button.setVisible(False)

        if hasattr(self, "current_worker_id"):
            self.current_worker_id = None

        self.worker = None
        # Node-воркер тоже завершён: guard on_single_host_command_executed
        # проверяет node_worker — без сброса single-host команды навсегда
        # блокировались после первого node-запуска.
        if hasattr(self, "current_node_worker_id"):
            self.current_node_worker_id = None
        self.node_worker = None
        logger.debug("MainWindow: Worker execution finished, UI reset")

    def load_commands(self):
        # self.command_combo_box.currentIndexChanged.connect(self.on_combo_changed)
        if os.path.exists(DEFAULT_COMMANDS_FILE):
            # ARCHITECTURE: Загружаем команды через сервис (стартап - тихая замена)
            success, message, _stats = self.command_service.import_commands_from_file(
                DEFAULT_COMMANDS_FILE, mode="replace"
            )

            if success:
                # Получаем данные из сервиса и загружаем в combobox
                data = self.command_service.get_all_commands()
                self.command_combo_box.load_tasks(data)
                if config.app.expand:
                    self.command_combo_box.expand_all_items()
                self.command_combo_box.select_first_task()

                logger.debug(f"MainWindow: {message}")
            else:
                logger.error(f"MainWindow: {message}")
                self._set_status("❌ Задачи не загружены", "error")
                QMessageBox.warning(
                    self,
                    "Задачи не загружены",
                    f"Не удалось загрузить список задач:\n{message}\n\n"
                    "Файл не будет перезаписан, пока его не исправят "
                    "или не заменят импортом задач.",
                )

    def on_result_double_clicked(self, index):
        """Handle double click on result row"""
        row = index.row()
        hostname = self.result_table.item(row, 0).text()
        status_item = self.result_table.item(row, self.result_table.COL_STATUS).text()
        iid = self.result_table.item(row, self.result_table.COL_IID).text()

        output_item = self.result_table.item(row, 1)
        output_from_table = output_item.text() if output_item else ""

        key = self.result_table.item(row, self.result_table.COL_IID).data(Qt.ItemDataRole.UserRole)
        key = tuple(key) if key else device_key(iid)
        stored_output = self.result_table.device_outputs.get(key, "")

        output = stored_output if stored_output else output_from_table
        if not output:
            output = "Нет данных"

        node_worker = getattr(self, "node_worker", None)
        worker = self.worker
        if node_worker is not None and any(
            device_identity(d) == key for d in node_worker._devices
        ):
            worker = node_worker
        dialog = CommandResultDialog(
            hostname, output, status_item, self, worker, device_iid=iid, node_iid=key[0]
        )
        dialog.exec_()

    def show_command_edit_dialog(self):
        editor_dialog = CommandEditorDialog(self)
        editor_dialog.exec()
        # Редактор сам сохраняет задачи («Применить», OK, вопрос при закрытии),
        # поэтому список перечитывается при любом закрытии окна.
        new_data = self.command_service.get_all_commands()
        self.command_combo_box.load_tasks(new_data)
        if config.app.expand:
            self.command_combo_box.expand_all_items()
        if self.command_combo_box.get_selected_task_data() is None:
            self.command_combo_box.select_first_task()

        # ARCHITECTURE: Публикуем событие об изменении команд
        self.event_bus.publish_typed(
            EventType.DATA_CHANGED,
            source="MainWindow",
            data={"type": "commands_updated"},
        )

        editor_dialog.deleteLater()

    def get_params_dict(self, params: list) -> dict:
        from src.ui.dialogs.common.params import ParameterInputDialog

        if not params:
            return None
        dialog = ParameterInputDialog(params)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.get_data()
        else:
            logger.debug("Пользователь отменил ввод.")
            return None

    def show_ssh_manage_dialog(self):
        from src.ui.dialogs.common.ssh_manage import SSHManageDialog

        dlg = SSHManageDialog(self)
        dlg.exec()

    def show_setup_wizard(self):
        """Показать мастер первоначальной настройки.

        После завершения мастера перезагружает дерево хостов и список команд
        из сервисов, чтобы отразить изменения в UI.
        """
        from src.ui.dialogs.wizard import SetupWizardDialog

        dlg = SetupWizardDialog(self, main_window=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Перезагружаем дерево хостов
            try:
                raw_data = self.device_service.get_devices_raw()
                self.treeview.load_tree_data(raw_data)
                self.treeview.setCurrentIndex(
                    self.treeview._model.invisibleRootItem().index()
                )
                if config.app.expand:
                    self.treeview.expandAll()
                else:
                    self._restore_tree_expanded_state()
            except Exception as e:
                logger.error(f"MainWindow: cannot reload hosts after wizard: {e}")

            # Перезагружаем список команд
            try:
                commands = self.command_service.get_all_commands()
                self.command_combo_box.load_tasks(commands)
                if config.app.expand:
                    self.command_combo_box.expand_all_items()
                if self.command_combo_box.get_selected_task_data() is None:
                    self.command_combo_box.select_first_task()
            except Exception as e:
                logger.error(f"MainWindow: cannot reload commands after wizard: {e}")

            # Очищаем список выбранных устройств
            try:
                self.listview.clear_devices()
            except Exception:
                pass

            # Обновляем видимость Veyon-элементов — мастер мог установить Veyon,
            # и кнопка миниатюр должна появиться сразу, а не ждать 60-сек таймера.
            try:
                self._refresh_veyon_ui_visibility()
            except Exception as e:
                logger.error(
                    f"MainWindow: veyon visibility refresh after wizard failed: {e}"
                )

            logger.info("MainWindow: UI reloaded after setup wizard")

    def _maybe_run_first_time_wizard(self):
        """Запустить мастер настройки при первом старте.

        Мастер запускается, если одновременно отсутствуют файлы хостов
        и команд (~/.dno-tool/hosts.json и ~/.dno-tool/commands.json).
        """
        try:
            hosts_exists = os.path.exists(DEFAULT_HOSTS_FILE)
            commands_exists = os.path.exists(DEFAULT_COMMANDS_FILE)
            if not hosts_exists and not commands_exists:
                logger.info("MainWindow: first run detected — launching setup wizard")
                QTimer.singleShot(100, self.show_setup_wizard)
        except Exception as e:
            logger.error(f"MainWindow: cannot check first-run: {e}")

    def show_pinger_dialog(self):
        try:
            from src.ui.dialogs.tools.pinger import PingMonitorDialog

            dlg = PingMonitorDialog(self)
            dlg.exec()
        except Exception as e:
            logger.exception("show_pinger_dialog: %s", e)

    def show_topology_dialog(self):
        try:
            from src.ui.dialogs.tools.topology_map import TopologyMapDialog

            existing = getattr(self, "_topology_dialog", None)
            if existing is not None:
                try:
                    existing.show()
                    existing.raise_()
                    existing.activateWindow()
                    return
                except Exception:
                    pass
            self._topology_dialog = TopologyMapDialog(self)
            self._topology_dialog.setAttribute(
                Qt.WidgetAttribute.WA_DeleteOnClose, False
            )
            self._topology_dialog.show()
        except Exception as e:
            logger.exception("show_topology_dialog: %s", e)

    def show_ffmpeg_dialog(self) -> None:
        """Открыть диагностику медиабиблиотек и кодировщиков GPU."""
        from src.ui.dialogs.tools.ffmpeg import FFmpegDialog

        FFmpegDialog(self).exec()

    def show_video_editor_dialog(self) -> None:
        """Открыть редактор отдельным окном с асинхронным закрытием."""
        try:
            from src.ui.dialogs.tools.video_editor import VideoEditorDialog

            dlg = VideoEditorDialog(self)
            dlg.setModal(False)
            dlg.finished.connect(dlg.deleteLater)
            dlg.show()
        except Exception as e:
            logger.exception("show_video_editor_dialog: %s", e)

    def _resolve_camera_service(self):
        from src.services import CameraService

        try:
            return self.container.resolve(CameraService)
        except Exception as e:
            logger.exception("resolve CameraService: %s", e)
            return None

    def show_cameras_dialog(self):
        try:
            from src.ui.dialogs.tools.cameras import CamerasDialog

            svc = self._resolve_camera_service()
            if svc is None:
                return
            # Переиспользуем существующий диалог, чтобы не прерывать
            # активные сеансы записи при повторном открытии окна.
            existing = getattr(self, "_cameras_dialog", None)
            if existing is not None:
                try:
                    existing.show()
                    existing.raise_()
                    existing.activateWindow()
                    return
                except Exception:
                    pass
            self._cameras_dialog = CamerasDialog(
                svc, event_bus=self.event_bus, parent=self
            )
            self._cameras_dialog.setAttribute(
                Qt.WidgetAttribute.WA_DeleteOnClose, False
            )
            self._cameras_dialog.show()
        except Exception as e:
            logger.exception("show_cameras_dialog: %s", e)

    def show_pool_monitor(self):
        try:
            from src.ui.dialogs.tools.pool_monitor import PoolMonitorDialog

            if getattr(self, "_pool_monitor", None) is not None:
                try:
                    self._pool_monitor.close()
                except Exception:
                    pass
            self._pool_monitor = PoolMonitorDialog(parent=self)
            self._pool_monitor.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            self._pool_monitor.show()
        except Exception as e:
            logger.exception("show_pool_monitor: %s", e)

    def show_camera_settings_dialog(self):
        try:
            from src.ui.dialogs.tools.camera_settings import CameraSettingsDialog

            svc = self._resolve_camera_service()
            if svc is None:
                return
            dlg = CameraSettingsDialog(svc, parent=self)
            dlg.exec()
        except Exception as e:
            logger.exception("show_camera_settings_dialog: %s", e)

    def _reject_if_command_running(self) -> bool:
        """Задание идёт локально или на нодах: предупредить и вернуть True."""
        if self._command_running():
            QMessageBox.critical(
                self,
                "Предупреждение",
                "Задача уже запущена. Отмените ее, чтобы запустить новую.",
            )
            return True
        return False

    def on_single_host_command_executed(self, device: DeviceModel, command_data: dict):
        if self._reject_if_command_running():
            return
        self.execute_command([device], command_data)

    def on_selected_hosts_command_executed(self, devices: list, command_data: dict):
        """Групповое ПКМ списка: задача на выделенных хостах."""
        if self._reject_if_command_running():
            return
        self.execute_command(list(devices), command_data)

    def execute_command(self, devices: list[DeviceModel], command_data: dict):
        if not devices:
            QMessageBox.information(
                self,
                "Выполнение задачи",
                "Нет хостов для выполнения задачи. Отметьте хотя бы один хост в дереве.",
            )
            return

        from src.domain.command_schema import task_error

        # Задача могла прийти не из проверенного хранилища (панель, старые данные).
        error = task_error(command_data)
        if error:
            QMessageBox.critical(
                self,
                "Выполнение задачи",
                f"Задача «{command_data.get('name') or 'без имени'}» не может быть "
                f"запущена: {error}.\nИсправьте её в редакторе задач.",
            )
            return

        timeout = command_data.get("timeout", config.app.ssh.command_timeout)
        commands_data = command_data.get("commands", [])
        commands = [Command(cmd) for cmd in commands_data]

        parameters = command_data.get("params", [])
        task_iid = command_data.get("iid")
        # Флаг session-задачи (D2): вся задача в общем shell.
        use_session = bool(command_data.get("session", False))
        desktop_session = command_data.get("desktop_session", False)

        params_dict: Optional[dict] = None
        if len(parameters) > 0:
            params_dict = self.get_params_dict(parameters)
            if params_dict is None:
                return

        # Ноды: отделяем хосты удалённых нод от локальных. Node-хосты
        # выполняются через API ноды-владельца (NodeCommandWorker), локальные —
        # через стандартный конвейер CommandWorker. Локальные хосты при
        # включённом тумблере «Ноды» имеют node_iid == LOCAL_NODE_IID —
        # они НЕ нод-хосты.
        from src.ui.widgets.device_tree import LOCAL_NODE_IID

        node_devices = [
            d
            for d in devices
            if getattr(d, "node_iid", None)
            and getattr(d, "node_iid", None) != LOCAL_NODE_IID
        ]
        local_devices = [
            d
            for d in devices
            if not getattr(d, "node_iid", None)
            or getattr(d, "node_iid", None) == LOCAL_NODE_IID
        ]

        # Clear previous results
        self.result_table.clear_results()

        # Initialize table with hosts
        self.result_table.add_initial_entries(devices, self.treeview.get_device_paths())

        # Статус-строка: начало выполнения команд.
        cmd_name = command_data.get("name") or "Команда"
        self._set_status(
            f"⏳ {cmd_name}: {len(devices)} {_plural_hosts(len(devices))}…",
            "busy",
            mood="bulk",
        )

        self.command_abort_button.setEnabled(True)
        self.command_abort_button.setVisible(True)

        self.execute_button.setEnabled(False)
        self.execute_button.setVisible(False)

        # ---- Локальные устройства: стандартный конвейер CommandWorker ----
        self.worker = None
        self.current_worker_id = None
        if local_devices:
            # ARCHITECTURE: Используем WorkerBridge для управления worker
            self.worker = CommandWorker(
                local_devices,
                commands,
                timeout,
                params=params_dict or {},
                session=use_session,
                desktop_session=desktop_session,
            )

            # Регистрируем worker в bridge для централизованного управления
            worker_id = f"command_{id(self.worker)}"
            self.worker_bridge.register_threaded_worker(
                self.worker,
                worker_id,
                client=self,
                context={
                    "devices": [d.host for d in local_devices],
                    "device_iids": [d.iid for d in local_devices],
                    "command_count": len(commands),
                    "task_iid": task_iid,
                    "session": use_session,
                    "desktop_session": desktop_session,
                },
                auto_start=False,
            )
            self.current_worker_id = worker_id

            # Подключаем сигналы результата
            self.worker.device_started.connect(self.result_table.set_executing)
            self.worker.progress_update.connect(self.result_table.update_progress)
            self.worker.result_ready.connect(self.result_table.set_result)
            self.worker.input_wait_changed.connect(self.result_table.set_input_wait_changed)

        # ---- Node-устройства: NodeCommandWorker на пул ----
        self.node_worker = None
        self.current_node_worker_id = None
        if node_devices:
            from src.workers.node.command_worker import NodeCommandWorker

            self.node_worker = NodeCommandWorker(
                node_devices,
                commands,
                timeout,
                # Исходные шаблоны и значения: подстановка один раз на владельце.
                params=params_dict or {},
                node_service=self.node_service,
                session=use_session,
                desktop_session=desktop_session,
            )
            node_worker_id = f"node_command_{id(self.node_worker)}"
            self.worker_bridge.register_threaded_worker(
                self.node_worker,
                node_worker_id,
                client=self,
                context={
                    "devices": [d.host for d in node_devices],
                    "device_iids": [d.iid for d in node_devices],
                    "node": True,
                    "task_iid": task_iid,
                    "session": use_session,
                    "desktop_session": desktop_session,
                },
                auto_start=False,
            )
            self.current_node_worker_id = node_worker_id

            self.node_worker.device_started.connect(self.result_table.set_executing)
            self.node_worker.progress_update.connect(self.result_table.update_progress)
            self.node_worker.result_ready.connect(self.result_table.set_result)

        # Публикуем событие о начале выполнения команды
        self.event_bus.publish_typed(
            EventType.COMMAND_STARTED,
            source="MainWindow",
            data={
                "device_count": len(devices),
                "command_count": len(commands),
                "task_iid": task_iid,
                "device_iids": [d.iid for d in devices],
            },
        )

        self.command_executed = True
        logger.debug("Starting command execution (local + node)")

        if (
            self.current_worker_id
            and self.current_worker_id in self.worker_bridge._threads
        ):
            self.worker_bridge._threads[self.current_worker_id].start()
        if (
            self.current_node_worker_id
            and self.current_node_worker_id in self.worker_bridge._threads
        ):
            self.worker_bridge._threads[self.current_node_worker_id].start()

    # ARCHITECTURE: Реализация интерфейса IWorkerClient
    def on_worker_started(self, worker_id: str, context: Dict[str, Any]) -> None:
        """Вызывается при запуске worker"""
        logger.debug(f"MainWindow: Worker {worker_id} started with context {context}")

    def on_worker_progress(self, worker_id: str, progress: Any) -> None:
        """Вызывается при обновлении прогресса worker"""
        # Прогресс обрабатывается чере�� сигналы напрямую для обратной совместимости
        pass

    def on_worker_finished(self, worker_id: str, result: Any) -> None:
        """Вызывается при завершении worker"""
        logger.debug(f"MainWindow: Worker {worker_id} finished")
        self.on_execute_thread_finished()

        # Статус-строка: итог выполнения команд.
        try:
            total, ok, fail, cancelled = self.result_table.count_results()
            if fail == 0 and cancelled == 0:
                self._set_status(f"✅ Готово: {ok}/{total}", "ok", mood="bulk")
            elif ok == 0 and fail > 0:
                self._set_status(f"❌ Ошибка: {fail}/{total}", "error", mood="bulk")
            else:
                msg = f"⚠ Готово: {ok}/{total} успешно"
                if fail:
                    msg += f", {fail} {plural_errors(fail)}"
                if cancelled:
                    msg += f", {cancelled} отменено"
                self._set_status(msg, "error" if fail else "ok", mood="bulk")
        except Exception as e:
            logger.debug(f"MainWindow: status count failed: {e}")

        # Публикуем событие о завершении команды
        self.event_bus.publish_typed(
            EventType.COMMAND_FINISHED,
            source="MainWindow",
            data={"worker_id": worker_id},
        )

    def on_worker_error(self, worker_id: str, error: str) -> None:
        """Вызывается при ошибке worker"""
        logger.error(f"MainWindow: Worker {worker_id} error: {error}")
        self._set_status(f"❌ Ошибка выполнения: {error}", "error", mood="bulk")
        QMessageBox.critical(
            self, "Ошибка выполнения", f"Ошибка при выполнении команды:\n{error}"
        )
