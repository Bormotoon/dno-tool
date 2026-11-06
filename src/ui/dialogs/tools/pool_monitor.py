"""Компактный немодальный монитор ресурсов и пулов; обновление раз в секунду."""

from __future__ import annotations

import math
import sys
import time
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizeGrip,
    QSizePolicy,
    QStackedLayout,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionProgressBar,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.config import config
from src.logger import logger
from src.utils.system.pool_registry import (
    QtPoolSnapshot,
    get_registered_pools,
    process_thread_summary,
)
from src.utils.text.time_format import format_duration as _fmt_duration
from src.workers.application_pools import get_created_application_pools
from src.workers.task_presentation import task_label as _task_label
from src.workers.tracked_pool import TrackedThreadPoolExecutor

if TYPE_CHECKING:
    from src.services.remote.veyon_service import VeyonService

REFRESH_INTERVAL_MS = 1000

_SOURCE_LABELS = {"application": "Приложение", "web": "Веб", "nodes": "Ноды"}

_ERROR_LABELS = {
    "offline": "Хост недоступен",
    "unavailable": "Источник недоступен",
    "auth_failed": "Ошибки аутентификации",
    "timeout": "Таймауты",
    "protocol_error": "Ошибки протокола",
    "unsupported": "Не поддерживается",
    "busy": "Источник занят",
    "cancelled": "Отменено",
}

_VEYON_ERROR_LABELS = {
    1: "Неверные данные",
    2: "Соединение закрыто",
    3: "Неизвестное действие",
    4: "Неверные учётные данные",
    5: "Метод аутентификации недоступен",
    6: "Ошибка аутентификации",
    7: "Превышен лимит подключений",
    8: "Таймаут подключения",
    10: "Экран недоступен",
    11: "Ошибка кодирования кадра",
    12: "Несовпадение протокола",
}


def _number(value: Any) -> float | None:
    """Принять только конечное неотрицательное значение метрики."""
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _fmt_number(value: Any, decimals: int = 0) -> str:
    number = _number(value)
    if number is None:
        return "нет данных"
    text = f"{number:,.{decimals}f}"
    if decimals:
        text = text.rstrip("0").rstrip(".")
    return text.replace(",", "\u00a0").replace(".", ",")


def _fmt_load(stats: dict[str, Any]) -> str:
    """Занятость пула: лимит из настройки и отдельно резерв для кнопок."""
    text = f"{_fmt_number(stats['active'])} / {_fmt_number(stats['max_workers'])}"
    reserved = stats.get("interactive_reserved") or 0
    return f"{text} +{_fmt_number(reserved)}" if reserved else text


def _fmt_bytes(value: Any) -> str:
    size = _number(value)
    if size is None:
        return "нет данных"
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ")
    for index, unit in enumerate(units):
        if size < 1024 or index == len(units) - 1:
            return f"{_fmt_number(size, 0 if index == 0 else 1)} {unit}"
        size /= 1024
    return "нет данных"


def _fmt_megapixels(value: Any) -> str:
    """Площадь экрана в мегапикселях (1 МП = 1 000 000 пикселей)."""
    number = _number(value)
    if number is None:
        return "нет данных"
    return f"{_fmt_number(number / 1_000_000, 1)} МП"


def _fmt_mem_mb(mb: Any) -> str:
    """Форматировать мегабайты в тех же единицах, что и остальные метрики."""
    value = _number(mb)
    return _fmt_bytes(value * 1048576 if value is not None else None)


def _task_target(metadata: dict[str, Any]) -> str:
    """Показать имя и адрес хоста вместе с явно заданной нодой."""
    device, host = metadata.get("device"), metadata.get("host")
    target = (
        f"{device} ({host})" if device and host and device != host else device or host
    )
    node = metadata.get("node") or metadata.get("node_iid")
    return " · ".join(str(value) for value in (target, node) if value) or "—"


def _source_label(metadata: dict[str, Any]) -> str:
    return _SOURCE_LABELS.get(metadata.get("source"), "Не указан")


class _WebApiStatsSignals(QObject):
    ready = Signal(dict)


class _WebApiStatsTask(QRunnable):
    """Сбор статуса процесса и TCP вне GUI-потока."""

    def __init__(self, service: VeyonService) -> None:
        super().__init__()
        self.service = service
        self.signals = _WebApiStatsSignals()

    def run(self) -> None:
        result: dict[str, Any] = {"status": "unknown", "stats": {}}
        try:
            result["status"] = (
                self.service.get_service_status()
                if self.service.is_cli_available()
                else "cli_missing"
            )
            result["stats"] = self.service.get_webapi_server_stats()
            if result["status"] != "cli_missing":
                result["limit"] = self.service.get_webapi_connection_limit()
        except Exception:  # noqa: BLE001 - сбой диагностики не останавливает монитор
            logger.debug("PoolMonitor: WebAPI stats unavailable", exc_info=True)
        self.signals.ready.emit(result)


def _configured_thumbnail_sources(
    default: str, hosts: list[dict[str, Any]]
) -> set[str]:
    """Локальные источники: общая настройка и явные переопределения хостов."""
    sources = {default}
    for host in hosts:
        node_iid = host.get("node_iid")
        if node_iid and node_iid != "local":
            continue
        backend = host.get("thumbnail_backend")
        if backend in {"vnc", "veyon"}:
            sources.add(backend)
    return sources & {"vnc", "veyon"}


def _error_rows(errors: Any) -> list[tuple[str, str]]:
    """Локализовать известные ошибки, не выводя произвольные строки backend-а."""
    if not isinstance(errors, dict):
        return []
    rows: list[tuple[str, str]] = []
    for code, count in sorted(errors.items(), key=lambda item: str(item[0])):
        name = _ERROR_LABELS.get(str(code))
        if name is None:
            try:
                number = int(code)
            except (TypeError, ValueError):
                name = "Неизвестная ошибка"
            else:
                name = _VEYON_ERROR_LABELS.get(number, f"Неизвестная ошибка ({number})")
        rows.append((name, _fmt_number(count)))
    return rows


def _thumbnail_metric_rows(values: dict[str, Any]) -> list[tuple[str, str]]:
    """Вернуть только разрешённые агрегаты с локализованными единицами."""
    counts = {
        "sessions": "Сеансы",
        "max_sessions": "Лимит сеансов",
        "active_captures": "Текущие захваты",
        "captures": "Подготовлено изображений (всего)",
        "total_captures": "Попытки захвата",
        "cache_hits": "Выдано из кэша (всего)",
        "connections": "Подключения (всего)",
        "reconnects": "Переподключения",
        "busy": "Запросы без свободных ресурсов (всего)",
        "evictions": "Удаления из кэша / сеансов (всего)",
        "auth_failed": "Ошибки аутентификации",
        "auth_failures": "Ошибки аутентификации",
        "timeout": "Таймауты",
        "timeouts": "Таймауты",
        "protocol_error": "Ошибки протокола",
        "protocol_errors": "Ошибки протокола",
        "black_frames": "Чёрные кадры",
        "server_port_closed": "Пропуски: порт Veyon закрыт",
        "idle_sweeps": "Очистки неактивных соединений",
        "cache_entries": "Записей в общем кэше",
        "cache_entries_limit": "Лимит записей кэша",
        "max_session_records": "Лимит записей реестра сеансов",
        "max_image_variants": "Лимит вариантов на сеанс",
        "max_thumbnail_dimension": "Макс. сторона миниатюры (px)",
    }
    sizes = {
        "framebuffer_bytes": "Экран: сырые пиксели RFB",
        "encoded_cache_bytes": "Кэш VNC: готовые PNG/JPEG",
        "memory_bytes": "Занято данными и буферами VNC",
        "memory_reserved_bytes": "Учтено памяти с рабочими буферами",
        "max_memory_bytes": "Бюджет памяти VNC",
        "cache_bytes": "Кэш готовых изображений (PNG/JPEG)",
        "cache_bytes_limit": "Лимит кэша",
        "bytes_received": "Сетевой трафик: получено (всего)",
        "bytes_sent": "Сетевой трафик: отправлено (всего)",
    }
    durations = {
        "capture_seconds": "Время захвата (суммарно)",
        "last_capture_seconds": "Последний захват",
        "encoding_seconds": "Время кодирования (суммарно)",
        "encode_seconds": "Время кодирования (суммарно)",
    }
    rows: list[tuple[str, str]] = []
    for labels, formatter in (
        (counts, _fmt_number),
        (sizes, _fmt_bytes),
        (durations, _fmt_duration),
    ):
        rows.extend(
            (label, formatter(values[key]))
            for key, label in labels.items()
            if key in values
        )
    if "max_screen_pixels" in values:
        rows.append(
            ("Лимит площади экрана", _fmt_megapixels(values["max_screen_pixels"]))
        )
    peak = values.get("memory_peak_bytes")
    if peak is not None:
        rows.append(("Пик резервирования (backend, не RSS)", _fmt_bytes(peak)))
    errors = values.get("errors")
    if errors is not None and not isinstance(errors, dict):
        rows.append(("Ошибки всего", _fmt_number(errors)))
    rows.extend(_error_rows(errors))
    rows.extend(_error_rows(values.get("errors_by_code")))
    return rows


class _DrawerResizeHandle(QWidget):
    """Левая граница панели, изменяющая её ширину при перетаскивании."""

    dragged = Signal(int)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setToolTip("Перетащите, чтобы изменить ширину панели")
        self._last_x: int | None = None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._last_x = round(event.globalPosition().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._last_x is not None:
            x = round(event.globalPosition().x())
            self.dragged.emit(self._last_x - x)
            self._last_x = x
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._last_x = None
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _TaskTable(QTableWidget):
    """Отдаёт прирост ширины задаче, сохраняя ручные размеры столбцов."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(0, 5, parent)
        self._task_width = 150
        self._adjusting_width = False
        self.horizontalHeader().sectionResized.connect(self._remember_width)

    def _remember_width(self, index: int, _old: int, width: int) -> None:
        if not self._adjusting_width and self.isVisible():
            if index == 0:
                self._task_width = width
            self._fit_task_width()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_task_width()

    def _fit_task_width(self) -> None:
        """Отдать действию свободное место после остальных четырёх колонок."""
        self._adjusting_width = True
        try:
            self.setColumnWidth(
                0,
                max(
                    self._task_width,
                    self.viewport().width()
                    - sum(self.columnWidth(column) for column in range(1, 5)),
                ),
            )
        finally:
            self._adjusting_width = False


class _PoolLoadDelegate(QStyledItemDelegate):
    """Полоса с числовой загрузкой в строке общего списка пулов."""

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        values = index.data(Qt.ItemDataRole.UserRole + 1)
        if values is None:
            super().paint(painter, option, index)
            return
        active, limit = values
        progress = QStyleOptionProgressBar()
        progress.rect = option.rect.adjusted(8, 5, -8, -5)
        progress.palette = option.palette
        progress.minimum = 0
        progress.maximum = max(1, limit)
        progress.progress = active
        progress.text = index.data(Qt.ItemDataRole.DisplayRole)
        progress.textVisible = True
        progress.textAlignment = Qt.AlignmentFlag.AlignCenter
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ProgressBar, progress, painter)


class _ResourceTile(QFrame):
    """Краткая загрузка ресурса; мышь и клавиатура открывают диагностику."""

    activated = Signal(str)

    def __init__(self, key: str, title: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.key = key
        self._compact = False
        self._primary = ("нет данных", "нет данных")
        self.setObjectName("resourceTile")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(116)
        self.setStyleSheet(
            "QFrame#resourceTile { background: palette(base); border: 1px solid palette(mid); border-radius: 7px; }"
            "QFrame#resourceTile:hover, QFrame#resourceTile:focus { border: 1px solid palette(highlight); }"
            "QFrame#resourceTile[selected=true] { border: 2px solid palette(highlight); }"
            "QLabel { border: none; background: transparent; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(1)
        heading = QHBoxLayout()
        self._title = QLabel(title, self)
        font = self._title.font()
        font.setBold(True)
        self._title.setFont(font)
        heading.addWidget(self._title)
        heading.addStretch()
        self._status = QLabel(self)
        self._status.setFixedSize(7, 7)
        heading.addWidget(self._status)
        layout.addLayout(heading)
        self._value = QLabel("нет данных", self)
        font = self._value.font()
        font.setPointSizeF(font.pointSizeF() + 4)
        font.setBold(True)
        self._value.setFont(font)
        layout.addWidget(self._value)
        self._limit = QLabel(self)
        layout.addWidget(self._limit)
        self._bar = QProgressBar(self)
        self._bar.setRange(0, 1000)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(4)
        layout.addWidget(self._bar)
        self._secondary = QLabel(self)
        layout.addWidget(self._secondary)
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        for label in (self._value, self._limit, self._secondary):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def update_usage(
        self,
        value: str,
        limit: str,
        secondary: str,
        ratio: float | None,
        state: str,
        warning: bool,
        selected: bool,
    ) -> None:
        self._primary = value, limit
        self._value.setText(f"{value} / {limit}" if self._compact else value)
        self._limit.setText("из " + limit)
        self._secondary.setText(secondary)
        color = (
            "#d84b4b"
            if warning
            else ("#d18c28" if ratio is not None and ratio >= 0.8 else "#389d8c")
        )
        if ratio is None and not warning:
            color = "palette(mid)"
        self._status.setStyleSheet(f"background: {color}; border-radius: 3px;")
        self._bar.setValue(round(min(1, ratio or 0) * 1000))
        style = (
            "QProgressBar { border: none; border-radius: 2px; background: palette(alternate-base); }"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 2px; }}"
        )
        if self._bar.styleSheet() != style:
            self._bar.setStyleSheet(style)
        self.setToolTip(
            f"{self._title.text()}: {value} / {limit}\n{secondary}\n{state}"
        )
        self.setAccessibleName(self.toolTip())
        if self.property("selected") != selected:
            self.setProperty("selected", selected)
            self.style().unpolish(self)
            self.style().polish(self)

    def set_compact(self, compact: bool) -> None:
        """В узком окне объединить значение и лимит, освобождая место пулам."""
        self._compact = compact
        self.setFixedHeight(82 if compact else 116)
        self._limit.setVisible(not compact)
        value, limit = self._primary
        self._value.setText(f"{value} / {limit}" if compact else value)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.key)
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.key)
            event.accept()
            return
        super().keyPressEvent(event)


class PoolMonitorDialog(QWidget):
    """Сводные таблицы ресурсов и пулов с общей областью диагностики."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Мониторинг ресурсов")
        self.resize(820, 620)
        self.setMinimumSize(640, 420)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self._veyon_service: VeyonService | None = None
        self._veyon_stats: dict[str, Any] = {}
        self._veyon_stats_pending = False
        self._veyon_stats_at = -math.inf
        self._veyon_stats_task: _WebApiStatsTask | None = None
        self._thumbnail_sources_dirty = True
        self._thumbnail_overrides: set[str] = set()
        self._metrics: dict[str, Any] = {}
        self._resources: dict[str, dict[str, Any]] = {}
        self._pools: dict[str, dict[str, Any]] = {}
        self._global_qt_pool: QtPoolSnapshot | None = None
        self._selected: tuple[str, str] | None = None
        from src.data.datastore import datastore

        self._datastore = datastore
        datastore.hosts_data_changed.connect(self._invalidate_thumbnail_sources)
        self._setup_ui()
        self._apply_palette()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(REFRESH_INTERVAL_MS)
        QTimer.singleShot(0, self._refresh)

    def _make_table(self, headers: list[str]) -> QTableWidget:
        table = (
            _TaskTable(self)
            if headers == ["Действие", "Хост / нода", "Источник", "Состояние", "Время"]
            else QTableWidget(0, len(headers), self)
        )
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(26)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.horizontalHeader().setMinimumSectionSize(55)
        table.horizontalHeader().setStretchLastSection(True)
        table.setMinimumHeight(70)
        return table

    def _setup_ui(self) -> None:
        self.setObjectName("resourceMonitor")
        self.setStyleSheet(
            "QWidget#resourceMonitor QTableWidget { border: none; background: palette(base); }"
            "QWidget#resourceMonitor QHeaderView::section { background: palette(window); border: none; padding: 5px 8px; color: palette(text); }"
            "QWidget#resourceMonitor QTableWidget::item { padding: 4px 8px; border-bottom: 1px solid palette(alternate-base); }"
            "QFrame#monitorDrawer { background: palette(window); border: 1px solid palette(mid); border-radius: 8px; }"
        )
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        dashboard = QWidget(self)
        outer = QVBoxLayout(dashboard)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        heading = QHBoxLayout()
        title = QLabel("Ресурсы", self)
        font = title.font()
        font.setPointSizeF(font.pointSizeF() + 3)
        font.setBold(True)
        title.setFont(font)
        heading.addWidget(title)
        heading.addStretch()
        cadence = QLabel("Обновление 1 с", self)
        cadence.setToolTip("Диагностика процесса Veyon обновляется раз в 5 секунд.")
        heading.addWidget(cadence)
        outer.addLayout(heading)
        self._tiles_layout = QGridLayout()
        self._tiles_layout.setSpacing(8)
        self._tiles: dict[str, _ResourceTile] = {}
        for key, title in (
            ("cache", "Общий кэш"),
            ("registry", "Реестр сеансов"),
            ("vnc", "VNC"),
            ("veyon", "Veyon WebAPI"),
        ):
            tile = _ResourceTile(key, title, dashboard)
            tile.activated.connect(self._select_resource)
            self._tiles[key] = tile
        self._tile_order: tuple[str, ...] = ()
        outer.addLayout(self._tiles_layout)
        self._warning_banner = QLabel(self)
        self._warning_banner.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self._warning_banner.setFixedHeight(self.fontMetrics().height() + 4)
        outer.addWidget(self._warning_banner)
        pool_heading = QHBoxLayout()
        label = QLabel("Пулы и задачи", self)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        pool_heading.addWidget(label)
        pool_heading.addStretch()
        self._pool_summary = QLabel(self)
        pool_heading.addWidget(self._pool_summary)
        outer.addLayout(pool_heading)
        self._pool_table = self._make_table(["Пул", "Занято / лимит", "Очередь"])
        self._pool_table.setItemDelegateForColumn(
            1, _PoolLoadDelegate(self._pool_table)
        )
        self._pool_table.setColumnWidth(0, 350)
        self._pool_table.setColumnWidth(1, 140)
        self._pool_table.setShowGrid(False)
        self._pool_table.verticalHeader().setDefaultSectionSize(30)
        outer.addWidget(self._pool_table, 1)
        self._stack.addWidget(dashboard)
        self._drawer_layer = QWidget(self)
        self._drawer_layer.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
        )
        self._drawer_layer.setStyleSheet(
            "QWidget#drawerShade { background: rgba(0, 0, 0, 65); }"
        )
        self._drawer_layer.setObjectName("drawerShade")
        drawer_layout = QHBoxLayout(self._drawer_layer)
        drawer_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        drawer_layout.setContentsMargins(10, 10, 10, 10)
        drawer_layout.addStretch()
        self._details_body = QFrame(self._drawer_layer)
        self._details_body.setObjectName("monitorDrawer")
        self._drawer_width = 440
        self._drawer_expanded = False
        self._details_body.setFixedWidth(self._drawer_width)
        self._drawer_handle = _DrawerResizeHandle(self._details_body)
        self._drawer_handle.dragged.connect(self._resize_drawer)
        self._details_body.installEventFilter(self)
        drawer_layout.addWidget(self._details_body)
        details = QVBoxLayout(self._details_body)
        details.setContentsMargins(18, 12, 14, 12)
        details.setSpacing(10)
        drawer_heading = QHBoxLayout()
        self._details_title = QLabel("Подробности", self)
        self._details_title.setWordWrap(True)
        font = self._details_title.font()
        font.setPointSizeF(font.pointSizeF() + 2)
        font.setBold(True)
        self._details_title.setFont(font)
        drawer_heading.addWidget(self._details_title, 1)
        self._drawer_expand = QToolButton(self._details_body)
        self._drawer_expand.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._update_drawer_expand_button()
        self._drawer_expand.clicked.connect(self._toggle_drawer_expanded)
        drawer_heading.addWidget(self._drawer_expand)
        close = QToolButton(self._details_body)
        close.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        close.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton)
        )
        close.setToolTip("Закрыть подробности")
        close.setAccessibleName("Закрыть подробности")
        close.clicked.connect(self._close_drawer)
        self._drawer_close = close
        drawer_heading.addWidget(close)
        details.addLayout(drawer_heading)
        self._details_scope = QLabel(self)
        self._details_scope.setWordWrap(True)
        details.addWidget(self._details_scope)
        self._pool_details = QWidget(self._details_body)
        summary = QVBoxLayout(self._pool_details)
        summary.setContentsMargins(0, 0, 0, 0)
        summary.setSpacing(6)
        self._pool_load = QLabel(self._pool_details)
        summary.addWidget(self._pool_load)
        self._pool_load_bar = QProgressBar(self._pool_details)
        self._pool_load_bar.setFixedHeight(8)
        self._pool_load_bar.setTextVisible(False)
        summary.addWidget(self._pool_load_bar)
        self._pool_queue = QLabel(self._pool_details)
        summary.addWidget(self._pool_queue)
        self._pool_sources = self._make_table(["Источник", "Выполняется", "В очереди"])
        self._pool_sources.setShowGrid(False)
        self._pool_sources.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._pool_sources.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._pool_sources.horizontalHeader().setStretchLastSection(False)
        self._pool_sources.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for column in (1, 2):
            self._pool_sources.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        summary.addWidget(self._pool_sources)
        details.addWidget(self._pool_details)
        self._detail_tabs = QTabWidget(self)
        self._detail_metrics = self._make_table(["Показатель", "Значение"])
        self._detail_metrics.setShowGrid(False)
        self._detail_metrics.setWordWrap(True)
        self._detail_metrics.setColumnWidth(0, 265)
        self._tasks = self._make_table(
            ["Действие", "Хост / нода", "Источник", "Состояние", "Время"]
        )
        self._tasks.horizontalHeaderItem(4).setToolTip(
            "Длительность выполнения или ожидания в очереди"
        )
        self._tasks.setShowGrid(False)
        self._tasks.horizontalHeader().setStretchLastSection(False)
        self._tasks.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for column, width in enumerate((150, 170, 105, 125, 90)):
            self._tasks.setColumnWidth(column, width)
        self._tasks_page = QWidget(self)
        tasks_layout = QVBoxLayout(self._tasks_page)
        tasks_layout.setContentsMargins(0, 0, 0, 0)
        self._tasks_empty = QLabel(
            "Нет выполняющихся и ожидающих заданий", self._tasks_page
        )
        self._tasks_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._tasks_empty.setWordWrap(True)
        tasks_layout.addWidget(self._tasks_empty, 1)
        tasks_layout.addWidget(self._tasks, 1)
        self._detail_tabs.addTab(self._tasks_page, "Задачи")
        self._detail_tabs.addTab(self._detail_metrics, "Показатели")
        details.addWidget(self._detail_tabs, 1)
        self._veyon_restart_btn = QPushButton("Перезапустить сервис Veyon", self)
        self._veyon_restart_btn.clicked.connect(self._on_restart_veyon_service)
        details.addWidget(self._veyon_restart_btn)
        self._stack.addWidget(self._drawer_layer)
        self._drawer_layer.hide()
        self._size_grip = QSizeGrip(self)
        self._size_grip.setToolTip("Изменить размер окна")
        self._size_grip.setAccessibleName("Изменить размер окна")
        self._position_size_grip()
        self._pool_table.itemSelectionChanged.connect(
            lambda: self._select("pool", self._pool_table)
        )
        self._pool_table.itemClicked.connect(
            lambda _item: self._select("pool", self._pool_table)
            if self._drawer_layer.isHidden()
            else None
        )
        self._arrange_tiles()

    def _arrange_tiles(self) -> None:
        visible = tuple(key for key in self._tiles if key in self._resources)
        columns = 4 if self.width() >= 780 else 2
        order = visible + (str(columns),)
        if order == self._tile_order:
            return
        self._tile_order = order
        for key, tile in self._tiles.items():
            self._tiles_layout.removeWidget(tile)
            tile.setVisible(key in visible)
            tile.set_compact(columns == 2)
        for column in range(4):
            self._tiles_layout.setColumnStretch(column, 1 if column < columns else 0)
        for index, key in enumerate(visible):
            self._tiles_layout.addWidget(
                self._tiles[key], index // columns, index % columns
            )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_tiles"):
            self._arrange_tiles()
            self._apply_drawer_width()
        if hasattr(self, "_size_grip"):
            self._position_size_grip()

    def _position_size_grip(self) -> None:
        self._size_grip.adjustSize()
        self._size_grip.move(
            self.width() - self._size_grip.width(),
            self.height() - self._size_grip.height(),
        )
        self._size_grip.raise_()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._details_body and event.type() == QEvent.Type.Resize:
            self._drawer_handle.setGeometry(0, 0, 7, self._details_body.height())
            self._drawer_handle.raise_()
        return super().eventFilter(watched, event)

    def _apply_drawer_width(self) -> None:
        margins = self._drawer_layer.layout().contentsMargins()
        available = max(0, self.width() - margins.left() - margins.right())
        width = (
            available if self._drawer_expanded else min(self._drawer_width, available)
        )
        self._details_body.setFixedWidth(width)

    def _resize_drawer(self, delta: int) -> None:
        if not delta:
            return
        margins = self._drawer_layer.layout().contentsMargins()
        available = max(0, self.width() - margins.left() - margins.right())
        self._drawer_width = min(
            available, max(360, self._details_body.width() + delta)
        )
        self._drawer_expanded = False
        self._update_drawer_expand_button()
        self._apply_drawer_width()

    def _toggle_drawer_expanded(self) -> None:
        self._drawer_expanded = not self._drawer_expanded
        self._update_drawer_expand_button()
        self._apply_drawer_width()

    def _update_drawer_expand_button(self) -> None:
        text = (
            "Свернуть подробности"
            if self._drawer_expanded
            else "Развернуть подробности"
        )
        icon = (
            QStyle.StandardPixmap.SP_TitleBarNormalButton
            if self._drawer_expanded
            else QStyle.StandardPixmap.SP_TitleBarMaxButton
        )
        self._drawer_expand.setIcon(self.style().standardIcon(icon))
        self._drawer_expand.setToolTip(text)
        self._drawer_expand.setAccessibleName(text)

    def _apply_palette(self) -> None:
        """Обновить палитру локальных QSS при переключении темы приложения."""
        palette = QApplication.palette()
        self._palette_key = palette.cacheKey()
        for widget in [self, *self.findChildren(QWidget)]:
            widget.setPalette(palette)
            if widget.styleSheet():
                widget.setStyleSheet(widget.styleSheet())

    def _open_drawer(self) -> None:
        self._apply_drawer_width()
        self._drawer_layer.show()
        self._stack.setCurrentWidget(self._drawer_layer)
        self._size_grip.raise_()
        self._render_details()
        self._detail_tabs.setCurrentWidget(
            self._tasks_page
            if self._selected and self._selected[0] == "pool"
            else self._detail_metrics
        )
        self._drawer_close.setFocus()

    def _close_drawer(self) -> None:
        self._drawer_layer.hide()
        if self._selected and self._selected[0] == "resource":
            self._tiles[self._selected[1]].setFocus()
        else:
            self._pool_table.setFocus()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and not self._drawer_layer.isHidden():
            self._close_drawer()
            event.accept()
            return
        super().keyPressEvent(event)

    def _select_resource(self, key: str) -> None:
        self._selected = "resource", key
        self._detail_tabs.setCurrentWidget(self._detail_metrics)
        self._pool_table.blockSignals(True)
        self._pool_table.clearSelection()
        self._pool_table.blockSignals(False)
        self._render_resources()
        self._open_drawer()

    def _select(self, kind: str, table: QTableWidget) -> None:
        items = table.selectedItems()
        if not items:
            return
        key = table.item(items[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        if key is None or key.startswith("group:"):
            return
        self._selected = kind, key
        self._detail_tabs.setCurrentWidget(self._tasks_page)
        self._render_resources()
        self._open_drawer()

    def _sync_table(
        self, table: QTableWidget, rows: list[tuple[str, list[str], bool]]
    ) -> None:
        """Обновить ячейки на месте, сохраняя выделение, ширины и прокрутку."""
        scroll = table.verticalScrollBar().value()
        horizontal = table.horizontalScrollBar().value()
        current = next(iter(table.selectedItems()), None)
        selected_key = (
            current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        )
        table.blockSignals(True)
        try:
            table.setRowCount(len(rows))
            for row, (key, values, warning) in enumerate(rows):
                for col, value in enumerate(values):
                    item = table.item(row, col)
                    if item is None:
                        item = QTableWidgetItem()
                        table.setItem(row, col, item)
                    if item.text() != value:
                        item.setText(value)
                        item.setToolTip(value)
                    item.setData(Qt.ItemDataRole.UserRole, key)
                    item.setForeground(
                        QBrush(QColor("#e53935")) if warning else QBrush()
                    )
            # Идентичность выделения не зависит от позиции после перестановки строк.
            if table is self._pool_table:
                selected_key = (
                    self._selected[1]
                    if self._selected and self._selected[0] == "pool"
                    else None
                )
            table.clearSelection()
            if selected_key is not None:
                for row, (key, _, _) in enumerate(rows):
                    if key == selected_key:
                        table.setCurrentCell(row, 0)
                        table.selectRow(row)
                        break
        finally:
            table.blockSignals(False)
        table.verticalScrollBar().setValue(scroll)
        table.horizontalScrollBar().setValue(horizontal)

    def _refresh(self) -> None:
        if QApplication.palette().cacheKey() != self._palette_key:
            self._apply_palette()
        try:
            from src.di import get_container
            from src.services.thumbnails.thumbnail_service import ThumbnailService
            from src.workers.veyon.thumbnail_timer import get_thumbnail_timer_manager

            self._metrics = get_container().resolve(ThumbnailService).get_metrics()
            self._metrics["active_timers"] = get_thumbnail_timer_manager().active_count
        except Exception:  # noqa: BLE001 - сбой источника не останавливает монитор
            self._metrics = {}
            logger.debug("PoolMonitor: Thumbnail refresh error", exc_info=True)
        self._refresh_pools()
        self._refresh_veyon()
        self._render_resources()
        self._render_details()

    def _refresh_pools(self) -> None:
        pools: dict[str, dict[str, Any]] = {}
        if self._global_qt_pool is None:
            self._global_qt_pool = QtPoolSnapshot(
                QThreadPool.globalInstance(), "Общий пул Qt"
            )
        entries = [
            *get_created_application_pools(),
            *TrackedThreadPoolExecutor.get_all_tracked_pools(),
            *get_registered_pools(),
            self._global_qt_pool,
        ]
        rows = []
        for pool in sorted(set(entries), key=lambda pool: (pool.label, id(pool))):
            key = str(id(pool))
            try:
                snapshot = pool.get_snapshot()
            except Exception:  # noqa: BLE001 - ошибка одного пула не скрывает другие
                logger.debug("PoolMonitor: pool stats unavailable", exc_info=True)
                continue
            stats = snapshot["stats"]
            pools[key] = {"title": pool.label, **snapshot}
            rows.append(
                (
                    key,
                    [
                        pool.label,
                        _fmt_load(stats),
                        _fmt_number(stats["pending"]),
                    ],
                    False,
                )
            )
        self._pools = pools
        threads, breakdown = process_thread_summary()
        self._pool_summary.setText(f"{len(pools)} пулов, потоков ОС: {threads}")
        self._pool_summary.setToolTip(breakdown)
        self._sync_table(self._pool_table, rows)
        for row, (key, _, _) in enumerate(rows):
            stats = pools[key]["stats"]
            self._pool_table.item(row, 1).setData(
                Qt.ItemDataRole.UserRole + 1, (stats["active"], stats["max_workers"])
            )
            for column in (1, 2):
                self._pool_table.item(row, column).setTextAlignment(
                    Qt.AlignmentFlag.AlignCenter
                )

    def _refresh_veyon(self) -> None:
        try:
            if self._veyon_service is None:
                from src.di import get_container
                from src.services.remote.veyon_service import VeyonService

                self._veyon_service = get_container().resolve(VeyonService)
            self._veyon_connections = self._veyon_service.get_webapi_client_count()
            self._veyon_relevant = (
                "veyon" in self._get_thumbnail_sources()
                or self._veyon_connections > 0
                or (
                    config.app.veyon.feature_transport == "webapi"
                    and self._veyon_service.is_cli_available()
                )
            )
            if self._veyon_relevant:
                self._request_veyon_stats(self._veyon_service)
        except Exception:  # noqa: BLE001 - диагностика сервисов независима
            self._veyon_connections = None
            self._veyon_relevant = "veyon" in self._get_thumbnail_sources()
            logger.debug("PoolMonitor: Veyon unavailable", exc_info=True)

    def _request_veyon_stats(self, service: VeyonService) -> None:
        if self._veyon_stats_pending or time.monotonic() - self._veyon_stats_at < 5:
            return
        self._veyon_stats_pending = True
        self._veyon_stats_task = _WebApiStatsTask(service)
        self._veyon_stats_task.signals.ready.connect(self._apply_veyon_stats)
        QThreadPool.globalInstance().start(self._veyon_stats_task)

    @Slot(dict)
    def _apply_veyon_stats(self, result: dict[str, Any]) -> None:
        self._veyon_stats_pending = False
        self._veyon_stats_at = time.monotonic()
        self._veyon_stats = result
        self._render_resources()
        self._render_details()

    @staticmethod
    def _budget_state(values: dict[str, Any]) -> tuple[str, bool]:
        warnings = []
        if values.get("over_budget"):
            warnings.append("Превышен бюджет")
        if values.get("sessions_over_limit"):
            warnings.append("Превышен лимит сеансов")
        if values.get("allocations_blocked"):
            warnings.append("Новые выделения заблокированы")
        return (
            " · ".join(warnings)
            if warnings
            else ("В пределах лимитов" if values else "нет данных"),
            bool(warnings),
        )

    def _render_resources(self) -> None:
        m = self._metrics
        resources: dict[str, dict[str, Any]] = {}
        rows = []

        def add(
            key: str,
            title: str,
            usage: str,
            state: str,
            warning: bool,
            details: list[tuple[str, str]],
            scope: str,
        ) -> None:
            resources[key] = {"title": title, "details": details, "scope": scope}
            rows.append((key, [title, usage, state], warning))

        common = [
            (label, _fmt_number(m.get(key)))
            for key, label in (
                ("captures", "Успешные ответы источников"),
                ("cache_hits", "Ответы из общего кэша"),
                ("evictions", "Удалено из общего кэша"),
            )
        ]
        common.extend(
            (f"Ошибки всех источников: {label}", value)
            for label, value in _error_rows(m.get("errors"))
        )
        add(
            "cache",
            "Общий кэш",
            f"{_fmt_bytes(m.get('cache_bytes'))} / {_fmt_bytes(m.get('cache_bytes_limit'))} · {_fmt_number(m.get('cache_entries'))} / {_fmt_number(m.get('cache_entries_limit'))} записей",
            "В пределах лимитов" if m else "нет данных",
            False,
            common,
            "Счётчики фасада миниатюр, с запуска приложения",
        )
        state, warning = self._budget_state(m)
        add(
            "registry",
            "Реестр сеансов",
            f"{_fmt_number(m.get('sessions'))} / {_fmt_number(m.get('max_session_records'))}",
            state,
            warning,
            [
                (label, _fmt_number(m.get(key)))
                for key, label in (
                    ("subscriptions", "Подписки"),
                    ("active_timers", "Локальные таймеры Qt"),
                    ("captures_active", "Текущие захваты"),
                )
            ],
            "Текущее состояние фасада миниатюр",
        )
        backends = m.get("backends", {})
        if "vnc" in self._get_thumbnail_sources():
            values = backends.get("vnc", {})
            state, warning = self._budget_state(values)
            details = _thumbnail_metric_rows(
                {
                    k: v
                    for k, v in values.items()
                    if k
                    not in {
                        "sessions",
                        "max_sessions",
                        "memory_reserved_bytes",
                        "max_memory_bytes",
                    }
                }
            )
            add(
                "vnc",
                "VNC",
                f"{_fmt_number(values.get('sessions'))} / {_fmt_number(values.get('max_sessions'))} сеансов · {_fmt_bytes(values.get('memory_reserved_bytes'))} / {_fmt_bytes(values.get('max_memory_bytes'))}",
                state,
                warning,
                details,
                "Счётчики backend VNC, с запуска приложения",
            )
        if getattr(self, "_veyon_relevant", "veyon" in self._get_thumbnail_sources()):
            stats = self._veyon_stats.get("stats", {})
            status = {
                "running": "Работает",
                "stopped": "Остановлен",
                "cli_missing": "CLI не найден",
                "unknown": "нет данных",
            }.get(self._veyon_stats.get("status"), "нет данных")
            details = [
                (label, formatter(stats.get(key)))
                for key, label, formatter in (
                    ("commit_mb", "Выделено процессу", _fmt_mem_mb),
                    ("pid", "PID", _fmt_number),
                    ("threads", "Потоки процесса", _fmt_number),
                    ("webapi_established", "TCP-подключения", _fmt_number),
                    ("webapi_close_wait", "TCP: ожидают закрытия", _fmt_number),
                )
            ]
            details.extend(
                _thumbnail_metric_rows(
                    {
                        k: v
                        for k, v in backends.get("veyon", {}).items()
                        if k != "sessions"
                    }
                )
            )
            add(
                "veyon",
                "Veyon WebAPI",
                f"{_fmt_number(getattr(self, '_veyon_connections', None))} / {_fmt_number(self._veyon_stats.get('limit'))} подключений · RSS {_fmt_mem_mb(stats.get('rss_mb'))}",
                status,
                status in {"Остановлен", "CLI не найден"},
                details,
                "Счётчики backend Veyon, с запуска приложения; процесс — сейчас",
            )
        self._resources = resources
        self._arrange_tiles()
        usage = {
            "cache": (
                m.get("cache_bytes"),
                m.get("cache_bytes_limit"),
                _fmt_bytes,
                f"{_fmt_number(m.get('cache_entries'))} / {_fmt_number(m.get('cache_entries_limit'))} записей",
            ),
            "registry": (
                m.get("sessions"),
                m.get("max_session_records"),
                _fmt_number,
                "Общие записи источников",
            ),
        }
        vnc = backends.get("vnc", {})
        usage["vnc"] = (
            vnc.get("memory_reserved_bytes"),
            vnc.get("max_memory_bytes"),
            _fmt_bytes,
            f"{_fmt_number(vnc.get('sessions'))} / {_fmt_number(vnc.get('max_sessions'))} сеансов · с буферами",
        )
        stats = self._veyon_stats.get("stats", {})
        usage["veyon"] = (
            getattr(self, "_veyon_connections", None),
            self._veyon_stats.get("limit"),
            _fmt_number,
            f"RSS {_fmt_mem_mb(stats.get('rss_mb'))}",
        )
        warnings = []
        for key, values, warning in rows:
            used, limit, formatter, secondary = usage[key]
            numerator, denominator = _number(used), _number(limit)
            ratio = (
                numerator / denominator
                if numerator is not None and denominator
                else None
            )
            if key == "veyon":
                secondary += " · " + values[2]
            limit_text = formatter(limit)
            if key == "veyon" and limit is not None:
                limit_text += " подключений"
            elif key == "registry" and limit is not None:
                limit_text += " записей"
            self._tiles[key].update_usage(
                formatter(used),
                limit_text,
                secondary,
                ratio,
                values[2],
                warning,
                self._selected == ("resource", key),
            )
            if warning:
                warnings.append(values[0] + ": " + values[2])
        blocked = m.get("allocations_blocked") or vnc.get("allocations_blocked")
        if warnings:
            self._warning_banner.setText(
                "Превышены лимиты · новые выделения заблокированы"
                if blocked
                else "Требуют внимания: " + ", ".join(r[1][0] for r in rows if r[2])
            )
            self._warning_banner.setStyleSheet("color: #d84b4b;")
            self._warning_banner.setToolTip("\n".join(warnings))
        else:
            self._warning_banner.setText(
                "Выберите ресурс или пул, чтобы открыть диагностику"
            )
            self._warning_banner.setStyleSheet("")
            self._warning_banner.setToolTip(
                "Память VNC включает рабочие буферы. RSS — память процесса Veyon. КБ = 1024 Б, МБ = 1024 КБ."
            )

    def _render_details(self) -> None:
        selected = self._selected
        entry = None
        if selected:
            entry = (self._resources if selected[0] == "resource" else self._pools).get(
                selected[1]
            )
        if entry is None:
            self._selected = None
            self._details_title.setText("Выберите ресурс или пул")
            self._sync_table(self._detail_metrics, [])
            self._sync_table(self._tasks, [])
            self._pool_details.hide()
            self._detail_tabs.setTabVisible(0, False)
            self._details_scope.setText("Выбранный элемент больше не доступен")
            self._veyon_restart_btn.hide()
            return
        is_pool = selected[0] == "pool"
        self._veyon_restart_btn.setVisible(selected == ("resource", "veyon"))
        self._pool_details.setVisible(is_pool)
        self._detail_tabs.setTabVisible(0, is_pool)
        if is_pool:
            self._details_title.setText(entry["title"])
            self._details_scope.setText("Общий пул · задания всех источников")
            stats = entry["stats"]
            active, limit, pending = (
                stats["active"],
                stats["max_workers"],
                stats["pending"],
            )
            unit = stats.get("limit_unit", "потоков")
            reserved = stats.get("interactive_reserved") or 0
            text = f"{active} из {limit} {unit}"
            if reserved:
                text += f", ещё {reserved} — только для кнопок"
            self._pool_load.setText(text)
            self._pool_load_bar.setRange(0, max(1, limit + reserved))
            self._pool_load_bar.setValue(active)
            self._pool_queue.setText(f"В очереди: {pending}")
            counts = {label: [0, 0] for label in _SOURCE_LABELS.values()}
            for task in entry["tasks"]:
                label = _source_label(task.get("metadata") or {})
                counts.setdefault(label, [0, 0])[task.get("state") != "running"] += 1
            self._sync_table(
                self._pool_sources,
                [
                    (label, [label, str(values[0]), str(values[1])], False)
                    for label, values in counts.items()
                ],
            )
            for row in range(self._pool_sources.rowCount()):
                for column in (1, 2):
                    self._pool_sources.item(row, column).setTextAlignment(
                        Qt.AlignmentFlag.AlignCenter
                    )
            self._pool_sources.setFixedHeight(
                self._pool_sources.horizontalHeader().height()
                + sum(
                    self._pool_sources.rowHeight(row)
                    for row in range(self._pool_sources.rowCount())
                )
                + 2 * self._pool_sources.frameWidth()
            )
            metrics = [
                (label, _fmt_number(stats.get(key)))
                for key, label in (
                    ("submitted", "Поступило (за время работы пула)"),
                    ("completed", "Завершено"),
                    ("failed", "Ошибок"),
                    ("cancelled", "Отменено"),
                )
            ]
            tasks = [
                (
                    str(t.get("task_id", index)),
                    [
                        _task_label(t.get("fn", "?"), t.get("metadata") or {}),
                        _task_target(t.get("metadata") or {}),
                        _source_label(t.get("metadata") or {}),
                        "В очереди" if t.get("state") == "pending" else "Выполняется",
                        _fmt_duration(t.get("duration", t.get("waiting_time"))),
                    ],
                    False,
                )
                for index, t in enumerate(entry["tasks"])
            ]
            self._sync_table(self._tasks, tasks)
            self._tasks.setVisible(bool(tasks))
            self._tasks_empty.setVisible(not tasks)
            dark = self.palette().base().color().lightness() < 128
            for row, task in enumerate(entry["tasks"]):
                running = task.get("state") == "running"
                item = self._tasks.item(row, 3)
                item.setForeground(
                    QColor(
                        ("#79c5ff" if dark else "#17639b")
                        if running
                        else ("#edc16a" if dark else "#885900")
                    )
                )
                font = item.font()
                font.setBold(running)
                item.setFont(font)
        else:
            self._details_title.setText(entry["title"])
            self._details_scope.setText(entry["scope"])
            metrics = entry["details"]
        self._details_title.setToolTip(self._details_title.text())
        self._sync_table(
            self._detail_metrics,
            [(str(index), list(values), False) for index, values in enumerate(metrics)],
        )
        self._detail_metrics.resizeRowsToContents()

    @Slot(dict)
    def _invalidate_thumbnail_sources(self, _change: dict[str, Any]) -> None:
        self._thumbnail_sources_dirty = True

    def _get_thumbnail_sources(self) -> set[str]:
        if self._thumbnail_sources_dirty:
            self._thumbnail_overrides = _configured_thumbnail_sources(
                "", self._datastore.get_hosts_data()
            )
            self._thumbnail_sources_dirty = False
        # Общая настройка читается каждый тик; список хостов только после CRUD.
        return self._thumbnail_overrides | {config.app.thumbnails.backend}

    def _on_restart_veyon_service(self) -> None:
        """Перезапуск локального сервиса Veyon (сброс памяти после утечки).

        Veyon 4.x (Windows) не освобождает память WebAPI-соединений после их
        закрытия (upstream-баг): единственный способ вернуть память -
        перезапуск процесса veyon-service. Миниатюры и RFB-сессии восстановятся
        автоматически (persistent-клиенты пересоздадутся по ошибке
        InvalidConnection, GUI-режим миниатюр остаётся включённым).
        """
        service = getattr(self, "_veyon_service", None)
        if service is None:
            return
        from src.utils.system.subprocess_env import is_running_elevated

        elevated = is_running_elevated()
        auth_hint = (
            ""
            if elevated
            else (
                "\n\nПоявится системный запрос авторизации (UAC) -\n"
                "подтвердите его для перезапуска."
                if sys.platform == "win32"
                else "\n\nПоявится системный запрос авторизации -\nподтвердите его для перезапуска."
            )
        )
        btn = QMessageBox.question(
            self,
            "Перезапуск сервиса Veyon",
            "Перезапустить сервис Veyon?\n\n"
            "Миниатюры и браузерные сеансы связи прервутся\n"
            "и восстановятся автоматически через 5-10 секунд." + auth_hint,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if btn != QMessageBox.StandardButton.Yes:
            return

        self._veyon_restart_btn.setEnabled(False)
        self._veyon_restart_btn.setText("Перезапуск…")

        # Рестарт сервиса - subprocess с elevation; выполняем в отдельном
        # потоке, чтобы не блокировать GUI, результат возвращаем в GUI-поток.
        from PySide6.QtCore import QThread

        class _RestartTask(QThread):
            result: tuple[bool, str] = (False, "")

            def run(self) -> None:
                try:
                    ok = service.restart_service()
                    err = "" if ok else (service.last_error() or "")
                    self.result = (ok, err)
                except Exception as e:  # noqa: BLE001 - передать ошибку в GUI-поток
                    self.result = (False, str(e))

        from src.workers.qthread_registry import track_qthread

        # Окно монитора можно закрыть, не дожидаясь перезапуска сервиса.
        task = track_qthread(_RestartTask(), "Перезапуск сервиса Veyon")
        task.result = (False, "")

        def on_finished():
            ok, err = task.result
            try:
                self._veyon_restart_btn.setEnabled(True)
                self._veyon_restart_btn.setText("Перезапустить сервис Veyon")
                service.invalidate_installed_cache()
                if ok:
                    QMessageBox.information(
                        self,
                        "Перезапуск сервиса Veyon",
                        "Сервис Veyon перезапущен. Память освобождена.",
                    )
                else:
                    detail = f"\n\n{err}" if err else ""
                    QMessageBox.warning(
                        self,
                        "Перезапуск сервиса Veyon",
                        "Не удалось перезапустить сервис Veyon." + detail,
                    )
            except RuntimeError:
                pass  # окно монитора уже закрыто

        task.finished.connect(on_finished)
        task.start()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._timer.stop()
        super().closeEvent(event)
