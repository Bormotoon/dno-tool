"""Диалог «Диагностика» — параллельная интерактивная диагностика SSH/Veyon/VNC.

Открывается из меню «Помощь → Диагностика».

Компоновка:
  - Верхняя строка: выбор компонентов, «Проверить N хостов» / «Прервать»,
    меню «Повторить» и «Отчёт», полоса прогресса.
  - Слева дерево хостов с галочками: галочки задают состав проверки.
  - Справа фильтры со счётчиками, поиск и таблица результатов. Параллельная
    проверка (ThreadPoolExecutor в DiagnosticsWorker) заполняет ячейки по мере
    готовности.
  - Внизу вкладки «Подробности» (сначала проблемы, успешные свёрнуты в одну
    строку) и «Журнал».
  - Двойной клик по строке — отчёт по хосту в отдельном окне, по колонке
    «Итог» — развернуть группу. Те же действия есть в контекстном меню строки.

Структура колонок таблицы (по выбранным компонентам):

    col 0: Хост (значок итога, имя, адрес серым)
    каждый компонент — двухуровневая группа: «Итог» | подпроверки
    SSH:   TCP-задержка | Порт | Авторизация | Echo | Sudo
    Veyon: Порт | Сервис | Автозапуск | Рукопожатие
    VNC:   Бекенд | Порт | RFB | Активность | Автозапуск | Авторизация
        - колонки следуют за бекендом, выбранным в настройках
          (veyon.vnc_server: x11vnc, kmsvnc или krfb): проверяются порт, юнит
          и авторизация именно этого бекенда;
        - «Бекенд» — детект фактически активного на хосте бекенда.

Щелчок по заголовку группы сворачивает её в колонку «Итог» с худшим
состоянием группы (подсказка ячейки перечисляет все проверки) или
разворачивает обратно в отдельные проверки. По умолчанию группы свёрнуты.
"""

from __future__ import annotations

import html
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from PySide6.QtCore import (
    QEventLoop,
    QRect,
    QSettings,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Slot,
)
from PySide6.QtGui import QFontMetrics, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.config import config, get_icon_theme
from src.config.settings import DEFAULT_VNC_SERVER
from src.logger import logger
from src.services.hosts.diagnostics_state import CheckResult, CheckState
from src.ui.icons import _icon_from_key  # zip-темы: иконки из памяти
from src.ui.widgets.device_tree import DeviceTreeView
from src.ui.widgets.grouped_header import GroupedHeaderView
from src.workers.diagnostics_worker import (
    ICON_ABORT,
    ICON_ERR,
    ICON_OK,
    ICON_PENDING,
    ICON_WARN,
    DiagnosticsWorker,
)

# ================================================================
#  Описание колонок компонентов
# ================================================================
#
# Каждый компонент: список (check_key, подзаголовок).
# Порядок определяет вид колонок в таблице.

_COMPONENT_COLS: Dict[str, List[Tuple[str, str]]] = {
    "ssh": [
        ("ssh.ping", "TCP-задержка"),
        ("ssh.port", "Порт"),
        ("ssh.auth", "Авторизация"),
        ("ssh.echo", "Echo"),
        ("ssh.sudo", "Sudo"),
    ],
    "veyon": [
        ("veyon.server_port", "Порт"),
        ("veyon.service", "Сервис"),
        ("veyon.autostart", "Автозапуск"),
        ("veyon.handshake", "Рукопожатие"),
    ],
    "vnc": [
        ("vnc.backend", "Бекенд"),
        ("vnc.port", "Порт"),
        ("vnc.rfb", "RFB"),
        ("vnc.activity", "Активность"),
        ("vnc.autostart", "Автозапуск"),
        ("vnc.auth", "Авторизация"),
    ],
}

_COMPONENT_LABELS: Dict[str, str] = {
    "ssh": "SSH",
    "veyon": "Veyon",
    "vnc": "VNC",
}

_COMPONENT_ORDER = ("ssh", "veyon", "vnc")

_CHECK_LABELS: Dict[str, str] = {
    key: f"{_COMPONENT_LABELS[component]} / {label}"
    for component, columns in _COMPONENT_COLS.items()
    for key, label in columns
}

# Короткие заголовки относятся только к таблице, не к отчёту.
_COMPACT_HEADERS = {
    "ssh.ping": "TCP, мс",
    "ssh.auth": "Вход",
    "veyon.autostart": "Авто",
    "veyon.handshake": "RFB",
    "vnc.activity": "Работа",
    "vnc.autostart": "Авто",
    "vnc.auth": "Вход",
}
_TEXT_CHECKS = {"ssh.ping", "ssh.sudo", "vnc.backend"}
_GROUP_HEADER = "Итог"

# Фильтры: (подпись, значок). Индекс совпадает с прежним комбобоксом.
_FILTERS: Tuple[Tuple[str, Optional[str]], ...] = (
    ("Все", None),
    ("Проблемы", None),
    ("Ошибки", ICON_ERR),
    ("Предупреждения", ICON_WARN),
    ("Неполные", "result_skip"),
    ("Без проблем", ICON_OK),
)
_FILTER_STATUS = {2: "Ошибки", 3: "Предупреждения", 4: "Неполные", 5: "Без проблем"}
_FILTER_TOOLTIP = (
    "Приоритет итога хоста: ошибка, предупреждение, неполная проверка, успех.\n"
    "Пропуски и отмена относятся к неполным."
)

_STATUS_ICONS = {
    "Без проблем": ICON_OK,
    "Предупреждения": ICON_WARN,
    "Ошибки": ICON_ERR,
}

# Подписи состояния группы в подсказке колонки «Итог».
_GROUP_STATE_TEXT = {
    ICON_ERR: "есть ошибки",
    ICON_WARN: "есть предупреждения",
    "result_executing": "выполняется",
    ICON_ABORT: "отменено",
    "result_skip": "проверено не всё",
    ICON_PENDING: "ожидание",
    ICON_OK: "без проблем",
}

_PROBLEM_PRIORITY = {
    CheckState.ERROR: 0,
    CheckState.WARNING: 1,
    CheckState.UNAVAILABLE: 2,
    CheckState.SKIPPED: 3,
    CheckState.CANCELLED: 4,
}

_SHORT_LABELS: Dict[str, str] = {
    key: label for columns in _COMPONENT_COLS.values() for key, label in columns
}
# Значение, которое колонка «Итог» показывает при успехе группы.
_GROUP_VALUE_KEYS = {"ssh": "ssh.ping", "vnc": "vnc.backend"}

_HOST_ADDRESS_ROLE = Qt.ItemDataRole.UserRole + 1
_DETAIL_ICON = 16


def _next_step_hint(key: str, state: CheckState) -> str:
    """Подсказка «Далее» для ошибки или непроверенной проверки, иначе пусто."""
    if state not in (CheckState.ERROR, CheckState.UNAVAILABLE):
        return ""
    if key.endswith(("port", "ping")):
        return "Проверьте адрес, порт, firewall и доступность службы."
    if key.startswith("ssh"):
        return "Проверьте учётную запись и ключ SSH."
    return "Проверьте настройки выбранного сервиса и права доступа."


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return few
    return many


def _split_setting(value) -> Optional[List[str]]:
    """Список из значения QSettings: строка через запятую, список или None."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    return [item for item in str(value).split(",") if item]


# ================================================================
#  Делегаты таблицы
# ================================================================


class _CenteredIconDelegate(QStyledItemDelegate):
    """Делегат ячеек проверок: значок и текст центрируются вместе.

    Ячейка только с текстом рисуется стандартно; значок без текста и пара
    «значок + текст» (колонка «Итог» SSH с задержкой) — по центру ячейки.
    """

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if opt.icon.isNull():
            super().paint(painter, option, index)
            return
        icon = QIcon(opt.icon)
        text = opt.text
        opt.icon = QIcon()
        opt.text = ""
        opt.features &= ~QStyleOptionViewItem.ViewItemFeature.HasDecoration
        style = opt.widget.style() if opt.widget else QApplication.style()
        # Стиль рисует фон, выделение и фокус всей ячейки перед содержимым.
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        mode = (
            QIcon.Mode.Disabled
            if not opt.state & QStyle.StateFlag.State_Enabled
            else QIcon.Mode.Selected
            if selected
            else QIcon.Mode.Normal
        )
        size = opt.decorationSize
        spacing = 4 if text else 0
        text_width = opt.fontMetrics.horizontalAdvance(text) if text else 0
        width = min(opt.rect.width(), size.width() + spacing + text_width)
        left = opt.rect.left() + max(0, (opt.rect.width() - width) // 2)
        icon_rect = QRect(
            left, opt.rect.top() + (opt.rect.height() - size.height()) // 2,
            size.width(), size.height(),
        )
        icon.paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter, mode)
        if not text:
            return
        painter.save()
        painter.setFont(opt.font)
        painter.setPen(
            opt.palette.color(
                QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
            )
        )
        text_rect = QRect(
            icon_rect.right() + 1 + spacing, opt.rect.top(),
            opt.rect.right() - icon_rect.right() - spacing, opt.rect.height(),
        )
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            opt.fontMetrics.elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width()),
        )
        painter.restore()


class _HostDelegate(QStyledItemDelegate):
    """Колонка «Хост»: значок итога, имя и серый адрес в одну строку."""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else QApplication.style()
        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, opt.widget
        )
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        address = index.data(_HOST_ADDRESS_ROLE) or ""
        if not address:
            return
        metrics = opt.fontMetrics
        margin = style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, None, opt.widget) + 1
        left = text_rect.left() + margin + metrics.horizontalAdvance(opt.text) + metrics.horizontalAdvance("  ")
        available = text_rect.right() - margin - left
        if available < metrics.horizontalAdvance("0.0…"):
            return
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setFont(opt.font)
        painter.setPen(
            opt.palette.color(
                QPalette.ColorRole.HighlightedText
                if selected
                else QPalette.ColorRole.PlaceholderText
            )
        )
        painter.drawText(
            QRect(left, text_rect.top(), available, text_rect.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            metrics.elidedText(address, Qt.TextElideMode.ElideRight, available),
        )
        painter.restore()


class _DetailsView(QTextBrowser):
    """Подробности по хосту; значки ``icon:<тема>/<ключ>`` берутся из темы иконок."""

    def __init__(self, icon_source, parent=None):
        super().__init__(parent)
        self._icon_source = icon_source

    def loadResource(self, kind, url):
        if url.scheme() == "icon":
            key = url.path().rsplit("/", 1)[-1]
            ratio = self.devicePixelRatioF()
            return self._icon_source(key).pixmap(QSize(_DETAIL_ICON, _DETAIL_ICON), ratio)
        return super().loadResource(kind, url)


def _separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


# ================================================================
#  Композитный виджет
# ================================================================


class DiagnosticsWidget(QWidget):
    """Виджет диагностики для встраивания в диалог."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: Optional[DiagnosticsWorker] = None
        self._thread: Optional[QThread] = None

        # Маппинги для динамического обновления таблицы
        self._host_row_map: Dict[str, int] = {}  # iid -> row
        self._row_host_map: Dict[int, str] = {}  # row -> iid
        self._row_host_name: Dict[str, str] = {}  # iid -> host
        self._host_logs: Dict[str, Dict[str, str]] = {}  # iid -> {check_key: detail}
        self._check_col_map: Dict[str, int] = {}  # check_key -> column
        self._col_check_key: Dict[int, str] = {}  # column -> check_key
        self._group_col_map: Dict[str, int] = {}  # компонент -> колонка «Итог»
        self._col_group: Dict[int, str] = {}  # колонка «Итог» -> компонент
        self._groups: List[Dict] = []  # групповые заголовки
        self._expanded: set = set()  # развёрнутые группы; остальные свёрнуты

        self._icons = {}
        self._icon_theme = get_icon_theme()
        self._run_id = None
        self._results = {}
        self._devices = {}
        self._active_iids: set = set()  # хосты текущего запуска
        self._result_queue = deque()
        self._run_devices = []
        self._progress_counts = (0, 0, 0)
        self._last_outcome = "Готово"
        self._status_text = "Готово"
        self._show_successes = False
        self._details_html = ""
        self._details_iid = None
        self._closing = False
        self._setup_ui()
        self._load_hosts_from_service()
        self._tree._model.dataChanged.connect(self._update_run_button)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._flush_results)
        self._timer.start()
        self._update_run_button()

    # ================================================================
    #  UI
    # ================================================================

    def _result_icon(self, key: str) -> QIcon:
        if key not in self._icons:
            self._icons[key] = _icon_from_key(key)
        return self._icons[key]

    def refresh_icons(self) -> None:
        """Перечитать иконки после изменения темы."""
        self._icon_theme = get_icon_theme()
        self._icons.clear()
        for row in range(self._table.rowCount()):
            for col in self._col_check_key:
                item = self._table.item(row, col)
                if item:
                    self._set_check_icon(item)
        for iid in self._host_row_map:
            self._update_group_cells(iid)
        self._run_btn.setIcon(self._result_icon("result_executing"))
        self._abort_btn.setIcon(self._result_icon("menu_abort"))
        for index, (_label, icon) in enumerate(_FILTERS):
            if icon:
                self._filter_group.button(index).setIcon(self._result_icon(icon))
        if not self._bottom_tabs.tabIcon(1).isNull():
            self._bottom_tabs.setTabIcon(1, self._result_icon(ICON_WARN))
        self._details_html = ""
        self._refresh_summary()
        self._show_details()

    def _set_check_icon(self, item: QTableWidgetItem) -> None:
        """Текстовые значения и ожидание не получают значка: меньше шума в таблице."""
        key = item.data(Qt.ItemDataRole.UserRole) or ICON_PENDING
        item.setIcon(
            QIcon() if item.text() or key == ICON_PENDING else self._result_icon(key)
        )

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # --- Панель действий: компоненты, запуск, повтор, отчёт, прогресс ---
        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.addWidget(QLabel("Проверять:"))
        self._comp_checks: Dict[str, QCheckBox] = {}
        for comp in _COMPONENT_ORDER:
            chk = QCheckBox(_COMPONENT_LABELS[comp])
            chk.setChecked(True)
            chk.stateChanged.connect(self._on_components_changed)
            bar.addWidget(chk)
            self._comp_checks[comp] = chk
        bar.addWidget(_separator())

        self._run_btn = QPushButton("Проверить")
        self._run_btn.setIcon(self._result_icon("result_executing"))
        self._run_btn.setIconSize(QSize(18, 18))
        self._run_btn.setToolTip("Проверить хосты, отмеченные галочками в дереве")
        self._run_btn.clicked.connect(self._run)
        bar.addWidget(self._run_btn)
        self._abort_btn = QPushButton("Прервать")
        self._abort_btn.setIcon(self._result_icon("menu_abort"))
        self._abort_btn.setIconSize(QSize(18, 18))
        self._abort_btn.clicked.connect(self._abort)
        self._abort_btn.setVisible(False)
        bar.addWidget(self._abort_btn)

        self._retry_btn = QPushButton("Повторить")
        retry_menu = QMenu(self._retry_btn)
        self._retry_problems_action = retry_menu.addAction(
            "Хосты с проблемами", self._retry_problems
        )
        self._retry_host_action = retry_menu.addAction(
            "Выбранный хост", self._retry_selected
        )
        retry_menu.aboutToShow.connect(self._update_menu_actions)
        self._retry_btn.setMenu(retry_menu)
        bar.addWidget(self._retry_btn)

        self._report_btn = QPushButton("Отчёт")
        report_menu = QMenu(self._report_btn)
        self._copy_host_action = report_menu.addAction(
            "Копировать выбранный хост", self._copy_host
        )
        report_menu.addAction("Копировать всё", self._copy_all)
        report_menu.addAction("Сохранить всё…", self._save_report)
        report_menu.aboutToShow.connect(self._update_menu_actions)
        self._report_btn.setMenu(report_menu)
        self._report_btn.setToolTip("Отчёт включает хосты, скрытые фильтром")
        bar.addWidget(self._report_btn)

        self._progress = QProgressBar()
        self._progress.setMinimumWidth(120)
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._progress.setFormat("Результатов пока нет")
        bar.addWidget(self._progress, 1)
        layout.addLayout(bar)

        # --- Сплиттер: дерево | (фильтры + таблица + вкладки) ---
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter = splitter
        splitter.setChildrenCollapsible(True)
        layout.addWidget(splitter, 1)

        # 1. Дерево хостов
        self._tree = DeviceTreeView(
            read_only=True, enable_ping=False, show_checkboxes=True
        )
        self._tree.setHeaderHidden(False)
        self._tree.setSelectionMode(self._tree.SelectionMode.ExtendedSelection)
        self._tree.setMinimumWidth(100)
        tree_panel = QWidget()
        tree_layout = QVBoxLayout(tree_panel)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(4)
        search_row = QHBoxLayout()
        search_row.setSpacing(4)
        self._tree_search = QLineEdit()
        self._tree_search.setPlaceholderText("Поиск хостов")
        self._tree_search.setToolTip(
            "Поиск только скрывает хосты; галочки задают состав проверки"
        )
        self._tree_search.textChanged.connect(self._tree.apply_filter)
        search_row.addWidget(self._tree_search, 1)
        mark_btn = QPushButton("Отметки")
        mark_btn.setToolTip(
            "Массовый выбор действует на всё дерево, включая хосты, скрытые поиском"
        )
        mark_menu = QMenu(mark_btn)
        mark_menu.addAction("Отметить все", lambda: self._select_all(True))
        mark_menu.addAction("Снять отметки", lambda: self._select_all(False))
        mark_btn.setMenu(mark_menu)
        search_row.addWidget(mark_btn)
        tree_layout.addLayout(search_row)
        tree_layout.addWidget(self._tree)
        self._tree_panel = tree_panel
        splitter.addWidget(tree_panel)

        # 2. Правая часть
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        # Фильтры со счётчиками заменяют и сводку, и выбор фильтра.
        filters = QHBoxLayout()
        filters.setSpacing(2)
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        for index, (label, icon) in enumerate(_FILTERS):
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            if icon:
                button.setIcon(self._result_icon(icon))
            button.setToolTip(_FILTER_TOOLTIP)
            self._filter_group.addButton(button, index)
            filters.addWidget(button)
        self._filter_group.button(0).setChecked(True)
        self._filter_group.idClicked.connect(self._apply_filters)
        filters.addSpacing(8)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Имя или адрес в результатах")
        self._search.setMinimumWidth(140)
        self._search.textChanged.connect(self._apply_filters)
        filters.addWidget(self._search, 1)
        right_layout.addLayout(filters)

        # Таблица результатов
        self._table = QTableWidget(0, 1)
        self._table.setMinimumHeight(110)
        self._table.setHorizontalHeaderLabels(["Хост"])
        self._table.verticalHeader().setVisible(False)
        self._table.setIconSize(QSize(18, 18))
        self._table.verticalHeader().setDefaultSectionSize(
            max(26, self.fontMetrics().height() + 10)
        )
        self._table.setWordWrap(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_table_menu)
        self._table.cellDoubleClicked.connect(self._on_table_double_click)
        self._table.currentCellChanged.connect(self._on_current_cell_changed)
        # GroupedHeaderView создаётся один раз — переиспользуется при rebuild.
        # Пересоздание header через setHorizontalHeader сбрасывает геометрию
        # и групповая часть пропадает, поэтому обновляем только через
        # setGroupedHeaders().
        self._header = GroupedHeaderView(Qt.Orientation.Horizontal, self._table)
        self._table.setHorizontalHeader(self._header)
        self._header.groupClicked.connect(self._on_group_clicked)
        # Делегаты: проверки — значок по центру, хост — имя и адрес в строку.
        self._table.setItemDelegate(_CenteredIconDelegate(self._table))
        self._table.setItemDelegateForColumn(0, _HostDelegate(self._table))
        self._table.itemSelectionChanged.connect(self._on_selection_changed)

        self._details = _DetailsView(self._result_icon)
        self._details.setMinimumHeight(60)
        self._details.setOpenLinks(False)
        self._details.anchorClicked.connect(self._on_details_link)
        self._details.setPlaceholderText(
            "Выберите хост в таблице: здесь появятся результаты его проверок, "
            "сначала проблемы."
        )
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(1000)
        self._log.setPlaceholderText(
            "Краткие сообщения о ходе диагностики появятся здесь."
        )
        self._bottom_tabs = QTabWidget()
        self._bottom_tabs.setDocumentMode(True)
        self._bottom_tabs.addTab(self._details, "Подробности")
        self._bottom_tabs.addTab(self._log, "Журнал")
        self._bottom_tabs.currentChanged.connect(self._on_tab_changed)

        self._details_splitter = QSplitter(Qt.Orientation.Vertical)
        self._details_splitter.addWidget(self._table)
        self._details_splitter.addWidget(self._bottom_tabs)
        self._details_splitter.setSizes([450, 180])
        right_layout.addWidget(self._details_splitter, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes([200, 800])

        # Первичная сборка таблицы
        self._rebuild_table()

    # ================================================================
    #  Состояние окна
    # ================================================================

    def view_state(self) -> Dict[str, str]:
        """Выбор компонентов, развёрнутые группы и фильтр для QSettings."""
        return {
            "components": ",".join(self._selected_components()),
            "expanded": ",".join(c for c in _COMPONENT_ORDER if c in self._expanded),
            "filter": str(max(0, self._filter_group.checkedId())),
        }

    def restore_view_state(self, components=None, expanded=None, filter_index=None) -> None:
        """Восстановить состояние; битые значения заменяются значениями по умолчанию."""
        if self._thread is not None:
            return
        chosen = set(_split_setting(components) or ()) & set(_COMPONENT_ORDER)
        for comp, check in self._comp_checks.items():
            check.blockSignals(True)
            check.setChecked(not chosen or comp in chosen)
            check.blockSignals(False)
        self._expanded = set(_split_setting(expanded) or ()) & set(_COMPONENT_ORDER)
        try:
            index = int(filter_index)
        except (TypeError, ValueError):
            index = 0
        button = self._filter_group.button(index if 0 <= index < len(_FILTERS) else 0)
        button.setChecked(True)
        self._results.clear()
        self._rebuild_table()
        self._update_run_button()

    # ================================================================
    #  Загрузка хостов
    # ================================================================

    def _load_hosts_from_service(self) -> None:
        """Авто-загрузка дерева хостов из DeviceService."""
        try:
            from src.di import get_container
            from src.services import DeviceService

            container = get_container()
            ds = container.resolve(DeviceService)
            self.set_tree_data(ds.get_devices_raw())
        except Exception as e:
            logger.error(f"DiagnosticsWidget: cannot load hosts: {e}")

    def set_tree_data(self, tree_data) -> None:
        """Загрузить дерево хостов в виджет."""
        if self._thread is not None:
            return
        if self._tree is not None:
            self._tree._model.load_tree_data(tree_data or [])

    # ================================================================
    #  Выбор компонентов
    # ================================================================

    def _selected_components(self) -> List[str]:
        return [c for c in _COMPONENT_ORDER if self._comp_checks[c].isChecked()]

    def _on_components_changed(self) -> None:
        """Перестроить таблицу при изменении выбора компонентов."""
        if self._thread is not None:
            return
        self._results.clear()
        self._rebuild_table()
        # Кнопка «Проверить» активна только если выбран хотя бы один компонент
        self._update_run_button()

    # ================================================================
    #  Таблица
    # ================================================================

    def _rebuild_table(self) -> None:
        """Перестроить таблицу по выбранным компонентам и отмеченным хостам."""
        comps = self._selected_components()
        devices = list(self._devices.values())
        self._table.setRowCount(0)

        # col 0 = «Хост», затем для каждого компонента «Итог» и подпроверки.
        self._check_col_map = {}
        self._col_check_key = {}
        self._group_col_map = {}
        self._col_group = {}
        col = 1
        headers = ["Хост"]
        groups: List[Dict] = []
        for comp in comps:
            self._group_col_map[comp] = col
            self._col_group[col] = comp
            headers.append(_GROUP_HEADER)
            group_cols: List[int] = [col]
            col += 1
            for check_key, sub_label in _COMPONENT_COLS[comp]:
                self._check_col_map[check_key] = col
                self._col_check_key[col] = check_key
                headers.append(_COMPACT_HEADERS.get(check_key, sub_label))
                group_cols.append(col)
                col += 1
            groups.append(
                {"key": comp, "label": _COMPONENT_LABELS[comp], "columns": group_cols}
            )
        self._groups = groups

        n_cols = col
        self._table.setColumnCount(n_cols)
        self._table.setHorizontalHeaderLabels(headers)
        # Ширины зависят от шрифта и коротких значений, ручной ресайз доступен.
        metrics = QFontMetrics(self._header.font())
        padding = metrics.horizontalAdvance("  ") + 4
        icon = self._table.iconSize().width()
        samples = {"ssh.ping": "9999 мс", "ssh.sudo": "NOPASSWD",
                   "vnc.backend": "x11vnc + krfb"}
        self._header.setMinimumSectionSize(icon + 8)
        for c in range(n_cols):
            self._header.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        for c, key in self._col_check_key.items():
            width = max(metrics.horizontalAdvance(headers[c]),
                        metrics.horizontalAdvance(samples.get(key, "")), icon)
            self._table.setColumnWidth(c, width + padding)
        for comp, c in self._group_col_map.items():
            longest = max(
                [metrics.horizontalAdvance(f"{label} +9") for _key, label in _COMPONENT_COLS[comp]]
                + [metrics.horizontalAdvance(samples[key]) for key in samples
                   if key == _GROUP_VALUE_KEYS.get(comp)]
            )
            content = icon + 4 + longest
            width = max(
                metrics.horizontalAdvance(_GROUP_HEADER) + padding,
                content + padding,
                self._header.groupMinimumWidth(_COMPONENT_LABELS[comp], True),
            )
            self._table.setColumnWidth(c, width)
        self._table.setColumnWidth(0, metrics.horizontalAdvance("Имя компьютера  000.000.000.000") + icon + padding)
        # Имя хоста занимает свободное место, проверки сохраняют компактную ширину.
        self._header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for comp in comps:
            self._table.horizontalHeaderItem(self._group_col_map[comp]).setToolTip(
                f"{_COMPONENT_LABELS[comp]}: худшее состояние проверок группы.\n"
                "Щелчок по заголовку группы показывает проверки по отдельности."
            )
            for key, label in _COMPONENT_COLS[comp]:
                self._table.horizontalHeaderItem(self._check_col_map[key]).setToolTip(
                    f"{_COMPONENT_LABELS[comp]} / {label}"
                )

        # Заполнить строки
        self._host_row_map = {}
        self._row_host_map = {}
        self._row_host_name = {}
        self._host_logs = {}

        for row, dev in enumerate(devices):
            self._table.insertRow(row)
            host_item = QTableWidgetItem(dev.name or dev.host)
            host_item.setData(
                _HOST_ADDRESS_ROLE, dev.host if dev.name and dev.name != dev.host else ""
            )
            self._table.setItem(row, 0, host_item)
            iid = getattr(dev, "iid", None) or dev.host or f"row{row}"
            self._host_row_map[iid] = row
            self._row_host_map[row] = iid
            self._row_host_name[iid] = dev.host or "?"
            # Ячейки проверок — ожидание
            for c in list(self._col_check_key) + list(self._col_group):
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, ICON_PENDING)
                item.setToolTip("Ожидание")
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table.setItem(row, c, item)
            self._host_logs[iid] = {}
            for key, result in self._results.get(iid, {}).items():
                self._on_check_result(
                    result.device, key, result.icon, result.text, result.detail
                )
            self._update_group_cells(iid)
        self._apply_group_visibility()
        self._refresh_summary()

    # ---------------------------------------------------------------- группы

    def _apply_group_visibility(self) -> None:
        """Свёрнутая группа показывает «Итог», развёрнутая — отдельные проверки."""
        for group in self._groups:
            comp = group["key"]
            expanded = comp in self._expanded
            self._table.setColumnHidden(self._group_col_map[comp], expanded)
            for key, _label in _COMPONENT_COLS[comp]:
                self._table.setColumnHidden(self._check_col_map[key], not expanded)
            group["collapsed"] = not expanded
            group["tooltip"] = (
                f"{group['label']}: щелчок сворачивает проверки в колонку «Итог»"
                if expanded
                else f"{group['label']}: щелчок показывает проверки по отдельности"
            )
        self._header.setGroupedHeaders(self._groups)

    def _toggle_group(self, comp: str, expand: Optional[bool] = None) -> None:
        if comp not in self._group_col_map:
            return
        if expand is None:
            expand = comp not in self._expanded
        if expand:
            self._expanded.add(comp)
        else:
            self._expanded.discard(comp)
        self._apply_group_visibility()

    @Slot(int)
    def _on_group_clicked(self, index: int) -> None:
        if 0 <= index < len(self._groups):
            self._toggle_group(self._groups[index]["key"])

    def _component_of_column(self, col: int) -> Optional[str]:
        if col in self._col_group:
            return self._col_group[col]
        key = self._col_check_key.get(col)
        return key.split(".", 1)[0] if key else None

    def _group_summary(self, iid: str, comp: str) -> Tuple[str, str, str]:
        """Значок, текст и подсказка колонки «Итог» компонента."""
        results = self._results.get(iid, {})
        keys = [key for key, _label in _COMPONENT_COLS[comp]]
        states = [results[key].state for key in keys if key in results]
        waiting = len(states) < len(keys) or CheckState.PENDING in states
        if CheckState.ERROR in states:
            icon = ICON_ERR
        elif CheckState.WARNING in states:
            icon = ICON_WARN
        elif CheckState.RUNNING in states or (
            iid in self._active_iids and states and waiting
        ):
            icon = "result_executing"
        elif CheckState.CANCELLED in states:
            icon = ICON_ABORT
        elif CheckState.UNAVAILABLE in states or CheckState.SKIPPED in states:
            icon = "result_skip"
        elif waiting:
            icon = ICON_PENDING
        else:
            icon = ICON_OK
        text = ""
        problems = sorted(
            (results[key] for key in keys
             if key in results and results[key].state in _PROBLEM_PRIORITY),
            key=lambda result: _PROBLEM_PRIORITY[result.state],
        )
        if icon == ICON_ABORT:
            text = "отменено"
        elif problems and icon != "result_executing":
            # Свёрнутая группа называет худшую проверку и число остальных проблем.
            text = _SHORT_LABELS[problems[0].key] + (
                f" +{len(problems) - 1}" if len(problems) > 1 else ""
            )
        elif icon == ICON_OK and comp in _GROUP_VALUE_KEYS:
            text = results[_GROUP_VALUE_KEYS[comp]].text
        lines = [f"{_COMPONENT_LABELS[comp]}: {_GROUP_STATE_TEXT[icon]}"]
        for key, label in _COMPONENT_COLS[comp]:
            result = results.get(key)
            lines.append(
                f"{label}: {result.state.value} · {result.text}"
                if result
                else f"{label}: ожидание"
            )
        return icon, text, "\n".join(lines)

    def _update_group_cells(self, iid: str, comps: Optional[Iterable[str]] = None) -> None:
        row = self._host_row_map.get(iid)
        if row is None:
            return
        for comp in self._group_col_map if comps is None else comps:
            col = self._group_col_map.get(comp)
            item = self._table.item(row, col) if col is not None else None
            if item is None:
                continue
            icon, text, tooltip = self._group_summary(iid, comp)
            item.setData(Qt.ItemDataRole.UserRole, icon)
            item.setText(text)
            item.setIcon(QIcon() if icon == ICON_PENDING else self._result_icon(icon))
            item.setToolTip(tooltip)

    # ================================================================
    #  Запуск / остановка
    # ================================================================

    def _run(self, _checked=False, devices=None) -> None:
        """Запустить одно поколение диагностики, сохранив прочие результаты при повторе."""
        if self._thread is not None:
            return
        retry = devices is not None
        devices = list(devices if retry else self._tree.get_all_checked_devices())
        comps = self._selected_components()
        if not devices or not comps:
            return
        if not retry:
            self._results.clear()
            self._devices.clear()
        for device in devices:
            iid = device.iid or device.host
            self._devices[iid] = device
            self._results[iid] = {}
        self._run_devices = devices
        self._active_iids = {device.iid or device.host for device in devices}
        self._progress_counts = (0, 0, len(devices))
        self._rebuild_table()
        self._log.clear()
        self._bottom_tabs.setTabIcon(1, QIcon())
        try:
            self._worker = DiagnosticsWorker(
                devices=devices,
                components=comps,
                vnc_password=config.app.veyon.vnc_password or "",
                veyon_server_port=config.app.veyon.server_port or 11100,
                vnc_port=config.app.veyon.vnc_port or 5900,
                ssh_port=config.app.ssh.port or 22,
                vnc_server=config.app.veyon.vnc_server or DEFAULT_VNC_SERVER,
            )
            self._run_id = self._worker.run_id
            self._thread = QThread(self)
            self._worker.moveToThread(self._thread)
            self._worker.result_ready.connect(self._queue_result)
            self._worker.progress.connect(self._on_progress)
            self._worker.message_ready.connect(self._on_host_log)
            self._worker.error_ready.connect(self._on_worker_error)
            self._worker.finished.connect(self._worker.deleteLater)
            self._worker.finished.connect(
                self._thread.quit, Qt.ConnectionType.DirectConnection
            )
            self._thread.finished.connect(self._on_finished)
            self._thread.finished.connect(self._thread.deleteLater)
            self._thread.started.connect(self._worker.execute)
            self._last_outcome = "Выполняется"
            self._set_status(self._last_outcome, 0, len(devices))
            self._set_running(True)
            self._thread.start()
        except Exception as error:
            self._log.appendPlainText(f"Ошибка запуска: {type(error).__name__}")
            if self._worker:
                self._worker.deleteLater()
            if self._thread:
                self._thread.deleteLater()
            self._worker = None
            self._thread = None
            self._active_iids = set()
            self._last_outcome = "Ошибка запуска"
            self._set_status(self._last_outcome)
            self._set_running(False)

    def _set_status(self, text: str, value: Optional[int] = None,
                    maximum: Optional[int] = None) -> None:
        """Текст состояния для полосы прогресса и отчёта."""
        self._status_text = text
        if maximum is not None:
            self._progress.setRange(0, max(1, maximum))
        if value is not None:
            self._progress.setValue(min(value, self._progress.maximum()))
        # «%» в формате QProgressBar — подстановка, экранируем его.
        self._progress.setFormat(text.replace("%", "%%"))
        self._progress.setToolTip(text)

    def _set_running(self, running: bool) -> None:
        self._tree_panel.setEnabled(not running)
        for check in self._comp_checks.values():
            check.setEnabled(not running)
        self._run_btn.setVisible(not running)
        self._abort_btn.setVisible(running)
        self._abort_btn.setEnabled(running)
        self._abort_btn.setText("Прервать")
        self._update_run_button()

    def _update_run_button(self, *_args) -> None:
        count = len(self._tree.get_all_checked_devices())
        word = _plural(count, "хост", "хоста", "хостов")
        self._run_btn.setText(f"Проверить {count} {word}")
        idle = self._thread is None
        self._run_btn.setEnabled(
            idle and count > 0 and bool(self._selected_components())
        )
        self._retry_btn.setEnabled(idle and bool(self._devices))
        self._report_btn.setEnabled(bool(self._devices))

    def _update_menu_actions(self) -> None:
        """Состояние пунктов меню считается при открытии, а не на каждом результате."""
        idle = self._thread is None
        selected = self._selected_iid() is not None
        self._retry_problems_action.setEnabled(
            idle and any(self._host_status(iid) != "Без проблем" for iid in self._devices)
        )
        self._retry_host_action.setEnabled(idle and selected)
        self._copy_host_action.setEnabled(selected)

    def _select_all(self, checked: bool) -> None:
        if self._thread is not None:
            return

        def visit(parent):
            for index in range(parent.rowCount()):
                child = parent.child(index)
                if child.isCheckable():
                    child.setCheckState(
                        Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                    )
                visit(child)

        visit(self._tree._model.invisibleRootItem())
        self._update_run_button()

    def _abort(self) -> None:
        if self._worker is not None:
            self._last_outcome = "Остановлено"
            self._worker.abort()
            self._abort_btn.setText("Остановка…")
            self._abort_btn.setEnabled(False)
            self._log.appendPlainText("Отмена очереди и активных сетевых операций…")

    @Slot(object)
    def _queue_result(self, result: CheckResult) -> None:
        if result.run_id == self._run_id:
            self._result_queue.append(result)

    @Slot()
    def _flush_results(self) -> None:
        if get_icon_theme() != self._icon_theme:
            self.refresh_icons()
        changed = False
        for _ in range(min(64, len(self._result_queue))):
            result = self._result_queue.popleft()
            if result.run_id != self._run_id:
                continue
            changed = True
            iid = result.device.iid or result.device.host
            self._results.setdefault(iid, {})[result.key] = result
            self._on_check_result(
                result.device, result.key, result.icon, result.text, result.detail
            )
        if changed:
            self._refresh_summary()
            self._apply_filters()
            self._show_details()

    @Slot(str, int, int, int)
    def _on_progress(
        self, run_id: str, completed: int, active: int, waiting: int
    ) -> None:
        if run_id == self._run_id:
            self._progress_counts = (completed, active, waiting)
            total = len(self._run_devices)
            self._set_status(
                f"{self._last_outcome} · завершено {completed} из {total} · "
                f"выполняется {active} · ожидает {waiting}",
                completed,
                total,
            )

    # ================================================================
    #  Обработчики сигналов worker
    # ================================================================

    def _on_check_result(
        self, device, check_key: str, icon: str, text: str, detail: str
    ) -> None:
        """Обновить ячейку таблицы по результату проверки.

        ``icon`` — строковый ключ в ICONS (см. diagnostics_worker.ICON_*).
        """
        iid = getattr(device, "iid", None) or device.host
        row = self._host_row_map.get(iid)
        col = self._check_col_map.get(check_key)
        if row is None or col is None:
            return
        item = self._table.item(row, col)
        if item is None:
            item = QTableWidgetItem()
            self._table.setItem(row, col, item)
        item.setData(Qt.ItemDataRole.UserRole, icon)
        result = self._results.get(iid, {}).get(check_key)
        transient = result and result.state in (CheckState.PENDING, CheckState.RUNNING)
        show_text = (
            check_key in _TEXT_CHECKS
            and not transient
            and icon not in (ICON_PENDING, "result_executing")
            and (check_key != "ssh.ping" or icon != ICON_ERR)
        )
        item.setText(text if show_text else "")
        self._set_check_icon(item)
        tooltip = text
        if detail:
            tooltip = f"{tooltip}\n\n{detail}"
        item.setToolTip(tooltip)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        log_entry = detail or text
        if log_entry:
            self._host_logs.setdefault(iid, {})[check_key] = log_entry
        self._update_group_cells(iid, (check_key.split(".", 1)[0],))

    @Slot(str, str, str, str)
    def _on_host_log(self, run_id: str, host: str, component: str, message: str) -> None:
        """Краткое сообщение в общий лог."""
        if run_id != self._run_id:
            return
        label = _COMPONENT_LABELS.get(component, component)
        self._log.appendPlainText(f"[{host}] {label}: {message}")

    @Slot(str, str)
    def _on_worker_error(self, run_id: str, message: str) -> None:
        """Ошибка воркера (исключение future): отмечаем вкладку журнала."""
        if run_id == self._run_id:
            self._log.appendPlainText(f"❌ {message}")
            if self._bottom_tabs.currentIndex() != 1:
                self._bottom_tabs.setTabIcon(1, self._result_icon(ICON_WARN))

    def _on_tab_changed(self, index: int) -> None:
        if index == 1:
            self._bottom_tabs.setTabIcon(1, QIcon())

    @Slot()
    def _on_finished(self) -> None:
        """Разблокировать UI только после drain задач и завершения QThread."""
        if self.sender() is not self._thread:
            return
        while self._result_queue:
            self._flush_results()
        if self._last_outcome != "Остановлено":
            self._last_outcome = (
                "Завершено с ошибками запуска"
                if any(
                    self._host_status(d.iid or d.host) == "Неполные"
                    for d in self._run_devices
                )
                else "Завершено"
            )
        self._worker = None
        self._thread = None
        self._active_iids = set()
        self._set_running(False)
        checked = sum(
            all(
                r.state
                not in (CheckState.CANCELLED, CheckState.PENDING, CheckState.RUNNING)
                for r in self._results.get(d.iid or d.host, {}).values()
            )
            and bool(self._results.get(d.iid or d.host))
            for d in self._run_devices
        )
        self._set_status(
            f"{self._last_outcome} · проверено {checked} из {len(self._run_devices)}",
            self._progress_counts[0] if self._last_outcome == "Остановлено"
            else len(self._run_devices),
        )
        self._log.appendPlainText(self._status_text)
        for iid in self._host_row_map:
            self._update_group_cells(iid)
        self._refresh_summary()
        self._show_details()

    def _host_status(self, iid: str) -> str:
        states = [r.state for r in self._results.get(iid, {}).values()]
        if CheckState.ERROR in states:
            return "Ошибки"
        if CheckState.WARNING in states:
            return "Предупреждения"
        expected = sum(len(_COMPONENT_COLS[c]) for c in self._selected_components())
        if len(states) < expected or any(
            state != CheckState.SUCCESS for state in states
        ):
            return "Неполные"
        return "Без проблем"

    def _refresh_summary(self) -> None:
        counts = {"Без проблем": 0, "Предупреждения": 0, "Ошибки": 0, "Неполные": 0}
        for iid, row in self._host_row_map.items():
            status = self._host_status(iid)
            counts[status] += 1
            item = self._table.item(row, 0)
            states = [result.state for result in self._results.get(iid, {}).values()]
            icon = _STATUS_ICONS.get(status)
            if icon is None:
                icon = (
                    "result_executing" if CheckState.RUNNING in states
                    else ICON_PENDING if not states or all(state == CheckState.PENDING for state in states)
                    else "result_skip"
                )
            item.setIcon(self._result_icon(icon))
            device = self._devices.get(iid)
            name = (device.name or device.host) if device else iid
            item.setToolTip(f"{name}\n{self._row_host_name.get(iid, '')}\nИтог: {status}")
        total = len(self._host_row_map)
        values = {
            0: total,
            1: total - counts["Без проблем"],
            **{index: counts[status] for index, status in _FILTER_STATUS.items()},
        }
        for index, (label, _icon) in enumerate(_FILTERS):
            self._filter_group.button(index).setText(f"{label} {values[index]}")

    def _apply_filters(self, *_args) -> None:
        query = self._search.text().casefold().strip()
        choice = self._filter_group.checkedId()
        for iid, row in self._host_row_map.items():
            device = self._devices[iid]
            status = self._host_status(iid)
            matches = (
                choice <= 0
                or (choice == 1 and status != "Без проблем")
                or status == _FILTER_STATUS.get(choice)
            )
            self._table.setRowHidden(
                row,
                not matches or query not in f"{device.name} {device.host}".casefold(),
            )

    def _selected_iid(self):
        return self._row_host_map.get(self._table.currentRow())

    def _report(self, iids=None) -> str:
        lines = [
            "Диагностика SSH / Veyon / VNC",
            f"Экспорт: {datetime.now():%d.%m.%Y %H:%M:%S}",
            "Область: "
            + (
                "все результаты, включая скрытые фильтром"
                if iids is None
                else "выбранный хост"
            ),
            self._status_text,
        ]
        for iid in self._devices if iids is None else iids:
            device = self._devices[iid]
            lines.extend(
                [
                    "",
                    f"{device.name or device.host} ({device.host}) — {self._host_status(iid)}",
                ]
            )
            for component in self._selected_components():
                lines.append(f"[{_COMPONENT_LABELS[component]}]")
                for key, label in _COMPONENT_COLS[component]:
                    result = self._results.get(iid, {}).get(key)
                    if result:
                        lines.append(
                            f"{label}: {result.state.value} · {result.text} · {result.duration:.2f} с · {datetime.fromtimestamp(result.timestamp):%d.%m.%Y %H:%M:%S}\n{result.detail}"
                        )
                        hint = _next_step_hint(key, result.state)
                        if hint:
                            lines.append("Далее: " + hint)
                    else:
                        lines.append(f"{label}: ожидание")
        return "\n".join(lines)

    # ================================================================
    #  Подробности
    # ================================================================

    def _icon_url(self, key: str) -> str:
        # Тема в адресе: документ кэширует картинки по URL.
        return f"icon:{html.escape(self._icon_theme or 'default')}/{html.escape(key)}"

    def _result_html(self, result: CheckResult) -> str:
        label = _CHECK_LABELS.get(result.key, result.key)
        parts = [
            f'<a name="{html.escape(result.key)}"></a>'
            '<p style="margin-top:8px; margin-bottom:0">'
            f'<img src="{self._icon_url(result.icon)}" width="{_DETAIL_ICON}" '
            f'height="{_DETAIL_ICON}"> <b>{html.escape(label)}</b> — '
            f"{html.escape(result.state.value)} · {html.escape(result.text)} · "
            f"{result.duration:.2f} с</p>"
        ]
        indent = '<p style="margin-left:22px; margin-top:2px; margin-bottom:0;'
        if result.detail and result.detail != result.text:
            parts.append(
                f'{indent} white-space:pre-wrap">{html.escape(result.detail)}</p>'
            )
        hint = _next_step_hint(result.key, result.state)
        if hint:
            parts.append(f'{indent}"><i>Далее:</i> {html.escape(hint)}</p>')
        return "".join(parts)

    def _details_document(self, iid: str) -> str:
        """HTML подробностей: проблемы, ожидающие проверки, свёрнутые успешные."""
        device = self._devices[iid]
        results = self._results.get(iid, {})
        name = device.name or device.host
        address = f' <span style="color:gray">{html.escape(device.host)}</span>' if name != device.host else ""
        parts = [
            f"<p><b>{html.escape(name)}</b>{address} — "
            f"{html.escape(self._host_status(iid))}</p>"
        ]
        problems, successes, waiting = [], [], []
        for component in self._selected_components():
            for key, _label in _COMPONENT_COLS[component]:
                result = results.get(key)
                if result is None or result.state in (CheckState.PENDING, CheckState.RUNNING):
                    waiting.append(_CHECK_LABELS[key])
                elif result.state == CheckState.SUCCESS:
                    successes.append(result)
                else:
                    problems.append(result)
        problems.sort(key=lambda result: _PROBLEM_PRIORITY.get(result.state, 9))
        parts.extend(self._result_html(result) for result in problems)
        if waiting:
            parts.append(
                '<p style="margin-top:8px; color:gray">Ожидают результата: '
                f"{html.escape(', '.join(waiting))}</p>"
            )
        if successes:
            count = len(successes)
            link = "свернуть" if self._show_successes else "показать"
            parts.append(
                '<p style="margin-top:8px; margin-bottom:0">'
                f'<img src="{self._icon_url(ICON_OK)}" width="{_DETAIL_ICON}" '
                f'height="{_DETAIL_ICON}"> Успешно: {count} '
                f"{_plural(count, 'проверка', 'проверки', 'проверок')} — "
                f'<a href="toggle:successes">{link}</a></p>'
            )
            if self._show_successes:
                parts.extend(self._result_html(result) for result in successes)
        return "".join(parts)

    def _show_details(self) -> None:
        iid = self._selected_iid()
        text = self._details_document(iid) if iid in self._devices else ""
        if text == self._details_html:
            return
        self._details_html = text
        bar = self._details.verticalScrollBar()
        position = bar.value()
        self._details.setHtml(text)
        if iid == self._details_iid:
            bar.setValue(position)
        self._details_iid = iid

    def _on_details_link(self, url: QUrl) -> None:
        if url.toString() == "toggle:successes":
            self._show_successes = not self._show_successes
            self._show_details()

    def _on_selection_changed(self) -> None:
        self._show_details()

    def _on_current_cell_changed(self, row: int, col: int, *_previous) -> None:
        """Щелчок по ячейке проверки прокручивает подробности к её записи."""
        iid = self._row_host_map.get(row)
        comp = self._component_of_column(col)
        if iid is None or comp is None:
            return
        results = self._results.get(iid, {})
        key = self._col_check_key.get(col)
        if key is None:
            # Колонка «Итог»: к первой проблеме группы.
            key = next(
                (k for k, _label in _COMPONENT_COLS[comp]
                 if k in results and results[k].state not in (CheckState.SUCCESS, CheckState.PENDING, CheckState.RUNNING)),
                None,
            )
        result = results.get(key) if key else None
        if result is None:
            return
        if result.state == CheckState.SUCCESS and not self._show_successes:
            self._show_successes = True
        self._show_details()
        self._bottom_tabs.setCurrentIndex(0)
        self._details.scrollToAnchor(key)

    # ================================================================
    #  Действия с хостом
    # ================================================================

    def _show_table_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        self._table.setCurrentCell(index.row(), index.column())
        menu = QMenu(self)
        retry = menu.addAction("Проверить этот хост", self._retry_selected)
        retry.setEnabled(self._thread is None)
        menu.addAction(
            "Открыть отчёт в окне", lambda row=index.row(): self._open_host_report(row)
        )
        menu.addAction("Копировать отчёт хоста", self._copy_host)
        comp = self._component_of_column(index.column())
        if comp:
            menu.addSeparator()
            label = _COMPONENT_LABELS[comp]
            text = (
                f"Свернуть {label} в колонку «Итог»"
                if comp in self._expanded
                else f"Показать проверки {label} по отдельности"
            )
            menu.addAction(text, lambda c=comp: self._toggle_group(c))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _copy_host(self) -> None:
        iid = self._selected_iid()
        if iid:
            QApplication.clipboard().setText(self._report([iid]))

    def _copy_all(self) -> None:
        QApplication.clipboard().setText(self._report())

    def _save_report(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить все результаты (включая скрытые)",
            "diagnostics.txt",
            "Текст (*.txt)",
        )
        if path:
            try:
                Path(path).write_text(self._report(), encoding="utf-8")
            except OSError as error:
                QMessageBox.warning(self, "Отчёт", f"Не удалось сохранить: {error}")

    def _retry_problems(self) -> None:
        self._run(
            devices=[
                device
                for iid, device in self._devices.items()
                if self._host_status(iid) != "Без проблем"
            ]
        )

    def _retry_selected(self) -> None:
        iid = self._selected_iid()
        if iid:
            self._run(devices=[self._devices[iid]])

    def _on_table_double_click(self, row: int, col: int) -> None:
        """Колонка «Итог» разворачивает группу, остальные — отчёт по хосту."""
        comp = self._col_group.get(col)
        if comp:
            self._toggle_group(comp, expand=True)
            return
        self._open_host_report(row)

    def _open_host_report(self, row: int) -> None:
        """Модальное окно с полным отчётом по хосту."""
        iid = self._row_host_map.get(row)
        if iid is None:
            return
        host = self._row_host_name.get(iid, "?")
        if not self._host_logs.get(iid):
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Диагностика — {host}")
        dlg.resize(720, 500)
        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.setContentsMargins(8, 8, 8, 8)
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(self._report([iid]))
        dlg_layout.addWidget(edit)
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(dlg.accept)
        dlg_layout.addWidget(close_btn)
        dlg.exec()

    # ================================================================
    #  Очистка
    # ================================================================

    def is_running(self) -> bool:
        return self._thread is not None

    def cleanup(self) -> None:
        """Запросить отмену и дождаться штатного завершения с работающим UI."""
        if not self.is_running():
            return
        self._abort()
        loop = QEventLoop(self)
        self._thread.finished.connect(loop.quit)
        if self._thread.isRunning():
            loop.exec()
        loop.deleteLater()


# ================================================================
#  Диалог-обёртка
# ================================================================


class DiagnosticsDialog(QDialog):
    """Диалог «Диагностика» — тонкая обёртка над DiagnosticsWidget."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Диагностика — удалённое управление")
        self.setMinimumSize(640, 480)
        self.resize(1100, 780)

        layout = QVBoxLayout(self)
        self.widget = DiagnosticsWidget(self)
        layout.addWidget(self.widget)

        buttons = QDialogButtonBox()
        close_btn = buttons.addButton("Закрыть", QDialogButtonBox.RejectRole)
        close_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self._settings = QSettings("dno-tool", "diagnostics")
        geometry = self._settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        for key, splitter in (
            ("tree", self.widget._splitter),
            ("details", self.widget._details_splitter),
        ):
            state = self._settings.value(key)
            if state:
                splitter.restoreState(state)
        self.widget.restore_view_state(
            self._settings.value("components"),
            self._settings.value("expanded"),
            self._settings.value("filter"),
        )
        available = self.screen().availableGeometry()
        self.resize(
            min(self.width(), available.width()), min(self.height(), available.height())
        )
        frame = self.frameGeometry()
        frame.moveLeft(
            max(
                available.left(),
                min(frame.left(), available.right() - frame.width() + 1),
            )
        )
        frame.moveTop(
            max(
                available.top(),
                min(frame.top(), available.bottom() - frame.height() + 1),
            )
        )
        self.move(frame.topLeft())
        self._closing = False

    def _save_geometry(self) -> None:
        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("tree", self.widget._splitter.saveState())
        self._settings.setValue("details", self.widget._details_splitter.saveState())
        for key, value in self.widget.view_state().items():
            self._settings.setValue(key, value)

    def _confirm_close(self) -> bool:
        """Подтверждение закрытия во время выполнения диагностики.

        Возвращает True если можно закрывать (диагностика не активна или
        пользователь подтвердил прерывание).
        """
        if self._closing:
            return False
        if not self.widget.is_running():
            return True
        reply = QMessageBox.question(
            self,
            "Диагностика",
            "Диагностика ещё выполняется. Прервать и закрыть?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def closeEvent(self, event):
        if not self._confirm_close():
            event.ignore()
            return
        self._closing = True
        try:
            self.widget.cleanup()
        except Exception:
            logger.exception("DiagnosticsDialog.closeEvent: cleanup failed")
            self._closing = False
            event.ignore()
            return
        self._save_geometry()
        self._closing = False
        event.accept()

    def reject(self):
        """Обработка Esc / кнопки «Закрыть» — проходит через confirmation."""
        if not self._confirm_close():
            return
        self._closing = True
        try:
            self.widget.cleanup()
        except Exception:
            logger.exception("DiagnosticsDialog.reject: cleanup failed")
            self._closing = False
            return
        self._save_geometry()
        self._closing = False
        super().reject()
