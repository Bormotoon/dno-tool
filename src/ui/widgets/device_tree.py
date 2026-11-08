"""Виджет иерархического дерева устройств с поддержкой drag-and-drop, чекбоксов и контекстного меню."""

from PySide6.QtGui import (
    QStandardItemModel,
    QIcon,
    QStandardItem,
    QDragEnterEvent,
    QDrag,
    QMouseEvent,
    QDropEvent,
    QDragMoveEvent,
    QCursor,
)
from PySide6.QtCore import Qt, Signal, QMimeData, QModelIndex
from PySide6.QtWidgets import (
    QTreeView,
    QAbstractItemView,
    QMessageBox,
    QMenu,
    QInputDialog,
    QLineEdit,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from src.domain.models.device import DeviceModel
from src.domain.device_identity import DeviceKey, device_identity, device_key
from src.domain.smart_filter import SmartHostQuery
from src.ui.icons import _icon_from_key, qicon_from_path  # zip-темы: иконки из памяти
from src.config import ICONS, config
from src.utils.text.natural_sort import natural_key
import json
import os
import re
from typing import Optional
from collections import defaultdict
from src.logger import logger

from src.workers.network import get_host_ping_timer_manager
from src.workers.veyon import get_thumbnail_timer_manager

MIME_TYPE = "application/x-device-treeitem"

# Глобальная переменная для хранения перетаскиваемых элементов
_drag_source_items = []

FOLDER_DATAROLE = Qt.ItemDataRole.UserRole + 2
ITEM_DATAROLE = Qt.ItemDataRole.UserRole + 1
PASSWORD_DATAROLE = (
    Qt.ItemDataRole.UserRole + 3
)  # Реальное значение пароля (DisplayRole — маска)

# Индексы колонок при show_columns=True
COL_NAME = 0
COL_HOST = 1
COL_BUNDLE = 2
COL_SUDO = 3
COL_MAC = 4
NUM_COLUMNS = 5

SUDO_DATAROLE = (
    Qt.ItemDataRole.UserRole + 4
)  # Реальное значение use_sudo (None/True/False)

BUNDLE_DATAROLE = (
    Qt.ItemDataRole.UserRole + 5
)  # iid выбранной связки (None = наследовать)

# Роль-маркер для секций нод (федерация): True на верхнеуровневом элементе ноды.
# Хосты ноды имеют DeviceModel.node_iid (iid ноды-владельца).
NODE_SECTION_DATAROLE = Qt.ItemDataRole.UserRole + 6
# Роль с iid ноды-владельца на элементе секции ноды (str).
DEVICE_NODE_DATAROLE = Qt.ItemDataRole.UserRole + 7
# iid локальной ноды («Эта машина») — фиксированный (не из конфига нод).
LOCAL_NODE_IID = "local"
# Отображаемое имя локальной ноды.
LOCAL_NODE_NAME = "Эта машина"

_SUDO_LABELS = {None: "По умолчанию", True: "Да", False: "Нет"}
_BUNDLE_INHERIT_LABEL = "(наследовать)"


class PasswordDelegate(QStyledItemDelegate):
    """Делегат для колонки «Пароль»: отображает маску, в редакторе показывает реальное значение."""

    def displayText(self, value, locale):
        if value is None:
            return ""
        text = value.toString() if hasattr(value, "toString") else str(value)
        if text:
            return "•" * min(len(text), 16)
        return ""

    def setEditorData(self, editor, index):
        real = index.data(PASSWORD_DATAROLE)
        editor.setText(real if real else "")

    def setModelData(self, editor, model, index):
        value = editor.text()
        # Сначала сохраняем реальное значение в PASSWORD_DATAROLE
        model.setData(index, value, PASSWORD_DATAROLE)
        # Потом в EditRole — это триггерит dataChanged → _on_column_edited
        model.setData(index, value, Qt.ItemDataRole.EditRole)


class SudoDelegate(QStyledItemDelegate):
    """Делегат для колонки «Sudo»: QComboBox с 3 пунктами (По умолчанию/Да/Нет).

    Хранит реальное значение (None/True/False) в SUDO_DATAROLE,
    отображает текст через _SUDO_LABELS.
    """

    def displayText(self, value, locale):
        if value is None:
            return ""
        text = value.toString() if hasattr(value, "toString") else str(value)
        return text

    def createEditor(self, parent, option, index):
        from PySide6.QtWidgets import QComboBox

        combo = QComboBox(parent)
        combo.addItem("По умолчанию", None)
        combo.addItem("Да", True)
        combo.addItem("Нет", False)
        combo.setAutoFillBackground(True)
        return combo

    def setEditorData(self, editor, index):
        real = index.data(SUDO_DATAROLE)
        # None → 0, True → 1, False → 2
        idx = 0 if real is None else (1 if real else 2)
        editor.setCurrentIndex(idx)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def setModelData(self, editor, model, index):
        value = editor.currentData()  # None/True/False
        label = _SUDO_LABELS.get(value, "По умолчанию")
        model.setData(index, value, SUDO_DATAROLE)
        model.setData(index, label, Qt.ItemDataRole.EditRole)


class BundleDelegate(QStyledItemDelegate):
    """Делегат для колонки «Связка»: QComboBox выбора связки учётных данных.

    Хранит iid выбранной связки в BUNDLE_DATAROLE (None = наследовать глобальную),
    отображает описание связки в EditRole.
    """

    def displayText(self, value, locale):
        if value is None:
            return ""
        text = value.toString() if hasattr(value, "toString") else str(value)
        return text

    def createEditor(self, parent, option, index):
        from PySide6.QtWidgets import QComboBox

        combo = QComboBox(parent)
        combo.addItem(_BUNDLE_INHERIT_LABEL, None)
        try:
            from src.ui.widgets.bundle_selector import credential_service_for
            svc = credential_service_for(parent)
            bundles = svc.get_bundles() if svc else []
        except Exception:
            bundles = []
        for b in bundles:
            desc = b.get("description") or b.get("login") or "(без описания)"
            combo.addItem(desc, b.get("iid", ""))
        combo.setAutoFillBackground(True)
        return combo

    def setEditorData(self, editor, index):
        real = index.data(BUNDLE_DATAROLE)
        # Найти индекс по iid
        if not real:
            editor.setCurrentIndex(0)
            return
        idx = editor.findData(real)
        if idx < 0:
            # Связка удалена — показываем «наследовать»
            editor.setCurrentIndex(0)
        else:
            editor.setCurrentIndex(idx)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def setModelData(self, editor, model, index):
        iid = editor.currentData()  # None или str
        label = editor.currentText()
        model.setData(index, iid, BUNDLE_DATAROLE)
        model.setData(index, label, Qt.ItemDataRole.EditRole)


class CustomTreeItem(QStandardItem):
    """Кастомный элемент дерева с дополнительными свойствами"""

    @property
    def device(self) -> DeviceModel | None:
        """Устройство принадлежит элементу модели Qt, а не его Python-обёртке."""
        return self.data(ITEM_DATAROLE)

    @device.setter
    def device(self, value: DeviceModel | None) -> None:
        self.setData(value, ITEM_DATAROLE)

    def __init__(
        self,
        text: str = "",
        device: DeviceModel = None,
        checkable: bool = True,
        show_columns: bool = False,
    ):
        """
        Инициализация элемента
        device - объект устройства
        checkable - показывать ли чекбокс
        show_columns - создавать ли дополнительные колонки (Логин/Пароль/MAC)
        """
        super().__init__(text)
        self.setEditable(True)
        self.setSelectable(True)
        if checkable:
            self.setCheckable(True)
            self.setFlags(self.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            self.setCheckState(Qt.CheckState.Unchecked)
        else:
            self.setCheckable(False)
            self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        self.setEditable(False)

        self.device = device
        # Флаг секции ноды (только для read-only верхнеуровневых элементов ноды)
        self._is_node_section = False
        self._node_iid = None
        # Последний известный статус связи секции (для refresh_icons).
        self._node_reachable = False
        self.show_columns = show_columns
        if self.device is None:
            self.setData(True, FOLDER_DATAROLE)  # Только для папок
        self.update()

    def update(self):
        if self.device:
            self.setText(self.device.name)
            self.setIcon(qicon_from_path(self.device.icon))
            self.setData(False, FOLDER_DATAROLE)  # Для устройств явно False
        else:
            self.setText(self.text())
            self.setIcon(_icon_from_key("folder"))
            self.setData(True, FOLDER_DATAROLE)

    def make_extra_columns(self):
        """Создаёт list из QStandardItem для колонок Адрес/Связка/Sudo/MAC.

        Возвращает [col_host, col_bundle, col_sudo, col_mac] — нужно добавить
        к колонке 0 при appendRow([self, col_host, col_bundle, col_sudo, col_mac]).
        Для папок — пустые нередактируемые элементы.
        """
        if not self.show_columns:
            return []

        if self.device is not None:
            # Адрес
            host_item = QStandardItem(self.device.host or "")
            host_item.setEditable(True)
            host_item.setData(False, FOLDER_DATAROLE)

            # Связка учётных данных — iid в BUNDLE_DATAROLE, описание в EditRole.
            # Редактируется через BundleDelegate (QComboBox).
            bundle_iid = self.device.credential_bundle_iid
            bundle_label = self._bundle_label(bundle_iid)
            bundle_item = QStandardItem(bundle_label)
            bundle_item.setEditable(True)
            bundle_item.setData(bundle_iid, BUNDLE_DATAROLE)
            bundle_item.setData(False, FOLDER_DATAROLE)

            # Sudo — реальное значение (None/True/False) в SUDO_DATAROLE,
            # отображается текстом через _SUDO_LABELS, редактируется QComboBox (SudoDelegate)
            sudo_value = self.device.use_sudo
            sudo_item = QStandardItem(_SUDO_LABELS.get(sudo_value, "По умолчанию"))
            sudo_item.setEditable(True)
            sudo_item.setData(sudo_value, SUDO_DATAROLE)
            sudo_item.setData(False, FOLDER_DATAROLE)

            # MAC
            mac_item = QStandardItem(self.device.mac_address or "")
            mac_item.setEditable(True)
            mac_item.setData(False, FOLDER_DATAROLE)

            return [host_item, bundle_item, sudo_item, mac_item]
        else:
            # Папка — пустые нередактируемые ячейки
            cols = []
            for _ in range(NUM_COLUMNS - 1):
                placeholder = QStandardItem("")
                placeholder.setEditable(False)
                placeholder.setData(True, FOLDER_DATAROLE)
                cols.append(placeholder)
            return cols

    @staticmethod
    def _bundle_label(bundle_iid) -> str:
        """Описание связки по iid для отображения в колонке. None = «(наследовать)»."""
        if not bundle_iid:
            return _BUNDLE_INHERIT_LABEL
        try:
            from src.di import get_container
            from src.services.configuration.credential_service import CredentialService

            svc = get_container().resolve_optional(CredentialService)
            if svc is not None:
                b = svc.get_bundle_by_iid(bundle_iid)
                if b is not None:
                    return b.description or b.login or "(без описания)"
        except Exception:
            pass
        return "(связка не найдена)"

    def row_items(self):
        """Возвращает [self] + extra columns для appendRow."""
        return [self] + self.make_extra_columns()

    # ----- API для нод -----------------------------------------------------

    @classmethod
    def make_node_section(cls, node_iid: str, name: str) -> "CustomTreeItem":
        """Создать верхнеуровневый элемент-секцию ноды.

        Секция ноды — read-only корень: не сериализуется в hosts.json,
        DnD из/в неё запрещён, контекстное меню ограничено. Хосты внутри
        имеют device.node_iid=node_iid.

        Локальная нода (node_iid == LOCAL_NODE_IID) — та же секция, но
        хосты внутри сериализуются в hosts.json (export_tree_data
        пропускает только секции с iid != LOCAL_NODE_IID).
        """
        item = cls(name or "Нода", device=None, checkable=False, show_columns=False)
        item._is_node_section = True
        item._node_iid = node_iid
        item.setData(True, NODE_SECTION_DATAROLE)
        item.setData(node_iid, DEVICE_NODE_DATAROLE)
        item.setEditable(False)
        try:
            # Стартовая иконка — красная (связь ещё не установлена);
            # set_node_reachable() переключит на зелёную после handshake.
            # Локальная нода всегда онлайн.
            if node_iid == LOCAL_NODE_IID:
                path = ICONS.get("node_online") or ICONS.get("menu_nodes", "")
                item._node_reachable = True
            else:
                path = ICONS.get("node_offline") or ICONS.get("menu_nodes", "")
                item._node_reachable = False
            if path:
                item.setIcon(qicon_from_path(path))
        except Exception:
            pass
        return item

    @property
    def is_node_section(self) -> bool:
        return bool(self._is_node_section)

    @property
    def node_iid(self):
        """iid ноды-владельца: у секции — свой, у хоста — из device."""
        if self._is_node_section:
            return self._node_iid
        if self.device is not None:
            return getattr(self.device, "node_iid", None)
        return self._node_iid

    def set_node_reachable(self, reachable: bool) -> None:
        """Обновить индикатор связи секции ноды: зелёная/красная иконка.

        Иконки - node_online.svg и node_offline.svg из папки активной
        темы (assets/icons/<тема>/). Текст не трогаем - статус видно по иконке.
        """
        if not self._is_node_section:
            return
        try:
            # Локальная нода всегда «онлайн» (мы на ней и запущены) —
            # иконку не меняем: зелёная нода проставлена при создании.
            if self._node_iid == LOCAL_NODE_IID:
                self._node_reachable = True
                return
            self._node_reachable = bool(reachable)
            key = "node_online" if reachable else "node_offline"
            self.setIcon(_icon_from_key(key))
        except Exception:
            pass

    def refresh_node_icon(self) -> None:
        """Перечитать иконку секции ноды под текущую тему (по _node_reachable)."""
        if not self._is_node_section:
            return
        try:
            key = "node_online" if self._node_reachable else "node_offline"
            self.setIcon(_icon_from_key(key))
        except Exception:
            pass

    # -----------------------------------------------------------------------

    def appendRow(self, item: QStandardItem) -> None:
        result = super(CustomTreeItem, self).appendRow(item)


class CustomTreeItemModel(QStandardItemModel):
    """Кастомная модель для дерева с поддержкой чекбоксов"""

    itemDataChanged = Signal(object, object)  # Сигнал об изменении состояния чекбокса
    data_changed = Signal(list)  # Сигнал об изменении данных дерева

    def __init__(
        self, parent=None, read_only=False, show_checkboxes=True, show_columns=False
    ):
        super().__init__(parent)
        self.read_only = read_only
        self.show_checkboxes = show_checkboxes
        self.show_columns = show_columns

    def setData(self, index, value, role=Qt.ItemDataRole.CheckStateRole):
        """
        Обработка изменения состояния чекбокса
        Реализует каскадное включение/выключение чекбоксов
        """
        oldvalue = index.data(role)
        result = super(CustomTreeItemModel, self).setData(index, value, role)

        if role != Qt.ItemDataRole.CheckStateRole:
            return result

        if result and value != oldvalue:
            self.itemDataChanged.emit(self.itemFromIndex(index), role)
            item = self.itemFromIndex(index)
            if item.checkState() == Qt.CheckState.Unchecked:
                item.setIcon(_icon_from_key("default"))
            for i in range(item.rowCount()):
                child = item.child(i)
                self.setData(child.index(), value, role)
            self.propagate_checkstate_up(item)
        return result

    def propagate_checkstate_up(self, item: CustomTreeItem):
        parent = item.parent()
        while parent is not None:
            # Секции нод (make_node_section, checkable=False) чекбоксов не
            # имеют: setCheckState на не-UserCheckable элементе неявно
            # включает чекабельность — прерываем каскад, чтобы чекбокс не
            # появлялся у секции при отметке хоста внутри неё.
            if not parent.isCheckable():
                break
            checked = self.collectChecked(parent)
            if len(checked) == 0:
                parent.setCheckState(Qt.CheckState.Unchecked)
            elif len(checked) == parent.rowCount():
                parent.setCheckState(Qt.CheckState.Checked)
            else:
                parent.setCheckState(Qt.CheckState.PartiallyChecked)
            item = parent
            parent = item.parent()

    def collectChecked(self, node):
        checked = []

        if node is None:
            return checked

        for i in range(node.rowCount()):
            child = node.child(i)
            if (
                child.checkState() == Qt.CheckState.Checked
                or child.checkState() == Qt.CheckState.PartiallyChecked
            ):
                checked.append(child)
        return checked

    def supportedDropActions(self):
        """Поддерживаемые действия при перетаскивании"""
        return Qt.DropAction.MoveAction | Qt.DropAction.LinkAction

    def export_tree_data(self) -> list:
        """Сохранение всего дерева с учетом иерархии.

        Секции нод (NODE_SECTION_DATAROLE) пропускаются — хосты ноды
        принадлежат ноде-владельцу и не попадают в hosts.json оператора.
        Локальная нода (LOCAL_NODE_IID) — исключение: её содержимое —
        собственно локальные хосты этого инстанса.
        """
        root = self.invisibleRootItem()
        data = []

        for i in range(root.rowCount()):
            group_item: CustomTreeItem = root.child(i)
            if bool(group_item.data(NODE_SECTION_DATAROLE)):
                if group_item.data(DEVICE_NODE_DATAROLE) == LOCAL_NODE_IID:
                    # Локальная нода: сериализуем её детей напрямую
                    data.extend(self._save_group_items(group_item))
                continue  # секция ноды — не сериализуем
            if group_item.hasChildren():
                data.append({group_item.text(): self._save_group_items(group_item)})
            else:
                device: DeviceModel = getattr(group_item, "device", None)
                if device is None:
                    if not bool(group_item.data(FOLDER_DATAROLE)):
                        raise ValueError(f"У хоста «{group_item.text()}» отсутствуют данные устройства")
                    data.append({group_item.text(): []})
                else:
                    data.append(device.export())

        return data

    def _save_group_items(self, group_item):
        """Сохранение элементов группы"""
        items = []
        for i in range(group_item.rowCount()):
            child = group_item.child(i)
            if child.hasChildren():
                # Если это подгруппа
                items.append({child.text(): self._save_group_items(child)})
            else:
                # Если это устройство
                device: DeviceModel = getattr(child, "device", None)
                if device is None:
                    if not bool(child.data(FOLDER_DATAROLE)):
                        raise ValueError(f"У хоста «{child.text()}» отсутствуют данные устройства")
                    items.append({child.text(): []})
                else:
                    items.append(device.export())
        return items

    def mimeTypes(self):
        return [
            MIME_TYPE,
            "text/plain",
        ]

    def mimeData(self, indexes):
        """Сохраняет дополнительные данные элемента при drag"""
        mime_data = QMimeData()
        if indexes and mime_data:
            items_data = bytearray()
            for index in indexes:
                item = self.itemFromIndex(index)
                if isinstance(item, CustomTreeItem):
                    is_folder = bool(item.data(FOLDER_DATAROLE))
                    # Сохраняем все свойства элемента
                    item_data = {
                        "text": item.text(),
                        "is_folder": str(is_folder),
                        "checkState": int(
                            item.checkState().value
                        ),  # Convert enum to int
                        "row": index.row(),
                        "parent_row": index.parent().row()
                        if index.parent().isValid()
                        else -1,
                    }
                    if hasattr(item, "device") and item.device is not None:
                        item_data["device"] = item.device.export()
                    items_data.extend(json.dumps(item_data, ensure_ascii=False).encode("utf-8"))
                    items_data.extend(b"\n")

            # Сохраняем данные в MIME
            mime_data.setData(MIME_TYPE, bytes(items_data))
        return mime_data

    def dropMimeData(
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: QModelIndex,
    ):
        global _drag_source_items

        # finally-блок: при любом выходе (успех/ошибка/ранний return) очищаем
        # _drag_source_items, чтобы уничтоженные C++ объекты («зомби») не
        # попали в следующий drag и не вызвали RuntimeError в snapshots_by_id.
        try:
            if self.read_only:
                return False

            if action == Qt.DropAction.IgnoreAction:
                return True

            if not data.hasFormat(MIME_TYPE):
                return False

            # Проверяем весь пакет до удаления исходных строк. Настоящий JSON
            # сохраняет кавычки, переносы строк и булевы параметры хоста.
            try:
                rows = [
                    json.loads(line.decode("utf-8"))
                    for line in bytes(data.data(MIME_TYPE)).splitlines() if line
                ]
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False
            if not rows or any(
                not isinstance(item, dict)
                or not isinstance(item.get("text"), str)
                or ("device" in item and not isinstance(item["device"], dict))
                for item in rows
            ):
                return False
            parent_item = (
                self.itemFromIndex(parent)
                if parent.isValid()
                else self.invisibleRootItem()
            )

            # Дроп разрешён только в папку или в корень. dropEvent уже отклоняет
            # дроп на устройство-лист, но проверим и здесь для надёжности:
            # молчаливое превращение устройства в папку (старое поведение)
            # приводило к потере данных устройства.
            if parent.isValid() and not bool(parent_item.data(FOLDER_DATAROLE)):
                logger.warning("dropMimeData: drop target is not a folder, rejected")
                return False

            insert_row = row
            if insert_row is None:
                insert_row = parent_item.rowCount()

            # node_iid целевой секции (дроп в локальную секцию/её папки —
            # пересозданные элементы должны сохранить node_iid=LOCAL_NODE_IID).
            target_node_iid = None
            if parent.isValid() and parent_item is not None:
                probe = parent_item
                while probe is not None:
                    if bool(probe.data(NODE_SECTION_DATAROLE)):
                        target_node_iid = probe.data(DEVICE_NODE_DATAROLE)
                        break
                    probe = probe.parent()

            # Нормализуем _drag_source_items: оставляем только живые
            # CustomTreeItem (как startDrag и mimeData). Это устраняет:
            #  - placeholder-ячейки (QStandardItem при show_columns=True)
            #  - «зомби» (уничтоженные C++ объекты из предыдущей ошибки)
            #  - дубликаты
            # Нормализованный список синхронен с MIME-записями по позиции,
            # что критично для snapshots_list (индексируем по позиции).
            seen_src_ids = set()
            live_source_items = []
            for src_item in _drag_source_items:
                if src_item is None:
                    continue
                if id(src_item) in seen_src_ids:
                    continue
                if not isinstance(src_item, CustomTreeItem):
                    continue
                try:
                    _ = src_item.data(FOLDER_DATAROLE)
                except RuntimeError:
                    # Зомби — пропускаем.
                    continue
                seen_src_ids.add(id(src_item))
                live_source_items.append(src_item)

            # Снимаем поддеревья исходных элементов ДО удаления.
            # После removeRow QStandardItem уничтожается, и обращение к его
            # children/rowCount возвращает 0 — папка пересоздавалась пустой.
            # Снимок — чистый dict, из которого затем восстанавливаем структуру.
            # snapshots_list индексируется по позиции в live_source_items,
            # что синхронно с MIME (startDrag и mimeData фильтруют одинаково).
            snapshots_list = []
            for src_item in live_source_items:
                is_folder = bool(src_item.data(FOLDER_DATAROLE))
                if is_folder:
                    snapshots_list.append(self._export_subtree(src_item))
                else:
                    snapshots_list.append(None)

            # Удаление исходных элементов.
            # Перестраиваем индекс через indexFromItem перед каждым removeRow,
            # а не используем сохранённые (parent, row) — если предыдущая
            # итерация удалила родителя, сохранённый item_parent уже уничтожен
            # и item_parent.child(row) бросит RuntimeError.
            # Используем live_source_items (нормализованный список живых
            # CustomTreeItem), синхронный с MIME/snapshots_list.
            deleted_ids = set()
            # Сохраняем исходные позиции (parent_index, row) ДО удаления для
            # последующей корректировки insert_row (перетаскивание внутри того
            # же родителя). После removeRow indexFromItem вернёт невалидный
            # индекс, поэтому запоминаем позиции здесь.
            original_positions = []  # list of (parent_index_for_compare, row, item_id)
            delete_groups = defaultdict(
                list
            )  # id(parent) -> [(row, item, parent_item, parent_idx)]
            for src_item in live_source_items:
                if id(src_item) in deleted_ids:
                    continue
                idx = self.indexFromItem(src_item)
                if not idx.isValid():
                    # Элемент уже не в модели (удалён) — пропускаем.
                    deleted_ids.add(id(src_item))
                    continue
                parent_idx = idx.parent()
                parent_item_for_del = (
                    self.itemFromIndex(parent_idx)
                    if parent_idx.isValid()
                    else self.invisibleRootItem()
                )
                delete_groups[id(parent_item_for_del)].append(
                    (idx.row(), src_item, parent_item_for_del, parent_idx)
                )
                original_positions.append((parent_idx, idx.row(), id(src_item)))
                deleted_ids.add(id(src_item))

            # Сортируем группы по глубине родителя по убыванию: сначала удаляем
            # элементы из самых глубоких родителей (потомков), потом из более
            # верхних. Иначе удаление родителя уничтожает дочерние элементы, и
            # последующая попытка удалить дочерний элемент из уничтоженного
            # родителя бросает RuntimeError.
            def _depth(parent_idx):
                d = 0
                p = parent_idx
                while p.isValid():
                    d += 1
                    p = p.parent()
                return d

            group_order = sorted(
                delete_groups.items(),
                key=lambda kv: _depth(kv[1][0][3]) if kv[1] else 0,
                reverse=True,
            )

            for parent_id, entries in group_order:
                # Берём parent_item из первой записи (все записи в группе
                # имеют одного родителя). Сортируем по row desc.
                entries.sort(key=lambda e: e[0], reverse=True)
                item_parent = entries[0][2]
                for row_idx, item_to_remove, _, _ in entries:
                    # Останавливаем пинг, если устройство было отмечено.
                    if (
                        item_to_remove
                        and hasattr(item_to_remove, "device")
                        and item_to_remove.device
                    ):
                        try:
                            if item_to_remove.checkState() == Qt.CheckState.Checked:
                                get_host_ping_timer_manager().stop_ping(
                                    item_to_remove.device.iid
                                )
                        except RuntimeError:
                            pass
                    # Удаляем строку. removeRow на родителе удаляет всю строку
                    # (все колонки), что корректно и для show_columns=True.
                    # try/except — страховка: если родитель всё же уничтожен
                    # (не должно случиться при правильной сортировке), не падаем.
                    try:
                        item_parent.removeRow(row_idx)
                    except RuntimeError:
                        logger.warning(
                            "dropMimeData: parent already destroyed, skip removeRow"
                        )

            # Корректируем insert_row если вставляем в того же родителя
            # (если удалённые элементы были до позиции вставки). Используем
            # сохранённые исходные позиции (до удаления), сравнивая родителя
            # источника с родителем цели по QModelIndex — без обращения к
            # уничтоженным QStandardItem, иначе RuntimeError.
            deleted_before_insert = 0
            target_parent_idx = parent
            for src_parent_idx, src_row, _ in original_positions:
                # Сравниваем QModelIndex оператором == (без обращения к C++ объектам).
                same_parent = (src_parent_idx == target_parent_idx) or (
                    not src_parent_idx.isValid() and not target_parent_idx.isValid()
                )
                if same_parent and src_row < insert_row:
                    deleted_before_insert += 1

            insert_row -= deleted_before_insert
            if insert_row < 0:
                insert_row = 0

            # Индекс текущей MIME-записи в live_source_items / snapshots_list.
            # startDrag и mimeData фильтруют элементы одинаково, поэтому
            # MIME-запись i соответствует snapshots_list[i].
            mime_index = 0
            for item_data in rows:
                try:
                    # Снимок детей папки для текущей MIME-записи (по позиции).
                    snapshot = None
                    if mime_index < len(snapshots_list):
                        snapshot = snapshots_list[mime_index]
                    mime_index += 1

                    # Создаем новый элемент
                    if "device" in item_data:
                        device = DeviceModel(item_data["device"])
                        if target_node_iid:
                            device.node_iid = target_node_iid
                        new_item = CustomTreeItem(
                            device.name,
                            device,
                            checkable=self.show_checkboxes,
                            show_columns=self.show_columns,
                        )
                        new_item.setData(False, FOLDER_DATAROLE)  # Для устройств
                        if target_node_iid:
                            new_item.setData(target_node_iid, DEVICE_NODE_DATAROLE)
                    else:
                        # Это папка - создаем и копируем дочерние элементы из снимка
                        new_item = CustomTreeItem(
                            item_data["text"],
                            checkable=self.show_checkboxes,
                            show_columns=self.show_columns,
                        )
                        new_item.setData(True, FOLDER_DATAROLE)  # Для папок
                        if target_node_iid:
                            new_item.setData(target_node_iid, DEVICE_NODE_DATAROLE)
                        # Копируем чекбокс состояние
                        if self.show_checkboxes:
                            new_item.setCheckState(
                                Qt.CheckState(item_data.get("checkState", 0))
                            )

                        # Восстанавливаем дочерние элементы из снимка, сделанного
                        # до удаления source_item из модели. snapshot берётся по
                        # позиции MIME-записи (не по id — source уже уничтожен).
                        if snapshot is not None:
                            self._build_from_snapshot(
                                new_item, snapshot, target_node_iid
                            )

                    new_item.setDropEnabled(True)
                    new_item.setDragEnabled(True)

                    # Вставляем элемент на позицию insert_row (если задано), иначе в конец
                    if parent_item:
                        row_items = new_item.row_items()
                        if (
                            insert_row is None
                            or insert_row < 0
                            or insert_row > parent_item.rowCount()
                        ):
                            parent_item.appendRow(row_items)
                        else:
                            parent_item.insertRow(insert_row, row_items)
                            insert_row += 1
                    else:
                        # Корень
                        row_items = new_item.row_items()
                        if (
                            insert_row is None
                            or insert_row < 0
                            or insert_row > self.rowCount()
                        ):
                            self.appendRow(row_items)
                        else:
                            self.insertRow(insert_row, row_items)
                            insert_row += 1

                except json.JSONDecodeError as e:
                    logger.error(f"Error decoding JSON: {e}")
                    return False
                except Exception as e:
                    logger.error(f"Error in dropMimeData: {e}")
                    return False

            return True
        finally:
            # Очищаем при любом выходе — уничтоженные C++ объекты не должны
            # остаться для следующего drag (иначе RuntimeError при доступе
            # к data(FOLDER_DATAROLE) в snapshots_list).
            _drag_source_items = []

    def _copy_child_items(
        self, target_item: CustomTreeItem, source_item: CustomTreeItem
    ):
        """Рекурсивное копирование дочерних элементов"""
        checkable = self.show_checkboxes
        show_cols = self.show_columns
        for i in range(source_item.rowCount()):
            child = source_item.child(i)
            if child.data(FOLDER_DATAROLE):  # Это папка
                new_child = CustomTreeItem(
                    child.text(), checkable=checkable, show_columns=show_cols
                )
                new_child.setData(True, FOLDER_DATAROLE)
                if checkable:
                    new_child.setCheckState(child.checkState())
                new_child.setDropEnabled(True)
                new_child.setDragEnabled(True)
                # Рекурсивно копируем детей этой папки
                if child.hasChildren():
                    self._copy_child_items(new_child, child)
                target_item.appendRow(new_child.row_items())
            else:  # Это устройство
                if hasattr(child, "device") and child.device:
                    if isinstance(child.device, dict):
                        device = DeviceModel(child.device)
                    else:
                        device = DeviceModel(child.device.export())
                    new_child = CustomTreeItem(
                        device.name, device, checkable=checkable, show_columns=show_cols
                    )
                    new_child.setData(False, FOLDER_DATAROLE)
                    if checkable:
                        new_child.setCheckState(child.checkState())
                    new_child.setDropEnabled(True)
                    new_child.setDragEnabled(True)
                    target_item.appendRow(new_child.row_items())

    def _export_subtree(self, item: CustomTreeItem) -> dict:
        """Рекурсивный снимок поддерева в чистый dict (без QStandardItem).

        Возвращает структуру:
          папка  -> {"text": str, "folder": True, "checkState": int, "children": [...]}
          устройcтво -> {"text": str, "folder": False, "device": <DeviceModel.export()>}
        Снимок строится ДО удаления элемента из модели, чтобы впоследствии
        восстановить детей из снимка, а не из уничтоженного QStandardItem.
        """
        if bool(item.data(FOLDER_DATAROLE)):
            children = []
            for i in range(item.rowCount()):
                child = item.child(i)
                if child is not None:
                    children.append(self._export_subtree(child))
            return {
                "text": item.text(),
                "folder": True,
                "checkState": int(item.checkState().value)
                if self.show_checkboxes
                else 0,
                "children": children,
            }
        device = getattr(item, "device", None)
        return {
            "text": item.text(),
            "folder": False,
            "device": device.export() if device is not None else None,
            "checkState": int(item.checkState().value) if self.show_checkboxes else 0,
        }

    def _build_from_snapshot(
        self, target_item: CustomTreeItem, snapshot: dict, node_iid: str = None
    ):
        """Восстанавливает дочерние элементы папки из снимка (зеркало _copy_child_items).

        Используется в dropMimeData, где исходный QStandardItem уже уничтожен
        (removeRow), но снимок детей сохранён до удаления. node_iid — iid ноды,
        если дроп внутри секции ноды (элементы наследуют маркер).
        """
        checkable = self.show_checkboxes
        show_cols = self.show_columns
        for child_snap in snapshot.get("children", []):
            if child_snap.get("folder"):
                new_child = CustomTreeItem(
                    child_snap["text"],
                    checkable=checkable,
                    show_columns=show_cols,
                )
                new_child.setData(True, FOLDER_DATAROLE)
                if node_iid:
                    new_child.setData(node_iid, DEVICE_NODE_DATAROLE)
                if checkable:
                    new_child.setCheckState(
                        Qt.CheckState(child_snap.get("checkState", 0))
                    )
                new_child.setDropEnabled(True)
                new_child.setDragEnabled(True)
                self._build_from_snapshot(new_child, child_snap, node_iid)
                target_item.appendRow(new_child.row_items())
            else:
                dev_data = child_snap.get("device")
                if dev_data is None:
                    continue
                device = DeviceModel(dev_data)
                if node_iid:
                    device.node_iid = node_iid
                new_child = CustomTreeItem(
                    device.name, device, checkable=checkable, show_columns=show_cols
                )
                new_child.setData(False, FOLDER_DATAROLE)
                if node_iid:
                    new_child.setData(node_iid, DEVICE_NODE_DATAROLE)
                if checkable:
                    new_child.setCheckState(
                        Qt.CheckState(child_snap.get("checkState", 0))
                    )
                new_child.setDropEnabled(True)
                new_child.setDragEnabled(True)
                target_item.appendRow(new_child.row_items())

    def _collect_descendant_iids(self, item: CustomTreeItem) -> list:
        """iid всех устройств в поддереве (рекурсивно). Для dropEvent: после
        пересоздания элементов dropMimeData найти их заново по iid."""
        iids = []

        def _walk(node):
            for i in range(node.rowCount()):
                child = node.child(i)
                if child is None:
                    continue
                dev = getattr(child, "device", None)
                if dev is not None:
                    iid = getattr(dev, "iid", None)
                    if iid:
                        iids.append(iid)
                if child.hasChildren():
                    _walk(child)

        _walk(item)
        return iids

    def _collect_descendant_devices(self, item: CustomTreeItem) -> list:
        """Все DeviceModel в поддереве (рекурсивно). Для move_selected_to_folder:
        элементы перемещаются без пересоздания, поэтому DeviceModel валидны."""
        devices = []

        def _walk(node):
            for i in range(node.rowCount()):
                child = node.child(i)
                if child is None:
                    continue
                dev = getattr(child, "device", None)
                if dev is not None:
                    devices.append(dev)
                if child.hasChildren():
                    _walk(child)

        _walk(item)
        return devices

    def load_tree_data(self, data):
        """Загрузка данных в дерево"""
        self.clear()
        root = self.invisibleRootItem()
        self._load_group_items(root, data)
        if self.show_columns:
            self.setHorizontalHeaderLabels(["Имя", "Адрес", "Связка", "Sudo", "MAC"])
        else:
            self.setHorizontalHeaderLabels(["Списки"])

    def _load_group_items(self, parent_item, data, node_iid: str = None):
        """Рекурсивная загрузка элементов группы

        node_iid: iid ноды-владельца (LOCAL_NODE_IID для локальной ноды) —
        проставляется в DeviceModel.node_iid, чтобы список/статусы знали,
        к какой секции относится хост. None = локальное дерево без секции.
        """
        checkable = self.show_checkboxes
        show_cols = self.show_columns
        for item in data:
            if isinstance(item, dict):
                if "name" in item and "host" in item:
                    device = DeviceModel(item)
                    if node_iid:
                        device.node_iid = node_iid
                    device_item = CustomTreeItem(
                        device.name, device, checkable=checkable, show_columns=show_cols
                    )
                    device_item.setData(False, FOLDER_DATAROLE)  # Для устройств
                    if node_iid:
                        device_item.setData(node_iid, DEVICE_NODE_DATAROLE)
                    parent_item.appendRow(device_item.row_items())
                else:
                    for key, value in item.items():
                        group_item = CustomTreeItem(
                            key, checkable=checkable, show_columns=show_cols
                        )
                        group_item.setData(True, FOLDER_DATAROLE)  # Для папок
                        if node_iid:
                            group_item.setData(node_iid, DEVICE_NODE_DATAROLE)
                        parent_item.appendRow(group_item.row_items())
                        self._load_group_items(group_item, value, node_iid)


class DeviceTreeView(QTreeView):
    itemDoubleClicked = Signal(object)
    itemDataChanged = Signal(object, object)  # Сигнал об изменении состояния чекбокса
    fileDropped = Signal(object)
    data_changed = Signal(list)  # Сигнал для уведомления об изменении данных дерева
    device_removed = Signal(object)  # Сигнал об удалении устройства (DeviceModel)
    device_updated = Signal(
        object
    )  # Сигнал об изменении устройства (DeviceModel) — для синхронизации со списком
    # Сигналы для нод (федерация): обработчики подключает MainWindow.
    node_refresh_requested = Signal(str)  # node_iid — обновить дерево хостов ноды
    node_hide_requested = Signal(str)  # node_iid — скрыть секцию из дерева
    node_remove_requested = Signal(str)  # node_iid — удалить ноду из настроек
    node_device_edit_requested = Signal(
        str, object
    )  # (node_iid, DeviceModel) — редактировать хост на ноде
    node_device_delete_requested = Signal(
        str, object
    )  # (node_iid, DeviceModel) — удалить хост на ноде
    node_device_add_requested = Signal(str)  # node_iid — добавить хост на ноде

    def __init__(
        self,
        parent=None,
        read_only=False,
        enable_ping=True,
        show_checkboxes=True,
        show_columns=False,
    ):
        super().__init__(parent)
        self._model = CustomTreeItemModel(
            read_only=read_only,
            show_checkboxes=show_checkboxes,
            show_columns=show_columns,
        )
        self.read_only = read_only
        self.show_checkboxes = show_checkboxes
        self.show_columns = show_columns
        self.enable_ping = enable_ping
        # Показ нод первым уровнем дерева (тулбар дерева). По умолчанию
        # выключен: ноды не показываются и клиенты нод не запускаются
        # (нет опроса), пока оператор не включит тумблер «Ноды».
        self.show_nodes = False
        self._saved_node_sections: list = []
        # Секции удалённых нод, скрытые переключателем «Ноды» (не в модели,
        # но живы — при показе возвращаются в корень с чекбоксами/статусами).
        self._hidden_node_sections: list = []
        # Текст временного фильтра поиска (пусто = фильтр выключен).
        # Применяется через setRowHidden (view-уровень): модель, сериализация,
        # чекбоксы и таймеры не затрагиваются.
        self._filter_text = ""
        self._filter_query: Optional[SmartHostQuery] = None
        self.setModel(self._model)

        self.setup_ui()

        self.doubleClicked.connect(self.on_item_double_clicked)
        self.customContextMenuRequested.connect(self.show_tree_context_menu)

        # Подключаем сигнал изменения чекбокса
        self._model.itemDataChanged.connect(self._on_item_checkstate_changed)

        # Подключаем сигнал изменения данных модели к сигналу дерева
        self._model.data_changed.connect(self.data_changed.emit)

        # Подключаем обработчик редактирования колонок (Связка/Sudo/MAC)
        if self.show_columns:
            self._model.dataChanged.connect(self._on_column_edited)
            # Делегат для колонки «Связка» (QComboBox выбора связки)
            self.setItemDelegateForColumn(COL_BUNDLE, BundleDelegate(self))
            # Делегат для колонки «Sudo» (QComboBox По умолчанию/Да/Нет)
            self.setItemDelegateForColumn(COL_SUDO, SudoDelegate(self))

        if self.enable_ping:
            get_host_ping_timer_manager().online_updated.connect(
                self._on_ping_online_updated
            )
            # Статусы хостов нод (per-host таймеры у нод-владельцев) — тот же
            # путь отрисовки, что у списка: результат опроса красит иконку
            # дерева сразу, а не после пересборки секции poll-циклом.
            try:
                from src.workers.node.status import get_node_status_manager

                get_node_status_manager().status_updated.connect(
                    self._on_ping_online_updated
                )
            except Exception as e:
                logger.debug(f"DeviceTreeView: node status manager недоступен: {e}")

    def _on_item_checkstate_changed(self, item, role):
        """Обработчик изменения чекбокса"""
        if role == Qt.ItemDataRole.CheckStateRole:
            self.on_item_checkstate_changed(item, item.checkState())

    def _on_column_edited(self, top_left, bottom_right, roles):
        """Синхронизация изменений в колонках Связка/Sudo/MAC с DeviceModel."""
        if not top_left.isValid():
            return
        col = top_left.column()
        if col == 0:
            return  # Колонка «Имя» — не редактируется через EditRole здесь
        if (
            Qt.ItemDataRole.EditRole not in roles
            and Qt.ItemDataRole.DisplayRole not in roles
        ):
            return

        row = top_left.row()
        parent = top_left.parent()
        # Берём элемент колонки 0 (CustomTreeItem с device)
        item0 = self._model.itemFromIndex(self._model.index(row, 0, parent))
        if item0 is None or not hasattr(item0, "device") or item0.device is None:
            return

        cell_item = self._model.itemFromIndex(top_left)
        if cell_item is None:
            return

        value = cell_item.text()
        if col == COL_HOST:
            item0.device.host = value if value else None
        elif col == COL_BUNDLE:
            # iid связки из BUNDLE_DATAROLE (установлено BundleDelegate.setModelData)
            iid = cell_item.data(BUNDLE_DATAROLE)
            item0.device.credential_bundle_iid = iid if iid else None
        elif col == COL_SUDO:
            # Для sudo: реальное значение (None/True/False) в SUDO_DATAROLE,
            # установлено SudoDelegate.setModelData.
            real = cell_item.data(SUDO_DATAROLE)
            item0.device.use_sudo = real
        elif col == COL_MAC:
            item0.device.mac_address = value if value else None

        self.data_changed.emit(self.get_tree_data())

    def _on_ping_online_updated(self, device: DeviceModel, is_online: bool):
        """Обновление иконки элемента дерева при изменении статуса пинга"""
        # Поиск по iid (первичный), fallback по host (для данных без iid)
        item = self._find_tree_item_by_iid(device.iid, node_iid=getattr(device, "node_iid", None)) if device.iid else None
        if item is None and not device.iid and device.host:
            item = self._find_tree_item_by_host(device.host)
        if item:
            try:
                item.setIcon(qicon_from_path(device.icon))
            except (RuntimeError, AttributeError):
                pass

    def _find_tree_item_by_iid(self, iid: str, parent=None, *, node_iid: str | None = None):
        """Рекурсивный поиск элемента дерева по iid устройства"""
        if not iid:
            return None
        if parent is None:
            parent = self._model.invisibleRootItem()
            node_iid = node_iid or LOCAL_NODE_IID
        elif node_iid is None:
            node_iid = parent.data(DEVICE_NODE_DATAROLE) or LOCAL_NODE_IID
        for i in range(parent.rowCount()):
            child = parent.child(i)
            if (
                hasattr(child, "device")
                and child.device
                and device_identity(child.device) == device_key(iid, node_iid)
            ):
                return child
            if child.hasChildren():
                result = self._find_tree_item_by_iid(iid, child, node_iid=node_iid)
                if result:
                    return result
        return None

    def reconcile_local_hosts(self, data: list) -> None:
        """Apply owner-node CRUD in the GUI without rewriting authoritative data."""
        from PySide6.QtCore import QSignalBlocker

        root = self.get_node_section_item(LOCAL_NODE_IID) if self.show_nodes else None
        root = root or self._model.invisibleRootItem()
        desired = {}

        def collect(nodes, path=()):
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if "host" in node and node.get("iid"):
                    desired[node["iid"]] = (node, path)
                else:
                    for name, children in node.items():
                        if isinstance(children, list):
                            collect(children, (*path, name))

        collect(data)
        existing = {}

        def walk(parent):
            for row in range(parent.rowCount()):
                item = parent.child(row)
                if getattr(item, "is_node_section", False):
                    continue
                device = getattr(item, "device", None)
                if device is not None:
                    existing[device.iid] = item
                walk(item)

        walk(root)
        removed, updated = [], []
        # Keep model row notifications intact; suppress only tree persistence signals.
        with QSignalBlocker(self):
            for iid, item in existing.items():
                if iid not in desired:
                    removed.append(item.device)
                    get_host_ping_timer_manager().stop_ping(iid)
                    (item.parent() or root).removeRow(item.row())
            for iid, (record, path) in desired.items():
                parent = root
                for name in path:
                    folder = next(
                        (
                            parent.child(r)
                            for r in range(parent.rowCount())
                            if parent.child(r).text() == name
                            and getattr(parent.child(r), "device", None) is None
                        ),
                        None,
                    )
                    if folder is None:
                        folder = CustomTreeItem(
                            name,
                            checkable=self.show_checkboxes,
                            show_columns=self.show_columns,
                        )
                        parent.appendRow(folder.row_items())
                    parent = folder
                item = existing.get(iid)
                if item is None:
                    device = DeviceModel(record)
                    if self.show_nodes:
                        device.node_iid = LOCAL_NODE_IID
                    item = CustomTreeItem(
                        device.name,
                        device,
                        checkable=self.show_checkboxes,
                        show_columns=self.show_columns,
                    )
                    parent.appendRow(item.row_items())
                    continue
                incoming = DeviceModel(record)
                fields = (
                    "name",
                    "host",
                    "port",
                    "credential_bundle_iid",
                    "mac_address",
                    "vnc_port",
                    "thumbnail_backend",
                    "use_sudo",
                    "locale",
                )
                if (
                    all(
                        getattr(item.device, field) == getattr(incoming, field)
                        for field in fields
                    )
                    and (item.parent() or root) is parent
                ):
                    continue
                item.device.update(incoming.to_dict())
                if (item.parent() or root) is not parent:
                    row = (item.parent() or root).takeRow(item.row())
                    parent.appendRow(row)
                item.setText(item.device.name)
                for column, cell in enumerate(item.make_extra_columns(), 1):
                    parent.setChild(item.row(), column, cell)
                updated.append(item.device)
        for device in removed:
            self.device_removed.emit(device)
        for device in updated:
            self.device_updated.emit(device)
        self._refilter()

    def _find_tree_item_by_host(self, host: str, parent=None):
        """Рекурсивный поиск элемента дерева по хосту (fallback)"""
        if parent is None:
            parent = self._model.invisibleRootItem()
        for i in range(parent.rowCount()):
            child = parent.child(i)
            if hasattr(child, "device") and child.device and child.device.host == host:
                return child
            if child.hasChildren():
                result = self._find_tree_item_by_host(host, child)
                if result:
                    return result
        return None

    # ----- Ноды (федерация) -------------------------------------------------

    def get_node_section_item(self, node_iid: str) -> Optional[CustomTreeItem]:
        """Найти верхнеуровневый элемент секции ноды по iid."""
        if not node_iid:
            return None
        root = self._model.invisibleRootItem()
        for i in range(root.rowCount()):
            child = root.child(i)
            if (
                isinstance(child, CustomTreeItem)
                and bool(child.data(NODE_SECTION_DATAROLE))
                and child.data(DEVICE_NODE_DATAROLE) == node_iid
            ):
                return child
        return None

    def get_or_create_node_section(self, node_iid: str, name: str) -> CustomTreeItem:
        """Получить или создать секцию ноды в дереве.

        При show_nodes=False секции удалённых нод не добавляются в модель —
        создаются в стэше _hidden_node_sections (poll-циклы продолжают
        обновлять их содержимое через get_node_section_item).
        """
        existing = self.get_node_section_item(node_iid)
        if existing is not None:
            return existing
        # Уже в стэше скрытых — вернуть оттуда (обновить имя, не дублируя)
        for s in self._hidden_node_sections:
            if s.data(DEVICE_NODE_DATAROLE) == node_iid:
                if node_iid != LOCAL_NODE_IID and name:
                    s.setText(name)
                return s
        section = CustomTreeItem.make_node_section(node_iid, name or "Нода")
        if (
            node_iid == LOCAL_NODE_IID
            or self.show_nodes
            or not hasattr(self, "_hidden_node_sections")
        ):
            self._model.invisibleRootItem().appendRow([section])
        else:
            self._hidden_node_sections.append(section)
        return section

    def remove_node_section(self, node_iid: str) -> bool:
        """Удалить секцию ноды из дерева (со всеми хостами)."""
        item = self.get_node_section_item(node_iid)
        if item is None:
            # Может лежать в стэше скрытых (переключатель «Ноды» выключен)
            self._hidden_node_sections = [
                s
                for s in self._hidden_node_sections
                if s.data(DEVICE_NODE_DATAROLE) != node_iid
            ]
            return False
        root = self._model.invisibleRootItem()
        root.removeRow(item.row())
        return True

    def update_node_section_hosts(self, node_iid: str, hosts_tree: list) -> int:
        """Перестроить содержимое секции ноды из дерева хостов.

        hosts_tree — формат get_devices_raw (список групп/устройств).
        Хосты получают device.node_iid=node_iid. Сохранённые чекбоксы
        восстанавливаются по старым iid (переиспользуем DeviceModel).
        Возвращает количество устройств в секции.
        """
        section = self.get_or_create_node_section(node_iid, "")
        # Собираем старые checked-устройства этой ноды по iid
        checked_by_iid: dict = {}
        old_state: dict = {}  # iid -> DeviceModel (для reuse и переноса чеков)
        # Раскрытость папок ноды (по пути в её дереве) — иначе после каждого
        # poll-цикла (обновление дерева) сворачивается то, что раскрыл оператор.
        expanded_by_path: set = set()
        # Чекбоксы папок ноды (по пути) — состояние сохраняется между
        # poll-циклами (каскад на детей делает setData модели при клике).
        checked_folders_by_path: dict = {}

        def _collect(item, path):
            for i in range(item.rowCount()):
                ch = item.child(i)
                ch_path = path + [ch.text()]
                if hasattr(ch, "device") and ch.device is not None:
                    old_state[ch.device.iid] = ch.device
                    if ch.checkState() == Qt.CheckState.Checked:
                        checked_by_iid[ch.device.iid] = True
                else:
                    # Папка: фиксируем путь, если раскрыта + чекбокс
                    idx = ch.index()
                    if idx.isValid() and self.isExpanded(idx):
                        expanded_by_path.add(tuple(ch_path))
                    if ch.isCheckable():
                        checked_folders_by_path[tuple(ch_path)] = ch.checkState()
                if ch.hasChildren():
                    _collect(ch, ch_path)

        _collect(section, [])
        section.removeRows(0, section.rowCount())

        # Рекурсивная загрузка (по образцу _load_group_items), с node_iid
        added = [0]
        updated_devices = []
        present_iids = set()

        def _load(parent_item, data, path_parts):
            for entry in data or []:
                if not isinstance(entry, dict):
                    continue
                if "name" in entry and "host" in entry:
                    # Устройство
                    device = DeviceModel(entry)
                    device.node_iid = node_iid
                    # Переиспользуем старую модель, если iid совпал
                    # (статусы/тултипы/иконки сохраняются).
                    # Fallback: дерево с сервера может прийти без iid
                    # (старый dno-tool на стороне ноды) — матчим по
                    # (host, name), иначе чекбоксы сбрасываются каждым
                    # poll-циклом hosts.
                    if not entry.get("iid") and device.iid not in old_state:
                        matched = None
                        for od in old_state.values():
                            if od.host == device.host and od.name == device.name:
                                matched = od
                                break
                        if matched is not None:
                            device._iid = matched.iid
                    present_iids.add(device.iid)
                    old_dev = old_state.get(device.iid)
                    if old_dev is not None:
                        # A full owner-node snapshot also clears omitted overrides.
                        update = device.to_dict()
                        update["thumbnail_backend"] = device.thumbnail_backend
                        old_dev.update(update)
                        old_dev.node_iid = node_iid
                        device = old_dev
                        # Карточке нужен новый tree_item даже при тех же полях
                        # устройства: папка могла измениться, старые строки удалены.
                        updated_devices.append(device)
                    device_item = CustomTreeItem(
                        device.name,
                        device,
                        checkable=self.show_checkboxes,
                        show_columns=self.show_columns,
                    )
                    device_item.setData(False, FOLDER_DATAROLE)
                    device_item.setData(node_iid, DEVICE_NODE_DATAROLE)
                    if self.show_checkboxes:
                        state = (
                            Qt.CheckState.Checked
                            if checked_by_iid.get(device.iid)
                            else Qt.CheckState.Unchecked
                        )
                        device_item.setCheckState(state)
                    parent_item.appendRow(device_item.row_items())
                    added[0] += 1
                else:
                    for key, value in entry.items():
                        # Папка ноды — чекбокс как у локальных папок
                        # (каскад через CustomTreeItemModel.setData работает).
                        # Состояние чекбокса сохраняется между poll-циклами:
                        # сбор ниже (checked_folders_by_path) + восстановление.
                        group_item = CustomTreeItem(
                            key,
                            checkable=self.show_checkboxes,
                            show_columns=False,
                        )
                        group_item.setData(True, FOLDER_DATAROLE)
                        group_item.setData(node_iid, DEVICE_NODE_DATAROLE)
                        if self.show_checkboxes:
                            path_key = tuple(path_parts + [key])
                            if path_key in checked_folders_by_path:
                                group_item.setCheckState(
                                    checked_folders_by_path[path_key]
                                )
                        parent_item.appendRow(group_item.row_items())
                        _load(group_item, value, path_parts + [key])
                        # Восстановить раскрытость папки (после заполнения детей)
                        if tuple(path_parts + [key]) in expanded_by_path:
                            self.expand(group_item.index())

        try:
            _load(section, hosts_tree or [], [])
        except Exception as e:
            logger.error(f"DeviceTreeView: update_node_section_hosts {node_iid}: {e}")
        # Фильтр поиска: перестроенная секция может не совпадать с фильтром
        if self._filter_text:
            self._refilter()
        for iid in old_state.keys() - present_iids:
            self.device_removed.emit(old_state[iid])
        # Сигналы публикуются после построения секции.
        for device in updated_devices:
            self.device_updated.emit(device)
        return added[0]

    def set_node_reachable(self, node_iid: str, reachable: bool) -> None:
        """Обновить индикатор доступности секции ноды."""
        item = self.get_node_section_item(node_iid)
        if item is not None:
            item.set_node_reachable(reachable)

    def refresh_icons(self) -> None:
        """Перечитать иконки дерева под текущую тему иконок.

        Секции нод - по сохранённому статусу связи; папки - свежий
        ICONS["folder"]; у устройств абсолютные пути иконок (device.icon,
        _icon_online/_icon_offline, персистятся в hosts.json) ремаппятся
        на текущую тему через remap_icon_path, затем item.update().
        """
        from src.config import remap_icon_path

        def remap_device(device) -> None:
            try:
                device.icon = remap_icon_path(getattr(device, "icon", ""))
                if hasattr(device, "_icon_online"):
                    device._icon_online = remap_icon_path(device._icon_online)
                if hasattr(device, "_icon_offline"):
                    device._icon_offline = remap_icon_path(device._icon_offline)
            except Exception:
                pass

        def walk(parent) -> None:
            try:
                count = parent.rowCount()
            except Exception:
                return
            for row in range(count):
                try:
                    child = parent.child(row)
                except Exception:
                    continue
                if child is None:
                    continue
                try:
                    if getattr(child, "_is_node_section", False):
                        child.refresh_node_icon()
                    elif getattr(child, "device", None) is not None:
                        remap_device(child.device)
                        if hasattr(child, "update"):
                            child.update()
                    elif hasattr(child, "update"):
                        child.update()  # папка: свежий ICONS["folder"]
                except Exception:
                    pass
                try:
                    if child.hasChildren():
                        walk(child)
                except Exception:
                    pass

        try:
            # Массовое обновление иконок (сотни item.update) без шторма
            # itemChanged: каждый setIcon иначе триггерит on_tree_item_changed
            # с каскадом setIcon вверх и рестартом сейв-таймера на событие.
            # Сигналы чекбоксов не затрагиваются (CheckStateRole не меняется).
            self._model.blockSignals(True)
            try:
                walk(self._model.invisibleRootItem())
            finally:
                self._model.blockSignals(False)
        except Exception:
            pass
        try:
            self.viewport().update()
        except Exception:
            pass

    def node_section_check_all(
        self, node_iid: str, checked: bool, root_item=None
    ) -> None:
        """Отметить/снять все хосты ноды (или конкретной папки ноды).

        Эквивалент ручного клика по каждому чекбоксу: добавляет/удаляет
        устройства в списке (on_tree_ItemDataChanged → add_to_listview /
        remove_from_listview). Вызывается из контекстного меню секции/папки
        ноды — у секции чекбокса нет.
        """
        if root_item is None:
            root_item = self.get_node_section_item(node_iid)
        if root_item is None:
            return

        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked

        # Через модель: триггерит CustomTreeItemModel.setData →
        # itemDataChanged → add/remove_from_listview (список наполняется).
        model = self._model

        def _walk(item):
            for i in range(item.rowCount()):
                ch = item.child(i)
                if hasattr(ch, "device") and ch.device is not None:
                    if ch.checkState() != state:
                        model.setData(ch.index(), state, Qt.ItemDataRole.CheckStateRole)
                if ch.hasChildren():
                    _walk(ch)

        _walk(root_item)
        # Чекбокс папки-корня (если отметка из ПКМ папки) — тоже каскадом
        if (
            root_item is not None
            and root_item.isCheckable()
            and root_item.checkState() != state
        ):
            model.setData(root_item.index(), state, Qt.ItemDataRole.CheckStateRole)

    # ------------------------------------------------------------------------

    def setup_ui(self):
        if self.show_columns:
            self.setHeaderHidden(False)
            self._model.setHorizontalHeaderLabels(
                ["Имя", "Адрес", "Связка", "Sudo", "MAC"]
            )
            header = self.header()
            from PySide6.QtWidgets import QHeaderView

            header.setStretchLastSection(False)
            header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(COL_HOST, QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(COL_BUNDLE, QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(COL_SUDO, QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(COL_MAC, QHeaderView.ResizeMode.Interactive)
            self._col_widths = {
                COL_NAME: 180,
                COL_HOST: 100,
                COL_BUNDLE: 140,
                COL_SUDO: 80,
                COL_MAC: 140,
            }
        else:
            self.setHeaderHidden(True)
            self._col_widths = None
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def showEvent(self, event):
        super().showEvent(event)
        if self.show_columns and self._col_widths:
            for col, width in self._col_widths.items():
                self.header().resizeSection(col, width)

    def add_device(self, parent: CustomTreeItem, device: DeviceModel):
        if not parent.hasChildren():
            parent.setData(True, FOLDER_DATAROLE)
            parent.device = None
            parent.setIcon(_icon_from_key("folder"))

        device_item = CustomTreeItem(
            device.name,
            device,
            checkable=self.show_checkboxes,
            show_columns=self.show_columns,
        )
        if self.show_checkboxes:
            device_item.setCheckState(Qt.CheckState.Unchecked)
        device_item.setIcon(_icon_from_key("default"))
        parent.appendRow(device_item.row_items())

        return device_item

    def add_devices_at_selected_folder(
        self, devices, folder=None, *, deduplicate=False,
        added_devices: Optional[list[DeviceModel]] = None, dialog_parent=None,
    ) -> int:
        """Добавить список устройств в выбранную папку (или в folder, или в root).

        Если folder is None — берётся текущая выделенная папка в дереве
        (элемент без device). Если ничего не выделено — добавляется в корень.
        Не сортирует дерево: новые хосты дописываются в конец папки,
        раскрытость остальных веток не трогается (sort_tree делал
        takeRow/appendRow и схлопывал дерево). Возвращает количество
        добавленных.

        Используется мастером первоначальной настройки и MainWindow для
        обработки импорта устройств из IPScannerDialog — устраняет дублирование
        логики разрешения целевой папки.
        При deduplicate показывает список совпадений с выбором повторных копий.
        В added_devices записывает только фактически добавленные исходные хосты.
        """
        current_folder = folder
        if current_folder is None:
            indexes = self.selectedIndexes()
            if indexes:
                item = self._model.itemFromIndex(indexes[0])
                if item is not None and (
                    not hasattr(item, "device") or item.device is None
                ):
                    current_folder = item

        root = self._model.invisibleRootItem()
        probe = current_folder
        while probe is not None and probe is not root:
            if bool(probe.data(NODE_SECTION_DATAROLE)) or (
                getattr(getattr(probe, "device", None), "node_iid", None)
                not in (None, "local")
            ):
                QMessageBox.warning(self, "Импорт", "Выберите локальную папку для импорта.")
                return 0
            probe = probe.parent()
        if current_folder is not None and (
            current_folder.model() is not self._model
            or getattr(current_folder, "device", None) is not None
        ):
            QMessageBox.warning(self, "Импорт", "Выберите папку для импорта.")
            return 0

        from copy import deepcopy
        from uuid import uuid4
        endpoints = {}
        default_port = config.app.ssh.port

        def endpoint(device):
            return device.host.strip().casefold(), device.port or default_port

        def folder_path(item):
            parts = []
            while item is not None and item is not root:
                parts.append(item.text())
                item = item.parent()
            return " / ".join(reversed(parts)) or "Корень"

        def collect(item):
            for i in range(item.rowCount()):
                child = item.child(i)
                if bool(child.data(NODE_SECTION_DATAROLE)) and (
                    child.data(DEVICE_NODE_DATAROLE) != LOCAL_NODE_IID
                ):
                    continue
                device = getattr(child, "device", None)
                if device is not None:
                    endpoints.setdefault(endpoint(device), []).append(folder_path(item))
                collect(child)
        devices = list(devices)
        duplicates = []
        duplicate_indices = set()
        allowed_duplicates = set()
        if deduplicate:
            collect(root)
            known = dict(endpoints)
            for index, device in enumerate(devices):
                target = endpoint(device)
                if target in known:
                    duplicate_indices.add(index)
                    duplicates.append((
                        index, device.name or device.host,
                        f"{device.host} : {target[1]}",
                        "; ".join(dict.fromkeys(known[target])),
                    ))
                else:
                    known[target] = [f"{folder_path(current_folder)} (в этом импорте)"]
            if duplicates:
                from PySide6.QtWidgets import QDialog
                from src.ui.dialogs.common.scanner_duplicates import ScannerDuplicatesDialog

                dialog = ScannerDuplicatesDialog(
                    duplicates, len(devices) - len(duplicates),
                    folder_path(current_folder), dialog_parent or self,
                )
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return 0
                allowed_duplicates = dialog.selected_indices()
        added = 0
        for index, device in enumerate(devices):
            if index in duplicate_indices and index not in allowed_duplicates:
                continue
            snapshot = deepcopy(device)
            snapshot._iid = uuid4().hex
            self.add_device(current_folder or root, snapshot)
            if added_devices is not None:
                added_devices.append(device)
            added += 1

        # Целевую папку раскрыть и показать (импорт из сканера),
        # остальное дерево не трогаем.
        if current_folder is not None:
            try:
                idx = current_folder.index()
                if idx.isValid():
                    self.expand(idx)
                    self.scrollTo(idx)
            except Exception:
                pass
        self._refilter()
        return added

    def get_device_paths(self) -> dict[DeviceKey, str]:
        """Получить снимок путей хостов, включая группы и видимые секции нод."""
        paths: dict[DeviceKey, str] = {}

        def collect(parent: QStandardItem, parts: tuple[str, ...]) -> None:
            for row in range(parent.rowCount()):
                item = parent.child(row, 0)
                if item is None:
                    continue
                current = (*parts, item.text())
                device = getattr(item, "device", None)
                if device is not None:
                    paths[device_identity(device)] = " → ".join(current)
                collect(item, current)

        collect(self._model.invisibleRootItem(), ())
        return paths

    def get_all_checked_devices(self):
        """Рекурсивно возвращает все отмеченные устройства (DeviceModel) в дереве.

        Хосты удалённых нод (device.node_iid != LOCAL_NODE_IID) не включаются —
        их статусы приходят по сети от ноды-владельца, локальный пинг не нужен.
        Хосты локальной ноды (LOCAL_NODE_IID) — обычные локальные устройства.
        """
        checked_devices = []

        def collect_checked(item):
            for i in range(item.rowCount()):
                child = item.child(i)
                if (
                    hasattr(child, "device")
                    and child.device
                    and child.checkState() == Qt.CheckState.Checked
                ):
                    # device.node_iid пуст или LOCAL_NODE_IID → локальное
                    niid = getattr(child.device, "node_iid", None)
                    if niid and niid != LOCAL_NODE_IID:
                        continue
                    checked_devices.append(child.device)
                if child.hasChildren():
                    collect_checked(child)

        root = self._model.invisibleRootItem()
        collect_checked(root)
        return checked_devices

    def get_selected_devices(self):
        """Возвращает список всех выбранных устройств (не папок) в текущем выделении"""
        selected_items = []
        for index in self.selectedIndexes():
            item = self._model.itemFromIndex(index)
            if item and hasattr(item, "device") and item.device is not None:
                selected_items.append(item)
        return selected_items

    def get_selected_items(self):
        """Возвращает список всех выбранных элементов (устройства и папки) в текущем выделении"""
        selected_items = []
        seen = set()
        for index in self.selectedIndexes():
            item = self._model.itemFromIndex(index)
            if item and id(item) not in seen:
                selected_items.append(item)
                seen.add(id(item))
        return selected_items

    def clear_devices(self):
        """Очистка всех устройств с остановкой таймеров"""
        get_host_ping_timer_manager().stop_all()
        self._model.clear()

    def on_item_double_clicked(self, index):
        item = self._model.itemFromIndex(index)
        if item and hasattr(item, "device"):
            self.itemDoubleClicked.emit(item)

    def add_device_to_group(self, parent_item):
        """Добавление нового устройства в группу"""
        from src.ui.dialogs.common.common import DeviceEditDialog

        success, device = DeviceEditDialog.add_device(self)
        if success:
            device_item = CustomTreeItem(
                device.name,
                device,
                checkable=self.show_checkboxes,
                show_columns=self.show_columns,
            )
            if self.show_checkboxes:
                device_item.setCheckState(Qt.CheckState.Unchecked)
            device_item.setIcon(qicon_from_path(device.icon))
            if parent_item is None:
                parent_item = self._model.invisibleRootItem()
            parent_item.appendRow(device_item.row_items())

            new_data = self.get_tree_data()
            self.data_changed.emit(new_data)  # ← Эмитим сигнал вместо вызова datastore

    def add_ip_range_to_group(self, parent_item):
        """Добавление диапазона IP-адресов в группу"""
        from src.ui.dialogs.common.ip_range import IpRangeDialog

        success, devices = IpRangeDialog.add_ip_range(self)
        if success and devices:
            if parent_item is None:
                parent_item = self._model.invisibleRootItem()
            for device in devices:
                device_item = CustomTreeItem(
                    device.name,
                    device,
                    checkable=self.show_checkboxes,
                    show_columns=self.show_columns,
                )
                if self.show_checkboxes:
                    device_item.setCheckState(Qt.CheckState.Unchecked)
                device_item.setIcon(_icon_from_key("default"))
                parent_item.appendRow(device_item.row_items())

            new_data = self.get_tree_data()
            self.data_changed.emit(new_data)

    def add_folder_to_group(self, parent_item):
        """Добавление новой папки в группу"""
        if parent_item is None:
            parent_item = self._model.invisibleRootItem()

        # Создаем диалог для ввода имени папки
        folder_name, ok = QInputDialog.getText(
            self, "Новая папка", "Введите имя папки:", QLineEdit.Normal, "Новая папка"
        )

        if ok and folder_name:
            folder_item = CustomTreeItem(
                folder_name,
                checkable=self.show_checkboxes,
                show_columns=self.show_columns,
            )
            folder_item.setIcon(_icon_from_key("folder"))
            parent_item.appendRow(folder_item.row_items())
            return folder_item

        return None

    def edit_device(self, item):
        """Редактирование существующего устройства"""
        from src.ui.dialogs.common.common import DeviceEditDialog

        if hasattr(item, "device"):
            device: DeviceModel = item.device
            success, updated_data = DeviceEditDialog.edit_device(device, self)
            if success:
                device.update(updated_data)
                item.setText(device.name)
                item.update()
                # Уведомить подписчиков (список отмеченных) об изменении устройства
                self.device_updated.emit(device)
                # Сохранить изменения в datastore
                new_data = self.get_tree_data()
                self.data_changed.emit(new_data)

    def remove_from_treeview(self, item: CustomTreeItem):
        """Удаление элемента из дерева (для обратной совместимости)"""
        self.remove_selected_items([item])

    def remove_selected_items(self, items: list):
        """Удаление списка выбранных элементов из дерева"""
        if not items:
            return

        if (
            QMessageBox.question(
                self,
                "Удаление элементов",
                f"Вы уверены, что хотите удалить {len(items)} элемент(ов)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.No
        ):
            return

        # Собираем устройства для эмита сигнала (включая вложенные в папки)
        devices_to_remove = []

        def collect_devices(item):
            """Рекурсивно собирает все устройства из элемента"""
            if hasattr(item, "device") and item.device:
                devices_to_remove.append(item.device)
            for i in range(item.rowCount()):
                child = item.child(i)
                collect_devices(child)

        for item in items:
            collect_devices(item)

        # Эмитим сигналы для всех устройств
        for device in devices_to_remove:
            self.device_removed.emit(device)

        # Группируем элементы по родителю для корректного удаления
        items_by_parent = defaultdict(list)
        for item in items:
            parent = item.parent()
            items_by_parent[id(parent)].append((parent, item.row()))

        # Удаляем элементы (с конца, чтобы не сбить индексы)
        for parent_id, parent_rows in items_by_parent.items():
            for parent, row in sorted(parent_rows, key=lambda x: x[1], reverse=True):
                if parent is not None:
                    parent.removeRow(row)
                else:
                    self._model.removeRow(row)

        self.clearSelection()
        new_data = self.get_tree_data()
        self.data_changed.emit(new_data)

    def move_selected_to_folder(self, items: list, target_folder: CustomTreeItem):
        """Перемещение списка выбранных элементов (устройств и/или папок) в целевую папку"""
        if not items or not target_folder:
            return

        # Убедимся, что целевой элемент - папка
        if not target_folder.data(FOLDER_DATAROLE):
            return

        # Потомки выбранной папки перемещаются вместе с ней, строки и
        # дополнительные колонки не должны обрабатываться повторно.
        candidates = {
            id(item): item for item in items
            if isinstance(item, CustomTreeItem) and item.model() is self._model
        }
        items = []
        for item in candidates.values():
            parent = item.parent()
            while parent is not None and id(parent) not in candidates:
                parent = parent.parent()
            if parent is None:
                items.append(item)

        # Защита от циклов: цель не должна быть одним из перемещаемых элементов
        # или его потомком (перемещение папки в саму себя / в свою подпапку).
        for item in items:
            if item is target_folder:
                return
            node = target_folder.parent()
            while node is not None:
                if node is item:
                    return
                node = node.parent()

        # Собираем устройства для уведомления подписчиков (до изменения структуры).
        # Для папок рекурсивно собираем все DeviceModel поддерева — иначе список
        # отмеченных не получит уведомления об изменении категории для устройств,
        # находившихся внутри перемещённой папки. Элементы перемещаются без
        # пересоздания, поэтому DeviceModel остаются валидными после move.
        moved_devices = []
        for item in items:
            if bool(item.data(FOLDER_DATAROLE)):
                moved_devices.extend(self._model._collect_descendant_devices(item))
            else:
                dev = getattr(item, "device", None)
                if dev is not None:
                    moved_devices.append(dev)

        # Перемещаем элементы (с конца, чтобы не сбить индексы)
        for item in sorted(items, key=lambda x: x.row(), reverse=True):
            # takeRow передаёт владение всей строкой без уничтожения Qt-объекта
            # и сохраняет DeviceModel, дочерние элементы и дополнительные колонки.
            parent = item.parent() or self._model.invisibleRootItem()
            row_items = parent.takeRow(item.row())
            target_folder.appendRow(row_items)

        self.clearSelection()
        new_data = self.get_tree_data()
        self.data_changed.emit(new_data)
        # Уведомить подписчиков (список отмеченных) об изменении категории устройств
        for device in moved_devices:
            self.device_updated.emit(device)

    def show_tree_context_menu(self, position):
        """Показать контекстное меню для элемента дерева"""

        if self.read_only:
            return

        menu = QMenu()

        item: CustomTreeItem = None
        index = self.indexAt(position)

        if index.isValid():
            item: CustomTreeItem = self._model.itemFromIndex(index)

        if item is None:
            item = self._model.invisibleRootItem()

        # ----- Секция ноды: ограниченное меню -----
        if bool(item.data(NODE_SECTION_DATAROLE)):
            node_iid = item.data(DEVICE_NODE_DATAROLE)
            is_local = node_iid == LOCAL_NODE_IID
            # Доступность ноды: меню секции зависит от связи — на оффлайновой
            # ноде операции через ноду («Добавить», «Обновить») не работают.
            node_reachable = True
            if not is_local:
                try:
                    from src.di import get_container
                    from src.services import NodeService

                    _svc = get_container().resolve_optional(NodeService)
                    node_reachable = _svc is not None and _svc.is_reachable(node_iid)
                except Exception:
                    node_reachable = False
            if is_local:
                add_action = menu.addAction(
                    _icon_from_key("menu_add_device"), "Добавить устройство"
                )
                # Локальная нода: стандартное добавление устройства в секцию.
                add_action.triggered.connect(lambda: self.add_device_to_group(item))
            if is_local or node_reachable:
                refresh_action = menu.addAction(
                    _icon_from_key("menu_refresh"), "Обновить список хостов"
                )
                refresh_action.triggered.connect(
                    lambda: self.node_refresh_requested.emit(node_iid)
                )
            check_all_action = menu.addAction(
                _icon_from_key("menu_check_all"), "Отметить все хосты"
            )
            check_all_action.triggered.connect(
                lambda: self.node_section_check_all(node_iid, True)
            )
            uncheck_all_action = menu.addAction(
                _icon_from_key("menu_uncheck_all"), "Снять отметку со всех хостов"
            )
            uncheck_all_action.triggered.connect(
                lambda: self.node_section_check_all(node_iid, False)
            )
            menu.addSeparator()
            if not is_local:
                hide_action = menu.addAction(_icon_from_key("menu_hide"), "Скрыть ноду")
                hide_action.triggered.connect(
                    lambda: self.node_hide_requested.emit(node_iid)
                )
                remove_action = menu.addAction(
                    _icon_from_key("menu_delete"), "Удалить ноду из настроек…"
                )
                remove_action.triggered.connect(
                    lambda: self.node_remove_requested.emit(node_iid)
                )
            menu.exec_(self.viewport().mapToGlobal(position))
            menu.deleteLater()
            return

        # Папка внутри секции ноды: отметить/снять отметку с хостов папки
        folder_node_iid = item.data(DEVICE_NODE_DATAROLE)
        if (
            folder_node_iid
            and folder_node_iid != LOCAL_NODE_IID
            and item.data(FOLDER_DATAROLE)
            and not bool(item.data(NODE_SECTION_DATAROLE))
            and item.hasChildren()  # CustomTreeItem(QStandardItem): hasChildren(), не childCount()
        ):
            chk_all = menu.addAction(
                _icon_from_key("menu_check_all"), "Отметить все хосты"
            )
            chk_all.triggered.connect(
                lambda: self.node_section_check_all(folder_node_iid, True, item)
            )
            unchk_all = menu.addAction(
                _icon_from_key("menu_uncheck_all"), "Снять отметку со всех хостов"
            )
            unchk_all.triggered.connect(
                lambda: self.node_section_check_all(folder_node_iid, False, item)
            )
            menu.exec_(self.viewport().mapToGlobal(position))
            menu.deleteLater()
            return

        # Хост внутри секции ноды: меню действий на ноде-владельце
        item_node_iid = getattr(getattr(item, "device", None), "node_iid", None)
        if item_node_iid and item_node_iid != LOCAL_NODE_IID:
            dev = item.device
            node_reachable = True
            try:
                from src.di import get_container
                from src.services import NodeService

                _svc = get_container().resolve_optional(NodeService)
                node_reachable = _svc is not None and _svc.is_reachable(item_node_iid)
            except Exception:
                node_reachable = False
            if not node_reachable:
                # Нода недоступна: операции через ноду (CRUD, свойства)
                # не работают — только заглушка.
                stub = menu.addAction("Нода недоступна")
                stub.setEnabled(False)
                menu.exec_(self.viewport().mapToGlobal(position))
                menu.deleteLater()
                return
            edit_action = menu.addAction(
                _icon_from_key("menu_settings"), "Редактировать"
            )
            edit_action.triggered.connect(
                lambda: self.node_device_edit_requested.emit(item_node_iid, dev)
            )
            menu.addSeparator()
            del_action = menu.addAction(_icon_from_key("menu_delete"), "Удалить")
            del_action.triggered.connect(
                lambda: self.node_device_delete_requested.emit(item_node_iid, dev)
            )
            menu.exec_(self.viewport().mapToGlobal(position))
            menu.deleteLater()
            return

        # Определяем целевую папку для добавления
        if item.data(FOLDER_DATAROLE):
            target_item = item
        else:
            target_item = item.parent()

        # Получаем выбранные устройства
        selected_devices = self.get_selected_devices()
        has_selection = len(selected_devices) > 0

        # Если нет выделения, но клик по устройству - используем это устройство
        if (
            not has_selection
            and item
            and hasattr(item, "device")
            and item.device is not None
        ):
            selected_devices = [item]
            has_selection = True

        # Add device action
        add_device_action = menu.addAction(
            _icon_from_key("menu_add_device"), "Добавить устройство"
        )
        add_device_action.triggered.connect(
            lambda: self.add_device_to_group(target_item)
        )

        add_ip_range_action = menu.addAction(
            _icon_from_key("menu_add_ip_range"), "Добавить диапазон IP"
        )
        add_ip_range_action.triggered.connect(
            lambda: self.add_ip_range_to_group(target_item)
        )

        add_folder_action = menu.addAction(
            _icon_from_key("menu_folder"), "Добавить папку"
        )
        add_folder_action.triggered.connect(
            lambda: self.add_folder_to_group(target_item)
        )

        menu.addSeparator()

        sort_action = menu.addAction(_icon_from_key("menu_sort"), "Сортировать")
        sort_action.triggered.connect(lambda: self.sort_current_group(target_item))

        menu.addSeparator()

        if item.data(FOLDER_DATAROLE):
            rename_folder_action = menu.addAction(
                _icon_from_key("menu_folder"), "Переименовать папку"
            )
            rename_folder_action.triggered.connect(
                lambda: self.rename_folder_action(item)
            )

        selected_items = self.get_selected_items()
        has_selection = len(selected_items) > 0

        if not has_selection and index.isValid():
            selected_items = [item]
            has_selection = True

        if has_selection:
            selected_devices = [
                it
                for it in selected_items
                if hasattr(it, "device") and it.device is not None
            ]

            if len(selected_devices) == 1 and len(selected_items) == 1:
                if selected_devices[0].device:
                    edit_action = menu.addAction(
                        _icon_from_key("menu_settings"), "Редактировать"
                    )
                    edit_action.triggered.connect(
                        lambda: self.edit_device(selected_devices[0])
                    )

            remove_text = (
                f"Удалить ({len(selected_items)})"
                if len(selected_items) > 1
                else "Удалить"
            )
            remove_action = menu.addAction(_icon_from_key("menu_delete"), remove_text)
            remove_action.triggered.connect(
                lambda: self.remove_selected_items(selected_items)
            )

        menu.exec_(self.viewport().mapToGlobal(position))
        menu.deleteLater()

    def _show_move_dialog(self, items: list):
        """Показать диалог для перемещения устройств в выбранную папку"""
        from PySide6.QtWidgets import QInputDialog

        # Собираем все папки из дерева
        folders = self._get_all_folders()
        if not folders:
            QMessageBox.information(
                self, "Перемещение", "Нет доступных папок для перемещения."
            )
            return

        # Показываем диалог выбора папки
        folder_names = [f[0] for f in folders]
        folder_name, ok = QInputDialog.getItem(
            self, "Переместить устройства", "Выберите папку:", folder_names, 0, False
        )

        if ok and folder_name:
            # Находим папку по имени
            target_folder = None
            for folder_path, folder_item in folders:
                if folder_path == folder_name:
                    target_folder = folder_item
                    break

            if target_folder:
                self.move_selected_to_folder(items, target_folder)

    def _get_all_folders(self):
        """Рекурсивно собирает все папки из дерева"""
        folders = []

        def collect_folders(item, path=""):
            for i in range(item.rowCount()):
                child = item.child(i)
                if child.data(FOLDER_DATAROLE):
                    folder_path = f"{path}/{child.text()}" if path else child.text()
                    folders.append((folder_path, child))
                    if child.hasChildren():
                        collect_folders(child, folder_path)

        root = self._model.invisibleRootItem()
        collect_folders(root)
        return folders

    def startDrag(self, supportedActions):
        # Read-only деревья (мастер, диагностика): drag стартовать бессмысленно —
        # dropMimeData модели всё равно отклоняет (return False), а жест
        # выглядит как разрешённый.
        if self.read_only:
            return
        # Получаем все выбранные индексы
        selected_indexes = self.selectedIndexes()
        if not selected_indexes:
            return

        # Сохраняем ссылки на перетаскиваемые элементы.
        # Фильтруем только CustomTreeItem (колонка 0) — при show_columns=True
        # selectedIndexes возвращает до 5 индексов на строку (по колонкам), а
        # placeholder-ячейки (QStandardItem) не должны перетаскиваться.
        # Дедуплицируем по id(item) и пропускаем потомков уже выбранных предков
        # (если выбрана папка и её дочерний элемент, тащим только папку —
        # иначе dropMimeData удалит папку и попытается удалить дочерний элемент
        # из уничтоженного родителя → RuntimeError + дубликат/потеря).
        global _drag_source_items
        seen_ids = set()
        candidate_items = []
        for idx in selected_indexes:
            item = self.model().itemFromIndex(idx)
            if not isinstance(item, CustomTreeItem):
                continue
            if id(item) in seen_ids:
                continue
            # Секции нод: запрещаем перетаскивать саму секцию и любые её
            # элементы (хосты ноды принадлежат ноде-владельцу, не перемещаются
            # в локальное дерево). Локальная нода — исключение: её содержимое
            # — обычные локальные хосты (drag-аемы как раньше).
            if bool(item.data(NODE_SECTION_DATAROLE)):
                if item.data(DEVICE_NODE_DATAROLE) != LOCAL_NODE_IID:
                    continue
                # Саму локальную секцию не тащим.
                if item.is_node_section:
                    continue
            niid = item.data(DEVICE_NODE_DATAROLE)
            if niid and niid != LOCAL_NODE_IID:
                continue
            if (
                getattr(getattr(item, "device", None), "node_iid", None)
                and getattr(getattr(item, "device", None), "node_iid", None)
                != LOCAL_NODE_IID
            ):
                continue
            seen_ids.add(id(item))
            candidate_items.append(item)

        # Пропускаем элементы, у которых среди candidate_items есть предок:
        # если тащить одновременно папку и её ребёнка, ребёнок переместится
        # вместе с папкой автоматически — отдельная операция над ним приведёт
        # к RuntimeError при удалении из уже уничтоженного родителя.
        def _has_selected_ancestor(item, items_set):
            parent = item.parent()
            while parent is not None:
                if id(parent) in items_set:
                    return True
                parent = parent.parent()
            return False

        items_set = {id(it) for it in candidate_items}
        _drag_source_items = [
            it for it in candidate_items if not _has_selected_ancestor(it, items_set)
        ]
        if not _drag_source_items:
            return

        # Разрешаем перетаскивание всех выбранных элементов
        drag_source_indexes = [
            self.model().indexFromItem(it) for it in _drag_source_items
        ]
        mimeData = self.model().mimeData(drag_source_indexes)
        drag = QDrag(self)
        drag.setMimeData(mimeData)

        # Визуализация перетаскиваемого элемента (первый элемент)
        if _drag_source_items:
            pixmap = _drag_source_items[0].icon().pixmap(32, 32)
            drag.setPixmap(pixmap)
            drag.setHotSpot(pixmap.rect().center())

        # Выполняем drag - удаление произойдет в dropMimeData
        drag.exec(Qt.DropAction.MoveAction)

    def dropEvent(self, event: QDropEvent):
        mime_data = event.mimeData()

        # Обработка перетаскивания элементов дерева (папки/устройства)
        if mime_data.hasFormat(MIME_TYPE):
            # Определяем целевой индекс и позицию вставки с учётом индикатора (OnItem / Above / Below)
            pos = event.position().toPoint()
            index = self.indexAt(pos)
            drop_pos = self.dropIndicatorPosition()

            if not index.isValid():
                # Пустая область — вставляем в корень в конец (или в конец
                # локальной секции, если она показана: хост вне секции при
                # show_nodes создал бы устройство вне всех нод).
                if self.show_nodes:
                    local = self.get_node_section_item(LOCAL_NODE_IID)
                    if local is not None:
                        target_parent_index = local.index()
                        insert_row = local.rowCount()
                    else:
                        target_parent_index = QModelIndex()
                        insert_row = self._model.rowCount()
                else:
                    target_parent_index = QModelIndex()
                    insert_row = self._model.rowCount()
            else:
                if drop_pos == QAbstractItemView.DropIndicatorPosition.OnItem:
                    # Поместить внутрь элемента — в конец его детей
                    target_parent_index = index
                    parent_item = self._model.itemFromIndex(index)
                    insert_row = (
                        parent_item.rowCount() if parent_item is not None else 0
                    )
                elif drop_pos == QAbstractItemView.DropIndicatorPosition.AboveItem:
                    target_parent_index = index.parent()
                    insert_row = index.row()
                elif drop_pos == QAbstractItemView.DropIndicatorPosition.BelowItem:
                    target_parent_index = index.parent()
                    insert_row = index.row() + 1
                else:
                    target_parent_index = index.parent()
                    insert_row = (
                        index.row() if index.isValid() else self._model.rowCount()
                    )

            # Получаем parent_item (корень если невалиден)
            parent_item = (
                self._model.itemFromIndex(target_parent_index)
                if target_parent_index.isValid()
                else self._model.invisibleRootItem()
            )

            # Разрешаем дроп только в папку или в корень
            parent_is_folder = True
            if parent_item is not None:
                parent_is_folder = bool(parent_item.data(FOLDER_DATAROLE))

            if target_parent_index.isValid() and not parent_is_folder:
                event.ignore()
                return

            # Дроп внутрь/рядом с секцией ноды запрещён (хосты ноды не
            # перемещаются между нодами drag&drop'ом).
            if target_parent_index.isValid() and parent_item is not None:
                probe = parent_item
                while probe is not None:
                    if bool(probe.data(NODE_SECTION_DATAROLE)):
                        event.ignore()
                        return
                    probe = probe.parent()

            # Защита от циклов: нельзя бросить папку в саму себя или в её
            # собственного потомка — иначе dropMimeData удалит исходную папку
            # (вместе с целевым потомком), а вставка произойдёт в уничтоженный
            # элемент и вся ветка исчезнет.
            source_indices = []
            for src_item in _drag_source_items:
                if src_item is not None:
                    src_idx = self._model.indexFromItem(src_item)
                    if src_idx.isValid():
                        source_indices.append(src_idx)
            # Поднимаемся от цели к корню и проверяем, не является ли один из
            # предков цели одним из перетаскиваемых элементов.
            ancestor = target_parent_index
            while ancestor.isValid():
                for src_idx in source_indices:
                    if ancestor == src_idx:
                        event.ignore()
                        return
                ancestor = ancestor.parent()
            # Дроп на самого себя (OnItem по перетаскиваемому элементу)
            if index.isValid():
                for src_idx in source_indices:
                    if index == src_idx:
                        event.ignore()
                        return

            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()

            # Собираем iid перемещаемых устройств ДО dropMimeData (он очищает
            # _drag_source_items и пересоздаёт элементы). Для папок рекурсивно
            # собираем iid всех устройств поддерева — иначе список отмеченных
            # не получит уведомления об изменении категории для устройств,
            # находившихся внутри перемещённой папки.
            moved_iids = []
            for src_item in _drag_source_items:
                if src_item is None:
                    continue
                if bool(src_item.data(FOLDER_DATAROLE)):
                    moved_iids.extend(self._model._collect_descendant_iids(src_item))
                else:
                    dev = getattr(src_item, "device", None)
                    iid = getattr(dev, "iid", None) if dev is not None else None
                    if iid:
                        moved_iids.append(iid)

            # Вызываем модельную обработку дропа с корректной позицией вставки
            success = self._model.dropMimeData(
                mime_data, event.dropAction(), insert_row, 0, target_parent_index
            )

            # Эмитим обновлённые данные дерева при успехе
            if success:
                self.data_changed.emit(self.get_tree_data())
                # Уведомить подписчиков (список отмеченных) об изменении категории.
                # dropMimeData пересоздаёт элементы, поэтому находим их заново по iid.
                for iid in moved_iids:
                    new_item = self._find_tree_item_by_iid(iid)
                    if (
                        new_item is not None
                        and hasattr(new_item, "device")
                        and new_item.device
                    ):
                        self.device_updated.emit(new_item.device)
            return

        # Обработка перетаскивания файлов (URLs)
        if mime_data.hasUrls():
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()

            # Определяем целевую папку
            index = self.indexAt(event.position().toPoint())
            target_item = None
            if index.isValid():
                item = self._model.itemFromIndex(index)
                if item and item.data(FOLDER_DATAROLE):
                    target_item = item
                elif item and item.parent():
                    target_item = item.parent()

            if target_item is None:
                target_item = self._model.invisibleRootItem()

            # Обрабатываем каждый файл
            files_processed = 0
            for url in mime_data.urls():
                file_path = url.toLocalFile()
                if file_path.lower().endswith(".txt"):
                    self._parse_and_import_txt_file(file_path, target_item)
                    files_processed += 1

            if files_processed > 0:
                self.sort_tree()
                self.data_changed.emit(self.get_tree_data())
                logger.info(f"Imported devices from {files_processed} txt file(s)")
            return

        # Обработка текстовых данных (plain text)
        if mime_data.hasText():
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()

            # Определяем целевую папку
            index = self.indexAt(event.position().toPoint())
            target_item = None
            if index.isValid():
                item = self._model.itemFromIndex(index)
                if item and item.data(FOLDER_DATAROLE):
                    target_item = item
                elif item and item.parent():
                    target_item = item.parent()

            if target_item is None:
                target_item = self._model.invisibleRootItem()

            # Пытаемся распарсить текст как список хостов
            text = mime_data.text()
            self._parse_text_and_import(text, target_item)
            return

        event.ignore()

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mouseButtons() != Qt.MouseButton.LeftButton:
            return

        if self.read_only:
            return

        # Принимаем перетаскивание элементов дерева
        if event.mimeData().hasFormat(MIME_TYPE):
            event.acceptProposedAction()
            return

        # Принимаем перетаскивание файлов
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return

        # Принимаем текстовые данные
        if event.mimeData().hasText():
            event.acceptProposedAction()
            return

        event.ignore()
        super().dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        """Обработка выхода за пределы виджета - сбрасываем источники если drag отменен"""
        global _drag_source_items
        # Сбрасываем ссылку при выходе из виджета (drag отменен)
        _drag_source_items = []
        super().dragLeaveEvent(event)

    def rename_folder_action(self, item: CustomTreeItem):
        if item.data(FOLDER_DATAROLE):
            new_name, ok = QInputDialog.getText(
                self,
                "Переименовать папку",
                "Введите новое имя папки:",
                QLineEdit.Normal,
                item.text(),
            )

            if ok and new_name:
                item.setText(new_name)

                # Собрать все устройства в папке (рекурсивно) для уведомления подписчиков
                affected_devices = []

                def _collect_devices(node):
                    for i in range(node.rowCount()):
                        child = node.child(i)
                        if hasattr(child, "device") and child.device:
                            affected_devices.append(child.device)
                        if child.hasChildren():
                            _collect_devices(child)

                _collect_devices(item)

                new_data = self.get_tree_data()
                self.data_changed.emit(
                    new_data
                )  # ← Эмитим сигнал вместо вызова datastore
                # Уведомить подписчиков (список отмеченных) об изменении категории
                for device in affected_devices:
                    self.device_updated.emit(device)

    def _sort_group_in_place(self, parent_item):
        """In-place сортировка строк группы с сохранением состояний чекбоксов
        и привязанных DeviceModel (без пересоздания элементов).

        Папки идут раньше устройств, сортировка алфавитная без учёта регистра.
        """
        n = parent_item.rowCount()
        if n < 2:
            return
        # Берём строки с конца, чтобы индексы оставшихся не сдвигались
        rows = [parent_item.takeRow(n - 1 - i) for i in range(n)]
        rows.reverse()  # восстанавливаем исходный порядок перед sort

        def sort_key(row):
            item = row[0]
            if hasattr(item, "device") and item.device is not None:
                # Устройство (leaf) — после папок
                return (1, natural_key(item.device.name or ""))
            # Папка (group) — перед устройствами
            return (0, natural_key(item.text() or ""))

        rows.sort(key=sort_key)
        for row in rows:
            parent_item.appendRow(row)

    def sort_current_group(self, group_item: CustomTreeItem):
        """Сортировка элементов в текущей группе с сохранением чекбоксов."""
        if group_item is None:
            group_item = self._model.invisibleRootItem()
        self._sort_group_in_place(group_item)
        new_data = self.get_tree_data()
        self.data_changed.emit(new_data)
        logger.info(
            f"Sorted group: {group_item.text() if group_item.text() else 'root'}"
        )

    def _resolve_toolbar_target(self):
        """Цель для кнопок тулбара: та же логика, что у контекстного меню.

        Папка в выделении — внутрь неё, хост — в его родителя, иначе
        корень (при show_nodes — секция локальной ноды). Для чужих нод
        (секция/папка/хост с node_iid != LOCAL_NODE_IID) возвращает None —
        CRUD там идёт через ноду-владельца, а не локальными методами.
        """
        item = None
        try:
            index = self.currentIndex()
            if index is not None and index.isValid():
                item = self._model.itemFromIndex(index)
        except (RuntimeError, AttributeError):
            item = None
        if item is None:
            selected = self.get_selected_items()
            item = selected[0] if selected else None
        if item is None:
            item = self._model.invisibleRootItem()

        # Секция ноды: локальная — цель она сама, чужая — запрет.
        if bool(item.data(NODE_SECTION_DATAROLE)):
            if item.data(DEVICE_NODE_DATAROLE) == LOCAL_NODE_IID:
                return item
            return None
        # Внутри чужой ноды: папка или хост — запрет (меню там ограничено).
        node_iid = item.data(DEVICE_NODE_DATAROLE)
        if node_iid and node_iid != LOCAL_NODE_IID:
            return None
        dev = getattr(item, "device", None)
        if dev is not None and getattr(dev, "node_iid", None):
            if dev.node_iid != LOCAL_NODE_IID:
                return None
        # Та же нормализация, что в show_tree_context_menu: папка — сама,
        # хост — её родитель.
        if bool(item.data(FOLDER_DATAROLE)):
            return item
        parent = item.parent()
        if parent is None:
            # Корень при show_nodes — секция «Эта машина», иначе корень.
            if self.show_nodes:
                try:
                    return self.get_or_create_node_section(
                        LOCAL_NODE_IID, LOCAL_NODE_NAME
                    )
                except (RuntimeError, AttributeError, NameError):
                    pass
            return self._model.invisibleRootItem()
        return parent

    def add_folder_via_toolbar(self):
        """Кнопка тулбара: добавить папку (как ПКМ)."""
        if self.read_only:
            return
        target = self._resolve_toolbar_target()
        if target is None:
            return
        self.add_folder_to_group(target)

    def add_host_via_toolbar(self):
        """Кнопка тулбара: добавить хост (как ПКМ)."""
        if self.read_only:
            return
        target = self._resolve_toolbar_target()
        if target is None:
            return
        self.add_device_to_group(target)

    def add_range_via_toolbar(self):
        """Кнопка тулбара: добавить диапазон IP (как ПКМ)."""
        if self.read_only:
            return
        target = self._resolve_toolbar_target()
        if target is None:
            return
        self.add_ip_range_to_group(target)

    def sort_via_toolbar(self):
        """Кнопка тулбара: сортировка текущей группы (как ПКМ)."""
        if self.read_only:
            return
        target = self._resolve_toolbar_target()
        if target is None:
            return
        self.sort_current_group(target)

    def mousePressEvent(self, event: QMouseEvent):
        super().mousePressEvent(event)

        if event.button() == Qt.MouseButton.LeftButton:
            index = self.indexAt(event.pos())
            if not index.isValid():
                self.selectionModel().clearSelection()
                self.setCurrentIndex(self._model.invisibleRootItem().index())
            else:
                model = self.model()
                if isinstance(model, QStandardItemModel):
                    self.current_selected_item = model.itemFromIndex(index)
                else:
                    self.setCurrentIndex(self._model.invisibleRootItem().index())

    def sort_tree(self, parent_item=None):
        """Рекурсивная in-place сортировка всего дерева с сохранением чекбоксов.

        Сортирует каждую группу (папку) на месте: папки — перед устройствами,
        внутри — по алфавиту без учёта регистра. Не эмитит data_changed —
        вызывающие пути (импорт, добавление, drop) эмитят самостоятельно.
        """
        if parent_item is None:
            parent_item = self._model.invisibleRootItem()
        self._sort_group_in_place(parent_item)
        for i in range(parent_item.rowCount()):
            child = parent_item.child(i)
            if child and child.hasChildren():
                self.sort_tree(child)

    def load_tree_data(self, data: dict):
        """Загрузка данных в дерево с запуском таймеров для отмеченных устройств"""
        # Останавливаем все таймеры перед перестроением дерева.
        # Иначе singleton-менеджеры продолжат работать со старыми DeviceModel,
        # а _sync_checked_devices_to_ping_manager надбавит ref_count → утечка
        # (фантомный пинг у неотмеченных хостов, рассинхрон is_online).
        if self.enable_ping:
            get_host_ping_timer_manager().stop_all()
            get_thumbnail_timer_manager().stop_all()

        # Сохраняем существующие секции нод (переносятся в новое дерево),
        # потом удалим те, что не понадобятся.
        self._saved_node_sections: list = []
        root = self._model.invisibleRootItem()
        for i in range(root.rowCount()):
            child = root.child(i)
            if isinstance(child, CustomTreeItem) and bool(
                child.data(NODE_SECTION_DATAROLE)
            ):
                self._saved_node_sections.append(child)

        self._model.clear()
        if self.show_columns:
            self._model.setHorizontalHeaderLabels(
                ["Имя", "Адрес", "Логин", "Пароль", "Sudo", "MAC"]
            )

        # Локальная нода «Эта машина»: локальные хосты грузим в её секцию,
        # а не в корень (show_nodes=False → секция не создаётся, грузим в
        # корень как раньше).
        if self.show_nodes and data:
            section = self.get_or_create_node_section(LOCAL_NODE_IID, LOCAL_NODE_NAME)
            self._load_group_items(section, data, node_iid=LOCAL_NODE_IID)
            # Чекбоксы/раскрытость локальных устройств восстанавливаются
            # вызывающим кодом (MainWindow._restore_tree_ui_state) — здесь
            # только структурная загрузка.
        else:
            root = self._model.invisibleRootItem()
            self._load_group_items(root, data)

        # Возвращаем секции нод на место (кроме локальной — уже создана).
        # При show_nodes=False секции удалённых нод — в стэш скрытых.
        root = self._model.invisibleRootItem()
        for section in self._saved_node_sections:
            nid = section.data(DEVICE_NODE_DATAROLE) or ""
            if nid == LOCAL_NODE_IID:
                continue
            if self.get_node_section_item(nid) is None and not any(
                s.data(DEVICE_NODE_DATAROLE) == nid for s in self._hidden_node_sections
            ):
                if self.show_nodes:
                    root.appendRow([section])
                else:
                    self._hidden_node_sections.append(section)
        self._saved_node_sections = []

        # Запускаем таймеры для уже отмеченных устройств после загрузки
        if self.enable_ping:
            self._sync_checked_devices_to_ping_manager()

        # Временный фильтр поиска: применить к новому дереву
        self._refilter()

    def _sync_checked_devices_to_ping_manager(self):
        """Синхронизация отмеченных устройств - запуск таймеров с разнесением по времени"""
        from PySide6.QtCore import QTimer

        checked_devices = self.get_all_checked_devices()
        if not checked_devices:
            return

        batch_size = config.app.network.thread_count
        delay_per_batch_ms = 500
        manager = get_host_ping_timer_manager()

        def start_batch(devices, delay_ms=0):
            if not devices:
                return

            def do_start():
                for device in devices:
                    manager.start_ping(device)

            if delay_ms > 0:
                QTimer.singleShot(delay_ms, do_start)
            else:
                do_start()

        for i in range(0, len(checked_devices), batch_size):
            batch = checked_devices[i : i + batch_size]
            batch_index = i // batch_size
            start_batch(batch, delay_ms=batch_index * delay_per_batch_ms)

        logger.info(
            f"Started ping timers for {len(checked_devices)} checked devices (batched)"
        )

    def on_item_checkstate_changed(self, item, check_state):
        """Обработчик изменения чекбокса устройства"""
        if not hasattr(item, "device") or item.device is None:
            return

        if not self.enable_ping:
            return

        # Хосты ноды: зеркальный путь локального пинга — per-host статус-таймер
        # (NodeStatusTimer запрашивает статус у ноды-владельца по HTTP с тем же
        # каденсом, что локальный ping). Миниатюры подписываются в списке
        # (DeviceListView.add_device) и стартуют только после онлайн-подтверждения.
        # Снятие галочки — стоп таймера и сброс иконки (как у локальных).
        # Хосты локальной ноды — обычные локальные устройства (пинг как раньше).
        node_niid = getattr(item.device, "node_iid", None)
        if node_niid and node_niid != LOCAL_NODE_IID:
            if check_state == Qt.CheckState.Checked:
                try:
                    from src.workers.node.status import get_node_status_manager

                    get_node_status_manager().start_ping(item.device)
                except Exception as e:
                    logger.debug(f"DeviceTreeView: node status start failed: {e}")
            else:
                item.device.set_online(False)
                item.device.icon = ICONS["default"]
                item.setIcon(_icon_from_key("default"))
                try:
                    from src.workers.node.status import get_node_status_manager

                    get_node_status_manager().stop_ping(item.device.iid, node_niid)
                except Exception as e:
                    logger.debug(f"DeviceTreeView: node status stop failed: {e}")
                try:
                    from src.workers.node.thumbnails import (
                        get_node_thumbnail_manager,
                    )

                    get_node_thumbnail_manager().stop_thumbnail(item.device.iid, node_iid=node_niid)
                except Exception:
                    pass
            logger.debug(
                f"DeviceTreeView: node device checkbox {item.device.host} = {check_state}"
            )
            return

        manager = get_host_ping_timer_manager()
        if check_state == Qt.CheckState.Checked:
            manager.start_ping(item.device)
            logger.debug(f"DeviceTreeView: Started ping for {item.device.host}")
        else:
            manager.stop_ping(item.device.iid)
            # Сбрасываем статус устройства в нейтральное (default) состояние:
            # пинг остановлен, онлайн-статус больше не актуален. Без этого
            # device.icon сохранял online/offline от последнего пинга, и
            # последующее редактирование хоста (item.update() → setIcon)
            # перерисовывало устаревший статус у неотмеченного хоста.
            item.device.set_online(False)
            item.device.icon = ICONS["default"]
            item.setIcon(_icon_from_key("default"))
            logger.debug(f"DeviceTreeView: Stopped ping for {item.device.iid}")

    def _load_group_items(self, parent_item, data, node_iid: str = None):
        """Рекурсивная загрузка элементов группы"""
        checkable = self.show_checkboxes
        show_cols = self.show_columns
        for item in data:
            if isinstance(item, dict):
                if "name" in item and "host" in item:
                    device = DeviceModel(item)
                    if node_iid:
                        device.node_iid = node_iid
                    device_item = CustomTreeItem(
                        device.name, device, checkable=checkable, show_columns=show_cols
                    )
                    device_item.setData(False, FOLDER_DATAROLE)  # Для устройств
                    if node_iid:
                        device_item.setData(node_iid, DEVICE_NODE_DATAROLE)
                    if checkable:
                        device_item.setCheckState(
                            Qt.CheckState.Unchecked
                        )  # Сбрасываем чекбокс
                    parent_item.appendRow(device_item.row_items())
                else:
                    for key, value in item.items():
                        group_item = CustomTreeItem(
                            key, checkable=checkable, show_columns=show_cols
                        )
                        group_item.setData(True, FOLDER_DATAROLE)  # Для папок
                        if node_iid:
                            group_item.setData(node_iid, DEVICE_NODE_DATAROLE)
                        parent_item.appendRow(group_item.row_items())
                        self._load_group_items(group_item, value, node_iid)

    def export_tree_data(self):
        return self._model.export_tree_data()

    def get_tree_data(self):
        return self._model.export_tree_data()

    # ----- Развернуть/свернуть/ноды -----------------------------------------

    def expand_all_safe(self) -> None:
        """Развернуть всё дерево: локальные папки и хосты нод.

        Отличие от QTreeView.expandAll: пустые секции нод (hosts ещё не
        приходили) остаются свёрнутыми — иначе expandAll помечает их
        «раскрытыми» и при появлении хостов всё равно всё видно; проблем
        нет, но пустая стрелка сбивает. Дополнительно не разворачиваем
        пустые локальные папки (без детей) — нечего показывать.
        """
        root = self._model.invisibleRootItem()

        def _walk(item):
            for i in range(item.rowCount()):
                ch = item.child(i)
                if ch is None:
                    continue
                if ch.rowCount() > 0:
                    self.expand(ch.index())
                    _walk(ch)

        _walk(root)

    def collapse_all_safe(self) -> None:
        """Свернуть всё дерево, включая секции нод."""
        self.collapseAll()

    # ----- Фильтр поиска ----------------------------------------------------

    def apply_filter(self, text: str) -> None:
        """Применить временный фильтр поиска к дереву (view-уровень).

        Поддерживает как простой поиск по подстроке, так и умные предикаты
        (is:online, is:offline, sudo:yes, mac:empty, os:mos13, user:student и др.).
        """
        raw = (text or "").strip()
        self._filter_text = raw.casefold()
        self._filter_query = SmartHostQuery.parse(raw) if raw else None
        self._apply_filter_recursive(self._model.invisibleRootItem(), self.rootIndex())

    def filter_active(self) -> bool:
        """Активен ли фильтр поиска (непустой текст)."""
        return bool(self._filter_text)

    def _item_matches_filter(self, item: CustomTreeItem) -> bool:
        """Совпадает ли элемент (имя; для устройства ещё и адрес host и предикаты)."""
        if item is None:
            return False
        if not self._filter_text:
            return True

        device = getattr(item, "device", None)
        if device is not None:
            if self._filter_query is not None and (
                self._filter_query.predicates or len(self._filter_query.text_terms) > 1
            ):
                info = None
                try:
                    from src.services.hosts.host_info_service import get_host_info_service

                    info = get_host_info_service().get_snapshot(device.host)
                except Exception:
                    pass
                return self._filter_query.matches_device(device, info)
            if self._filter_text in item.text().casefold():
                return True
            if self._filter_text in (device.host or "").casefold():
                return True
            if self._filter_text in (device.mac_address or "").casefold():
                return True
            return False

        if self._filter_query and self._filter_query.text_terms:
            return all(
                term in item.text().casefold()
                for term in self._filter_query.text_terms
            )
        return self._filter_text in item.text().casefold()

    def _apply_filter_recursive(self, item: CustomTreeItem, parent_index) -> bool:
        """Обход дерева: скрыть/показать строки, вернуть видимость элемента.

        Возвращает True, если элемент сам совпал или содержит совпавшего
        потомка (родитель с совпавшим потомком остаётся видимым).
        """
        matched_child = False
        for row in range(item.rowCount()):
            child = item.child(row)
            if child is None:
                continue
            child_visible = self._apply_filter_recursive(
                child, self._model.indexFromItem(child)
            )
            self.setRowHidden(row, parent_index, not child_visible)
            if child_visible:
                matched_child = True

        self_visible = (
            self._item_matches_filter(item) or item is self._model.invisibleRootItem()
        )
        visible = self_visible or matched_child
        if self._filter_text and visible and (matched_child or item.rowCount() == 0):
            self.expand(item.index())
        elif not self._filter_text:
            pass  # сброс фильтра — раскрытость не трогаем
        return visible

    def _refilter(self) -> None:
        """Ре-применить фильтр после структурных изменений дерева."""
        if self._filter_text:
            self.apply_filter(self._filter_text)

    def set_show_nodes(self, show: bool) -> None:
        """Переключить режим «ноды первым уровнем».

        show=True: локальные хосты переносятся в секцию «Эта машина»
        (структурная перестройка секции из текущего дерева); скрытые секции
        удалённых нод возвращаются в корень. show=False: содержимое локальной
        секции переносится обратно в корень, секция удаляется; секции
        удалённых нод снимаются в стэш (_hidden_node_sections) — poll-циклы
        продолжают обновлять их данные, но в дереве они не видны.
        """
        if show == self.show_nodes:
            return
        self.show_nodes = show
        root = self._model.invisibleRootItem()

        if show:
            # Собрать локальные top-level элементы (не нод-секции) и перенести
            # в секцию «Эта машина». Устройствам проставить node_iid.
            section = self.get_or_create_node_section(LOCAL_NODE_IID, LOCAL_NODE_NAME)
            # takeRow/insertRow триггерят signals модели; делаем всю
            # перестройку при заблокированных сигналах, затем разовый
            # layoutChanged — иначе бесконечный цикл обработки itemChanged.
            self._model.blockSignals(True)
            try:
                # Снимок локальных top-level элементов (не нод-секции).
                local_rows = []
                for i in range(root.rowCount()):
                    ch = root.child(i)
                    if isinstance(ch, CustomTreeItem) and not bool(
                        ch.data(NODE_SECTION_DATAROLE)
                    ):
                        local_rows.append(i)
                for row in reversed(local_rows):
                    child = root.child(row)
                    self._apply_node_iid_recursive(child, LOCAL_NODE_IID)
                    section.appendRow(root.takeRow(row))
                # Локальная секция наверх.
                if section.row() != 0:
                    root.removeRow(section.row())
                    root.insertRow(0, [section])
                # Вернуть скрытые секции удалённых нод в корень.
                for sec in self._hidden_node_sections:
                    root.appendRow([sec])
                self._hidden_node_sections = []
            finally:
                self._model.blockSignals(False)
            self._model.layoutChanged.emit()
        else:
            section = self.get_node_section_item(LOCAL_NODE_IID)
            if section is None:
                self.show_nodes = not show
                return
            # Перенести содержимое локальной секции в корень (перед секциями
            # нод), снять node_iid.
            self._model.blockSignals(True)
            try:
                insert_at = 0
                while section.rowCount() > 0:
                    child = section.child(0)
                    self._clear_node_iid_recursive(child)
                    root.insertRow(insert_at, section.takeRow(0))
                    insert_at += 1
                root.removeRow(section.row())
                # Секции удалённых нод — в стэш (с дерева снять).
                remote_secs = []
                for i in reversed(range(root.rowCount())):
                    ch = root.child(i)
                    if isinstance(ch, CustomTreeItem) and bool(
                        ch.data(NODE_SECTION_DATAROLE)
                    ):
                        if ch.data(DEVICE_NODE_DATAROLE) != LOCAL_NODE_IID:
                            row = root.takeRow(i)
                            if row:
                                remote_secs.append(row[0])
                # Порядок как был: takeRow шёл с конца — reverse.
                self._hidden_node_sections = list(reversed(remote_secs)) + [
                    s
                    for s in self._hidden_node_sections
                    if s.data(DEVICE_NODE_DATAROLE) != LOCAL_NODE_IID
                ]
            finally:
                self._model.blockSignals(False)
            self._model.layoutChanged.emit()
            self._refilter()

    def _apply_node_iid_recursive(self, item: CustomTreeItem, node_iid: str) -> None:
        """Проставить node_iid (DEVICE_NODE_DATAROLE + DeviceModel.node_iid)
        всему поддереву (перенос локальных хостов в секцию «Эта машина»)."""
        dev = getattr(item, "device", None)
        if dev is not None:
            dev.node_iid = node_iid
        item.setData(node_iid, DEVICE_NODE_DATAROLE)
        for i in range(item.rowCount()):
            ch = item.child(i)
            if ch is not None:
                self._apply_node_iid_recursive(ch, node_iid)

    def _clear_node_iid_recursive(self, item: CustomTreeItem) -> None:
        """Снять маркеры ноды с поддерева (перенос обратно в корень)."""
        dev = getattr(item, "device", None)
        if dev is not None and getattr(dev, "node_iid", None) == LOCAL_NODE_IID:
            try:
                dev.node_iid = None
            except Exception:
                pass
        item.setData(None, DEVICE_NODE_DATAROLE)
        for i in range(item.rowCount()):
            ch = item.child(i)
            if ch is not None:
                self._clear_node_iid_recursive(ch)

    def _parse_and_import_txt_file(self, file_path: str, parent_item: CustomTreeItem):
        """
        Парсинг txt файла и импорт устройств в дерево.

        Поддерживаемые форматы:
        - Один хост на строку (только IP/hostname)
        - Формат: name,host,port,mac (CSV-подобный)
        - Формат: name:host:port:mac (через двоеточие)
        - Комментарии начинаются с #
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            devices_added = 0
            filename = os.path.basename(file_path)

            for line in lines:
                line = line.strip()

                # Пропускаем пустые строки и комментарии
                if not line or line.startswith("#"):
                    continue

                device = self._parse_device_line(line, filename)
                if device:
                    self.add_device(parent_item, device)
                    devices_added += 1

            if devices_added > 0:
                logger.info(f"Added {devices_added} devices from file: {filename}")

        except Exception as e:
            logger.error(f"Error parsing txt file {file_path}: {e}")

    def _parse_text_and_import(self, text: str, parent_item: CustomTreeItem):
        """
        Парсинг текста и импорт устройств в дерево.

        Поддерживаемые форматы:
        - Один хост на строку
        - Разделители: запятая, точка с запятой, пробел, двоеточие
        """
        lines = text.strip().split("\n")
        devices_added = 0

        for line in lines:
            line = line.strip()

            # Пропускаем пустые строки и комментарии
            if not line or line.startswith("#"):
                continue

            device = self._parse_device_line(line, "clipboard")
            if device:
                self.add_device(parent_item, device)
                devices_added += 1

        if devices_added > 0:
            logger.info(f"Added {devices_added} devices from clipboard/text")

    def _parse_device_line(self, line: str, source: str = "") -> Optional[DeviceModel]:
        """
        Парсинг одной строки в объект DeviceModel.

        Поддерживаемые форматы:
        1. Только хост: "192.168.1.1" -> имя = хост
        2. CSV: "name,host,port,mac"
        3. Colon: "name:host:port:mac"
        4. С пробелами: "name host port mac"
        """
        line = line.strip()
        if not line:
            return None

        # Пробуем CSV формат (запятая)
        if "," in line:
            parts = [p.strip() for p in line.split(",")]
            return self._create_device_from_parts(parts, source)

        # Пробуем формат с двоеточием (но не IPv6 без скобок)
        # IPv6 адреса в скобках: [::1]:22
        if ":" in line and not line.startswith("["):
            # Проверяем, не похожа ли строка на IPv6
            colon_count = line.count(":")
            if colon_count <= 4:  # Скорее всего не IPv6
                parts = [p.strip() for p in line.split(":")]
                return self._create_device_from_parts(parts, source)

        # Пробуем формат с точкой с запятой
        if ";" in line:
            parts = [p.strip() for p in line.split(";")]
            return self._create_device_from_parts(parts, source)

        # Пробуем формат с пробелом/табуляцией
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
            return self._create_device_from_parts(parts, source)

        if "  " in line:
            parts = [p.strip() for p in line.split()]
            return self._create_device_from_parts(parts, source)

        # Просто хост (IP или hostname)
        if self._is_valid_host(line):
            return DeviceModel(
                {
                    "name": line,
                    "host": line,
                    "port": config.app.ssh.port,
                }
            )

        return None

    def _create_device_from_parts(
        self, parts: list, source: str = ""
    ) -> Optional[DeviceModel]:
        """Создание DeviceModel из списка частей"""
        if not parts or not parts[0]:
            return None

        device_data = {
            "name": parts[0] if len(parts) > 0 and parts[0] else "",
            "host": parts[1] if len(parts) > 1 and parts[1] else parts[0],
            "port": config.app.ssh.port,
        }

        # Парсим порт если есть
        if len(parts) > 2 and parts[2]:
            try:
                device_data["port"] = int(parts[2])
            except ValueError:
                pass

        # Парсим MAC-адрес если есть (позиция 3; исторические форматы с
        # login/password больше не поддерживаются — учётные данные через связки)
        if len(parts) > 3 and parts[3]:
            device_data["mac_address"] = parts[3]

        # Если имя пустое, используем хост
        if not device_data["name"]:
            device_data["name"] = device_data["host"]

        # Проверяем валидность хоста
        if not self._is_valid_host(device_data["host"]):
            return None

        return DeviceModel(device_data)

    def _is_valid_host(self, host: str) -> bool:
        """
        Проверка валидности хоста (IP, hostname, domain).
        """
        if not host:
            return False

        # IPv4 адрес
        ipv4_pattern = r"^(\d{1,3}\.){3}\d{1,3}$"
        if re.match(ipv4_pattern, host):
            # Проверяем что каждая часть <= 255
            parts = host.split(".")
            return all(0 <= int(p) <= 255 for p in parts)

        # IPv6 адрес (упрощенно)
        if host.startswith("[") and host.endswith("]"):
            return True

        # Hostname / domain name
        hostname_pattern = r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$"
        if re.match(hostname_pattern, host):
            return True

        # localhost
        if host.lower() in ["localhost", "localhost.localdomain"]:
            return True

        return False
