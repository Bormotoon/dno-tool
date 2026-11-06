"""Диалог сканирования IP-адресов сети: ICMP и порт-сканирование, определение MAC и импорт устройств."""

import csv
import io
import time
from copy import deepcopy
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QWidget,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QTableWidgetItem,
    QCheckBox,
    QHBoxLayout,
    QFrame,
    QRadioButton,
    QButtonGroup,
    QInputDialog,
    QMessageBox,
    QSizePolicy,
    QGridLayout,
    QTreeView,
    QSlider,
    QSpinBox,
    QProgressBar,
    QLayout,
    QLayoutItem,
    QStyledItemDelegate,
    QComboBox,
    QToolButton,
)
from PySide6.QtCore import (
    Qt,
    QObject,
    QThread,
    QTimer,
    Signal,
    Slot,
    QSortFilterProxyModel,
    QPoint,
    QRect,
    QSize,
    QIODevice,
    QSaveFile,
)
from PySide6.QtGui import QStandardItemModel, QStandardItem, QIcon, QShowEvent
import ipaddress
from typing import List, Dict
from src.domain.models.device import DeviceModel
from src.ui.icons import _icon_from_key  # zip-темы: иконки из памяти
from src.ui.widgets.checkable_combo import CheckableComboBox
from src.ui.widgets.motion_tracker import MotionTracker
from src.ui.widgets.thread_count_slider import ThreadCountSlider
from src.config import config, PORTS
from src.utils.scanner.scanner_icmp import check_icmplib
from src.utils.scanner.scanner_types import HostScanResult, ScanOutcome
from src.utils.scanner.device_facts import (
    CLASSIFICATION_PORTS as _ICMP_PROBE_PORTS,
)
from src.di import get_container
from src.services.configuration.config_service import ConfigService
from src.utils.network.net_utils import get_default_ipv4_interface
from src.utils.scanner.device_classifier import (
    scanner_state_icon_key,
)
from src.utils.scanner.scanner_protocol import nbstat_request
from src.utils.scanner.scanner_range import MAX_SCAN_ADDRESSES, ScannerAddressRange
from src.logger import logger


def _save_scanner_config(**changes):
    service = get_container().resolve_optional(ConfigService)
    if service is None:
        # Standalone dialogs still use the same transactional service.
        service = ConfigService()
    success, message = service.save_scanner_config(**changes)
    if not success:
        raise OSError(message)


class FlowLayout(QLayout):
    """Раскладка в одну строку с переносом виджетов на следующую при нехватке ширины."""

    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._items: List[QLayoutItem] = []

    def addItem(self, item):  # noqa: N802 - Qt API
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):  # noqa: N802 - Qt API
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):  # noqa: N802 - Qt API
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):  # noqa: N802 - Qt API
        return Qt.Orientation.Horizontal

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect):  # noqa: N802 - Qt API
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt API
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        x = effective.x()
        y = effective.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            w = hint.width()
            h = hint.height()
            next_x = x + w + self._spacing
            if next_x - self._spacing > effective.right() + 1 and line_height > 0:
                x = effective.x()
                y = y + line_height + self._spacing
                next_x = x + w + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(w, h)))
            x = next_x
            line_height = max(line_height, h)
        return y + line_height - rect.y() + margins.bottom()


# Wire parsing lives outside Qt so malformed replies can be tested in isolation.
_NBNS_PACKET = nbstat_request(0)


from src.utils.scanner.engine import (
    ScannerEngine, shutdown_scanner_pools,
    _SCANNER_RUN_LOCK,
)


# Категорийные порты фазы «Пробы» (были инлайн в _analyze_host):
# ICMP-режим — лёгкие активные пробы онлайн-хостов для классификации;
# порт-режим — dedicated пробы портов, которых может не быть в списке
# сканирования, + AP-сигнатуры — одним плоским пакетом.
_PORT_EXTRA_TARGETS = _ICMP_PROBE_PORTS


# Роль данных с ключом сортировки (строка/число, сравнение без QVariant-магии)
SORT_ROLE = Qt.ItemDataRole.UserRole + 101


def _zero_pad_ip(ip: str) -> str:
    """IP → zero-padded строка («192.168.3.7» → «192.168.003.007»): лексико = численно."""
    try:
        return ".".join(f"{int(p):03d}" for p in ip.split("."))
    except Exception:
        return ip.casefold()


def _ports_sort_key(ports_text: str) -> str:
    """Текст портов → сортируемая строка (ICMP отдельно, порты zero-padded)."""
    t = (ports_text or "").strip()
    if not t:
        return "1"
    if t == "ICMP":
        return "2"
    try:
        parts = sorted(int(p) for p in t.split(","))
        return "0" + ",".join(f"{p:05d}" for p in parts)
    except Exception:
        return "1" + t.casefold()


def _sort_key(value: str) -> str:
    """Общий строковый ключ (casefold)."""
    return (value or "").casefold()


class ScannerTreeItem(QStandardItem):
    """Элемент дерева сканера с устройством"""

    def __init__(self, text: str, device: DeviceModel = None, is_folder: bool = False):
        super().__init__(text)
        self.device = device
        self.is_folder = is_folder
        self.setCheckable(True)
        self.setCheckState(Qt.CheckState.Unchecked)
        self._last_check_state = Qt.CheckState.Unchecked
        self.setEditable(False)
        if is_folder:
            self.setIcon(_icon_from_key("folder"))
        else:
            self.setIcon(_icon_from_key("offline"))
            self.setEditable(False)


class ScannerSortProxyModel(QSortFilterProxyModel):
    """Прокси сортировки дерева сканера: ключи из SORT_ROLE (casefold, IP/порты).

    dynamicSortFilter=False — сортировка только по клику на заголовок
    (и явному sort()), без переупорядочивания во время скана/чекбоксов.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDynamicSortFilter(False)
        self.setSortRole(SORT_ROLE)
        self.search_text = ""
        self.device_type = ""

    def filterAcceptsRow(self, row, parent):
        if not self.search_text and not self.device_type:
            return True
        model = self.sourceModel()
        item = model.itemFromIndex(model.index(row, 0, parent))
        if getattr(item, "is_folder", False):
            return any(self.filterAcceptsRow(i, item.index()) for i in range(item.rowCount()))
        values = [model.index(row, col, parent).data() or "" for col in (0, 1, 3)]
        kind = model.index(row, 4, parent).data() or ""
        return (not self.search_text or self.search_text in " ".join(values).casefold()) and (
            not self.device_type or kind == self.device_type
        )



class IPScannerTree(QTreeView):
    """Дерево результатов сканирования с колонками для хостов"""

    # Индексы колонок (сортируемые)
    COL_NAME, COL_IP, COL_PORTS, COL_MAC, COL_TYPE, COL_STATE = range(6)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._folders = {}
        self._hosts = {}
        self._model = QStandardItemModel()
        # Existing column indices stay stable for saved sorting preferences.
        self._model.setHorizontalHeaderLabels(
            ["Имя хоста", "IP-адрес", "Порты", "MAC-адрес", "Тип", "Состояние"]
        )
        # Proxy: view видит сортированную копию, source-модель (чекбоксы,
        # раскрытость, поиск папок) не затрагивается сортировкой.
        self._proxy = ScannerSortProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self.setModel(self._proxy)
        # И source-сортировка (QStandardItemModel.sort/sortChildren), и прокси
        # сравнивают ключи SORT_ROLE (zero-padded IP/порты), а не DisplayRole.
        self._model.setSortRole(SORT_ROLE)
        self.setHeaderHidden(False)
        self.setAnimated(False)
        self.setUniformRowHeights(True)
        self.setExpandsOnDoubleClick(False)
        self.setAllColumnsShowFocus(True)
        self.setAlternatingRowColors(True)
        self.setIndentation(20)
        self.setRootIsDecorated(True)
        self.setSortingEnabled(True)

        # Настройка колонок
        header = self.header()
        header.setStretchLastSection(True)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        self.setColumnWidth(0, 170)
        self.setColumnWidth(1, 110)
        self.setColumnWidth(2, 80)
        self.setColumnWidth(3, 125)
        self.setColumnWidth(4, 150)
        header.setMinimumSectionSize(70)
        self.setColumnWidth(5, 180)

        # Подключаем сигнал изменения чекбокса
        self._model.itemChanged.connect(self._on_item_changed)

        # Контекстное меню «Отметить по типу устройства»
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_type_context_menu)

    def sort_recursive(self, column: int, order: Qt.SortOrder) -> None:
        """Sort folders by name and hosts by column in the native Qt proxy.

        Keep host source rows stable: sorting both source and proxy doubles
        work and invalidates layouts twice for large discovered subnets.
        """
        order_enum = (
            Qt.SortOrder.AscendingOrder
            if order == Qt.SortOrder.AscendingOrder
            else Qt.SortOrder.DescendingOrder
        )
        self.setSortingEnabled(False)
        try:
            # Папки между собой — по имени (колонка 0)
            if self._model.rowCount() > 1:
                self._model.invisibleRootItem().sortChildren(0, order_enum)
            self._update_sort_indicators(column, order)
            self._proxy.sort(column, order_enum)
        finally:
            self.setSortingEnabled(True)

    def _update_sort_indicators(self, column: int, order: Qt.SortOrder) -> None:
        """Индикатор на заголовке колонки (без обратного вызова sort())."""
        header = self.header()
        try:
            header.blockSignals(True)
            header.setSortIndicator(column, order)
        finally:
            header.blockSignals(False)

    def apply_saved_sort(self, column: int, order: Qt.SortOrder) -> None:
        """Применить сохранённую сортировку (без эмиссии сигналов заголовка)."""
        self.sort_recursive(column, order)
        self._update_sort_indicators(column, order)

    def _on_item_changed(self, item):
        """Обработка изменения элемента (чекбокса)"""
        # itemChanged also fires for icons/text/tooltips during every probe.
        # Only a changed selection requires walking siblings/ancestors.
        state = item.checkState()
        if getattr(item, "_last_check_state", state) == state:
            return
        item._last_check_state = state
        if item.isCheckable() and item.checkState() != Qt.CheckState.PartiallyChecked:
            if hasattr(item, "is_folder") and getattr(item, "is_folder", False):
                self._set_children_checkstate(item, item.checkState())
            elif hasattr(item, "device") and getattr(item, "device", None) is not None:
                self._update_parent_checkstate(item)

    def _set_children_checkstate(self, parent_item, check_state):
        """Bulk selection must not rescan all siblings after each child."""
        blocked = self._model.blockSignals(True)
        try:
            stack = [parent_item]
            while stack:
                parent = stack.pop()
                for i in range(parent.rowCount()):
                    child = parent.child(i)
                    if child is None:
                        continue
                    child._last_check_state = check_state
                    child.setCheckState(check_state)
                    if getattr(child, "is_folder", False):
                        stack.append(child)
        finally:
            self._model.blockSignals(blocked)
        self._update_parent_checkstate(parent_item)
        self.viewport().update()

    def _update_parent_checkstate(self, child_item):
        """Обновить состояние чекбокса родителя на основе детей"""
        parent = child_item.parent()
        if not parent:
            return
        checked_count = 0
        total_count = parent.rowCount()
        for i in range(total_count):
            child = parent.child(i)
            if child is None:
                continue
            if child.checkState() == Qt.CheckState.Checked:
                checked_count += 1
        if checked_count == 0:
            parent.setCheckState(Qt.CheckState.Unchecked)
        elif checked_count == total_count:
            parent.setCheckState(Qt.CheckState.Checked)
        else:
            parent.setCheckState(Qt.CheckState.PartiallyChecked)
        self._update_parent_checkstate(parent)

    def clear_tree(self):
        """Очистить результаты (заголовки колонок и sortRole сохраняются)."""
        # model.clear() сбрасывает заголовки и требует пересоздания;
        # removeRows() удаляет только данные, header-лейблы остаются.
        self._hosts.clear()
        self._folders.clear()
        self._model.removeRows(0, self._model.rowCount())

    def add_folder(self, folder_name: str) -> ScannerTreeItem:
        """Добавить папку (подсеть)"""
        folder_item = ScannerTreeItem(folder_name, is_folder=True)
        folder_item.setData(_sort_key(folder_name), SORT_ROLE)
        self._folders[folder_name] = folder_item
        self._model.appendRow(folder_item)
        return folder_item

    def add_device_to_folder(
        self,
        folder: ScannerTreeItem,
        device: DeviceModel,
        ports: List = None,
        mac_address: str = None,
        is_online: bool = False,
        device_type: str = None,
        type_reason: str = None,
        state: str = "done",
    ) -> ScannerTreeItem:
        """Добавить устройство в папку с информацией в колонках"""
        device_item = ScannerTreeItem(device.name, device)

        # Колонка 1: IP адрес
        ip_item = QStandardItem(device.host)

        # Колонка 2: Порты
        ports_text = ", ".join(map(str, ports)) if ports else ""
        if ports == ["icmp"]:
            ports_text = "ICMP"
        ports_item = QStandardItem(ports_text)

        # Колонка 3: MAC адрес
        mac_item = QStandardItem(mac_address if mac_address else "")

        # Колонка 4: Тип устройства
        type_item = QStandardItem(device_type if device_type else "")
        state_item = QStandardItem(self._state_text(is_online, state))
        state_item.setData(_sort_key(state_item.text()), SORT_ROLE)
        if device_type:
            type_item.setToolTip(self._type_tooltip(device_type, type_reason))
        elif state == "scan":
            # Хост онлайн, тип определяется (стадия «Анализ»)
            type_item.setToolTip("Определение типа…")

        # Ключи сортировки (SORT_ROLE): IP zero-padded, порты zero-padded
        device_item.setData(_sort_key(device.name), SORT_ROLE)
        ip_item.setData(_zero_pad_ip(device.host), SORT_ROLE)
        ports_item.setData(_ports_sort_key(ports_text), SORT_ROLE)
        mac_item.setData(_sort_key(mac_address), SORT_ROLE)
        type_item.setData(_sort_key(device_type), SORT_ROLE)

        for item in (device_item, ip_item, ports_item, mac_item, type_item, state_item):
            item.setEditable(False)
        device_item.scan_state = "online" if is_online else "offline"
        self._hosts[device.host] = device_item
        folder.appendRow([device_item, ip_item, ports_item, mac_item, type_item, state_item])
        self.expand(self._proxy.mapFromSource(folder.index()))

        # Обновляем иконку (статус сканирования / тип после анализа)
        device_item.setIcon(self._state_icon(is_online, state, device_type))

        return device_item

    @staticmethod
    def _state_text(is_online, state):
        if state == "unverified":
            return "Не проверен в этом скане"
        if state in ("scan", "scan2"):
            return "Уточнение данных…" if is_online else "Проверяется…"
        return "Доступен" if is_online else "Не ответил"

    @staticmethod
    def _state_icon(
        is_online: bool, state: str = "done", device_type: str = ""
    ) -> QIcon:
        """Иконка статуса строки сканера.

        state="scan"  — промежуточная (пинг ещё не завершён) → scan_ping;
        state="scan2" — анализ (тип определяется) → scan_analyze;
        state="done"  — итог: онлайн с известным типом → иконка типа
        (topo_*), онлайн без типа → online, офлайн → offline.
        """
        try:
            return _icon_from_key(scanner_state_icon_key(is_online, state, device_type))
        except Exception:
            return QIcon()

    @staticmethod
    def _type_tooltip(device_type: str, type_reason: str = None) -> str:
        """Tooltip колонки «Тип»: тип + причины классификации."""
        if not type_reason:
            return device_type
        return f"{device_type}\nПризнаки: {type_reason}"

    def update_device_in_folder(
        self,
        folder: ScannerTreeItem,
        device: DeviceModel,
        is_online: bool,
        ports: List = None,
        mac_address: str = None,
        device_type: str = None,
        type_reason: str = None,
        state: str = "done",
    ):
        """Обновить устройство в папке"""
        existing = self._hosts.get(device.host)
        for i in ([existing.row()] if existing is not None else []):
            folder = existing.parent()
            child = existing
            if (
                hasattr(child, "device")
                and child.device
                and child.device.host == device.host
            ):
                # Обновляем имя
                child.scan_state = "online" if is_online else "offline"
                status_item = folder.child(i, self.COL_STATE)
                if status_item is not None:
                    status_item.setText(self._state_text(is_online, state))
                    status_item.setData(_sort_key(status_item.text()), SORT_ROLE)
                child.setToolTip("Ответил" if is_online else "Проверен — не ответил")
                child.setText(device.name)
                child.setData(_sort_key(device.name), SORT_ROLE)
                # Обновляем IP (колонка 1)
                ip_item = folder.child(i, 1)
                if ip_item:
                    ip_item.setText(device.host)
                    ip_item.setData(_zero_pad_ip(device.host), SORT_ROLE)
                # Обновляем порты (колонка 2)
                if ports is not None:
                    ports_text = ", ".join(map(str, ports)) if ports else ""
                    if ports == ["icmp"]:
                        ports_text = "ICMP"
                    ports_item = folder.child(i, 2)
                    if ports_item:
                        ports_item.setText(ports_text)
                        ports_item.setData(_ports_sort_key(ports_text), SORT_ROLE)
                # Обновляем MAC (колонка 3): при офлайн-результате
                # (mac_address None/пусто) затирается прежний MAC
                mac_item = folder.child(i, 3)
                if mac_item:
                    mac_item.setText(mac_address or "")
                    mac_item.setData(_sort_key(mac_address or ""), SORT_ROLE)
                # Обновляем Тип (колонка 4): при офлайн-результате затирается
                type_item = folder.child(i, 4)
                if device_type:
                    if type_item:
                        type_item.setText(device_type)
                        type_item.setData(_sort_key(device_type), SORT_ROLE)
                        type_item.setToolTip(
                            self._type_tooltip(device_type, type_reason)
                        )
                elif type_item:
                    if state == "scan":
                        # Стадия «Анализ»: тип сбрасывается на «определяется…»
                        type_item.setText("")
                        type_item.setData("", SORT_ROLE)
                        type_item.setToolTip("Определение типа…")
                    elif state == "done":
                        # Офлайн-результат или классификация не дала типа:
                        # прежний тип затирается
                        type_item.setText("")
                        type_item.setData("", SORT_ROLE)
                        type_item.setToolTip("")
                # Обновляем иконку (статус сканирования / тип после анализа).
                # Тип берем итоговый: новый либо уже показанный в колонке.
                shown_type = device_type or (type_item.text() if type_item else "")
                child.setIcon(self._state_icon(is_online, state, shown_type))
                # Синхронизируем объект: иначе get_checked_devices вернет
                # stale-имя (IP с фазы пинга), а импорт уведет IP в name.
                # host всегда IP, name - найденный hostname.
                try:
                    child.device.name = device.name
                    child.device.host = device.host
                    child.device.mac_address = mac_address or None
                except Exception:
                    pass
                return
        # Если не найдено, добавляем новое
        self.add_device_to_folder(
            folder,
            device,
            ports,
            mac_address,
            is_online,
            device_type,
            type_reason,
            state,
        )

    def get_checked_devices(self) -> List[DeviceModel]:
        """Получить все отмеченные устройства (исключая папки)"""
        devices = []

        def collect_checked(item):
            # Пропускаем папки
            if hasattr(item, "is_folder") and item.is_folder:
                for i in range(item.rowCount()):
                    collect_checked(item.child(i))
            # Добавляем только устройства с checked
            elif (
                hasattr(item, "device")
                and item.device
                and item.checkState() == Qt.CheckState.Checked
            ):
                if getattr(item, "scan_state", "online") == "online":
                    devices.append(deepcopy(item.device))
            else:
                for i in range(item.rowCount()):
                    collect_checked(item.child(i))

        for i in range(self._model.rowCount()):
            collect_checked(self._model.item(i))

        return devices

    def _collect_devices_by_type(self) -> Dict[str, List[ScannerTreeItem]]:
        """Сгруппировать устройства по тексту колонки «Тип» (пустые пропустить)."""
        grouped: Dict[str, List[ScannerTreeItem]] = {}

        def collect(item) -> None:
            if getattr(item, "is_folder", False):
                for i in range(item.rowCount()):
                    child = item.child(i)
                    if child is not None:
                        collect(child)
                return
            if getattr(item, "device", None) is None:
                return
            parent = item.parent()
            if parent is None:
                return
            type_item = parent.child(item.row(), self.COL_TYPE)
            device_type = type_item.text().strip() if type_item else ""
            if not device_type:
                return
            grouped.setdefault(device_type, []).append(item)

        for i in range(self._model.rowCount()):
            top = self._model.item(i)
            if top is not None:
                collect(top)
        return grouped

    def check_devices_by_type(self, device_type: str) -> int:
        """Отметить все найденные устройства выбранного типа.

        Только добавляет отметки (остальные устройства не трогает).
        Возвращает число вновь отмеченных устройств.
        """
        grouped = self._collect_devices_by_type()
        checked = 0
        for item in grouped.get(device_type, []):
            if item.checkState() != Qt.CheckState.Checked:
                item.setCheckState(Qt.CheckState.Checked)
                checked += 1
        return checked

    def _show_type_context_menu(self, pos) -> None:
        """Контекстное меню: отметить все устройства выбранного типа."""
        grouped = self._collect_devices_by_type()
        menu = QMenu(self)
        header = menu.addAction("Выберите тип устройства")
        header.setEnabled(False)
        menu.addSeparator()
        if not grouped:
            empty = menu.addAction("Нет найденных типов")
            empty.setEnabled(False)
        else:
            for device_type in sorted(grouped, key=str.casefold):
                items = grouped[device_type]
                action = menu.addAction(f"{device_type} ({len(items)})")
                # Захват по значению, не по переменной цикла
                action.triggered.connect(
                    lambda _checked=False, t=device_type: self.check_devices_by_type(t)
                )
        menu.exec(self.mapToGlobal(pos))


class ScannerRunnable(QObject):
    finished = Signal()
    result_ready = Signal(object)
    phase_ready = Signal(str, str, int, int, int)
    outcome_ready = Signal(object)
    error = Signal(str)
    scan_finished = Signal(
        str, str, bool, list, str, str, str, str
    )  # ip, hostname, is_online, ports, mac_address, device_type, type_reason, state ("scan"/"scan2"/"done")
    phase = Signal(
        str, int, int, int
    )  # name, done, total, active ("ping"/"names"/"ports"/"finish")

    def __init__(self, *args, **kwargs):
        QObject.__init__(self)
        self._engine = ScannerEngine(*args, **kwargs)
        for name in ('finished', 'result_ready', 'phase_ready', 'outcome_ready', 'error', 'scan_finished', 'phase'):
            setattr(self._engine, name, getattr(self, name))

    def __getattr__(self, name):
        engine = self.__dict__.get('_engine')
        if engine is None:
            raise AttributeError(name)
        return getattr(engine, name)

    @Slot()
    def execute(self):
        """Выполнить общий алгоритм в потоке Qt-координатора."""
        self._engine.execute()

    def abort(self):
        """Отмена не зависит от event loop занятого координатора."""
        self._engine.abort()


class PortNumberDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = QSpinBox(parent)
        editor.setRange(1, 65535)
        return editor

    def setEditorData(self, editor, index):
        try:
            editor.setValue(int(index.data()))
        except (TypeError, ValueError):
            editor.setValue(1)

    def setModelData(self, editor, model, index):
        editor.interpretText()
        model.setData(index, str(editor.value()), Qt.ItemDataRole.EditRole)


class PortsEditorDialog(QDialog):
    def __init__(self, parent=None, ports: dict[int, bool] | None = None):
        super().__init__(parent)
        self.ports = PORTS.copy() if ports is None else dict(ports)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Редактирование портов для сканирования")
        self.setMinimumSize(400, 300)
        self.layout = QVBoxLayout(self)

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setItemDelegateForColumn(0, PortNumberDelegate(self.table))
        self.table.setHorizontalHeaderLabels(["Порт", "Включен"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.add_btn = QPushButton("Добавить")
        self.remove_btn = QPushButton("Удалить")
        self.save_btn = QPushButton("Применить")
        self.cancel_btn = QPushButton("Отмена")
        btn_layout.addWidget(self.add_btn)
        btn_layout.addWidget(self.remove_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.cancel_btn)
        self.layout.addLayout(btn_layout)

        self.add_btn.clicked.connect(self.add_port)
        self.remove_btn.clicked.connect(self.remove_selected)
        self.save_btn.clicked.connect(self.on_save)
        self.cancel_btn.clicked.connect(self.reject)

        self.load_ports()

    def load_ports(self):
        self.table.setRowCount(0)
        for port in sorted(self.ports):
            row = self.table.rowCount()
            self.table.insertRow(row)
            item = QTableWidgetItem(str(port))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, item)
            chk = QCheckBox()
            chk.setChecked(bool(self.ports.get(port, False)))
            w = QWidget()
            l = QVBoxLayout(w)
            l.addWidget(chk)
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            l.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(row, 1, w)

    def add_port(self):
        # QInputDialog.getInt требует позиционные аргументы: (parent, title, label, value, min, max)
        port, ok = QInputDialog.getInt(
            self, "Добавить порт", "Номер порта:", 1, 1, 65535
        )
        if ok:
            # Avoid duplicates
            if any(
                self.table.item(r, 0) and self.table.item(r, 0).text().strip() == str(port)
                for r in range(self.table.rowCount())
            ):
                QMessageBox.warning(self, "Внимание", f"Порт {port} уже существует.")
                return
            row = self.table.rowCount()
            self.table.insertRow(row)
            item = QTableWidgetItem(str(port))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, item)
            chk = QCheckBox()
            chk.setChecked(True)
            w = QWidget()
            l = QVBoxLayout(w)
            l.addWidget(chk)
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            l.setContentsMargins(0, 0, 0, 0)
            self.table.setCellWidget(row, 1, w)

    def remove_selected(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            QMessageBox.information(self, "Инфо", "Выберите строку для удаления.")
            return
        for r in rows:
            self.table.removeRow(r)

    def on_save(self):
        new_ports = {}
        try:
            for r in range(self.table.rowCount()):
                item = self.table.item(r, 0)
                if not item:
                    continue
                try:
                    p = int(item.text())
                except Exception:
                    QMessageBox.warning(
                        self, "Ошибка", f"Неверный номер порта в строке {r + 1}."
                    )
                    return
                if not 1 <= p <= 65535 or p in new_ports:
                    QMessageBox.warning(self, "Ошибка", f"Порт в строке {r + 1}: допустимы уникальные значения 1..65535.")
                    return
                # read checkbox
                cell_widget = self.table.cellWidget(r, 1)
                chk = cell_widget.findChild(QCheckBox) if cell_widget else None
                enabled = bool(chk.isChecked()) if chk else False
                new_ports[p] = enabled
            if not new_ports:
                # confirm empty list
                resp = QMessageBox.question(
                    self,
                    "Подтверждение",
                    "Использовать пустой список портов?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if resp != QMessageBox.StandardButton.Yes:
                    return
            self.ports = new_ports
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Ошибка при сохранении: {e}")


class IPScannerDialog(QDialog):
    exported_device = Signal(
        object, object
    )  # devices list, folder (None для определения в MW)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)

        self.setWindowTitle("Сканер хостов")
        self._ports = PORTS.copy()
        self._preferred_ping_backend = config.app.network.scanner_ping_backend
        self._init_ui()

        self.total_hosts = 0
        self.hosts_scanned = 0
        self._found = 0
        self._scan_phase = "done"  # ping -> analyze -> done
        self.ip_range = []
        self.scan_in_progress = False
        self._closing_after_abort = False
        self._clear_pending = False
        self._outcome = "success"
        self._scan_error = ""
        self._result_limit_reached = False
        self.worker = None
        self.thread = None
        self._coordinator_done = False
        self._delivery_timer = QTimer(self)
        self._delivery_timer.setInterval(16)
        self._delivery_timer.timeout.connect(self._drain_results)

    def _init_ui(self):

        self.setMinimumSize(720, 540)
        self.resize(980, 720)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        controls_layout = QVBoxLayout()
        controls_layout.setSpacing(6)
        controls_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # === Группа: Диапазон сканирования ===
        range_group = QWidget()
        range_layout = QVBoxLayout(range_group)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.setSpacing(6)

        self.ip_label = QLabel("IP-адрес / подсеть / диапазон:")
        self.ip_input = QLineEdit(self._detect_default_subnet())
        self.ip_input.setPlaceholderText(
            "192.168.1.0/24 · 192.168.1.1-15 · 192.168.1.1-192.168.1.254"
        )
        self.ip_input.setToolTip(
            "Подсеть: 192.168.1.0/24\n"
            "Короткий диапазон: 192.168.1.1-15\n"
            "Полный диапазон: 192.168.1.1-192.168.1.254\n"
            "Одиночный IP: 192.168.1.1"
        )
        range_layout.addWidget(self.ip_label)
        launch_row = QHBoxLayout()
        launch_row.addWidget(self.ip_input, 1)
        self.scan_button = QToolButton()
        self.scan_button.setText("Сканировать")
        self.scan_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.scan_button.setAutoRaise(False)
        self.scan_button.setIcon(_icon_from_key("menu_scanner"))
        self.scan_button.setIconSize(QSize(28, 28))
        self.scan_button.setFixedHeight(self.ip_input.sizeHint().height() + 4)
        self.scan_button.clicked.connect(self._on_scan_button_clicked)
        launch_row.addWidget(self.scan_button)
        range_layout.addLayout(launch_row)
        self.range_hint = QLabel()
        self.range_hint.setWordWrap(True)
        controls_layout.addWidget(self.range_hint)
        self.ip_input.textChanged.connect(self._update_range_hint)
        self.ip_input.returnPressed.connect(self._on_scan_button_clicked)

        scan_label = QLabel("Тип сканирования:")

        self.port_combo_box = CheckableComboBox()

        scan_frame = QFrame()
        scan_layout = QGridLayout()
        scan_layout.setContentsMargins(0, 0, 0, 0)
        scan_layout.setHorizontalSpacing(6)
        scan_layout.setVerticalSpacing(4)

        self.radio1 = QRadioButton("ICMP")
        self.radio2 = QRadioButton("По открытым TCP-портам")
        self.radio3 = QRadioButton("ICMP + TCP")
        self.radio3.setToolTip("Хост найден, если отвечает ICMP или хотя бы один выбранный TCP-порт. TCP проверяется после отсутствия ICMP-ответа.")
        scan_layout.addWidget(self.radio3, 0, 3)
        self.radio1.setChecked(True)

        scan_layout.addWidget(
            self.radio1,
            0,
            1,
            alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        )
        scan_layout.addWidget(
            self.radio2,
            0,
            2,
            alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        )
        scan_frame.setLayout(scan_layout)
        scan_layout.addWidget(scan_label, 0, 0)
        scan_layout.setColumnStretch(4, 1)

        port_edit_container = QWidget()
        port_edit_layout = QHBoxLayout(port_edit_container)
        port_edit_layout.setContentsMargins(0, 0, 0, 0)
        port_edit_layout.setSpacing(6)

        # кнопка редактирования портов рядом с комбобоксом
        self.edit_ports_button = QPushButton("Редактировать порты")
        self.edit_ports_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.port_combo_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self.edit_ports_button.clicked.connect(self.open_ports_editor)

        port_edit_layout.addWidget(self.port_combo_box)
        port_edit_layout.addWidget(self.edit_ports_button)
        port_edit_layout.setStretch(0, 1)
        port_edit_layout.setStretch(1, 0)
        port_edit_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        port_edit_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.port_edit_container = port_edit_container

        scan_frame.setLayout(scan_layout)
        controls_layout.addWidget(scan_frame)

        self.radio_group = QButtonGroup(self)
        self.radio_group.addButton(self.radio1, 1)
        self.radio_group.addButton(self.radio2, 2)
        self.radio_group.addButton(self.radio3, 3)

        self.radio_group.buttonClicked.connect(self.radio_group_clicked)

        for port, permit in self._ports.items():
            self.port_combo_box.addItem(text=f"{port}", checked=permit)

        self.port_combo_box.setEnabled(False)

        self.port_combo_box.item_check_state_changed.connect(self.check_state_changed)

        layout.addWidget(range_group)

        # === Группа: Параметры ===
        params_group = QWidget()
        params_layout = QVBoxLayout(params_group)
        params_layout.setSpacing(6)
        params_layout.setContentsMargins(0, 0, 0, 0)
        params_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Ползунок потоков
        threads_row = QHBoxLayout()
        threads_row.setSpacing(8)
        threads_label = QLabel("Потоки:")
        self.threads_slider = ThreadCountSlider(
            value=int(getattr(config.app.network, "scanner_thread_count", 32))
        )
        threads_row.addWidget(threads_label)
        threads_row.addWidget(self.threads_slider, 1)

        # Чекбоксы опций: одна строка с переносом при сужении формы
        options_layout = FlowLayout(spacing=12)
        self.check_resolve_names = QCheckBox("Имена (DNS / NetBIOS)")
        self.check_resolve_names.setChecked(True)
        self.check_resolve_mac = QCheckBox("Определять MAC-адреса")
        self.check_resolve_mac.setChecked(True)
        options_layout.addWidget(self.check_resolve_names)
        options_layout.addWidget(self.check_resolve_mac)

        # Определять тип устройства + зависимость от портовых сигнатур
        self.check_resolve_type = QCheckBox("Определять тип устройства")
        self.check_resolve_type.setChecked(True)
        self.check_resolve_type.setToolTip(
            "Добавляет столбец «Тип» (роутер, точка доступа, принтер и т.д.) по hostname/MAC"
        )
        self.check_use_port_signatures = QCheckBox("Уточнять тип по открытым портам")
        self.check_use_port_signatures.setChecked(True)
        self.check_use_port_signatures.setToolTip(
            "Использовать открытые порты для уточнения типа (для точек доступа: 22/80/443/11100 + баннер). "
            "Если выключено - фаза портовых проб и опрос баннеров пропускаются, "
            "тип определяется только по hostname/MAC/TTL, скан идет быстрее."
        )
        options_layout.addWidget(self.check_resolve_type)
        self.check_udp_probes = QCheckBox("UDP-пробы (NetBIOS / SNMP public)")
        self.check_udp_probes.setChecked(True)
        self.check_udp_probes.setToolTip("Дополнительные запросы NBSTAT и SNMPv1 GET sysDescr с community public. Отсутствие ответа считается неизвестным состоянием, не закрытым портом.")
        controls_layout.addLayout(options_layout)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("Дополнительно")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        params_layout.addWidget(self.advanced_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self.advanced_panel = QWidget()
        advanced_layout = QGridLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(12, 0, 0, 0)
        advanced_layout.setHorizontalSpacing(20)
        advanced_layout.setColumnStretch(0, 1)
        advanced_layout.setColumnStretch(1, 1)
        advanced_layout.setColumnStretch(2, 1)
        advanced_layout.addLayout(threads_row, 0, 0)
        ping_timeout_row = QHBoxLayout()
        ping_timeout_row.setSpacing(8)
        ping_timeout_row.addWidget(QLabel("Таймаут ICMP:"))
        self.ping_timeout_slider = QSlider(Qt.Orientation.Horizontal)
        # Единица шкалы равна 100 мс, включая перемещение мышью.
        self.ping_timeout_slider.setRange(1, 100)
        self.ping_timeout_slider.setSingleStep(1)
        self.ping_timeout_slider.setPageStep(1)
        self.ping_timeout_value_label = QLabel()
        self.ping_timeout_value_label.setMinimumWidth(
            self.ping_timeout_value_label.fontMetrics().horizontalAdvance("10000 мс")
        )
        self._set_ping_timeout_value(config.app.network.scanner_ping_timeout_ms)
        self.ping_timeout_slider.setToolTip("Время ожидания ICMP-ответа только при сканировании хостов. Шаг — 100 мс.")
        self.ping_timeout_slider.valueChanged.connect(self._on_ping_timeout_changed)
        ping_timeout_row.addWidget(self.ping_timeout_slider, 1)
        ping_timeout_row.addWidget(self.ping_timeout_value_label)
        advanced_layout.addLayout(ping_timeout_row, 0, 1)
        ping_backend_row = QHBoxLayout()
        ping_backend_row.addWidget(QLabel("Способ пинга:"))
        self.ping_backend_combo = QComboBox()
        self.ping_backend_combo.addItem("Системный ping", "system")
        self.ping_backend_combo.addItem("Библиотека", "icmplib")
        self.ping_backend_combo.setToolTip(
            "Способ ICMP-проверки только для сканера IP. Библиотека не получает TTL: "
            "признак ОС по TTL отсутствует; определение по имени, MAC и портам сохраняется."
        )
        ping_backend_row.addWidget(self.ping_backend_combo, 1)
        advanced_layout.addLayout(ping_backend_row, 0, 2)
        self.ping_backend_notice = QLabel()
        self.ping_backend_notice.setWordWrap(True)
        self.ping_backend_notice.setTextFormat(Qt.TextFormat.PlainText)
        self._refresh_ping_backends()
        self.ping_backend_combo.currentIndexChanged.connect(self._on_ping_backend_changed)
        self.ping_backend_combo.activated.connect(self._on_ping_backend_changed)
        advanced_layout.addWidget(self.ping_backend_notice, 2, 0, 1, 3)
        advanced_layout.addWidget(self.check_use_port_signatures, 1, 0)
        advanced_layout.addWidget(self.check_udp_probes, 1, 1)
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("TCP-порты:"))
        port_row.addWidget(port_edit_container, 1)
        advanced_layout.addLayout(port_row, 1, 2)
        self.advanced_panel.setVisible(False)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        params_layout.addWidget(self.advanced_panel)

        # зависимость: второй чекбокс активен только если первый включён
        def _on_resolve_type_toggled(checked: bool):
            self.check_use_port_signatures.setEnabled(checked)
            # скрываем/показываем столбец «Тип» (дерево может ещё не создано при ините)
            try:
                if hasattr(self, "results_tree"):
                    self.results_tree.setColumnHidden(4, not checked)
            except Exception:
                pass

        self.check_resolve_type.toggled.connect(_on_resolve_type_toggled)
        # инит состояние зависимого чекбокса (столбец скроем после создания дерева)
        self.check_use_port_signatures.setEnabled(self.check_resolve_type.isChecked())

        params_row = QHBoxLayout()
        params_row.setSpacing(12)
        params_row.addLayout(controls_layout, 1)
        self.motion_tracker = MotionTracker(self)
        self.motion_tracker.hide()
        params_row.addWidget(self.motion_tracker, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(params_row)
        layout.addWidget(params_group)

        # === Панель кнопок сканирования ===
        self.clear_button = QPushButton("Очистить результаты")
        self.clear_button.clicked.connect(self.clear_results)

        self.status_label = QLabel("Готово к сканированию")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(8)
        layout.addWidget(self.progress_bar)

        result_tools = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск по имени, IP или MAC")
        self.type_filter = QComboBox()
        self.type_filter.addItem("Все типы", "")
        self.type_filter.setMinimumContentsLength(10)
        self.type_filter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.select_all_button = QPushButton("Отметить все")
        self.select_all_button.setToolTip("Отметить доступные хосты, соответствующие текущему фильтру.")
        self.select_none_button = QPushButton("Снять отметки")
        result_tools.addWidget(self.search_input, 1)
        result_tools.addWidget(self.type_filter)
        result_tools.addWidget(self.select_all_button)
        result_tools.addWidget(self.select_none_button)
        layout.addLayout(result_tools)

        # === Результаты: таблица без фрейма-группы ===
        self.results_tree = IPScannerTree()
        # применить начальное состояние видимости столбца «Тип»
        try:
            self.results_tree.setColumnHidden(
                4, not self.check_resolve_type.isChecked()
            )
        except Exception:
            pass
        # Сортировка: восстановить из конфига + персист по клику на заголовок
        try:
            saved_col = int(getattr(config.app.network, "scanner_sort_column", 1))
            saved_order = int(getattr(config.app.network, "scanner_sort_order", 0))
            self.results_tree.apply_saved_sort(
                max(0, min(5, saved_col)),
                Qt.SortOrder.AscendingOrder
                if saved_order == 0
                else Qt.SortOrder.DescendingOrder,
            )
        except Exception as e:
            logger.debug(f"IPScannerDialog: restore sort failed: {e}")
        self.results_tree.header().sortIndicatorChanged.connect(
            self._on_sort_indicator_changed
        )

        layout.addWidget(self.results_tree, 1)

        self.search_input.textChanged.connect(self._filter_results)
        self.type_filter.currentIndexChanged.connect(self._filter_results)
        self.select_all_button.clicked.connect(lambda: self._mark_results(True))
        self.select_none_button.clicked.connect(lambda: self._mark_results(False))
        self.check_resolve_mac.toggled.connect(lambda checked: self.results_tree.setColumnHidden(3, not checked))
        self._summary_timer = QTimer(self)
        self._summary_timer.setSingleShot(True)
        self._summary_timer.setInterval(120)
        self._summary_timer.timeout.connect(self._refresh_result_summary)
        model = self.results_tree._model
        for signal in (model.itemChanged, model.rowsInserted, model.rowsRemoved):
            signal.connect(self._schedule_result_summary)

        summary_row = QHBoxLayout()
        self.selection_label = QLabel("Показано: 0 · Отмечено: 0")
        summary_row.addWidget(self.selection_label, 1)
        summary_row.addWidget(self.clear_button)
        layout.addLayout(summary_row)

        # Экспорт слева, добавление и закрытие справа на одной строке.
        import_btn_layout = QHBoxLayout()
        self.export_button = QPushButton("Экспорт…")
        self.export_button.setToolTip(
            "Сохранить все результаты текущего фильтра в CSV в порядке сортировки."
        )
        self.export_button.clicked.connect(self.export_results)
        import_btn_layout.addWidget(self.export_button)
        import_button = QPushButton("Добавить отмеченные · 0")
        self.import_button = import_button
        import_button.clicked.connect(self.export_scanned_devices)
        import_btn_layout.addStretch()
        import_btn_layout.addWidget(import_button)
        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(self.close)
        import_btn_layout.addWidget(close_button)
        layout.addLayout(import_btn_layout)

        self.setLayout(layout)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self._update_range_hint()
        self.radio_group_clicked(self.radio1)
        self._refresh_result_summary()

    def showEvent(self, event: QShowEvent) -> None:
        """Повторно проверить доступ при открытии сохранённого экземпляра диалога."""
        if not self.scan_in_progress:
            self._refresh_ping_backends()
        super().showEvent(event)

    def _refresh_ping_backends(self) -> None:
        """Обновить доступность без перезаписи сохранённого предпочтения."""
        self._icmp_availability = check_icmplib()
        python_item = self.ping_backend_combo.model().item(1)
        python_item.setEnabled(self._icmp_availability.available)
        python_item.setToolTip(self._icmp_availability.reason or self.ping_backend_combo.toolTip())
        previous = self.ping_backend_combo.blockSignals(True)
        self.ping_backend_combo.setCurrentIndex(
            1 if self._preferred_ping_backend == "icmplib"
            and self._icmp_availability.available else 0
        )
        self.ping_backend_combo.blockSignals(previous)
        self.ping_backend_notice.setText(
            f"Библиотека недоступна: {self._icmp_availability.reason}. "
            "Используется системный ping. Сохранённое предпочтение не изменено."
            if not self._icmp_availability.available else ""
        )
        self.ping_backend_notice.setVisible(not self._icmp_availability.available)

    def _toggle_advanced(self, expanded):
        self.advanced_panel.setVisible(expanded)
        self.advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )

    def _update_range_hint(self):
        try:
            addresses = ScannerAddressRange(self.ip_input.text())
            self.range_hint.setText(f"{addresses[0]} – {addresses[-1]} · Адресов: {len(addresses)}")
            self.scan_button.setEnabled(True)
        except ValueError as exc:
            self.range_hint.setText(f"Проверьте диапазон: {exc}")
            self.scan_button.setEnabled(False)

    def _filter_results(self, *_):
        proxy = self.results_tree._proxy
        proxy.search_text = self.search_input.text().strip().casefold()
        proxy.device_type = self.type_filter.currentData() or ""
        proxy.invalidateFilter()
        self.results_tree.expandAll()
        self._schedule_result_summary()

    def _schedule_result_summary(self, *_):
        # Do not restart on each result: a sustained stream must not starve
        # the summary. During scans the final batch refreshes it instead.
        if not getattr(self, "scan_in_progress", False) and not self._summary_timer.isActive():
            self._summary_timer.start()

    def _refresh_result_summary(self):
        if not hasattr(self, "selection_label"):
            return
        if getattr(self, "scan_in_progress", False):
            self.selection_label.setText("Результаты обновляются · добавление доступно после завершения")
            self.import_button.setEnabled(False)
            self.export_button.setEnabled(False)
            self.clear_button.setEnabled(False)
            return
        tree = self.results_tree
        selected = 0
        shown = 0
        types = set()
        for item in tree._hosts.values():
            if item.checkState() == Qt.CheckState.Checked and getattr(item, "scan_state", "") == "online":
                selected += 1
            if tree._proxy.mapFromSource(item.index()).isValid():
                shown += 1
            kind = item.parent().child(item.row(), tree.COL_TYPE)
            if kind is not None and kind.text():
                types.add(kind.text())
        current_types = {self.type_filter.itemData(i) for i in range(1, self.type_filter.count())}
        if types != current_types:
            current = self.type_filter.currentData()
            self.type_filter.blockSignals(True)
            self.type_filter.clear()
            self.type_filter.addItem("Все типы", "")
            for kind in sorted(types):
                self.type_filter.addItem(kind, kind)
            index = self.type_filter.findData(current)
            self.type_filter.setCurrentIndex(max(0, index))
            self.type_filter.blockSignals(False)
            if (self.type_filter.currentData() or "") != tree._proxy.device_type:
                self._filter_results()
        self.selection_label.setText(f"Показано: {shown} из {len(tree._hosts)} · Отмечено к добавлению: {selected}")
        self.import_button.setText(f"Добавить отмеченные · {selected}")
        self.import_button.setEnabled(selected > 0 and not getattr(self, "scan_in_progress", False))
        self.clear_button.setEnabled(bool(tree._hosts) and not getattr(self, "scan_in_progress", False))
        self.export_button.setEnabled(shown > 0)

    def _mark_results(self, checked):
        tree = self.results_tree
        model = tree._model
        previous = model.blockSignals(True)
        try:
            for item in tree._hosts.values():
                if checked and (getattr(item, "scan_state", "") != "online" or not tree._proxy.mapFromSource(item.index()).isValid()):
                    continue
                state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                item._last_check_state = state
                item.setCheckState(state)
            for folder in tree._folders.values():
                if folder.rowCount():
                    tree._update_parent_checkstate(folder.child(0))
        finally:
            model.blockSignals(previous)
        tree.viewport().update()
        self._refresh_result_summary()

    def _set_ping_timeout_value(self, value: int) -> None:
        """Восстановить ползунок и точное сохранённое значение таймаута."""
        self.ping_timeout_slider.setValue((value + 50) // 100)
        self.ping_timeout_value_label.setText(f"{value} мс")
        self._ping_timeout_ms = value

    def _on_ping_backend_changed(self, index: int) -> None:
        """Запомнить выбор в диалоге до нажатия «Сканировать»."""
        self._preferred_ping_backend = self.ping_backend_combo.itemData(index)

    def _on_ping_timeout_changed(self, step: int) -> None:
        """Обновить черновик таймаута без записи конфигурации."""
        value = step * 100
        self.ping_timeout_value_label.setText(f"{value} мс")
        self._ping_timeout_ms = value

    def _current_thread_count(self) -> int:
        return self.threads_slider.value()

    def _on_sort_indicator_changed(self, column: int, order: Qt.SortOrder) -> None:
        """Применить сортировку; выбранный порядок сохранится при запуске скана."""
        self.results_tree.sort_recursive(column, order)

    def _save_scan_settings(self) -> bool:
        """Сохранить весь выбранный снимок одной транзакцией перед запуском."""
        header = self.results_tree.header()
        try:
            _save_scanner_config(
                ports=self._ports,
                scanner_thread_count=self._current_thread_count(),
                scanner_ping_timeout_ms=self._ping_timeout_ms,
                scanner_ping_backend=self._preferred_ping_backend,
                scanner_sort_column=header.sortIndicatorSection(),
                scanner_sort_order=0 if header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder else 1,
            )
        except OSError as exc:
            QMessageBox.critical(self, "Настройки сканера", str(exc))
            return False
        return True

    def generate_ip_range(self, network_mask_str):
        """Проверить IPv4 и размер диапазона без перечисления адресов."""
        try:
            return ScannerAddressRange(network_mask_str)
        except ValueError as exc:
            QMessageBox.warning(self, "Диапазон сканирования", str(exc))
            return []

    def _detect_default_subnet(self) -> str:
        """Подсеть машины для автозаполнения поля ввода.

        Определяет интерфейс основного маршрута (IP + маска) через
        get_default_ipv4_interface(). /32 (VPN point-to-point) нормализуется
        в /24 от этого IP - скан одиночного адреса бесполезен.
        Без результата - прежний дефолт 192.168.1.0/24.
        """
        try:
            iface = get_default_ipv4_interface()
            if iface is not None:
                network = iface.network
                if network.prefixlen < 32:
                    return str(network)
                octets = str(iface.ip).split(".")
                return ".".join(octets[:3]) + ".0/24"
        except Exception as e:
            logger.debug(f"IPScannerDialog: subnet autodetect failed: {e}")
        return "192.168.1.0/24"

    def _parse_network_input(self, input_text: str) -> tuple:
        """
        Парсит ввод пользователя и определяет маску подсети.

        Returns:
            tuple: (network с маской или None, prefix_len - длина маски)
        """
        input_text = input_text.strip()

        if not input_text:
            return None, 24  # Значение по умолчанию

        try:
            # Пробуем распарсить как сеть с маской (192.168.1.0/24)
            network = ipaddress.ip_network(input_text, strict=False)
            return network, network.prefixlen
        except ValueError:
            pass

        # Проверяем, это диапазон через дефис? (192.168.1.1-192.168.1.100)
        if "-" in input_text:
            # Для диапазонов используем /24 по умолчанию
            return None, 24

        # Проверяем, это одиночный IP?
        try:
            ipaddress.ip_address(input_text)
            # Одиночный IP - использовать /32
            return None, 32
        except ValueError:
            pass

        # Не удалось распарсить - используем /24 по умолчанию
        return None, 24

    def _get_subnet_for_ip(self, ip_address_str: str) -> str:
        """
        Определяет подсеть для заданного IP-адреса на основе введённой маски.

        Args:
            ip_address_str: IP-адрес хоста

        Returns:
            Строка подсети в формате "192.168.0.0/23"
        """
        input_text = self.ip_input.text().strip()
        network, prefix_len = self._parse_network_input(input_text)

        try:
            ip = ipaddress.ip_address(ip_address_str)

            if network is not None:
                # Вычисляем подсеть с заданной маской
                ip_int = int(ip)
                # Маска подсети
                mask = (0xFFFFFFFF << (32 - prefix_len)) & 0xFFFFFFFF
                network_int = ip_int & mask
                # Создаём сеть для получения корректного представления
                subnet_network = ipaddress.ip_network(
                    (network_int, prefix_len), strict=False
                )
                return str(subnet_network)
            else:
                # Для одиночного IP или диапазона без явной маски
                if prefix_len == 32:
                    # Одиночный IP - возвращаем /32
                    return f"{ip_address_str}/32"
                else:
                    # Используем /24 по умолчанию
                    subnet = (
                        ".".join(ip_address_str.split(".")[:3]) + f".0/{prefix_len}"
                    )
                    return subnet
        except ValueError:
            # В случае ошибки возвращаем подсеть по умолчанию /24
            try:
                return ".".join(ip_address_str.split(".")[:3]) + ".0/24"
            except Exception:
                return f"{ip_address_str}/32"

    def _set_inputs_enabled(self, enabled: bool):
        """Включить/выключить элементы ввода во время/после сканирования."""
        for w in (
            self.ip_input,
            self.radio1,
            self.radio2,
            self.radio3,
            self.check_udp_probes,
            self.port_combo_box,
            self.edit_ports_button,
            self.import_button,
            self.check_resolve_names,
            self.check_resolve_mac,
            self.check_resolve_type,
            self.check_use_port_signatures,
            self.threads_slider,
            self.ping_backend_combo,
            self.ping_timeout_slider,
        ):
            try:
                w.setEnabled(enabled)
            except Exception:
                pass
        # восстановить зависимость второго чекбокса
        if enabled:
            try:
                self.check_use_port_signatures.setEnabled(
                    self.check_resolve_type.isChecked()
                )
            except Exception:
                pass
            if hasattr(self, "radio_group"):
                self.radio_group_clicked(self.radio_group.checkedButton())
        if hasattr(self, "clear_button"):
            self.clear_button.setEnabled(enabled and bool(self.results_tree._hosts))

    def _on_scan_button_clicked(self):
        """Диспетчер: запуск сканирования или отмена активного."""
        if self.scan_in_progress:
            self.cancel_scan()
        else:
            self.start_threaded_scan()

    def start_threaded_scan(self):
        # Guard: не стартовать поверх живого скана — иначе два набора окон
        # поверх одного пула (старый поток осиротевает).
        if self.scan_in_progress or (
            self.thread is not None and self.thread.isRunning()
        ):
            logger.warning("IPScannerDialog: scan already in progress")
            return
        if _SCANNER_RUN_LOCK.locked():
            QMessageBox.warning(self, "Сканирование", "Другой скан ещё выполняется.")
            return
        if not self.radio1.isChecked() and not any(self._ports.values()):
            QMessageBox.warning(self, "Сканирование", "Выберите хотя бы один порт.")
            return
        self._closing_after_abort = False
        self._clear_pending = False
        self._outcome = "running"
        self._scan_error = ""
        self._result_limit_reached = False
        network_mask = self.ip_input.text()
        self.ip_range = self.generate_ip_range(network_mask)
        self.total_hosts = len(self.ip_range)

        self.hosts_scanned = 0
        self._found = 0
        # НЕ очищаем дерево, а обновляем существующие записи
        if not self.ip_range:
            logger.warning("Некорректная маска сети или нет доступных хостов.")
            self.scan_in_progress = False
            return
        if not self._save_scan_settings():
            return
        self.scan_in_progress = True

        # Old results remain visible, but are not importable until rechecked.
        for item in self.results_tree._hosts.values():
            item.scan_state = "unverified"
            item.setToolTip("Не проверен в текущем скане")
            status_item = item.parent().child(item.row(), self.results_tree.COL_STATE)
            if status_item is not None:
                status_item.setText("Не проверен в этом скане")
                status_item.setData(_sort_key(status_item.text()), SORT_ROLE)
        self.scan_button.setText("Отменить")
        self.motion_tracker.show()
        self.motion_tracker.start()
        self._set_inputs_enabled(False)
        self._refresh_result_summary()
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, max(1, self.total_hosts))
        self.progress_bar.setValue(0)
        self._scan_phase = "ping"
        self._phase_order = ["ping"]
        if self.check_resolve_names.isChecked():
            self._phase_order.append("names")
        if self.check_resolve_type.isChecked() and self.check_use_port_signatures.isChecked():
            self._phase_order.append("ports")
        self._phase_order.append("finish")
        self.status_label.setText(f"{'Пинг' if self.radio1.isChecked() else 'Порты'}: 0/{self.total_hosts} · Найдено: 0")

        icmp = self.radio1.isChecked() or self.radio3.isChecked()
        resolve_mac = self.check_resolve_mac.isChecked()
        max_workers = self._current_thread_count()
        classify_enabled = self.check_resolve_type.isChecked()
        use_port_signatures = (
            self.check_use_port_signatures.isChecked() and classify_enabled
        )
        self.worker = ScannerRunnable(
            self.ip_range,
            icmp,
            resolve_mac,
            max_workers,
            classify_enabled=classify_enabled,
            use_port_signatures=use_port_signatures,
            resolve_names=self.check_resolve_names.isChecked(),
            combined=self.radio3.isChecked(),
            udp_probes=self.check_udp_probes.isChecked(),
            ping_backend=self.ping_backend_combo.currentData(),
        )
        self.thread = QThread()

        self._coordinator_done = False
        self.worker.enable_buffered_delivery()
        self._delivery_timer.start()
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.execute)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self._scan_id = self.worker.scan_id
        self.worker.phase_ready.connect(self._on_scan_phase)
        self.worker.outcome_ready.connect(self._on_outcome)
        self.thread.finished.connect(self.thread_finished)

        self.thread.start()

    def _on_result(self, result: HostScanResult):
        if result.scan_id == self._scan_id and self._accept_scan_event():
            self.update_results(*result.legacy_args())
            if result.online:
                self.motion_tracker.add_contact(result.host)
            item = self.results_tree._hosts.get(result.host)
            if item is not None and result.probes:
                labels = {"open": "ответил", "closed": "закрыт", "timeout": "таймаут (нет ответа)",
                          "unreachable": "сеть/хост недоступны", "error": "ошибка проверки",
                          "no_reply": "нет Echo Reply",
                          "cancelled": "отменено", "invalid": "неподтверждённый ответ"}
                lines = [f"{probe.protocol.upper()}:{probe.port}: {labels[probe.state]}"
                         for probe in result.probes]
                item.parent().child(item.row(), 2).setToolTip("\n".join(lines))

    def _drain_results(self):
        if self.worker is None:
            return
        deadline = time.monotonic() + .008
        self.results_tree.setUpdatesEnabled(False)
        try:
            for _ in range(64):
                results = self.worker.take_results(1)
                if not results:
                    break
                self._on_result(results[0])
                if time.monotonic() >= deadline:
                    break
        finally:
            self.results_tree.setUpdatesEnabled(True)
        if self._coordinator_done and self.worker._results.empty():
            self._delivery_timer.stop()
            self._finish_scan_ui()

    def _on_scan_phase(self, scan_id, *args):
        if scan_id == self._scan_id and self._accept_scan_event():
            self._on_phase(*args)

    def _on_outcome(self, outcome: ScanOutcome):
        if outcome.scan_id == self._scan_id:
            self._outcome = outcome.status
            self._scan_error = outcome.error
            self._scan_diagnostics = outcome.diagnostics
            self.status_label.setToolTip("\n".join(
                f"{state}: {count}" for state, count in outcome.diagnostics
            ))

    def _accept_scan_event(self):
        return self.scan_in_progress and not self._clear_pending

    def cancel_scan(self):
        if self.worker is not None:
            self.worker.abort()
            self.status_label.setText("Отмена · ожидание завершения сетевых операций…")

    def _on_phase(self, name: str, done: int, total: int, found: int):
        """Статус строгих фаз сканера: Пинг → Имена → Порты → Анализ.

        У каждой фазы своя шкала с нуля (max = total фазы).
        Эвенты идут по ходу фазы (троттлинг в runner'е), поэтому полоса
        и строки таблицы обновляются синхронно.
        """
        if not self._accept_scan_event() or (self.worker is not None and self.worker.is_aborting):
            return
        self._found = found
        self.motion_tracker.set_count(found)
        self.progress_bar.setMaximum(max(1, total))
        self.progress_bar.setValue(max(0, min(done, total)))
        if name == "ping":
            self._scan_phase = "ping"
            self.hosts_scanned = done
            self.status_label.setText(f"{'Пинг' if self.radio1.isChecked() else 'Порты'}: {done}/{total} · Найдено: {found}")
        elif name == "tcp_discovery":
            self._scan_phase = "ping"
            self.status_label.setText(f"TCP после ICMP: {done}/{total} · Найдено: {found}")
        elif name == "names":
            self._scan_phase = "analyze"
            self.status_label.setText(f"Имена: {done}/{total} · Найдено: {found}")
        elif name == "ports":
            self._scan_phase = "analyze"
            self.status_label.setText(f"Пробы: {done}/{total} · Найдено: {found}")
        elif name == "finish":
            self._scan_phase = "analyze"
            self.status_label.setText(f"Анализ: {done}/{total} · Найдено: {found}")
        phase_order = getattr(self, "_phase_order", ["ping", "names", "ports", "finish"])
        phase = "ping" if name == "tcp_discovery" else name
        if phase in phase_order:
            self.status_label.setText(
                f"Этап {phase_order.index(phase) + 1} из {len(phase_order)} · {self.status_label.text()}"
            )

    def _update_status_label(self):
        """Статус вне скана (фаза «Готово»): исходный/финальный текст."""
        self._scan_phase = "done"
        self.status_label.setText(
            f"Сканировано: {self.hosts_scanned}/{self.total_hosts} · "
            f"Найдено: {self._found}"
        )

    def thread_finished(self):
        self._coordinator_done = True
        self._drain_results()

    def _finish_scan_ui(self):
        if self.thread is not None:
            self.thread.deleteLater()
        self.thread = None
        self.worker = None
        self.scan_in_progress = False
        self.motion_tracker.stop()
        self.motion_tracker.hide()
        self.motion_tracker.set_count(self._found)
        self.progress_bar.setVisible(False)
        self.scan_button.setText("Сканировать")
        self._set_inputs_enabled(True)
        if hasattr(self, "search_input"):
            self._filter_results()
        self._refresh_result_summary()
        self._update_status_label()
        if self._outcome == "success":
            self.status_label.setText(
                f"Готово · Сканировано: {self.hosts_scanned}/{self.total_hosts} · "
                f"Найдено: {self._found}"
            )
        elif self._outcome == "cancelled":
            self.status_label.setText(f"Отменено · Сканировано: {self.hosts_scanned}/{self.total_hosts} · Найдено: {self._found}")
        elif self._outcome == "error":
            self.status_label.setText(f"Ошибка сканирования: {self._scan_error}")
        if self._result_limit_reached:
            self.status_label.setText(
                self.status_label.text() + f" · Лимит списка: {MAX_SCAN_ADDRESSES}. Очистите результаты."
            )
        if self._clear_pending:
            self._clear_pending = False
            self.clear_results()
        self._scan_phase = "done"
        # Результаты под текущую сортировку (новые хосты добавлялись в конец)
        try:
            header = self.results_tree.header()
            col = header.sortIndicatorSection()
            order = header.sortIndicatorOrder()
            if col >= 0:
                self.results_tree.sort_recursive(col, order)
        except Exception as e:
            logger.debug(f"IPScannerDialog: post-scan sort failed: {e}")

    def reject(self):
        """Escape/системное закрытие: единая точка выхода через closeEvent."""
        self.close()

    def closeEvent(self, event):
        thread = getattr(self, "thread", None)
        closing = getattr(self, "_closing_after_abort", False)
        scan_active = (
            self.scan_in_progress
            and self.worker is not None
            and self.thread is not None
            and self.thread.isRunning()
        )
        if scan_active and not closing:
            answer = QMessageBox.question(
                self,
                "Сканирование",
                "Сканирование ещё идёт. Прервать и закрыть?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.abort()
            self._closing_after_abort = True
        if thread is not None and thread.isRunning():
            # Keep the dialog and its unparented coordinator strongly owned
            # until actual QThread.finished, not merely an expired wait().
            thread.quit()
            event.ignore()
            scan_id = getattr(self, "_scan_id", None)
            # The menu can reopen this persistent dialog after drain but
            # before the old timer fires. Do not close the new lifecycle.
            QTimer.singleShot(100, self, lambda: self.close() if (
                self._closing_after_abort and getattr(self, "_scan_id", None) == scan_id
            ) else None)
            return
        shutdown_scanner_pools()
        event.accept()

    def has_pending_workers(self) -> bool:
        thread = getattr(self, "thread", None)
        return self.scan_in_progress or (thread is not None and thread.isRunning())

    def done(self, result: int) -> None:
        """DialogManager.done() must obey the same ownership guard as close()."""
        thread = getattr(self, "thread", None)
        if thread is not None and thread.isRunning():
            if not self._closing_after_abort:
                self._closing_after_abort = True
                if self.worker is not None:
                    self.worker.abort()
                thread.quit()
            scan_id = getattr(self, "_scan_id", None)
            QTimer.singleShot(100, self, lambda: self.done(result) if (
                self._closing_after_abort and getattr(self, "_scan_id", None) == scan_id
            ) else None)
            return
        shutdown_scanner_pools()
        super().done(result)

    def export_results(self) -> None:
        """Сохранить отфильтрованные результаты с текущей сортировкой в CSV."""
        if self.scan_in_progress:
            return
        model = self.results_tree._proxy
        rows = []
        for folder_row in range(model.rowCount()):
            folder = model.index(folder_row, 0)
            for row in range(model.rowCount(folder)):
                rows.append([
                    model.index(row, column, folder).data() or ""
                    for column in range(model.columnCount())
                ])
        if not rows:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт результатов сканирования", "scan-results.csv", "CSV (*.csv)"
        )
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter=";")
        writer.writerow([
            model.headerData(column, Qt.Orientation.Horizontal)
            for column in range(model.columnCount())
        ])
        writer.writerows(rows)
        payload = stream.getvalue().encode("utf-8-sig")
        output = QSaveFile(path)
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            QMessageBox.critical(self, "Экспорт", output.errorString())
            return
        if output.write(payload) != len(payload):
            error = output.errorString()
            output.cancelWriting()
            QMessageBox.critical(self, "Экспорт", error)
            return
        if not output.commit():
            QMessageBox.critical(self, "Экспорт", output.errorString())

    def export_scanned_devices(self):
        if self.scan_in_progress:
            return
        devices = self.results_tree.get_checked_devices()

        if not devices:
            QMessageBox.warning(
                self, "Сканирование", "Не выбраны хосты для добавления."
            )
            return

        # Эмитим сигнал с устройствами, а папку определим в MainWindow
        self.exported_device.emit(devices, None)

    def check_state_changed(self, text, checked):
        port = int(text)
        self._ports[port] = checked

    def radio_group_clicked(self, button: QRadioButton):
        checked_id = self.radio_group.checkedId()
        self.port_combo_box.setEnabled(checked_id in (2, 3))
        self.edit_ports_button.setEnabled(checked_id in (2, 3))
        icmp_enabled = checked_id in (1, 3) and not getattr(self, "scan_in_progress", False)
        self.ping_backend_combo.setEnabled(icmp_enabled)
        self.ping_timeout_slider.setEnabled(icmp_enabled)

    def clear_results(self):
        """Очистить результаты сканирования. Останавливает активный скан, если есть."""
        if self.has_pending_workers():
            self._clear_pending = True
            self.cancel_scan()
            self.status_label.setText("Отмена сканирования перед очисткой…")
            return
        self.results_tree.clear_tree()
        self.motion_tracker.reset()
        if hasattr(self, "search_input"):
            self._filter_results()
        self._refresh_result_summary()
        self.hosts_scanned = 0
        self._found = 0
        self._scan_phase = "done"
        self._update_status_label()
        self.status_label.setText("Результаты очищены")
        logger.debug("IPScannerDialog: Results cleared")

    def update_results(
        self,
        ip_address,
        hostname,
        is_active,
        ok_ports,
        mac_address="",
        device_type="",
        type_reason="",
        state="done",
    ):
        """Обновить результаты сканирования.

        Тип устройства и причина классификации вычисляются в воркере
        (ScannerRunnable.execute) и передаются через ``device_type`` /
        ``type_reason``. Повторная классификация в UI-потоке не выполняется.

        state="scan" — промежуточный (после фазы пинга, тип определяется);
        state="scan2" — промежуточный (после фазы имён);
        state="done" — финальный.
        """
        if not self._accept_scan_event():
            return
        if not is_active and ip_address not in self.results_tree._hosts:
            return
        if ip_address not in self.results_tree._hosts and len(self.results_tree._hosts) >= MAX_SCAN_ADDRESSES:
            self._result_limit_reached = True
            return

        display_mac = mac_address if mac_address else None

        device_data = {
            "name": hostname or ip_address,
            "host": ip_address,
            "port": None,
            "mac_address": display_mac,
        }
        device = DeviceModel(device_data)

        # Определяем подсеть для группировки
        subnet = self._get_subnet_for_ip(ip_address)

        # Ищем или создаем папку
        existing = self.results_tree._hosts.get(ip_address)
        folder = existing.parent() if existing is not None else self.results_tree._folders.get(subnet)
        if folder is None:
            folder = self.results_tree.add_folder(subnet)

        # Обновляем или добавляем устройство в папке
        self.results_tree.update_device_in_folder(
            folder,
            device,
            is_active,
            ok_ports,
            display_mac,
            device_type,
            type_reason,
            state,
        )

    def open_ports_editor(self):
        dlg = PortsEditorDialog(self, ports=self._ports)
        if dlg.exec():
            self._ports = dict(dlg.ports)
            self.port_combo_box.blockSignals(True)
            try:
                self.port_combo_box.clear()
                for port, permit in self._ports.items():
                    self.port_combo_box.addItem(text=str(port), checked=permit)
            finally:
                self.port_combo_box.blockSignals(False)
