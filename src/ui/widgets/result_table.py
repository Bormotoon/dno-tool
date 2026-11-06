"""Таблица результатов выполнения команд с отслеживанием статуса и контекстным меню."""

import html
import re
import time
from collections import deque
from collections.abc import Iterator, Mapping
from datetime import datetime

from PySide6.QtWidgets import (
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QMenu,
    QFileDialog,
    QMessageBox,
    QStyle,
    QStyleOptionViewItem,
    QStyleOptionHeader,
    QToolButton,
    QHBoxLayout,
    QLayout,
    QWidget,
)
from PySide6.QtGui import QIcon, QAction, QPainter, QResizeEvent
from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer, Signal, Slot
from src.domain.models.device import DeviceModel
from src.domain.device_identity import DeviceKey, device_identity
from src.ui.icons import _icon_from_key, qicon_from_path  # zip-темы: иконки из памяти
from src.config import COMMAND_OUTPUT_HEAD_CHARS, COMMAND_OUTPUT_TAIL_CHARS, ICONS
from src.logger import logger
from src.utils.text.command_output import CommandOutputText
from src.utils.text.output_stream import BoundedOutput
import csv

# Интервал обновления столбца «Время» в реальном времени, мс.
ELAPSED_TICK_MS = 200

# Таблица результатов: видимое превью в ячейке и хинт при наведении.
PREVIEW_CHARS = 200
TOOLTIP_LINES = 20
TOOLTIP_HEAD_LINES = 10
TOOLTIP_TAIL_LINES = 10
TOOLTIP_CHARS = 2000
TOOLTIP_LINE_CHARS = 160
# Отображение живого вывода: обновление по таймеру с бюджетом времени на тик.
PROGRESS_FLUSH_INTERVAL_MS = 100
PROGRESS_FLUSH_RETRY_MS = 16
PROGRESS_FLUSH_BUDGET_MS = 8
PROGRESS_KEY_SLICE_CHARS = 16 * 1024
PROGRESS_PENDING_MAX_CHARS = 256 * 1024
# Итог длиннее лимита разбирается только по началу и концу: остальное не видно.
RESULT_RENDER_MAX_CHARS = 2 * 1024 * 1024
# Для ячейки и подсказки нужны лишь первые и последние строки.
_PREVIEW_RETAIN_LINES = (PREVIEW_CHARS + 56, TOOLTIP_TAIL_LINES + 6)
_TERMINAL_STATUSES = ("Отменено", "Потеря связи", "Успех", "Неудача")
_RESULT_STATUSES = ("Ожидание", "Выполнение", "Ожидание ввода", *_TERMINAL_STATUSES)


class _DeviceOutputs(Mapping[DeviceKey, str]):
    """Вывод хостов: ограниченный живой буфер до итога, затем итоговый текст."""

    def __init__(self) -> None:
        self._live: dict[DeviceKey, BoundedOutput] = {}
        self._final: dict[DeviceKey, str] = {}

    def __getitem__(self, key: DeviceKey) -> str:
        final = self._final.get(key)
        return final if final is not None else self._live[key].text()

    def __iter__(self) -> Iterator[DeviceKey]:
        return iter(self._live)

    def __len__(self) -> int:
        return len(self._live)

    def start(self, key: DeviceKey) -> None:
        self._live[key] = BoundedOutput(
            COMMAND_OUTPUT_HEAD_CHARS, COMMAND_OUTPUT_TAIL_CHARS
        )
        self._final.pop(key, None)

    def append(self, key: DeviceKey, chunk: str) -> None:
        self._live[key].append(chunk)

    def set_final(self, key: DeviceKey, text: str) -> None:
        self._final[key] = text

    def clear(self) -> None:
        self._live.clear()
        self._final.clear()


def _bounded_for_display(text: str) -> str:
    """Разбирать для ячейки только края очень длинного итога."""
    if len(text) <= RESULT_RENDER_MAX_CHARS:
        return text
    half = RESULT_RENDER_MAX_CHARS // 2
    return text[:half] + "\n" + text[-half:]


class ResultHeaderView(QHeaderView):
    """Заголовок с переключателем полного пути в первой колонке."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.path_button = QToolButton(self.viewport())
        self.path_button.setAutoRaise(True)
        self.path_button.setCheckable(True)
        self.path_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.path_button.setAccessibleName("Полный путь хоста")
        self._button_layout = QHBoxLayout(self.viewport())
        self._button_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._button_layout.setDirection(QHBoxLayout.Direction.LeftToRight)
        self._button_layout.setSpacing(0)
        self._button_layout.addWidget(
            self.path_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        self._button_layout.addStretch()
        self.path_button.toggled.connect(self._update_button)
        self.sectionResized.connect(self._layout_button)
        self.sectionMoved.connect(self._layout_button)
        self.geometriesChanged.connect(self._layout_button)
        self.refresh_icons()

    def refresh_icons(self) -> None:
        """Обновить значок и метрики кнопки для текущего оформления."""
        size = self.style().pixelMetric(QStyle.PixelMetric.PM_SmallIconSize)
        self.path_button.setIconSize(QSize(size, size))
        self._update_button()
        self._layout_button()
        self.updateGeometry()

    def _update_button(self) -> None:
        """Подсказка описывает действие следующего нажатия."""
        full = self.path_button.isChecked()
        self.path_button.setIcon(
            _icon_from_key("result_host_path" if full else "result_host_name")
        )
        self.path_button.setToolTip(
            "Показывать только имя хоста" if full else "Показывать полный путь хоста"
        )

    def sizeHint(self) -> QSize:
        size = super().sizeHint()
        if hasattr(self, "path_button"):
            size.setHeight(max(size.height(), self.path_button.sizeHint().height() + 4))
        return size

    def _layout_button(self) -> None:
        """Привязать кнопку к левому краю видимой части секции."""
        if not hasattr(self, "_button_layout"):
            return
        start = self.sectionViewportPosition(0) if self.count() else 0
        end = min(self.viewport().width(), start + self.sectionSize(0))
        grip = self.style().pixelMetric(QStyle.PixelMetric.PM_HeaderGripMargin) + 2
        visible = (
            self.count() > 0
            and not self.isSectionHidden(0)
            and end - max(0, start) >= self.path_button.sizeHint().width() + 2 * grip
        )
        self.path_button.setVisible(visible)
        self._button_layout.setContentsMargins(
            max(0, start) + grip, 0, max(0, self.viewport().width() - end) + grip, 0
        )
        self._button_layout.activate()
        self.viewport().update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._layout_button()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        self._layout_button()

    def paintSection(self, painter: QPainter, rect: QRect, logical_index: int) -> None:
        if logical_index != 0 or self.path_button.isHidden():
            super().paintSection(painter, rect, logical_index)
            return
        option = QStyleOptionHeader()
        self.initStyleOptionForIndex(option, logical_index)
        option.rect = rect
        painter.save()
        self.style().drawControl(
            QStyle.ControlElement.CE_HeaderSection, option, painter, self
        )
        option.rect.setLeft(self.path_button.geometry().right() + 2)
        self.style().drawControl(
            QStyle.ControlElement.CE_HeaderLabel, option, painter, self
        )
        painter.restore()


class CommandResultTable(QTableWidget):
    """Model for command execution results table"""

    COL_HOST = 0
    COL_TEXT = 1
    COL_ELAPSED = 2
    COL_STATUS = 3
    COL_IID = 4
    HOST_NAME_ROLE = Qt.ItemDataRole.UserRole + 1
    HOST_PATH_ROLE = Qt.ItemDataRole.UserRole + 2

    full_host_path_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.device_rows = {}
        self.device_outputs = _DeviceOutputs()
        self._output_text: dict[DeviceKey, CommandOutputText] = {}
        self._pending_output: dict[DeviceKey, deque[str]] = {}
        self._pending_chars: dict[DeviceKey, int] = {}
        self._progress_timer = QTimer(self)
        self._progress_timer.setSingleShot(True)
        self._progress_timer.timeout.connect(self._flush_progress)
        self._start_times: dict[DeviceKey, float] = {}
        self._elapsed: dict[DeviceKey, float] = {}
        self._finished: set[DeviceKey] = set()
        self._show_full_host_path = False
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(ELAPSED_TICK_MS)
        self._tick_timer.timeout.connect(self._update_elapsed)
        self._load_status_icons()
        self.setup_ui()
        self.setup_context_menu()

    def _load_status_icons(self) -> None:
        """Перечитать иконки статусов из ICONS (текущая тема иконок)."""
        self._icon_pending = _icon_from_key("result_pending")
        self._icon_executing = _icon_from_key("result_executing")
        self._icon_success = _icon_from_key("result_success")
        self._icon_failure = _icon_from_key("result_failure")
        self._icon_cancelled = _icon_from_key("result_cancelled")
        self._icon_connection_lost = _icon_from_key("result_connection_lost")
        self._icon_warning = _icon_from_key("result_warning")

    def refresh_icons(self) -> None:
        """Обновить иконки под текущую тему: кэш + уже выведенные строки."""
        self._load_status_icons()
        self.horizontalHeader().refresh_icons()
        icon_by_text = {
            "Ожидание": self._icon_pending,
            "Выполнение": self._icon_executing,
            "Ожидание ввода": self._icon_warning,
            "Успех": self._icon_success,
            "Отменено": self._icon_cancelled,
            "Потеря связи": self._icon_connection_lost,
            "Неудача": self._icon_failure,
        }
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_STATUS)
            if item is not None and item.text() in icon_by_text:
                item.setIcon(icon_by_text[item.text()])

    def setup_ui(self):
        """Setup table UI and behavior"""
        self.setColumnCount(5)
        self.setHorizontalHeaderLabels(
            ["Имя хоста", "Текст выполнения", "Время", "Результат", "iid"]
        )

        self.setHorizontalHeader(ResultHeaderView(self))
        header = self.horizontalHeader()
        header.path_button.toggled.connect(self.set_show_full_host_path)
        self.horizontalScrollBar().valueChanged.connect(header._layout_button)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionHidden(4, True)

        self.setColumnWidth(self.COL_HOST, 150)
        self._update_column_widths()
        self.iconSizeChanged.connect(self._update_column_widths)
        self.setColumnWidth(4, 0)

        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def _update_column_widths(self) -> None:
        """Рассчитать постоянные ширины по образцам, не просматривая строки."""
        header = self.horizontalHeader()
        option = QStyleOptionViewItem()
        self.initViewItemOption(option)
        option.features = QStyleOptionViewItem.ViewItemFeature.HasDisplay

        def sample_width(text: str) -> int:
            option.text = text
            return self.style().sizeFromContents(
                QStyle.ContentsType.CT_ItemViewItem, option, QSize(), self
            ).width() + 1  # Место для линии сетки.

        elapsed_width = max(sample_width(text) for text in ("99:99", "99,9 с"))
        option.features |= QStyleOptionViewItem.ViewItemFeature.HasDecoration
        status_width = max(sample_width(text) for text in _RESULT_STATUSES)
        self.setColumnWidth(
            self.COL_ELAPSED, max(header.sectionSizeHint(self.COL_ELAPSED), elapsed_width)
        )
        self.setColumnWidth(
            self.COL_STATUS, max(header.sectionSizeHint(self.COL_STATUS), status_width)
        )

    def changeEvent(self, event: QEvent) -> None:
        """Обновить ширины при изменении метрик оформления таблицы."""
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            if self.columnCount() == 5:
                self.horizontalHeader().refresh_icons()
                self._update_column_widths()

    @property
    def show_full_host_path(self) -> bool:
        """Выбранный режим отображения имени хоста."""
        return self._show_full_host_path

    @Slot(bool)
    def set_show_full_host_path(self, full: bool) -> None:
        """Переключить существующие и будущие строки без изменения результатов."""
        if self._show_full_host_path == full:
            return
        self._show_full_host_path = full
        self.horizontalHeader().path_button.setChecked(full)
        role = self.HOST_PATH_ROLE if full else self.HOST_NAME_ROLE
        for row in range(self.rowCount()):
            item = self.item(row, self.COL_HOST)
            if item is not None:
                text = item.data(role)
                if text is not None:
                    item.setText(text)
        self.full_host_path_changed.emit(full)

    def setup_context_menu(self):
        """Setup context menu for the table"""
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    def show_context_menu(self, position):
        """Show context menu at the given position"""
        menu = QMenu()

        save_current_action = QAction(
            _icon_from_key("menu_save"), "Сохранить текущий результат", self
        )
        save_current_action.triggered.connect(self.save_current_result)
        menu.addAction(save_current_action)

        save_all_action = QAction(
            _icon_from_key("menu_export"), "Сохранить все результаты", self
        )
        save_all_action.triggered.connect(self.save_all_results)
        menu.addAction(save_all_action)

        clear_all_action = QAction(_icon_from_key("menu_delete"), "Очистить все", self)
        clear_all_action.triggered.connect(self.clear_results)
        menu.addAction(clear_all_action)

        menu.exec_(self.viewport().mapToGlobal(position))

    def save_current_result(self):
        """Save the currently selected result to a text file"""
        selected_items = self.selectedItems()
        if not selected_items:
            QMessageBox.warning(
                self, "Предупреждение", "Пожалуйста, выберите строку для сохранения."
            )
            return

        selected_row = selected_items[0].row()

        hostname_item = self.item(selected_row, self.COL_HOST)
        output_item = self.item(selected_row, self.COL_TEXT)
        result_item = self.item(selected_row, self.COL_STATUS)
        elapsed_item = self.item(selected_row, self.COL_ELAPSED)

        hostname = hostname_item.text() if hostname_item else ""
        short_name = (
            (hostname_item.data(self.HOST_NAME_ROLE) or hostname)
            if hostname_item else ""
        )
        filename_host = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", short_name).strip(" .")
        output = output_item.text() if output_item else ""
        result = result_item.text() if result_item else ""
        elapsed = elapsed_item.text() if elapsed_item else ""

        device_iid = None
        for iid_key, row_num in self.device_rows.items():
            if row_num == selected_row:
                device_iid = iid_key
                break

        if device_iid and device_iid in self.device_outputs:
            full_output = self.device_outputs[device_iid]
        else:
            full_output = output

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить результат",
            f"Результат_{filename_host or 'хост'}.txt",
            "Текстовые файлы (*.txt);;Все файлы (*)",
        )

        if file_path:
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(f"Hostname: {hostname}\n")
                    f.write(f"Result: {result}\n")
                    f.write(f"Время: {elapsed}\n")
                    f.write("-" * 50 + "\n")
                    f.write(full_output)
                QMessageBox.information(
                    self, "Успех", f"Результат сохранен в {file_path}"
                )
            except Exception as e:
                QMessageBox.critical(
                    self, "Ошибка", f"Не удалось сохранить файл:\n{str(e)}"
                )

    def save_all_results(self):
        """Save all results to a CSV file"""
        if self.rowCount() == 0:
            QMessageBox.warning(
                self, "Предупреждение", "Нет результатов для сохранения."
            )
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить все результаты",
            "Все_результаты_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".txt",
            "Текстовый файл с разделителем Tab (*.txt);;All Files (*)",
        )

        if file_path:
            try:
                with open(file_path, "w", newline="", encoding="utf-8") as f:
                    headers = []
                    for col in range(self.columnCount() - 1):
                        header_item = self.horizontalHeaderItem(col)
                        if header_item:
                            headers.append(header_item.text())
                        else:
                            headers.append(f"Column {col}")
                    f.write("\t".join(headers) + "\n")

                    for row in range(self.rowCount()):
                        row_data = []
                        for col in range(self.columnCount() - 1):
                            item = self.item(row, col)
                            if item:
                                row_data.append(item.text())
                            else:
                                row_data.append("")
                        f.write("\t".join(row_data) + "\n")

                QMessageBox.information(
                    self, "Успех", f"Все результаты сохранены в {file_path}"
                )
            except Exception as e:
                QMessageBox.critical(
                    self, "Ошибка", f"Не удалось сохранить файл:\n{str(e)}"
                )

    def clear_results(self):
        """Clear all results"""
        self._tick_timer.stop()
        self._progress_timer.stop()
        self.setRowCount(0)
        self.device_rows.clear()
        self.device_outputs.clear()
        self._output_text.clear()
        self._pending_output.clear()
        self._pending_chars.clear()
        self._start_times.clear()
        self._elapsed.clear()
        self._finished.clear()

    @staticmethod
    def format_elapsed(seconds: float) -> str:
        """Отформатировать длительность: <60с — '12,3 с', иначе 'мм:сс'."""
        if seconds < 0:
            seconds = 0.0
        if seconds < 60:
            return f"{seconds:.1f} с".replace(".", ",")
        total = int(seconds)
        if total >= 3600:
            h = total // 3600
            m = (total % 3600) // 60
            s = total % 60
            return f"{h}:{m:02d}:{s:02d}"
        return f"{total // 60:02d}:{total % 60:02d}"

    def _set_elapsed_item(self, row: int, text: str):
        item = self.item(row, self.COL_ELAPSED)
        if item is None:
            item = QTableWidgetItem(text)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self.setItem(row, self.COL_ELAPSED, item)
        else:
            item.setText(text)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )

    def _ensure_started(self, device_iid: DeviceKey):
        if device_iid not in self._start_times:
            self._start_times[device_iid] = time.monotonic()
        if not self._tick_timer.isActive():
            self._tick_timer.start()

    def _freeze(self, device_iid: DeviceKey):
        start = self._start_times.get(device_iid)
        if start is None:
            start = self._start_times[device_iid] = time.monotonic()
        if device_iid not in self._elapsed:
            self._elapsed[device_iid] = time.monotonic() - start
        self._finished.add(device_iid)
        row = self.device_rows.get(device_iid)
        if row is not None:
            self._set_elapsed_item(row, self.format_elapsed(self._elapsed[device_iid]))
        if len(self._finished) >= len(self.device_rows):
            self._tick_timer.stop()

    def _update_elapsed(self):
        try:
            now = time.monotonic()
            for device_iid, row in self.device_rows.items():
                if device_iid in self._finished:
                    continue
                start = self._start_times.get(device_iid)
                if start is None:
                    continue
                self._set_elapsed_item(row, self.format_elapsed(now - start))
        except Exception as e:
            logger.debug("result_table elapsed update: %s", e)

    def add_initial_entries(
        self, devices: list[DeviceModel], host_paths: dict[DeviceKey, str] | None = None
    ) -> None:
        """Добавить начальные строки с именами и снимком путей хостов.

        Стартовое время НЕ ставится: у каждого хоста свой отсчёт —
        с момента фактического старта (set_executing/update_progress).
        До старта строка показывает 0,0 с и тиком не обновляется.
        """
        for device in devices:
            row = self.rowCount()
            self.insertRow(row)
            full_path = (host_paths or {}).get(device_identity(device)) or device.name
            host_item = QTableWidgetItem(
                full_path if self._show_full_host_path else device.name
            )
            host_item.setData(self.HOST_NAME_ROLE, device.name)
            host_item.setData(self.HOST_PATH_ROLE, full_path)
            host_item.setToolTip(f"<qt>{html.escape(full_path)}</qt>")
            self.setItem(row, self.COL_HOST, host_item)
            self._set_text_item(row, "")
            self._set_status_item(row, "Ожидание", self._icon_pending)
            self._set_elapsed_item(row, self.format_elapsed(0.0))
            iid_item = QTableWidgetItem(str(device.iid))
            iid_item.setData(Qt.ItemDataRole.UserRole, device_identity(device))
            self.setItem(row, 4, iid_item)
            self.device_rows[device_identity(device)] = row
            self.device_outputs.start(device_identity(device))
            self._output_text[device_identity(device)] = CommandOutputText(
                retain_lines=_PREVIEW_RETAIN_LINES
            )
            self._drop_pending(device_identity(device))
            self._start_times.pop(device_identity(device), None)
            self._finished.discard(device_identity(device))
            self._elapsed.pop(device_identity(device), None)

    def set_executing(self, device: DeviceModel):
        """Set status to executing (connection/command started) for a device"""
        if device_identity(device) in self.device_rows:
            row = self.device_rows[device_identity(device)]

            status_item = self.item(row, self.COL_STATUS)
            if status_item and status_item.text() in (
                "Отменено",
                "Потеря связи",
                "Успех",
                "Неудача",
            ):
                return

            self._ensure_started(device_identity(device))
            self._set_status_item(row, "Выполнение", self._icon_executing)

    def set_input_requested(self, device: DeviceModel) -> None:
        """Обработать прежний сигнал входа в ожидание ввода."""
        self.set_input_wait_changed(device, True)

    @Slot(DeviceModel, bool)
    def set_input_wait_changed(self, device: DeviceModel, waiting: bool) -> None:
        """Обновить ожидание ввода, не меняя завершённый результат."""
        if device_identity(device) in self.device_rows:
            row = self.device_rows[device_identity(device)]

            status_item = self.item(row, self.COL_STATUS)
            if status_item and status_item.text() in _TERMINAL_STATUSES:
                return

            self._ensure_started(device_identity(device))
            if waiting:
                self._set_status_item(row, "Ожидание ввода", self._icon_warning)
            else:
                self._set_status_item(row, "Выполнение", self._icon_executing)

    def update_progress(self, device: DeviceModel, current_output: str) -> None:
        """Дополнить вывод за O(1); отображение обновляется по таймеру.

        Сырой текст доступен сразу, разбор откладывается: поток сигналов от
        многих хостов не должен занимать GUI-поток на каждый фрагмент.
        """
        try:
            key = device_identity(device)
            row = self.device_rows.get(key)
            if row is None:
                return
            status_item = self.item(row, self.COL_STATUS)
            if status_item and status_item.text() in _TERMINAL_STATUSES:
                return
            chunk = current_output or ""
            self.device_outputs.append(key, chunk)
            pending = self._pending_output.setdefault(key, deque())
            pending.append(chunk)
            total = self._pending_chars.get(key, 0) + len(chunk)
            # Отображение отстаёт: старое вытесняется, полный текст уже сохранён.
            while total > PROGRESS_PENDING_MAX_CHARS and len(pending) > 1:
                total -= len(pending.popleft())
            self._pending_chars[key] = total
            if not self._progress_timer.isActive():
                self._progress_timer.start(PROGRESS_FLUSH_INTERVAL_MS)
        except Exception as e:
            logger.debug("result_table status update: %s", e)

    def _drop_pending(self, key: DeviceKey) -> None:
        self._pending_output.pop(key, None)
        self._pending_chars.pop(key, None)

    def _flush_progress(self) -> None:
        """Отобразить накопленное по кругу, пока не исчерпан бюджет времени."""
        deadline = time.perf_counter() + PROGRESS_FLUSH_BUDGET_MS / 1000
        for key in list(self._pending_output):
            if time.perf_counter() >= deadline:
                break
            # Обработанный ключ уходит в конец: следующий тик начнёт с других.
            pending = self._pending_output.pop(key)
            try:
                self._render_progress(key, pending)
            except (RuntimeError, KeyError) as e:
                # Строка могла исчезнуть вместе с таблицей или новым запуском.
                logger.debug("result_table status update: %s", e)
                self._pending_chars.pop(key, None)
                continue
            if pending:
                self._pending_output[key] = pending
            else:
                self._pending_chars.pop(key, None)
        if self._pending_output:
            self._progress_timer.start(PROGRESS_FLUSH_RETRY_MS)

    def _render_progress(self, key: DeviceKey, pending: deque[str]) -> None:
        row = self.device_rows.get(key)
        if row is None:
            pending.clear()
            return
        status_item = self.item(row, self.COL_STATUS)
        if status_item and status_item.text() in _TERMINAL_STATUSES:
            pending.clear()
            return
        self._ensure_started(key)
        pieces: list[str] = []
        taken = 0
        while pending and taken < PROGRESS_KEY_SLICE_CHARS:
            piece = pending.popleft()
            room = PROGRESS_KEY_SLICE_CHARS - taken
            if len(piece) > room:
                pending.appendleft(piece[room:])
                piece = piece[:room]
            pieces.append(piece)
            taken += len(piece)
        self._pending_chars[key] = max(0, self._pending_chars.get(key, 0) - taken)
        output = self._output_text[key]
        output.feed("".join(pieces))
        self._set_output_item(row, output)
        current = status_item.text() if status_item else ""
        if current not in ("Ожидание ввода", "Выполнение"):
            self._set_status_item(row, "Выполнение", self._icon_executing)

    def _set_status_item(self, row: int, text: str, icon: QIcon):
        item = QTableWidgetItem(icon, text)
        self.setItem(row, self.COL_STATUS, item)

    @staticmethod
    def _make_preview(full: str) -> str:
        """Превью для ячейки: одна строка, первые PREVIEW_CHARS символов."""
        one_line = (
            (full or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        )
        one_line = one_line.replace("\t", " ")
        if len(one_line) > PREVIEW_CHARS:
            return one_line[:PREVIEW_CHARS] + "..."
        return one_line

    @staticmethod
    def _clean_tooltip_line(line: str) -> str:
        """Нормализовать одну строку для хинта.

        Прогресс-бары перерисовываются через ``\\r`` - как в терминале виден
        только текст после последнего ``\\r``. Длинные строки режутся до
        TOOLTIP_LINE_CHARS, чтобы хинт не растягивался на весь экран.
        """
        line = line.rstrip("\r")
        if "\r" in line:
            line = line.rsplit("\r", 1)[-1]
        if len(line) > TOOLTIP_LINE_CHARS:
            line = line[:TOOLTIP_LINE_CHARS] + "..."
        return line

    @staticmethod
    def _plural_lines(n: int) -> str:
        """Множественное число для 'строка': 1 строка, 3 строки, 10 строк."""
        if n % 10 == 1 and n % 100 != 11:
            return "строка"
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return "строки"
        return "строк"

    @staticmethod
    def _make_tooltip(full: str) -> str:
        """Хинт: до 20 строк целиком, иначе первые 10, ... и последние 10.

        Возвращает HTML с <pre> и моноширинным шрифтом, чтобы табличный
        вывод (ps, df, ls) не разъезжался. Qt определяет rich-text по тегам.
        """
        if not full:
            return ""
        parts = full.split("\n", TOOLTIP_LINES)
        if len(parts) > TOOLTIP_LINES:
            return CommandResultTable._format_tooltip(
                parts[:TOOLTIP_HEAD_LINES],
                full.rsplit("\n", TOOLTIP_TAIL_LINES)[-TOOLTIP_TAIL_LINES:],
                full.count("\n") + 1 - TOOLTIP_HEAD_LINES - TOOLTIP_TAIL_LINES,
            )
        return CommandResultTable._format_tooltip(parts, None, 0)

    @staticmethod
    def _tooltip_from_output(output: CommandOutputText) -> str:
        """Та же подсказка, что у _make_tooltip, но без сборки всего текста."""
        preview = output.preview_parts(
            TOOLTIP_LINES, TOOLTIP_TAIL_LINES, TOOLTIP_LINE_CHARS + 1
        )
        if preview.empty:
            return ""
        if preview.newlines >= TOOLTIP_LINES:
            return CommandResultTable._format_tooltip(
                preview.head[:TOOLTIP_HEAD_LINES],
                preview.tail,
                preview.newlines + 1 - TOOLTIP_HEAD_LINES - TOOLTIP_TAIL_LINES,
            )
        return CommandResultTable._format_tooltip(preview.head, None, 0)

    @staticmethod
    def _format_tooltip(head_lines, tail_lines, skipped: int) -> str:
        if tail_lines is not None:
            sep = (
                f"... пропущено {skipped} "
                f"{CommandResultTable._plural_lines(skipped)} ..."
            )
            head = [CommandResultTable._clean_tooltip_line(l) for l in head_lines]
            tail = [CommandResultTable._clean_tooltip_line(l) for l in tail_lines]
            lines = head
            if head[-1].strip() != "":
                lines.append("")
            lines.append(sep)
            if tail[0].strip() != "":
                lines.append("")
            lines.extend(tail)
        else:
            lines = [CommandResultTable._clean_tooltip_line(l) for l in head_lines]
        head = "\n".join(lines)
        if len(head) > TOOLTIP_CHARS:
            head = head[:TOOLTIP_CHARS] + "..."
        body = html.escape(head)
        return (
            "<pre style=\"font-family: Consolas, 'DejaVu Sans Mono', monospace; "
            'white-space: pre;">'
            f"{body}</pre>"
        )

    def _set_text_item(self, row: int, full: str):
        """Записать ячейку COL_TEXT: превью + хинт первых строк."""
        item = QTableWidgetItem(self._make_preview(full))
        tooltip = self._make_tooltip(full)
        if tooltip:
            item.setToolTip(tooltip)
        self.setItem(row, 1, item)

    def _set_output_item(self, row: int, output: CommandOutputText) -> None:
        """То же содержимое, что у _set_text_item, за время, не зависящее от объёма."""
        preview = self._make_preview(output.head_text(PREVIEW_CHARS + 1))
        tooltip = self._tooltip_from_output(output)
        item = self.item(row, 1)
        if item is None:
            item = QTableWidgetItem(preview)
            self.setItem(row, 1, item)
        else:
            item.setText(preview)
        item.setToolTip(tooltip)

    def _show_final_output(self, row: int, key: DeviceKey, text: str) -> None:
        output = self._output_text[key]
        output.reset(_bounded_for_display(text))
        self._set_output_item(row, output)

    def set_result(self, device: DeviceModel, success: bool, error_message: str = ""):
        """Set final result for host"""
        if success is None:
            success = False

        if device_identity(device) in self.device_rows:
            row = self.device_rows[device_identity(device)]

            status_item = self.item(row, self.COL_STATUS)
            if status_item and status_item.text() in ("Отменено", "Потеря связи"):
                return

            key = device_identity(device)
            # Итог авторитетен: отложенные фрагменты больше не отображаются.
            self._drop_pending(key)
            if success:
                if error_message:
                    self.device_outputs.set_final(key, error_message)
                self._show_final_output(row, key, self.device_outputs.get(key, ""))
                self._set_status_item(row, "Успех", self._icon_success)
            else:
                error_text = error_message if error_message else "Неизвестная ошибка"
                self.device_outputs.set_final(key, error_text)
                self._show_final_output(row, key, error_text)
                if "aborted" in error_text.lower() or "прервано" in error_text.lower():
                    self._set_status_item(row, "Отменено", self._icon_cancelled)
                elif (
                    "connection lost" in error_text.lower()
                    or "потеря связи" in error_text.lower()
                ):
                    self._set_status_item(
                        row, "Потеря связи", self._icon_connection_lost
                    )
                else:
                    self._set_status_item(row, "Неудача", self._icon_failure)
            self._freeze(key)

    def mark_pending_as_cancelled(self):
        """Mark all pending/waiting results as cancelled by user"""
        for device_iid, row in self.device_rows.items():
            status_item = self.item(row, self.COL_STATUS)
            if status_item:
                current_status = status_item.text()
                if current_status in ("Ожидание", "Выполнение", "Ожидание ввода"):
                    self._set_status_item(row, "Отменено", self._icon_cancelled)
                    self._freeze(device_iid)

    def count_results(self) -> tuple:
        """Вернуть (total, success, fail, cancelled) по содержимому таблицы.

        Используется статус-строкой MainWindow для итогового сообщения
        после завершения выполнения команд.
        """
        total = self.rowCount()
        success = 0
        fail = 0
        cancelled = 0
        for row in range(total):
            status_item = self.item(row, self.COL_STATUS)
            if status_item is None:
                continue
            text = status_item.text()
            if text == "Успех":
                success += 1
            elif text == "Неудача":
                fail += 1
            elif text in ("Отменено", "Потеря связи"):
                cancelled += 1
        return (total, success, fail, cancelled)
