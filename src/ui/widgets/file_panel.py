"""Панель файлового командера (локальная или удалённая) для SFTP-диалога.

Инкапсулирует тулбар, breadcrumb-адресную строку, ``FileTreeView`` с D&D,
модель/proxy, историю навигации и контекстное меню.
"""

import json
import os
import platform
import posixpath
from datetime import datetime
from pathlib import PurePosixPath
from stat import S_IMODE, S_ISDIR

from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from src.ui.icons import _icon_from_key, set_themed_icon
from src.ui.widgets.breadcrumb_bar import BreadcrumbBar
from src.ui.widgets.device_list_toolbar import BaseDeviceToolbar
from src.ui.widgets.file_tree_view import FileTreeView
from src.ui.widgets.navigation_history import NavigationHistory
from src.utils.filesystem.sftp_files import local_child, remote_child, validate_name
from src.workers.sftp_archive import LOCAL_ARCHIVE_FORMATS, REMOTE_ARCHIVE_FORMATS
from src.workers.sftp_tasks import (
    DirectoryWorker,
    FileEntry,
    TransferItem,
    TransferWorker,
)

# --- Утилиты --------------------------------------------------------------


def format_size(size: int) -> str:
    for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ"]:
        if size < 1024:
            if unit == "Б":
                return f"{size} {unit}"
            return f"{size:.1f} {unit}" if size != int(size) else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} ЭБ"


class FileItem(QStandardItem):
    """Элемент файла/папки в модели."""

    def __init__(
        self,
        name,
        size=0,
        date=None,
        permissions="",
        is_dir=False,
        is_parent_dir=False,
        attrs_missing=False,
    ):
        super().__init__(name)
        self.setData(is_dir, Qt.UserRole + 2)
        self.setData(is_parent_dir, Qt.UserRole + 1)
        self.setData(attrs_missing, Qt.UserRole + 3)
        self.file_size = size
        self.file_date = date
        self.file_permissions = permissions
        self.setEditable(False)


class NameSortProxyModel(QSortFilterProxyModel):
    """Сортировка с фиксированным положением ".." и группировкой папок."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sort_order = Qt.SortOrder.AscendingOrder

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self._sort_order = order
        super().sort(column, order)

    def lessThan(self, left, right):
        src = self.sourceModel()
        left_name_index = src.index(left.row(), 0)
        right_name_index = src.index(right.row(), 0)

        left_is_parent = bool(src.data(left_name_index, Qt.UserRole + 1))
        right_is_parent = bool(src.data(right_name_index, Qt.UserRole + 1))
        if left_is_parent and not right_is_parent:
            return True if self._sort_order == Qt.SortOrder.AscendingOrder else False
        if right_is_parent and not left_is_parent:
            return False if self._sort_order == Qt.SortOrder.AscendingOrder else True

        left_is_dir = bool(src.data(left_name_index, Qt.UserRole + 2))
        right_is_dir = bool(src.data(right_name_index, Qt.UserRole + 2))
        if left_is_dir and not right_is_dir:
            return True
        if right_is_dir and not left_is_dir:
            return False

        col = left.column()
        left_index = src.index(left.row(), col)
        right_index = src.index(right.row(), col)
        ldata = src.data(left_index, Qt.DisplayRole) or ""
        rdata = src.data(right_index, Qt.DisplayRole) or ""

        if col == 1:
            ls = (
                getattr(src.item(left.row(), 0), "file_size", None)
                if hasattr(src, "item")
                else None
            )
            rs = (
                getattr(src.item(right.row(), 0), "file_size", None)
                if hasattr(src, "item")
                else None
            )
            try:
                if ls is not None and rs is not None:
                    return float(ls) < float(rs)
            except Exception:
                pass

        try:
            return str(ldata).lower() < str(rdata).lower()
        except Exception:
            return str(ldata) < str(rdata)


# =====================================================================
# FilePanel
# =====================================================================


class FilePanel(QWidget):
    """Панель файлового командера.

    Parameters
    ----------
    is_remote : bool
        True для удалённой панели, False для локальной.
    device : DeviceModel | None
        Устройство (для remote).
    dialog : SFTPDialog
        Ссылка на родительский диалог для доступа к воркерам/сервисам.
    """

    activated = Signal()
    pathChanged = Signal(str)
    selectionChanged = Signal(int, int)  # count, bytes
    requestTransfer = Signal(object, str, str)  # items_json, source_path, dest_path
    requestArchive = Signal(str, str, str)  # operation, item_path, archive_path

    def __init__(self, is_remote: bool, device, dialog, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(260)
        self.is_remote = is_remote
        self.device = device
        self.dialog = dialog
        self.folder_icon = _icon_from_key("folder")

        self.current_path = ""
        self.history = NavigationHistory()

        self.drive_combo: QComboBox | None = None
        self._listing_worker = None
        self._listing_generation = 0
        self._listing_pending = None
        self._render_model = None

        self._build()

    # ------------------------------------------------------------------
    # Построение UI
    # ------------------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        # Тулбар в стиле главных панелей (BaseDeviceToolbar: высота 36,
        # иконки 32, общий QSS, overflow-кнопка при нехватке места).
        # Кнопки - QAction (Qt отрисовывает их как QToolButton, при
        # переполнении уходят в popup-меню с иконкой и тултипом).
        self.toolbar = BaseDeviceToolbar(self)
        layout.addWidget(self.toolbar)
        self._build_toolbar()

        # Адресная строка: диск (Windows, локальная) + breadcrumb в одной строке
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(4)

        if not self.is_remote and platform.system().lower() == "windows":
            path_row.addWidget(QLabel("Диск:"))
            self.drive_combo = QComboBox()
            self.drive_combo.setMinimumWidth(80)
            self.drive_combo.currentIndexChanged.connect(self._on_drive_changed)
            path_row.addWidget(self.drive_combo)
            self._populate_drives()

        self.breadcrumb = BreadcrumbBar(self.is_remote, self)
        self.breadcrumb.pathRequested.connect(self.navigate)
        path_row.addWidget(self.breadcrumb, 1)
        layout.addLayout(path_row)

        # Модель + view
        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels(["Имя", "Размер", "Дата", "Права"])
        self.proxy = NameSortProxyModel()
        self.proxy.setSourceModel(self.model)

        self.view = FileTreeView(self.is_remote, self)
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.doubleClicked.connect(self.on_double_clicked)
        self.view.activated.connect(self.on_double_clicked)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.on_context_menu)
        self.view.externalDrop.connect(self._on_external_drop)
        self.view.internalDrop.connect(self._on_internal_drop)
        self.view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.view, 1)

        self._setup_header()

    def _add_toolbar_action(self, icon_key: str, text: str, slot) -> QAction:
        """Создать QAction с тематической иконкой (как главные тулбары).

        Иконка ставится через set_themed_icon (авто-refresh при смене
        темы). Пара (action, key) запоминается для FilePanel.refresh_icons.
        """
        action = QAction(text, self)
        set_themed_icon(action, icon_key)
        action.triggered.connect(slot)
        self.toolbar.addAction(action)
        self._themed_toolbar_actions.append((action, icon_key))
        return action

    def _build_toolbar(self) -> None:
        tb = self.toolbar
        self._themed_toolbar_actions: list = []
        self.act_home = self._add_toolbar_action("menu_home", "Домой", self.go_home)

        self.act_back = self._add_toolbar_action("menu_back", "Назад", self.go_back)

        self.act_forward = self._add_toolbar_action(
            "menu_forward", "Вперёд", self.go_forward
        )

        self.act_up = self._add_toolbar_action("menu_up", "Вверх (Alt+↑)", self.go_up)

        tb.addSeparator()

        self.act_refresh = self._add_toolbar_action(
            "menu_refresh", "Обновить (Ctrl+R)", self.refresh
        )

        self.act_mkdir = self._add_toolbar_action(
            "menu_add_folder", "Новая папка (F7)", self.create_folder
        )

        self.act_newfile = self._add_toolbar_action(
            "menu_new_file", "Новый файл", self.create_file
        )

        tb.addSeparator()

        if self.is_remote:
            self.act_download = self._add_toolbar_action(
                "menu_download", "Скачать (F5)", lambda: self.transfer_to_other()
            )
        else:
            self.act_upload = self._add_toolbar_action(
                "menu_upload", "Загрузить (F5)", lambda: self.transfer_to_other()
            )

        self.act_delete = self._add_toolbar_action(
            "menu_delete", "Удалить (F8/Del)", self.delete_selected
        )

        self.act_rename = self._add_toolbar_action(
            "menu_rename", "Переименовать (F2)", self.rename_selected
        )

        tb.addSeparator()

        self.act_copy_path = self._add_toolbar_action(
            "menu_copy_path", "Копировать путь", self.copy_path
        )

    def refresh_icons(self) -> None:
        """Перечитать иконки под текущую тему: тулбар + папки в листинге."""
        for action, icon_key in getattr(self, "_themed_toolbar_actions", []):
            try:
                set_themed_icon(action, icon_key)
            except RuntimeError:
                continue  # C++ объект удалён
        self.folder_icon = _icon_from_key("folder")
        try:
            root = self.model.invisibleRootItem()
            for row in range(root.rowCount()):
                item = root.child(row)
                if item is not None and bool(item.data(Qt.UserRole + 2)):
                    item.setIcon(self.folder_icon)
        except Exception:
            pass

    def _setup_header(self) -> None:
        header = self.view.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1, 100)
        header.resizeSection(2, 140)
        header.resizeSection(3, 60)

    # ------------------------------------------------------------------
    # SFTP-доступ
    # ------------------------------------------------------------------

    @property
    def sftp(self):
        return self.dialog.sftp if self.is_remote else None

    # ------------------------------------------------------------------
    # Навигация
    # ------------------------------------------------------------------

    def set_initial_path(self, path: str) -> None:
        self._request_listing(path, "initial")

    def navigate(self, path: str) -> None:
        if self.dialog._closing:
            return
        self._request_listing(self._normalize_path(path), "navigate")

    def _normalize_path(self, path: str) -> str:
        if self.is_remote:
            return posixpath.normpath(path or ".")
        return os.path.abspath(path or self.current_path or os.path.expanduser("~"))

    def go_up(self) -> None:
        if self.is_remote:
            parent = str(PurePosixPath(self.current_path).parent)
            if parent == self.current_path:
                return
            self.navigate(parent)
        else:
            parent = os.path.dirname(self.current_path)
            if parent == self.current_path:
                return
            self.navigate(parent)

    def go_back(self) -> None:
        if self.history.can_back():
            self._request_listing(self.history._items[self.history._index - 1], "back")

    def go_forward(self) -> None:
        if self.history.can_forward():
            self._request_listing(
                self.history._items[self.history._index + 1], "forward"
            )

    def go_home(self) -> None:
        if self.is_remote:
            self._request_listing(".", "navigate")
        else:
            self.navigate(os.path.expanduser("~"))

    # ------------------------------------------------------------------
    # Обновление списка
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        if not self.dialog._closing:
            self._request_listing(
                self.current_path
                or ("." if self.is_remote else os.path.expanduser("~")),
                "refresh",
            )

    # ------------------------------------------------------------------
    # Двойной клик / вход в каталог
    # ------------------------------------------------------------------

    def on_double_clicked(self, index) -> None:
        if not index.isValid():
            return
        item = self.model.itemFromIndex(
            self.proxy.mapToSource(index.siblingAtColumn(0))
        )
        if item is None:
            return
        if item.text() == "..":
            self.go_up()
        elif item.data(Qt.UserRole + 2) or (
            self.is_remote and item.data(Qt.UserRole + 3)
        ):
            self.navigate(self._join_path(item.text()))
        elif self.is_remote:
            self.dialog.preview_remote_file(item)

    # ------------------------------------------------------------------
    # Выделение
    # ------------------------------------------------------------------

    def selected_items(self) -> list[FileItem]:
        indexes = self.view.selectionModel().selectedRows(0)
        items: list[FileItem] = []
        for idx in indexes:
            src = self.proxy.mapToSource(idx)
            item = self.model.itemFromIndex(src)
            if item and item.text() != "..":
                items.append(item)
        return items

    def _on_selection_changed(self, *_):
        items = self.selected_items()
        count = len(items)
        size = 0
        for it in items:
            size += getattr(it, "file_size", 0) or 0
        self.selectionChanged.emit(count, size)

    # ------------------------------------------------------------------
    # Операции
    # ------------------------------------------------------------------

    def create_folder(self) -> None:
        self._create_object("mkdir", "Создание папки", "Имя новой папки:")

    def create_file(self) -> None:
        self._create_object("create", "Создание файла", "Имя нового файла:")

    def delete_selected(self) -> None:
        items = self.selected_items()
        if not items:
            return
        if self.is_remote:
            self.dialog.delete_remote_items(items)
        else:
            self.dialog.delete_local_items(items)

    def rename_selected(self) -> None:
        items = self.selected_items()
        if len(items) != 1 or self.dialog.operation_busy():
            return
        old_name = items[0].text()
        new_name, ok = QInputDialog.getText(
            self, "Переименование", "Новое имя:", text=old_name
        )
        if not ok or not new_name or new_name == old_name:
            return
        try:
            self.dialog.run_file_action(
                self.is_remote,
                "rename",
                [(self._join_path(old_name), self._join_path(new_name))],
            )
        except ValueError as exc:
            self._error("Ошибка имени", str(exc))

    def copy_path(self) -> None:
        items = self.selected_items()
        if not items:
            text = self.current_path
        elif len(items) == 1:
            text = self._join_path(items[0].text())
        else:
            text = self.current_path
        QApplication.clipboard().setText(text)
        self.dialog.log_operation(f"Путь скопирован: {text}")

    def transfer_to_other(self) -> None:
        items = self.selected_items()
        if not items:
            return
        self.dialog.transfer_items(self, items)

    # ------------------------------------------------------------------
    # Архивы
    # ------------------------------------------------------------------

    def archive_selected(self) -> None:
        if self.dialog.operation_busy():
            return
        items = self.selected_items()
        if len(items) != 1:
            self._info("Архивация", "Выберите один объект для упаковки.")
            return
        item = items[0]
        default_name = item.text() + ".tar.gz"
        name, ok = QInputDialog.getText(
            self, "Упаковка в архив", "Имя архива:", text=default_name
        )
        if not (ok and name):
            return
        try:
            validate_name(name, local=not self.is_remote)
        except ValueError as exc:
            self._error("Ошибка имени", str(exc))
            return
        item_path = self._join_path(item.text())
        if self.is_remote:
            arch_path = str(PurePosixPath(self.current_path).joinpath(name))
        else:
            arch_path = os.path.join(self.current_path, name)
        self.dialog.run_archive("pack", self.is_remote, item_path, arch_path)

    def extract_selected(self) -> None:
        if self.dialog.operation_busy():
            return
        items = self.selected_items()
        if len(items) != 1:
            self._info("Распаковка", "Выберите один архив для распаковки.")
            return
        item = items[0]
        name = item.text().lower()
        supported = REMOTE_ARCHIVE_FORMATS if self.is_remote else LOCAL_ARCHIVE_FORMATS
        if not name.endswith(supported):
            self._warn(
                "Не архив",
                "Поддерживаются: " + ", ".join(supported),
            )
            return
        if self.is_remote:
            remote_arch = self._join_path(item.text())
            dest = self.current_path
            self.dialog.run_archive("unpack", True, remote_arch, dest)
        else:
            arch_path = self._join_path(item.text())
            dest = self.current_path
            self.dialog.run_archive("unpack", False, arch_path, dest)

    # ------------------------------------------------------------------
    # Drag & Drop обработка
    # ------------------------------------------------------------------

    def _on_external_drop(self, paths: list, target_path: str) -> None:
        """Drop внешних URL (из Windows Explorer) — импорт в текущую/целевую папку."""
        if self.is_remote:
            # Загрузка внешних файлов на сервер через SFTP
            self.dialog.upload_external_to_remote(paths, target_path)
        else:
            # Копирование внешних файлов в локальную папку
            self._import_local(paths, target_path)

    def _import_local(self, paths: list, target_dir: str) -> None:
        if self.dialog.operation_busy():
            return
        try:
            tasks = [
                TransferItem(path, local_child(target_dir, os.path.basename(path)))
                for path in paths
            ]
        except ValueError as exc:
            self._error("Ошибка пути", str(exc))
            return
        self.dialog._start_transfer(
            TransferWorker(None, tasks, False, self.dialog, local=True)
        )

    def _on_internal_drop(
        self, source_is_remote: bool, payload: str, target_path: str
    ) -> None:
        try:
            data = json.loads(payload)
            if data.get("session_id") != self.dialog.session_id:
                self._warn(
                    "Другое подключение",
                    "Перенос между разными окнами SFTP не поддерживается.",
                )
                return
            items = data["items"]
            if not isinstance(items, list):
                return
            for item in items:
                validate_name(item["name"], local=not source_is_remote)
                if not isinstance(item["path"], str):
                    return
        except (ValueError, KeyError, TypeError):
            return
        if source_is_remote == self.is_remote:
            self._move_items(items, target_path)
        elif source_is_remote:
            self.dialog.download_items_from_drop(items, target_path)
        else:
            self.dialog.upload_items_from_drop(items, target_path)

    def _move_items(self, items: list, target_dir: str) -> None:
        if self.dialog.operation_busy():
            return
        try:
            paths = [
                (item["path"], self._join_path_for_target(target_dir, item["name"]))
                for item in items
            ]
            paths = [
                (source, destination)
                for source, destination in paths
                if source != destination
            ]
        except (KeyError, ValueError) as exc:
            self._error("Ошибка переноса", str(exc))
            return
        if paths:
            self.dialog.run_file_action(self.is_remote, "move", paths)

    # ------------------------------------------------------------------
    # Диски (Windows)
    # ------------------------------------------------------------------

    def _populate_drives(self) -> None:
        if self.drive_combo is None:
            return
        self.drive_combo.blockSignals(True)
        self.drive_combo.clear()
        import ctypes
        import string

        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase:
            if bitmask & 1:
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    self.drive_combo.addItem(drive)
            bitmask >>= 1
        cur = os.path.splitdrive(self.current_path)[0] + "\\"
        idx = self.drive_combo.findText(cur)
        if idx >= 0:
            self.drive_combo.setCurrentIndex(idx)
        self.drive_combo.blockSignals(False)

    def _on_drive_changed(self, index: int) -> None:
        if self.drive_combo is None or index < 0:
            return
        self.navigate(self.drive_combo.currentText())

    # ------------------------------------------------------------------
    # Утилиты путей
    # ------------------------------------------------------------------

    def _join_path(self, name: str) -> str:
        return self._join_path_for_target(self.current_path, name)

    # ------------------------------------------------------------------
    # Контекстное меню
    # ------------------------------------------------------------------

    def on_context_menu(self, pos) -> None:
        menu = QMenu(self)
        items = self.selected_items()

        if items:
            if len(items) == 1:
                is_dir = bool(items[0].data(Qt.UserRole + 2))
                if is_dir:
                    menu.addAction(
                        "Открыть",
                        lambda: self.on_double_clicked(self.view.currentIndex()),
                    )
                else:
                    act_view = menu.addAction(
                        _icon_from_key("menu_edit_file"),
                        "Просмотр",
                        lambda: self._open_item(items[0]),
                    )
                    if not self.is_remote:
                        act_view.setEnabled(False)
                menu.addSeparator()

            if self.is_remote:
                menu.addAction(
                    _icon_from_key("menu_download"),
                    "Скачать",
                    self.transfer_to_other,
                )
            else:
                menu.addAction(
                    _icon_from_key("menu_upload"),
                    "Загрузить",
                    self.transfer_to_other,
                )
            menu.addAction(
                _icon_from_key("menu_rename"),
                "Переименовать",
                self.rename_selected,
            )
            menu.addAction(
                _icon_from_key("menu_delete"), "Удалить", self.delete_selected
            )
            menu.addSeparator()

            if len(items) == 1:
                menu.addAction(
                    _icon_from_key("menu_archive"),
                    "Упаковать в tar.gz",
                    self.archive_selected,
                )
                if (
                    items[0]
                    .text()
                    .lower()
                    .endswith(
                        REMOTE_ARCHIVE_FORMATS
                        if self.is_remote
                        else LOCAL_ARCHIVE_FORMATS
                    )
                ):
                    menu.addAction(
                        _icon_from_key("menu_extract"),
                        "Распаковать",
                        self.extract_selected,
                    )
                menu.addSeparator()
                menu.addAction(
                    _icon_from_key("menu_copy_name"),
                    "Копировать имя",
                    lambda: QApplication.clipboard().setText(items[0].text()),
                )
                menu.addAction(
                    _icon_from_key("menu_copy_path"),
                    "Копировать путь",
                    self.copy_path,
                )
                menu.addSeparator()
                if self.is_remote:
                    menu.addAction(
                        _icon_from_key("shield"),
                        "Права доступа",
                        lambda: self.dialog.show_remote_info(items[0]),
                    )
                else:
                    menu.addAction(
                        _icon_from_key("menu_info"),
                        "Свойства",
                        lambda: self.dialog.show_local_info(items[0]),
                    )
        else:
            menu.addAction(
                _icon_from_key("menu_add_folder"), "Создать папку", self.create_folder
            )
            menu.addAction(
                _icon_from_key("menu_new_file"), "Создать файл", self.create_file
            )
            menu.addAction(_icon_from_key("menu_refresh"), "Обновить", self.refresh)
            menu.addSeparator()
            menu.addAction("Копировать путь текущей папки", self.copy_path)

        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _open_item(self, item: FileItem) -> None:
        if self.is_remote:
            self.dialog.preview_remote_file(item)

    # ------------------------------------------------------------------
    # Хелперы диалогов
    # ------------------------------------------------------------------

    def _warn(self, title, msg) -> None:
        QMessageBox.warning(self, title, msg)

    def _error(self, title, msg) -> None:
        QMessageBox.critical(self, title, msg)

    def _info(self, title, msg) -> None:
        QMessageBox.information(self, title, msg)

    def _join_path_for_target(self, target_dir: str, name: str) -> str:
        validate_name(name, local=not self.is_remote)
        return (
            remote_child(target_dir, name)
            if self.is_remote
            else os.path.join(target_dir, name)
        )

    def _create_object(self, operation: str, title: str, prompt: str) -> None:
        if self.dialog.operation_busy():
            return
        name, ok = QInputDialog.getText(self, title, prompt)
        if not ok or not name:
            return
        try:
            destination = self._join_path(name)
            self.dialog.run_file_action(self.is_remote, operation, [("", destination)])
        except ValueError as exc:
            self._error("Ошибка имени", str(exc))

    def cancel_listing(self) -> None:
        self._listing_generation += 1
        self._listing_pending = None
        if self._listing_worker is not None:
            self._listing_worker.cancel()
        if self._render_model is not None:
            self._render_model.deleteLater()
            self._render_model = None

    def _request_listing(self, path: str, mode: str) -> None:
        if self.dialog._closing or (self.is_remote and self.dialog.ssh is None):
            return
        self._listing_generation += 1
        self._listing_pending = (path, mode, self._listing_generation)
        if self._render_model is not None:
            self._render_model.deleteLater()
            self._render_model = None
        if self._listing_worker is not None:
            self._listing_worker.cancel()
            return
        self._start_listing()

    def _start_listing(self) -> None:
        if self._listing_pending is None or self.dialog._closing:
            return
        path, mode, generation = self._listing_pending
        self._listing_pending = None
        transport = (
            self.dialog.ssh.get_transport()
            if self.is_remote and self.dialog.ssh
            else None
        )
        worker = DirectoryWorker(transport, path, self.is_remote, generation, self)
        worker.navigation_mode = mode
        self._listing_worker = worker
        worker.finished.connect(self._listing_finished)
        worker.start()

    @Slot()
    def _listing_finished(self) -> None:
        worker = self._listing_worker
        self._listing_worker = None
        if worker is None:
            return
        if worker.generation == self._listing_generation and not self.dialog._closing:
            if worker.result.success:
                self._begin_render(worker)
            elif not worker.result.cancelled:
                self.dialog.log_operation(
                    f"Не удалось открыть {worker.path}: {worker.result.message}"
                )
                self.breadcrumb.set_path(self.current_path)
        worker.deleteLater()
        self._start_listing()

    def _begin_render(self, worker: DirectoryWorker) -> None:
        model = QStandardItemModel(self)
        model.setHorizontalHeaderLabels(["Имя", "Размер", "Дата", "Права"])
        parent = FileItem("..", is_dir=True, is_parent_dir=True)
        parent.setIcon(self.folder_icon)
        model.appendRow([parent, QStandardItem(), QStandardItem(), QStandardItem()])
        self._render_model = model
        self._render_entries = iter(worker.entries)
        self._render_path = worker.normalized_path
        self._render_mode = worker.navigation_mode
        self._render_generation = worker.generation
        self._render_batch()

    def _render_batch(self) -> None:
        model = self._render_model
        if (
            model is None
            or self.dialog._closing
            or self._render_generation != self._listing_generation
        ):
            return
        for _ in range(128):
            entry = next(self._render_entries, None)
            if entry is None:
                old = self.model
                self.model = model
                self._render_model = None
                self.proxy.setDynamicSortFilter(False)
                self.proxy.setSourceModel(model)
                self.proxy.setDynamicSortFilter(True)
                old.deleteLater()
                self.current_path = self._render_path
                if self.drive_combo is not None:
                    drive = os.path.splitdrive(self.current_path)[0].upper() + "\\"
                    index = self.drive_combo.findText(drive)
                    if index >= 0:
                        self.drive_combo.blockSignals(True)
                        self.drive_combo.setCurrentIndex(index)
                        self.drive_combo.blockSignals(False)
                if self._render_mode == "initial":
                    self.history = NavigationHistory(self.current_path)
                elif self._render_mode == "back":
                    self.history.back()
                elif self._render_mode == "forward":
                    self.history.forward()
                elif self._render_mode == "navigate":
                    self.history.navigate(self.current_path)
                self.breadcrumb.set_path(self.current_path)
                self._on_selection_changed()
                self.pathChanged.emit(self.current_path)
                return
            self._append_entry(model, entry)
        QTimer.singleShot(0, self._render_batch)

    def _append_entry(self, model: QStandardItemModel, entry: FileEntry) -> None:
        directory = S_ISDIR(entry.mode or 0)
        try:
            date = (
                datetime.fromtimestamp(entry.modified)
                if entry.modified is not None
                else None
            )
        except (ValueError, OSError, OverflowError):
            date = None
        permissions = oct(S_IMODE(entry.mode))[-3:] if entry.mode is not None else "—"
        item = FileItem(
            entry.name,
            entry.size if not directory else 0,
            date,
            permissions,
            directory,
            attrs_missing=entry.mode is None,
        )
        item.setToolTip(entry.name)
        if directory:
            item.setIcon(self.folder_icon)
        size = QStandardItem("" if directory else format_size(entry.size))
        size.setTextAlignment(Qt.AlignRight)
        modified = QStandardItem(date.strftime("%Y-%m-%d %H:%M:%S") if date else "")
        modified.setTextAlignment(Qt.AlignRight)
        model.appendRow([item, size, modified, QStandardItem(permissions)])


def target_path_of(target_dir: str, name: str) -> str:
    """Хелпер для построения пути цели в _move_items."""
    return str(PurePosixPath(target_dir).joinpath(name))
