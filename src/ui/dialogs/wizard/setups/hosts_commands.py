"""Страницы мастера настройки: хосты и команды.

Выделены из монолита setup_wizard.py без изменения логики:
тексты виджетов и поведение скопированы дословно.
"""

import json
import os
import tempfile
from copy import deepcopy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QHeaderView as QHV,
)

from src.logger import logger
from src.ui.icons import _icon_from_key
from src.ui.dialogs.tools.ip_scanner import IPScannerDialog
from src.ui.widgets.device_tree import DeviceTreeView
from src.ui.dialogs.wizard.setups.base import SetupPage
from src.services.configuration.setup_draft import CommandDraft, normalize_tree
from src.workers.command_download import CommandDownloadWorker


class HostsPage(SetupPage):
    """Страница добавления хостов."""

    sid = "hosts"

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        hint = QLabel(
            "Добавьте хосты любым удобным способом:\n"
            "• «Сканировать сеть» — автоматически найти хосты в заданном диапазоне IP.\n"
            "• «Добавить хост» — ввести один хост вручную.\n"
            "• «Добавить диапазон» — задать диапазон IP-адресов (CIDR или диапазон).\n"
            "• «Добавить папку» — создать группу для сортировки хостов.\n\n"
            "Перетаскивание (drag'n'drop):\n"
            "• Перетаскивайте хосты и папки внутри дерева, чтобы менять их расположение.\n"
            "• Бросьте элемент на папку — он станет её дочерним.\n"
            "• Чтобы добавить хосты в конкретную папку, выделите её перед добавлением."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        self._scan_btn = QPushButton(_icon_from_key("menu_scanner"), "Сканировать сеть")
        self._scan_btn.clicked.connect(self._open_scanner)
        btn_row.addWidget(self._scan_btn)

        self._add_host_btn = QPushButton("Добавить хост")
        self._add_host_btn.clicked.connect(self._add_host)
        btn_row.addWidget(self._add_host_btn)

        self._add_range_btn = QPushButton("Добавить диапазон")
        self._add_range_btn.clicked.connect(self._add_ip_range)
        btn_row.addWidget(self._add_range_btn)

        self._add_folder_btn = QPushButton("Добавить папку")
        self._add_folder_btn.clicked.connect(self._add_folder)
        btn_row.addWidget(self._add_folder_btn)

        self._clear_hosts_btn = QPushButton("Очистить")
        self._clear_hosts_btn.clicked.connect(self._clear_hosts)
        btn_row.addWidget(self._clear_hosts_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._hosts_tree = DeviceTreeView(
            read_only=False, enable_ping=False, show_checkboxes=False, show_columns=True
        )
        layout.addWidget(self._hosts_tree, 1)

    # --- контракт SetupPage ---

    def on_enter(self, ctx) -> None:
        pass

    def collect(self, ctx) -> None:
        ctx.hosts_data = normalize_tree(self.get_hosts_data(), "hosts")

    def set_data(self, data):
        self._hosts_tree._model.load_tree_data(deepcopy(data))

    def summary_html(self, ctx) -> str:
        data = ctx.hosts_data if ctx.hosts_data else self.get_hosts_data()
        n = self._count_hosts_in_data(data)
        return f"<b>Хосты:</b> {n}"

    # --- публичные данные ---

    def get_hosts_data(self) -> list:
        if self._hosts_tree is not None:
            return self._hosts_tree.get_tree_data()
        return []

    def load_existing_data(self, device_service) -> None:
        if self._hosts_tree is not None and device_service:
            try:
                data = device_service.get_devices_raw()
                self._hosts_tree._model.load_tree_data(data)
            except Exception as e:
                logger.error(f"SetupWizard: cannot load existing hosts: {e}")

    def clear_silent(self) -> None:
        if self._hosts_tree is None:
            return
        self._hosts_tree._model.clear()
        self._hosts_tree._model.setHorizontalHeaderLabels(
            ["Имя", "Адрес", "Связка", "Sudo", "MAC"]
        )

    def count_hosts(self) -> int:
        return self._count_hosts_in_data(self.get_hosts_data())

    @staticmethod
    def _count_hosts_in_data(data) -> int:
        count = 0
        if isinstance(data, list):
            for item in data:
                count += HostsPage._count_hosts_in_data(item)
        elif isinstance(data, dict):
            if "name" in data and "host" in data:
                count += 1
            else:
                for value in data.values():
                    count += HostsPage._count_hosts_in_data(value)
        return count

    # --- приватные слоты ---

    def _open_scanner(self):
        dlg = IPScannerDialog(self)
        dlg.exported_device.connect(self._on_scanner_import)
        dlg.exec()

    def _on_scanner_import(self, devices, folder):
        """Импорт устройств из сканера — делегируется DeviceTreeView."""
        if self._hosts_tree is not None:
            self._hosts_tree.add_devices_at_selected_folder(devices, folder)

    def _add_host(self):
        if self._hosts_tree is not None:
            parent = None
            selected_indexes = self._hosts_tree.selectedIndexes()
            if selected_indexes:
                parent = self._hosts_tree._model.itemFromIndex(selected_indexes[0])
            self._hosts_tree.add_device_to_group(parent)

    def _add_folder(self):
        if self._hosts_tree is not None:
            parent = None
            selected_indexes = self._hosts_tree.selectedIndexes()
            if selected_indexes:
                parent = self._hosts_tree._model.itemFromIndex(selected_indexes[0])
            self._hosts_tree.add_folder_to_group(parent)

    def _add_ip_range(self):
        if self._hosts_tree is not None:
            parent = None
            selected_indexes = self._hosts_tree.selectedIndexes()
            if selected_indexes:
                parent = self._hosts_tree._model.itemFromIndex(selected_indexes[0])
            self._hosts_tree.add_ip_range_to_group(parent)

    def _clear_hosts(self):
        if self._hosts_tree is None:
            return
        if (
            QMessageBox.question(
                self,
                "Очистка хостов",
                "Удалить все хосты из дерева?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.clear_silent()


class CommandsPage(SetupPage):
    """Страница загрузки библиотеки команд."""

    sid = "commands"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loaded = False
        self._command_service = None
        self._guard = None
        self._download_worker = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        hint = QLabel(
            "Команды - это готовые задачи, которые вы потом запускаете на хостах: "
            "выполнить команду по SSH, передать файлы или выполнить что-то "
            "на своей машине.\n"
            "Загрузите готовый набор кнопкой «Загрузить с сервера» или своим "
            "файлом через «Загрузить из файла». Загруженный список показан ниже."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        self._load_server_btn = QPushButton("Загрузить с сервера")
        self._load_server_btn.clicked.connect(self._load_commands_from_server)
        btn_row.addWidget(self._load_server_btn)

        self._load_file_btn = QPushButton("Загрузить из файла")
        self._load_file_btn.clicked.connect(self._load_commands_from_file)
        btn_row.addWidget(self._load_file_btn)

        btn_row.addStretch()

        self._clear_commands_btn = QPushButton("Очистить")
        self._clear_commands_btn.clicked.connect(self._clear_commands)
        btn_row.addWidget(self._clear_commands_btn)

        layout.addLayout(btn_row)

        self._commands_tree = QTreeWidget()
        self._commands_tree.setHeaderLabels(["Имя", "Описание"])
        header = self._commands_tree.header()
        header.setSectionResizeMode(0, QHV.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHV.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self._commands_tree.setColumnWidth(0, 300)
        self._commands_tree.setColumnWidth(1, 300)
        layout.addWidget(self._commands_tree, 1)

    # --- контракт SetupPage ---

    def on_enter(self, ctx) -> None:
        pass

    def collect(self, ctx) -> None:
        ctx.commands_loaded = self._loaded
        ctx.commands_data = self._command_service.get_all_commands()

    def summary_html(self, ctx) -> str:
        n = self.tasks_count()
        return f"<b>Команды:</b> {n}"

    # --- публичные данные ---

    def set_command_service(self, svc) -> None:
        self._source_service = svc
        self._command_service = CommandDraft()

    def set_data(self, data):
        self._command_service = CommandDraft(data)
        self._loaded = True
        self._refresh_commands_tree()

    def is_loaded(self) -> bool:
        return self._loaded

    def load_existing_data(self) -> None:
        if self._commands_tree is not None and self._command_service:
            try:
                self.set_data(self._source_service.get_all_commands())
            except Exception as e:
                logger.error(f"SetupWizard: cannot load existing commands: {e}")

    def clear_silent(self) -> None:
        if self._commands_tree is not None:
            self._commands_tree.clear()
        self._command_service = CommandDraft()
        self._loaded = True

    def tasks_count(self) -> int:
        if self._loaded and self._command_service:
            try:
                return self._command_service.get_tasks_count()
            except Exception:
                return 0
        return 0

    # --- приватные слоты ---

    def _ask_command_import_mode(self, file_path: str):
        """Диалог режима импорта с превью и разрешением конфликтов.

        Возвращает (mode, resolution) или None (отмена).
        """
        from src.ui.dialogs.command.conflict_dialog import CommandConflictDialog
        from src.ui.dialogs.command.import_dialog import CommandImportDialog

        ok, msg, preview = self._command_service.preview_import_from_file(file_path)
        if not ok:
            QMessageBox.critical(self, "Ошибка импорта", msg)
            return None
        dlg = CommandImportDialog(
            self,
            preview_stats=preview,
            local_count=self._command_service.get_tasks_count(),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        mode = dlg.selected_mode()
        resolution = None
        if mode == CommandImportDialog.MODE_MERGE:
            conflicts = self._command_service.find_import_conflicts(file_path)
            if conflicts:
                cdlg = CommandConflictDialog(self, conflicts=conflicts)
                if cdlg.exec() != QDialog.DialogCode.Accepted:
                    return None
                resolution = cdlg.resolution()
        return mode, resolution

    def _load_commands_from_server(self):
        if self._download_worker is not None and self._download_worker.isRunning():
            return
        self._download_worker = CommandDownloadWorker(self)
        self._load_server_btn.setEnabled(False)
        self._download_worker.ready.connect(self._on_commands_downloaded)
        self._download_worker.finished.connect(
            lambda: self._load_server_btn.setEnabled(True)
        )
        if self._guard is not None:
            self._guard.register_thread(
                "commands_download",
                self._download_worker,
                16000,
                abort=self._download_worker.requestInterruption,
            )
        self._download_worker.start()

    def _on_commands_downloaded(self, result):
        if not result["success"]:
            if result.get("error_code") == "certificate_verify_failed":
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle("Не удалось загрузить команды")
                box.setTextFormat(Qt.TextFormat.PlainText)
                box.setText(result["message"])
                box.setDetailedText(result["error"])
                box.setStandardButtons(QMessageBox.StandardButton.Close)
                download_btn = box.addButton(
                    "Открыть страницу загрузки", QMessageBox.ButtonRole.ActionRole
                )
                box.exec()
                if box.clickedButton() == download_btn:
                    from PySide6.QtCore import QUrl
                    from PySide6.QtGui import QDesktopServices

                    QDesktopServices.openUrl(QUrl(result["manual_url"]))
            else:
                QMessageBox.critical(self, "Ошибка", result["message"])
            return

        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            tmp_path = os.path.join(directory, "commands.json")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(result["data"], f, ensure_ascii=False, indent=2)

            asked = self._ask_command_import_mode(tmp_path)
            if asked is None:
                return
            mode, resolution = asked
            success, message, _stats = self._command_service.import_commands_from_file(
                tmp_path, mode=mode, resolution=resolution
            )

        if success:
            self._loaded = True
            self._refresh_commands_tree()
            QMessageBox.information(self, "Команды", message)
        else:
            QMessageBox.critical(self, "Ошибка импорта", message)

    def _load_commands_from_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Импортировать команды", "", "JSON Files (*.json)"
        )
        if not file_path:
            return

        asked = self._ask_command_import_mode(file_path)
        if asked is None:
            return
        mode, resolution = asked
        success, message, _stats = self._command_service.import_commands_from_file(
            file_path, mode=mode, resolution=resolution
        )
        if success:
            self._loaded = True
            self._refresh_commands_tree()
            QMessageBox.information(self, "Команды", message)
        else:
            QMessageBox.critical(self, "Ошибка импорта", message)

    def _refresh_commands_tree(self):
        self._commands_tree.clear()
        commands = (
            self._command_service.get_all_commands() if self._command_service else []
        )
        self._populate_commands_tree(commands, self._commands_tree.invisibleRootItem())

    def _populate_commands_tree(self, items, parent_item):
        for item in items:
            if not isinstance(item, dict):
                continue
            if "name" in item and "commands" in item:
                name = item.get("name", "")
                desc = item.get("description", "")
                tree_item = QTreeWidgetItem(parent_item, [name, desc])
                tree_item.setIcon(0, _icon_from_key("command"))
            elif len(item) == 1:
                key = next(iter(item.keys()))
                children = item[key]
                cat_item = QTreeWidgetItem(parent_item, [key, ""])
                cat_item.setIcon(0, _icon_from_key("folder"))
                if isinstance(children, list):
                    self._populate_commands_tree(children, cat_item)

    def _clear_commands(self):
        if self._commands_tree is None:
            return
        if (
            QMessageBox.question(
                self,
                "Очистка команд",
                "Удалить все команды из списка?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.clear_silent()
