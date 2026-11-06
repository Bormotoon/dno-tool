"""Модель данных для редактирования иерархической структуры задач (Tasks)."""
# (Импорты и константы обновлены под новую терминологию Tasks/Commands)
import uuid
from src.ui.icons import _icon_from_key, qicon_from_path  # zip-темы: иконки из памяти
from PySide6.QtWidgets import QDialog, QTreeView, QLineEdit, QTextEdit, QSpinBox, \
                              QPushButton, QListWidget, QListWidgetItem, QHBoxLayout, \
                              QVBoxLayout, QGroupBox, QSplitter, QMessageBox, QInputDialog, \
                              QDialogButtonBox, QApplication, QMainWindow, QWidget, QLabel, \
                              QSizePolicy
from PySide6.QtGui import QStandardItemModel, QStandardItem, QIcon
from PySide6.QtCore import Qt, QMimeData, QModelIndex, Signal, QObject, Slot
from ...config import ICONS, config

# Роль для хранения полного словаря Task (задачи)
TASK_DATA_ROLE = Qt.UserRole + 1

class TaskEditModel(QStandardItemModel):
    """Модель данных для редактирования иерархической структуры Tasks."""
    structureChanged = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.setHorizontalHeaderLabels(["Элемент"])
        self.default_task = {
            "name": "Новая задача",
            "description": "",
            "commands": [{"type": "ssh", "text": ""}],
            "timeout": config.app.ssh.command_timeout,
            "params": []
        }
        # Обратносовместимое имя атрибута, используемое старыми диалогами
        self.default_command = self.default_task

    def create_task(self, command_type: str = "ssh") -> dict:
        """Создает новую Task с одним шагом (Command) указанного типа"""
        t = self.default_task.copy()
        t["commands"] = [{"type": command_type, "text": ""}]
        t["iid"] = uuid.uuid4().hex
        return t

    def load_data(self, data_structure: list) -> None:
        """Загружает данные из структуры словарей в модель (Tasks и категории)."""
        from src.domain.task_options import validate_desktop_session_tree

        validate_desktop_session_tree(data_structure)
        self.clear()
        root_item = self.invisibleRootItem()
        self._populate_recursive(root_item, data_structure)

    def _populate_recursive(self, parent_item: QStandardItem, data_list: list) -> None:
        """Рекурсивно заполнить модель элементами из структуры данных."""
        if not isinstance(data_list, list): return
        for item_data in data_list:
            if not isinstance(item_data, dict): continue

            # Task (имеет name и commands)
            if "name" in item_data and "commands" in item_data:
                from src.domain.task_options import move_desktop_session_to_steps

                item_data = move_desktop_session_to_steps(item_data)
                if isinstance(item_data["commands"], list):
                    if item_data["commands"] and not isinstance(item_data["commands"][0], dict):
                        item_data["commands"] = [{"type": "ssh", "text": cmd} for cmd in item_data["commands"]]

                task_name = item_data.get("name", "Без имени")
                task_item = QStandardItem(task_name)
                task_item.setEditable(False)
                task_item.setSelectable(True)
                task_item.setIcon(_icon_from_key('command'))
                task_item.setData(item_data, TASK_DATA_ROLE)
                parent_item.appendRow(task_item)

            # Категория (папка)
            elif len(item_data) == 1:
                category_name = list(item_data.keys())[0]
                child_data_list = item_data[category_name]
                if isinstance(child_data_list, list):
                    category_item = QStandardItem(category_name)
                    category_item.setEditable(False)
                    category_item.setSelectable(True)
                    category_item.setIcon(_icon_from_key('folder'))
                    parent_item.appendRow(category_item)
                    self._populate_recursive(category_item, child_data_list)

    def get_task_data(self, index: QModelIndex) -> dict | None:
        """Возвращает данные Task (словарь) или None для категории."""
        if not index.isValid():
            return None
        return self.data(index, TASK_DATA_ROLE)
    
    def get_task_iid(self, index: QModelIndex) -> str | None:
        """Возвращает iid Task или None для категории/невалидного индекса."""
        data = self.get_task_data(index)
        if data is None:
            return None
        return data.get('iid')
    
    # Обратносовместимое имя метода, ожидаемое диалогами (get_item_data)
    def get_item_data(self, index: QModelIndex) -> dict | None:
        """Обратносовместимый алиас для get_task_data."""
        return self.get_task_data(index)

    def is_category(self, index: QModelIndex) -> bool:
        """Проверяет, является ли элемент категорией (т.е. не имеет данных Task)."""
        return self.get_task_data(index) is None and index.isValid()

    def dropMimeData(self, data: QMimeData, action: Qt.DropAction, row: int, column: int, parent: QModelIndex) -> bool:
        """DnD с восстановлением иконок.

        Штатный QStandardItemModel гоняет DecorationRole (QIcon) через
        QMimeData/QDataStream, а тематические иконки - это QIcon с кастомным
        движком (_SvgBytesIconEngine без сериализации read/write), который
        round-trip не переживает. Вставленные элементы приезжают с null
        иконкой. Данные (TASK_DATA_ROLE) при этом целы, поэтому иконки
        восстанавливаем по типу: категория - folder, задача - command.
        Проход по всему дереву идемпотентен и закрывает все кейсы
        (команда в папку, папка в корень, reorder, папка с вложенностью).
        """
        ok = super().dropMimeData(data, action, row, column, parent)
        if ok:
            try:
                self._restore_dnd_icons()
            except Exception:  # noqa: BLE001 - иконки не должны ронять дроп
                pass
        return ok

    def _restore_dnd_icons(self) -> None:
        """Переставить иконки всему дереву по типу элемента."""
        root = self.invisibleRootItem()
        stack = [root]
        while stack:
            parent_item = stack.pop()
            for row in range(parent_item.rowCount()):
                child = parent_item.child(row)
                if child is None:
                    continue
                if child.data(TASK_DATA_ROLE) is None:
                    child.setIcon(_icon_from_key('folder'))
                else:
                    child.setIcon(_icon_from_key('command'))
                if child.rowCount() > 0:
                    stack.append(child)

    def add_item(self, parent_index: QModelIndex, item_data: dict, is_category: bool) -> bool:
        """Добавляет новый элемент (Task или категория) к родителю."""
        from src.domain.task_options import validate_desktop_session_tree

        validate_desktop_session_tree([item_data])
        if not is_category:
            from src.domain.task_options import move_desktop_session_to_steps

            item_data = move_desktop_session_to_steps(item_data)
        parent_item = self.itemFromIndex(parent_index) if parent_index.isValid() else self.invisibleRootItem()
        if not parent_item:
            return False

        if is_category:
            category_name = list(item_data.keys())[0]
            new_item = QStandardItem(category_name)
            new_item.setEditable(False)
            new_item.setSelectable(True)
            new_item.setIcon(_icon_from_key('folder'))
        else:
            task_name = item_data.get("name", "Без имени")
            new_item = QStandardItem(task_name)
            new_item.setEditable(False)
            new_item.setSelectable(True)
            new_item.setIcon(_icon_from_key('command'))
            # Гарантировать iid для новой Task
            if 'iid' not in item_data or not item_data['iid']:
                item_data['iid'] = uuid.uuid4().hex
            new_item.setData(item_data, TASK_DATA_ROLE)

        parent_item.appendRow(new_item)
        self.structureChanged.emit()
        return True

    def remove_item(self, index: QModelIndex) -> bool:
        """Удалить элемент из модели по индексу."""
        if not index.isValid():
            return False
        parent_index = index.parent()
        row_to_remove = index.row()
        removed = self.removeRow(row_to_remove, parent_index)
        if removed:
            self.structureChanged.emit()
        return removed

    def update_item(self, index: QModelIndex, new_data: dict) -> bool:
        """Обновляет Task или имя категории."""
        from src.domain.task_options import validate_desktop_session_tree

        validate_desktop_session_tree([new_data])
        if "commands" in new_data:
            from src.domain.task_options import move_desktop_session_to_steps

            new_data = move_desktop_session_to_steps(new_data)
        if not index.isValid():
            return False

        item = self.itemFromIndex(index)
        if not item:
            return False

        is_cat = self.is_category(index)

        if is_cat:
            new_name = list(new_data.keys())[0]
            item.setText(new_name)
        else:
            updated_data = new_data.copy()

            # Сохранить iid существующей Task, либо сгенерировать для новой
            existing_iid = self.get_task_iid(index)
            if existing_iid and 'iid' not in updated_data:
                updated_data['iid'] = existing_iid
            elif 'iid' not in updated_data or not updated_data['iid']:
                updated_data['iid'] = uuid.uuid4().hex

            # Убедимся что все шаги (commands) имеют тип
            if "commands" in updated_data:
                new_commands = []
                for cmd in updated_data["commands"]:
                    if isinstance(cmd, str):
                        new_commands.append({"type": "ssh", "text": cmd})
                    else:
                        new_commands.append(cmd.copy())
                updated_data["commands"] = new_commands

            new_name = updated_data.get("name", "Без имени")
            item.setText(new_name)
            item.setData(updated_data, TASK_DATA_ROLE)

        self.structureChanged.emit()
        return True

    def get_data_structure(self) -> list:
        """Реконструирует структуру Tasks из модели."""
        root_index = self.invisibleRootItem().index()
        return self._build_recursive_structure(root_index)

    def _build_recursive_structure(self, parent_index: QModelIndex) -> list:
        """Рекурсивно собрать структуру данных из модели."""
        data_list = []
        row_count = self.rowCount(parent_index)
        for row in range(row_count):
            current_index = self.index(row, 0, parent_index)
            item_data = self.get_task_data(current_index)

            if item_data:
                data_list.append(item_data.copy())
            else:
                item = self.itemFromIndex(current_index)
                if item:
                    category_name = item.text()
                    children_list = self._build_recursive_structure(current_index)
                    data_list.append({category_name: children_list})
        return data_list

# Обратносовместимый псевдоним для кода, ожидающего старое имя модели
CommandEditModel = TaskEditModel
