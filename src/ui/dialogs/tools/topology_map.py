"""Диалог карты сети: ping-sweep подсетей + traceroute + граф топологии.

Портирован из netmap/__main__.py. Немодальное окно (как сканер хостов),
только просмотр - экспорта в дерево устройств нет.
"""

from __future__ import annotations

import ipaddress
import math
import time
from collections import defaultdict
from dataclasses import replace

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from src.logger import logger
from src.ui.icons import _icon_from_key
from src.ui.widgets.topology_scene import NetworkView, NetworkNode
from src.ui.widgets.thread_count_slider import ThreadCountSlider
from src.utils.scanner.device_classifier import (
    device_type_icon_key,
    TYPE_UNKNOWN,
)
from src.utils.scanner.device_facts import DeviceFacts
from src.utils.network.topology import TraceResult
from src.workers.network.topology_worker import (
    SingleTraceWorker,
    TopologyWorker,
    parse_networks,
)

# Русские подписи этапов сканирования.
_PHASE_NAMES = {
    "idle": "Ожидание",
    "ping": "Поиск хостов (1/2)",
    "trace": "Трассировка маршрутов (2/2)",
    "dns": "Уточнение имен",
    "identify": "Определение типов устройств",
}


def _format_elapsed(seconds: float) -> str:
    """Длительность в виде мм:сс или чч:мм:сс."""
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _hop_node_id(destination: str, hop: dict, index: int) -> str:
    """ID узла хопа: реальный IP либо unknown:{destination}:{hop} для молчуна.

    Кто скрывается за молчащим хопом - неизвестно, поэтому такой узел
    живет только в своем маршруте (дедуплика между маршрутами невозможна).
    Принадлежность к сканируемым подсетям не проверяется.
    """
    hop_ip = hop.get("ip", "")
    if hop_ip:
        return hop_ip
    return f"unknown:{hop.get('measurement_id', destination)}:{hop.get('hop', '') or (index + 1)}"


# Общий id узла для молчащих хопов при «Объединить неизвестные».
SHARED_UNKNOWN_ID = "unknown:shared"


def _hop_display(node_id: str) -> str:
    """Строка хопа для панели деталей: IP либо '* (без ответа, хоп N)'."""
    if node_id == SHARED_UNKNOWN_ID:
        return "* (без ответа)"
    if node_id.startswith("unknown:"):
        return f"* (без ответа, хоп {node_id.rsplit(':', 1)[-1]})"
    return node_id


def _is_gateway_candidate(ip: str, network: str) -> bool:
    """IP - вероятный шлюз подсети: первый/последний usable-адрес.

    Шлюз отвечает на пинг и находился бы зеленым хостом, хотя это
    маршрутизатор. /31 и /32 пропускаем - там понятия шлюза нет
    и heuristic превратил бы все хосты в роутеры.
    """
    try:
        net = ipaddress.ip_network(network, strict=False)
        if net.version != 4 or net.prefixlen >= 31:
            return False
        addr = ipaddress.ip_address(ip)
        return addr in (net.network_address + 1, net.broadcast_address - 1)
    except ValueError:
        return False


def _narrow(widget: QWidget, min_width: int = 120) -> QWidget:
    """Поле правой колонки формы.

    Поле тянется до правого края формы без зазоров, все поля одной
    ширины (общая колонка формы). Узкая колонка достигается стартовой
    шириной самой панели (setSizes сплиттера), не вёрсткой строк.
    """
    widget.setMinimumWidth(min_width)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return widget


def update_subnets_text(text: str, network) -> tuple[str, bool]:
    """Вписать подсеть в CIDR-список, заменив поглощенные строки.

    Строка, равная новой подсети или содержащая ее (суперсеть) -
    ничего не меняем. Строки, целиком входящие в новую подсеть
    (подсети, например /24 внутри исправленной /23), удаляем, новая
    встает на место первой удаленной. Комментарии, пустые строки и
    порядок остального сохраняются. Возвращает (текст, менялся ли).
    """
    try:
        new_net = ipaddress.ip_network(network, strict=False)
    except ValueError:
        return text, False
    lines = text.splitlines()
    out: list = []
    changed = False
    insert_at: int | None = None
    for line in lines:
        stripped = line.strip()
        covered = False
        absorbed = False
        if stripped and not stripped.startswith("#"):
            try:
                old_net = ipaddress.ip_network(stripped, strict=False)
            except ValueError:
                old_net = None
            if old_net is not None:
                if old_net.supernet_of(new_net) or old_net == new_net:
                    covered = True
                elif new_net.supernet_of(old_net):
                    absorbed = True
        if covered:
            # Уже покрыто (равно или суперсеть) - ничего не добавляем.
            return text, False
        if absorbed:
            if insert_at is None:
                insert_at = len(out)
            changed = True
            continue
        out.append(line)
    if insert_at is None:
        insert_at = len(out)
        changed = True
    out.insert(insert_at, str(new_net))
    result = "\n".join(out) + "\n"
    return result, changed


class TopologyMapDialog(QDialog):
    """Немодальная карта сети (пинг + traceroute + граф)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Карта сети")
        # Полноценное окно: свернуть, развернуть, закрыть.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.resize(1200, 800)
        self.setMinimumSize(900, 600)
        self.setModal(False)
        self.worker = TopologyWorker(self)
        self._generation = 0
        self._closed = False
        self._single_jobs = set()
        self._scan_error = ""
        self._rescan_network = None
        self.probes = {}
        self.device_facts: dict[str, DeviceFacts] = {}
        self.host_network: dict = {}
        self.hostnames: dict = {}
        # Персональные маршруты (откат проблемных подсетей): host -> route.
        self.routes: dict = {}
        # Общие маршруты подсетей: network -> {representative, prefix, hops, src}.
        self.network_routes: dict = {}
        # Src-IP оператора для каждого назначения (для пути "от меня").
        self.route_source: dict = {}
        # Сохраненные подписи узлов (id -> имя): переименования через
        # «Свойства ...» переживают ресканы и рестарты приложения.
        self._node_names: dict = {}
        # Персональная трассировка (кнопка "Проверить маршрут").
        self._single_trace = None
        # Состояние сканирования для панели статусов.
        self._phase = "idle"
        self._phase_done = 0
        self._phase_total = 0
        self._routes_done = 0
        self._routes_total = 0
        self._started_at = 0.0
        self._clock = QTimer(self)
        self._clock.setInterval(500)
        self._clock.timeout.connect(self._tick_clock)
        self._build_ui()
        self._load_settings()
        self._connect_worker()
        self._render_status("Готов. Введите подсети и нажмите «Сканировать».")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        # Шапка сканирования: кнопки всегда видны, настройки - боковая панель.
        scan_header = QHBoxLayout()
        self.scan_button = QPushButton("Сканировать")
        self.scan_button.setToolTip("Запустить поиск хостов и построение маршрутов.")
        self.scan_button.clicked.connect(self.start_scan)
        scan_header.addWidget(self.scan_button)
        self.stop_button = QPushButton("Остановить")
        self.stop_button.setToolTip("Прервать сканирование.")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_scan)
        scan_header.addWidget(self.stop_button)
        scan_header.addStretch(1)
        layout.addLayout(scan_header)

        # Панель настроек тянется сплиттером: [панель | граф | правая колонка].
        # Панель ресайзится мышью за разделитель, схлопывается влево.
        self.params_body = QWidget()
        self.params_body.setMinimumWidth(270)
        params_body_layout = QVBoxLayout(self.params_body)
        params_body_layout.setContentsMargins(0, 0, 0, 0)
        self.params_scroll = QScrollArea()
        self.params_scroll.setWidgetResizable(True)
        self.params_scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter = main_splitter
        layout.addWidget(main_splitter, 1)
        params_panel = QWidget()
        panel_layout = QVBoxLayout(params_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)

        subnets_box = QGroupBox("Подсети для сканирования")
        subnets_layout = QVBoxLayout(subnets_box)
        self.subnets = QPlainTextEdit()
        self.subnets.setPlaceholderText("192.168.1.0/24\n192.168.2.0/24\n10.0.0.0/24")
        self.subnets.setToolTip(
            "По одной подсети в формате CIDR на строку. "
            "Пустые строки и строки с # пропускаются."
        )
        subnets_layout.addWidget(self.subnets, 1)
        panel_layout.addWidget(subnets_box)

        params_box = QGroupBox("Параметры")
        params_layout = QVBoxLayout(params_box)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.threads = ThreadCountSlider(value=32)
        self.threads.setToolTip("Параллельных проверок ping на этапе поиска.")
        form.addRow("Потоки пинга:", self.threads)
        self.discovery_mode = QComboBox()
        self.discovery_mode.addItem("ICMP", "icmp")
        self.discovery_mode.addItem("ICMP или TCP", "icmp_tcp")
        form.addRow("Обнаружение:", _narrow(self.discovery_mode))
        self.tcp_ports = QLineEdit("22, 80, 443")
        self.tcp_ports.setToolTip(
            "От 1 до 8 портов; TCP проверяется после отсутствия ICMP-ответа."
        )
        form.addRow("Порты TCP:", _narrow(self.tcp_ports))
        self.trace_threads = ThreadCountSlider(value=8)
        self.trace_threads.setToolTip(
            "Параллельных трассировок на этапе маршрутов. "
            "Большие значения нагружают сеть - 8 достаточно."
        )
        form.addRow("Потоки трассировки:", self.trace_threads)
        self.max_hops = QSpinBox()
        self.max_hops.setRange(1, 40)
        self.max_hops.setValue(20)
        self.max_hops.setToolTip("Сколько хопов искать до каждого хоста.")
        form.addRow("Макс. хопов:", _narrow(self.max_hops))
        self.trace_timeout = QSpinBox()
        self.trace_timeout.setRange(200, 10000)
        self.trace_timeout.setSingleStep(100)
        self.trace_timeout.setValue(5000)
        self.trace_timeout.setSuffix(" мс")
        self.trace_timeout.setToolTip(
            "Сколько ждать ответа каждого хопа. Медленные каналы и VPN - "
            "ставить 2000 и выше, иначе будут узлы «Без ответа»."
        )
        form.addRow("Таймаут хопа:", _narrow(self.trace_timeout))
        self.trace_retries = QSpinBox()
        self.trace_retries.setRange(0, 3)
        self.trace_retries.setValue(1)
        self.trace_retries.setToolTip(
            "Сколько раз повторно опрашивать хосты с молчащими хопами. "
            "0 - без повторов (быстрее, но больше узлов «Без ответа»)."
        )
        form.addRow("Ретраи:", _narrow(self.trace_retries))
        self.scan_found = QCheckBox("Сканировать найденные подсети")
        self.scan_found.setToolTip(
            "/24 каждого приватного хопа из трассировок добавляется в скан "
            "(пинг + маршруты, до 2 уровней и 16 подсетей). Публичные, "
            "loopback и link-local пропускаются. Скан дольше, карта полнее."
        )
        self.scan_found.toggled.connect(lambda _checked: self._persist_settings())
        form.addRow(self.scan_found)
        self.exact_routes = QCheckBox("Точные маршруты (каждый хост)")
        self.exact_routes.setToolTip(
            "Трассировать каждый живой хост персонально вместо выборки "
            "до трех представителей подсети. Нужно при расходящихся путях "
            "внутри подсети, но скан заметно дольше."
        )
        self.exact_routes.toggled.connect(lambda _checked: self._persist_settings())
        form.addRow(self.exact_routes)
        self.merge_unknown = QCheckBox("Объединить неизвестные")
        self.merge_unknown.setToolTip(
            "Визуально совместить молчащие хопы в одной точке. "
            "Измерения, узлы и связи маршрутов остаются независимыми."
        )
        self.merge_unknown.toggled.connect(self._on_merge_unknown_toggled)
        form.addRow(self.merge_unknown)
        self.layout_combo = QComboBox()
        self.layout_combo.addItem("Круги", "circles")
        self.layout_combo.addItem("Кольца", "rings")
        self.layout_combo.setToolTip(
            "Как раскладывать хосты вокруг сети. Круги - один круг, "
            "кольца - несколько концентрических колец."
        )
        self.layout_combo.currentIndexChanged.connect(self._on_layout_option_changed)
        form.addRow("Раскладка:", _narrow(self.layout_combo))
        self.edge_style_combo = QComboBox()
        self.edge_style_combo.addItem("Линия", "plain")
        self.edge_style_combo.addItem("Муравьи", "ants")
        self.edge_style_combo.setToolTip(
            "Стиль связей на графе. Линия - обычная линия со стрелкой "
            "направления. Муравьи - бегущие стрелки в сторону цели."
        )
        self.edge_style_combo.currentIndexChanged.connect(self._on_edge_style_changed)
        form.addRow("Линии:", _narrow(self.edge_style_combo))
        self.node_style_combo = QComboBox()
        self.node_style_combo.addItem("Схема", "scheme")
        self.node_style_combo.addItem("Иконки", "icons")
        self.node_style_combo.setToolTip(
            "Отображение узлов: Схема - цветные прямоугольники, "
            "Иконки - иконка сверху, подпись снизу, без прямоугольника."
        )
        self.node_style_combo.currentIndexChanged.connect(self._on_node_style_changed)
        form.addRow("Отображение:", _narrow(self.node_style_combo))
        spread_row = QHBoxLayout()
        spread_row.setContentsMargins(0, 0, 0, 0)
        self.spread_slider = QSlider(Qt.Orientation.Horizontal)
        self.spread_slider.setRange(40, 150)
        self.spread_slider.setValue(100)
        self.spread_slider.setTickInterval(10)
        self.spread_slider.setToolTip(
            "Множитель расстояний между узлами. 100% - без наложений, "
            "меньше - компактнее, больше - свободнее."
        )
        self.spread_slider.valueChanged.connect(self._on_spread_moved)
        self.spread_slider.sliderReleased.connect(self._on_layout_option_changed)
        spread_row.addWidget(self.spread_slider, 1)
        self.spread_label = QLabel("100%")
        self.spread_label.setMinimumWidth(42)
        spread_row.addWidget(self.spread_label)
        spread_box = QWidget()
        spread_box.setLayout(spread_row)
        form.addRow("Разброс:", spread_box)
        params_box = QGroupBox("Параметры")
        params_box_layout = QVBoxLayout(params_box)
        params_box_layout.addLayout(form)
        panel_layout.addWidget(params_box)
        panel_layout.addStretch(1)
        self.params_scroll.setWidget(params_panel)
        params_body_layout.addWidget(self.params_scroll)

        # Правая часть сплиттера: граф + вертикальный блок
        # [сведения об узле | управление картой].
        graph_panel = QWidget()
        graph_layout = QVBoxLayout(graph_panel)
        graph_layout.setContentsMargins(0, 0, 0, 0)
        self.view = NetworkView()
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_graph_context_menu)
        graph_layout.addWidget(self.view, 1)

        info_box = QGroupBox("Сведения об узле")
        info_layout = QVBoxLayout(info_box)
        self.info = QPlainTextEdit()
        self.info.setReadOnly(True)
        self.info.setPlaceholderText("Кликните по узлу на графе.")
        info_layout.addWidget(self.info, 1)
        self.retrace_button = QPushButton("Проверить маршрут")
        self.retrace_button.setToolTip(
            "Перетрассировать выбранный хост персонально. Реальный путь "
            "заменит общий маршрут подсети (помогает при разных путях)."
        )
        self.retrace_button.setEnabled(False)
        self.retrace_button.clicked.connect(self._retrace_selected_host)
        info_layout.addWidget(self.retrace_button)

        # Блок управления под сведениями: поиск, действия, легенда внизу.
        tools_box = QWidget()
        tools_layout = QVBoxLayout(tools_box)
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Поиск:"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("IP или имя хоста…")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Найти узел на графе по IP или имени.")
        self.search.returnPressed.connect(self._find_node)
        search_row.addWidget(self.search, 1)
        find_button = QPushButton("Найти")
        find_button.setToolTip("Выделить найденный узел на графе.")
        find_button.clicked.connect(self._find_node)
        search_row.addWidget(find_button)
        tools_layout.addLayout(search_row)
        buttons_grid = QGridLayout()
        self.fit_button = QPushButton("Вписать в окно")
        self.fit_button.setToolTip("Показать весь граф целиком (без смены позиций).")
        self.fit_button.clicked.connect(self._fit_view)
        self.reset_button = QPushButton("Разложить")
        self.reset_button.setToolTip(
            "Вернуть стандартную раскладку (ручные перемещения узлов "
            "будут потеряны) и вписать граф в окно."
        )
        self.reset_button.clicked.connect(self.reset_map)
        self.clear_button = QPushButton("Очистить")
        self.clear_button.setToolTip("Убрать все узлы с графа.")
        self.clear_button.clicked.connect(self.clear_graph)
        buttons_grid.addWidget(self.fit_button, 0, 0)
        buttons_grid.addWidget(self.reset_button, 0, 1)
        buttons_grid.addWidget(self.clear_button, 1, 0)
        tools_layout.addLayout(buttons_grid)
        # Легенда вертикально, прижата к низу блока. В режиме «Иконки»
        # вместо цветных точек показываются иконки типов узлов.
        legend_layout = QVBoxLayout()
        legend_layout.setContentsMargins(0, 0, 0, 0)
        legend_layout.setSpacing(0)
        self._legend_entries = (
            ("topo_network", "Сеть", "#4f9cff"),
            ("topo_router", "Маршрутизатор", "#ffb74d"),
            ("topo_host", "Хост", "#55d98b"),
            ("topo_local", "Этот компьютер", "#b388ff"),
            ("topo_unknown", "Без ответа", "#8a93a3"),
        )
        self._legend_labels = []
        for _key, text, _color in self._legend_entries:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            entry = QLabel()
            entry.setFixedWidth(16)
            entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
            entry.setToolTip("Цвета типов узлов на графе.")
            row.addWidget(entry, 0, Qt.AlignmentFlag.AlignVCenter)
            label = QLabel(text)
            label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            row.addWidget(label, 1)
            legend_layout.addLayout(row)
            self._legend_labels.append(entry)
        self._update_legend()
        edge_legend = QLabel(
            "Сплошная — наблюдаемый переход\nШтрихи — неизвестный участок\nШтрих-точки — гипотеза\nТочки — принадлежность CIDR"
        )
        edge_legend.setWordWrap(True)
        legend_layout.addWidget(edge_legend)
        legend_box = QWidget()
        legend_box.setLayout(legend_layout)
        tools_layout.addStretch(1)
        tools_layout.addWidget(legend_box)

        # Вертикальный сплиттер: сведения и управление 50/50 по высоте.
        right_splitter = QSplitter(Qt.Orientation.Vertical)
        right_splitter.addWidget(info_box)
        right_splitter.addWidget(tools_box)
        right_splitter.setStretchFactor(0, 1)
        right_splitter.setStretchFactor(1, 1)
        right_splitter.setSizes([300, 300])
        # Секции сплиттера: [панель настроек | граф | сведения/управление].
        right_splitter.setMinimumWidth(230)
        right_splitter.setMaximumWidth(360)
        main_splitter.addWidget(self.params_body)
        main_splitter.addWidget(graph_panel)
        main_splitter.addWidget(right_splitter)
        # Пропорции: панель настроек 270px, правая колонка 230px, граф - остальное.
        main_splitter.setStretchFactor(0, 0)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setStretchFactor(2, 0)
        main_splitter.setSizes([270, 580, 230])

        # Панель состояния сканирования - прижата к низу: все лейблы
        # Fixed по вертикали (иначе Preferred растягивался на пол-окна).
        counters = QHBoxLayout()
        self.phase_label = QLabel()
        self.phase_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.phase_label.setToolTip("Текущий этап сканирования.")
        counters.addWidget(self.phase_label)
        self.checked_label = QLabel()
        self.checked_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.checked_label.setToolTip("Сколько адресов уже проверено на этапе.")
        counters.addWidget(self.checked_label)
        self.found_label = QLabel()
        self.found_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.found_label.setToolTip("Сколько живых хостов найдено пингом.")
        counters.addWidget(self.found_label)
        self.routes_label = QLabel()
        self.routes_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.routes_label.setToolTip("Для скольких хостов построены маршруты.")
        counters.addWidget(self.routes_label)
        self.time_label = QLabel()
        self.time_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.time_label.setToolTip("Время с начала сканирования.")
        counters.addWidget(self.time_label)
        counters.addStretch(1)
        layout.addLayout(counters)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(18)
        # Виден только во время сканирования, в простое скрыт (место графу).
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        layout.addWidget(self.status)
        self.view.network_scene.selectionChanged.connect(self.node_selected)
        self.view.delete_requested.connect(self._delete_selected)

    # ------------------------------------------------------------------
    # Настройки (persist в settings.json)
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        try:
            from src.config import config

            net = config.app.network
            self.discovery_mode.setCurrentIndex(
                max(0, self.discovery_mode.findData(net.topology_discovery_mode))
            )
            self.tcp_ports.setText(", ".join(map(str, net.topology_tcp_ports)))
            if getattr(net, "topology_subnets", ""):
                self.subnets.setPlainText(net.topology_subnets)
            self.threads.setValue(int(getattr(net, "topology_threads", 32) or 32))
            self.trace_threads.setValue(
                int(getattr(net, "topology_trace_threads", 8) or 8)
            )
            self.max_hops.setValue(int(getattr(net, "topology_max_hops", 20) or 20))
            self.trace_timeout.setValue(
                int(getattr(net, "topology_trace_timeout", 5000) or 5000)
            )
            self.trace_retries.setValue(
                int(getattr(net, "topology_trace_retries", 1) or 0)
            )
            self.scan_found.blockSignals(True)
            try:
                self.scan_found.setChecked(
                    bool(getattr(net, "topology_scan_found_subnets", False))
                )
            finally:
                self.scan_found.blockSignals(False)
            self.exact_routes.blockSignals(True)
            try:
                self.exact_routes.setChecked(
                    bool(getattr(net, "topology_exact_routes", False))
                )
            finally:
                self.exact_routes.blockSignals(False)
            self.merge_unknown.blockSignals(True)
            try:
                self.merge_unknown.setChecked(
                    bool(getattr(net, "topology_merge_unknown", False))
                )
            finally:
                self.merge_unknown.blockSignals(False)
            try:
                saved_names = dict(getattr(net, "topology_node_names", {}) or {})
            except Exception:
                saved_names = {}
            self._node_names = {
                str(k): str(v) for k, v in saved_names.items() if str(v).strip()
            }
            layout = str(getattr(net, "topology_layout", "circles") or "circles")
            index = self.layout_combo.findData(layout)
            self.layout_combo.blockSignals(True)
            try:
                self.layout_combo.setCurrentIndex(index if index >= 0 else 0)
            finally:
                self.layout_combo.blockSignals(False)
            edge_style = str(getattr(net, "topology_edge_style", "ants") or "ants")
            index = self.edge_style_combo.findData(edge_style)
            self.edge_style_combo.blockSignals(True)
            try:
                self.edge_style_combo.setCurrentIndex(index if index >= 0 else 1)
            finally:
                self.edge_style_combo.blockSignals(False)
            # Стиль применен с blockSignals - сцене сообщаем напрямую.
            self.view.network_scene.set_edge_style(
                str(self.edge_style_combo.currentData() or "ants")
            )
            node_style = str(getattr(net, "topology_node_style", "scheme") or "scheme")
            index = self.node_style_combo.findData(node_style)
            self.node_style_combo.blockSignals(True)
            try:
                self.node_style_combo.setCurrentIndex(index if index >= 0 else 0)
            finally:
                self.node_style_combo.blockSignals(False)
            self.view.network_scene.set_node_style(self._node_style())
            self._update_legend()
            spread = int(getattr(net, "topology_spread", 100) or 100)
            self.spread_slider.blockSignals(True)
            try:
                self.spread_slider.setValue(max(40, min(150, spread)))
            finally:
                self.spread_slider.blockSignals(False)
            self._update_spread_label()
        except Exception as e:
            logger.debug(f"TopologyMapDialog: load settings failed: {e}")

    def _persist_settings(self) -> None:
        from src.di import get_container
        from src.services.configuration.config_service import ConfigService

        try:
            service = get_container().resolve_optional(ConfigService) or ConfigService()
            success, message = service.save_topology_config(
                topology_subnets=self.subnets.toPlainText(),
                topology_discovery_mode=self.discovery_mode.currentData(),
                topology_tcp_ports=self._tcp_port_values(),
                topology_threads=self.threads.value(),
                topology_trace_threads=self.trace_threads.value(),
                topology_max_hops=self.max_hops.value(),
                topology_trace_timeout=self.trace_timeout.value(),
                topology_trace_retries=self.trace_retries.value(),
                topology_scan_found_subnets=self.scan_found.isChecked(),
                topology_exact_routes=self.exact_routes.isChecked(),
                topology_merge_unknown=self.merge_unknown.isChecked(),
                topology_node_names=dict(self._node_names),
                topology_layout=str(self.layout_combo.currentData() or "circles"),
                topology_edge_style=str(self.edge_style_combo.currentData() or "ants"),
                topology_node_style=self._node_style(),
                topology_spread=self.spread_slider.value(),
            )
            if not success:
                self._render_status(f"Ошибка сохранения: {message}")
        except (ValueError, OSError) as exc:
            self._render_status(f"Ошибка настроек: {exc}")

    def _tcp_port_values(self) -> tuple[int, ...]:
        from src.utils.network.topology_settings import validate_option

        ports = tuple(int(p.strip()) for p in self.tcp_ports.text().split(","))
        validate_option("topology_tcp_ports", ports)
        return ports

    def _update_spread_label(self) -> None:
        self.spread_label.setText(f"{self.spread_slider.value()}%")

    def _on_spread_moved(self) -> None:
        self._update_spread_label()

    def _on_layout_option_changed(self) -> None:
        """Стиль или разброс изменены: сохранить и переразложить граф."""
        self._update_spread_label()
        self._persist_settings()
        if self.view.network_scene.nodes:
            self.layout_graph()

    def _on_edge_style_changed(self, index: int) -> None:
        """Стиль линий изменен: сохранить и перекрасить все ребра."""
        self._persist_settings()
        style = str(self.edge_style_combo.currentData() or "ants")
        self.view.network_scene.set_edge_style(style)

    def _node_style(self) -> str:
        """Текущий стиль узлов: scheme | icons."""
        try:
            return str(self.node_style_combo.currentData() or "scheme")
        except Exception:
            return "scheme"

    def _on_node_style_changed(self, index: int) -> None:
        """Отображение узлов изменено: сохранить, применить, обновить легенду."""
        self._persist_settings()
        try:
            self.view.network_scene.set_node_style(self._node_style())
        except Exception as e:
            logger.debug(f"TopologyMapDialog: set_node_style failed: {e}")
        self._update_legend()

    def _update_legend(self) -> None:
        """Текст легенды под стиль узлов: точки (схема) или иконки типов."""
        icons = self._node_style() == "icons"
        for label, (key, _text, color) in zip(
            self._legend_labels, self._legend_entries
        ):
            label.clear()
            if icons:
                try:
                    pixmap = _icon_from_key(key).pixmap(16, 16)
                except Exception:
                    pixmap = None
                if pixmap is not None and not pixmap.isNull():
                    label.setPixmap(pixmap)
                    continue
            label.setText(f'<font color="{color}">●</font>')

    def _device_presentation(self, ip: str, role: str) -> tuple[str, str, str, str]:
        """Единый выбор подписи, иконки, типа и основания для адресного узла."""
        facts = self.device_facts.get(ip, DeviceFacts(hostname=self.hostnames.get(ip, "")))
        device_type, reason = facts.classify()
        hostname = self.hostnames.get(ip, "").strip()
        if not hostname or hostname == ip:
            hostname = facts.hostname.strip()
        if hostname == ip:
            hostname = ""
        if role == "local":
            name, key = "Этот компьютер", "topo_local"
        else:
            known = device_type != TYPE_UNKNOWN
            default = "Маршрутизатор" if role == "router" else "Хост"
            name = hostname or (device_type if known else default)
            key = device_type_icon_key(device_type) if known else ""
            key = key or ("topo_router" if role == "router" else "topo_host")
        return self._saved_name_for(ip, name), key, device_type, reason

    def refresh_icons(self) -> None:
        """Перечитать иконки узлов под текущую тему иконок (live-переключение)."""
        try:
            self.view.network_scene.refresh_theme()
        except Exception as e:
            logger.debug(f"TopologyMapDialog: refresh_icons scene failed: {e}")
        try:
            self._update_legend()
        except Exception as e:
            logger.debug(f"TopologyMapDialog: refresh_icons legend failed: {e}")

    def _on_merge_unknown_toggled(self, checked: bool) -> None:
        """Галочка «Объединить неизвестные»: пересобрать узлы молчунов."""
        self._persist_settings()
        self._rebuild_unknown_nodes()

    def _hop_id_for(self, destination: str, hop: dict, index: int) -> str:
        """Независимые id даже при визуальном объединении неизвестных."""
        return _hop_node_id(destination, hop, index)

    def _rebuild_unknown_nodes(self) -> None:
        """Визуальная группировка сохраняет маршруты, id и графические связи."""
        scene = self.view.network_scene
        unknown = [n for n in scene.nodes.values() if n.node_type == "unknown"]
        grouped = self.merge_unknown.isChecked()
        for index, node in enumerate(unknown):
            if grouped:
                if not hasattr(node, "_ungrouped_position"):
                    node._ungrouped_position = node.pos()
                node.setPos(-250, -150)
                node.update_label(name="Без ответа (визуальная группа)")
            else:
                if hasattr(node, "_ungrouped_position"):
                    node.setPos(node._ungrouped_position)
                    del node._ungrouped_position
                node.update_label(name="Без ответа")

    def _chain_ids(self, destination: str, route: list) -> list:
        """Цепочка id хопов маршрута с учетом галочки «Объединить»."""
        return [self._hop_id_for(destination, hop, i) for i, hop in enumerate(route)]

    # ------------------------------------------------------------------
    # Воркер
    # ------------------------------------------------------------------

    def _connect_worker(self) -> None:
        for signal, handler in (
            (self.worker.status, self._on_worker_status),
            (self.worker.progress, self.update_progress),
            (self.worker.phase_changed, self._on_phase_changed),
            (self.worker.host_found, self.on_host_found),
            (self.worker.route_found, self.on_route_found),
            (self.worker.name_found, self._on_name_found),
            (self.worker.probe_found, self._on_probe_found),
            (self.worker.device_found, self._on_device_found),
            (self.worker.error, self.on_error),
            (self.worker.finished, self.scan_finished),
        ):
            signal.connect(
                lambda *args, target=handler: self._dispatch_scan(target, args)
            )

    def _dispatch_scan(self, handler, args: tuple) -> None:
        if not self._closed and self.worker.generation == self._generation:
            handler(*args)
        elif not self._closed and handler == self.scan_finished:
            self.scan_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self._set_inputs_enabled(True)
            self._clock.stop()
            self.progress.setVisible(False)

    def _on_probe_found(self, ip: str, probes: list) -> None:
        if any(probe.state in ("reply", "open", "closed") for probe in probes):
            self.probes[ip] = probes
            self.device_facts[ip] = DeviceFacts(
                hostname=self.hostnames.get(ip, ""), probes=tuple(probes),
                ttl=next((p.ttl for p in probes if p.protocol == "icmp" and p.state == "reply"), None),
            )
        else:
            self.probes.pop(ip, None)
        if self._rescan_network and self.host_network.get(ip) == self._rescan_network:
            # При отмене непроверенная часть карты должна сохранить прежние измерения.
            scene = self.view.network_scene
            previous = self.routes.pop(ip, None)
            old_nodes = {
                self._hop_id_for(ip, hop, index)
                for index, hop in enumerate(previous or [])
            }
            scene.remove_owners(
                [f"route:{ip}", f"hypothesis:{self._rescan_network}:{ip}"]
            )
            self.route_source.pop(ip, None)
            scene.remove_nodes({
                nid for nid in old_nodes
                if nid not in self.host_network and not scene.incident.get(nid)
                and nid in scene.nodes and scene.nodes[nid].node_type != "local"
            })

    def _on_name_found(self, ip: str, name: str) -> None:
        scene = self.view.network_scene
        if ip not in scene.nodes:
            return
        self.hostnames[ip] = name
        if ip in self.device_facts:
            self.device_facts[ip] = replace(self.device_facts[ip], hostname=name)
        self._refresh_device_node(ip)
        if scene.nodes[ip].isSelected():
            self.node_selected()

    def _on_device_found(self, ip: str, facts: DeviceFacts) -> None:
        """Применить признаки только к существующему узлу актуального поколения."""
        node = self.view.network_scene.nodes.get(ip)
        if node is None:
            return
        self.device_facts[ip] = replace(
            facts, hostname=(self.hostnames.get(ip) if self.hostnames.get(ip) != ip else "") or facts.hostname
        )
        self.probes[ip] = list(facts.probes)
        self._refresh_device_node(ip)
        if node.isSelected():
            self.node_selected()

    def _refresh_device_node(self, ip: str) -> None:
        """Согласованно обновить подпись, иконку и подсказку без смены L3-роли."""
        node = self.view.network_scene.nodes.get(ip)
        if node is None or node.node_type not in ("host", "router", "local"):
            return
        name, key, device_type, reason = self._device_presentation(ip, node.node_type)
        if node.name != name:
            node.update_label(name=name)
        node.set_icon_key(key)
        node.setToolTip(f"{ip}\nТип оборудования: {device_type}\n{reason}")

    def _set_node_name(self, node: NetworkNode, name: str) -> None:
        """Сохранить ручную подпись или вернуть автоматическое имя по текущим данным."""
        name = name.strip()
        if name:
            self._node_names[node.node_id] = name
        else:
            self._node_names.pop(node.node_id, None)
        if node.node_type in ("host", "router", "local"):
            self._refresh_device_node(node.node_id)
        else:
            default = "Сеть" if node.node_type == "network" else "Без ответа"
            node.update_label(name=self._saved_name_for(node.node_id, default))

    def _invalidate_jobs(self) -> None:
        self._generation += 1
        for worker in self._single_jobs:
            worker.stop()
        self._single_trace = None

    def _set_inputs_enabled(self, enabled: bool) -> None:
        self.discovery_mode.setEnabled(enabled)
        self.tcp_ports.setEnabled(enabled)
        self.subnets.setEnabled(enabled)
        self.threads.setEnabled(enabled)
        self.trace_threads.setEnabled(enabled)
        self.max_hops.setEnabled(enabled)
        self.trace_timeout.setEnabled(enabled)
        self.trace_retries.setEnabled(enabled)
        self.scan_found.setEnabled(enabled)
        self.exact_routes.setEnabled(enabled)
        self.merge_unknown.setEnabled(enabled)

    def _setup_run_state(self) -> None:
        """Состояние запуска (кнопки, счетчики, часы) без очистки графа."""
        self._closed = False
        self._invalidate_jobs()
        self.worker.generation = self._generation
        self._scan_error = ""
        self._phase = "ping"
        self._phase_done = 0
        self._phase_total = 0
        self._routes_done = 0
        self._routes_total = 0
        self._started_at = time.monotonic()
        self._clock.start()
        self.progress.setValue(0)
        self.progress.setFormat("Подготовка…")
        self.progress.setVisible(True)
        self.scan_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.retrace_button.setEnabled(False)
        self._set_inputs_enabled(False)

    def start_scan(self) -> None:
        try:
            self._tcp_port_values()
        except ValueError as exc:
            self._render_status(f"Ошибка портов TCP: {exc}")
            return
        networks = parse_networks(self.subnets.toPlainText())
        if not networks:
            QMessageBox.warning(self, "Ошибка", "Не найдено корректных IPv4 подсетей.")
            return
        if self.worker.is_running():
            return
        self._rescan_network = None
        self.probes.clear()
        self.device_facts.clear()
        self.host_network.clear()
        self.hostnames.clear()
        self.routes.clear()
        self.network_routes.clear()
        self.route_source.clear()
        self.view.network_scene.clear_graph()
        self.info.clear()
        self.retrace_button.setEnabled(False)
        if self._single_trace is not None:
            self._single_trace.stop()
            self._single_trace = None
        self._setup_run_state()
        self._persist_settings()
        started = self.worker.start(
            networks=networks,
            ping_workers=self.threads.value(),
            trace_workers=self.trace_threads.value(),
            max_hops=self.max_hops.value(),
            traceroute_timeout=self.trace_timeout.value(),
            trace_retries=self.trace_retries.value(),
            scan_found_subnets=self.scan_found.isChecked(),
            trace_exact_routes=self.exact_routes.isChecked(),
            discovery_mode=self.discovery_mode.currentData(),
            tcp_ports=self._tcp_port_values(),
        )
        if not started:
            self.scan_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self._set_inputs_enabled(True)
            self._clock.stop()
            return
        self._render_status("Сканирование запущено.")

    def stop_scan(self) -> None:
        self.worker.stop()
        self.stop_button.setEnabled(False)
        self._render_status("Остановка - ждем завершения фоновых проверок…")

    @staticmethod
    def _node_scan_target(node: NetworkNode) -> str | None:
        """CIDR для досканирования по узлу или None.

        Роутер - его /24 по умолчанию (размер правится в диалоге),
        сеть - ее собственный CIDR. Остальным меню не показываем.
        """
        try:
            if node.node_type == "router":
                addr = ipaddress.ip_address(node.node_id)
                if addr.version != 4:
                    return None
                return str(ipaddress.ip_network(f"{node.node_id}/24", strict=False))
            if node.node_type == "network":
                net = ipaddress.ip_network(node.node_id, strict=False)
                if net.version != 4:
                    return None
                return str(net)
        except ValueError:
            return None
        return None

    def _on_graph_context_menu(self, pos) -> None:
        """Правый клик по графу: свойства узла, доскан, удаление выделенного.

        Клик по узлу вне выделения - выделить только его (меню действует
        на одного). Клик по узлу из выделения - меню действует на всю
        выделенную группу.
        """
        item = self.view.itemAt(pos)
        node = None
        while item is not None:
            if isinstance(item, NetworkNode):
                node = item
                break
            item = item.parentItem()
        if node is None:
            return
        scene = self.view.network_scene
        if not node.isSelected():
            scene.clearSelection()
            node.setSelected(True)
        # Группа = все выделенные узлы; правый клик всегда по выделенному.
        targets = [n for n in scene.selectedItems() if isinstance(n, NetworkNode)]
        if node not in targets:
            targets = [node]
        menu = QMenu(self)
        if len(targets) == 1:
            node = targets[0]
            if node.node_type in ("host", "router"):
                act_rename = menu.addAction("Свойства устройства…")
            elif node.node_type == "network":
                act_rename = menu.addAction("Свойства Подсети…")
            else:
                act_rename = None
        else:
            act_rename = None
        deletable = [n for n in targets if n.node_type != "local"]
        if deletable:
            menu.addSeparator()
            if len(deletable) == 1 and deletable[0] is targets[0]:
                act_delete = menu.addAction("Удалить")
            else:
                act_delete = menu.addAction(f"Удалить выделенное ({len(deletable)})")
            act_delete.setToolTip(
                "Убрать узел(-ы) с карты после подтверждения. "
                "Узел «Этот компьютер» не удаляется."
            )
        else:
            act_delete = None
        if act_rename is None and act_delete is None:
            return
        chosen = menu.exec(self.view.mapToGlobal(pos))
        if chosen is None:
            return
        if act_rename is not None and chosen == act_rename:
            self._node_properties(node)
        elif act_delete is not None and chosen == act_delete:
            self._delete_nodes(deletable)

    def _node_properties(self, node: NetworkNode) -> None:
        """Свойства узла: подпись + досканирование подсети в одном диалоге.

        Кнопка «Применить» сохраняет изменения и оставляет диалог
        открытым, «Закрыть» - закрывает. Для сети CIDR правится на
        месте (подпись переносится), для роутера/хоста - доскан его
        подсети. Неизвестным и локальному узлу - только подпись.
        """
        titles = {
            "host": "Свойства устройства",
            "router": "Свойства устройства",
            "network": "Свойства Подсети",
        }
        node_type = node.node_type
        title = titles.get(node_type, "Свойства узла")
        scan_target = self._node_scan_target(node)
        is_network = node_type == "network"
        current_cidr = node.node_id if is_network else (scan_target or "")

        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        scan_check = None
        if is_network:
            # CIDR сети правится на месте: узел переадресовывается.
            cidr_edit = QLineEdit(current_cidr)
            cidr_edit.setToolTip(
                "Адрес подсети (CIDR, размер на выбор): например "
                "192.168.250.0/24, /25 для половины, /23 для двойной. "
                "Изменение переносит подпись и досканирует новую подсеть."
            )
            form.addRow("Подсеть (CIDR):", cidr_edit)
        elif scan_target is not None:
            cidr_edit = None
            scan_check = QCheckBox(f"Досканировать подсети {scan_target}")
            scan_check.setToolTip(
                "Запустить досканирование этой подсети при нажатии "
                "«Применить» (одна подсеть за раз)."
            )
            form.addRow("", scan_check)
        else:
            cidr_edit = None
        name_edit = QLineEdit(node.name)
        name_edit.setPlaceholderText("Пусто — вернуть автоматическую подпись")
        name_edit.selectAll()
        form.addRow("Подпись на карте:", name_edit)
        layout.addLayout(form)
        if is_network:
            hint = QLabel("Изменение CIDR переносит подпись на новую подсеть.")
            hint.setWordWrap(True)
            layout.addWidget(hint)

        # Кнопки вручную (не QDialogButtonBox): ApplyRole/CloseRole рисуются
        # платформой в своем порядке (Close слева на Windows), а нужен
        # [Применить][Закрыть]. QHBoxLayout слева направо - как в тексте.
        buttons_row = QHBoxLayout()
        buttons_row.addStretch(1)
        apply_btn = QPushButton("Применить")
        apply_btn.setToolTip("Сохранить изменения, не закрывая окно.")
        close_btn = QPushButton("Закрыть")
        close_btn.setToolTip("Закрыть окно без сохранения.")
        buttons_row.addWidget(apply_btn)
        buttons_row.addWidget(close_btn)
        close_btn.setDefault(True)
        layout.addLayout(buttons_row)

        def _apply() -> bool:
            """Сохранить правки. False - ошибка ввода (диалог остается)."""
            nonlocal node, current_cidr
            if cidr_edit is not None:
                networks = parse_networks(cidr_edit.text())
                if len(networks) != 1:
                    QMessageBox.warning(
                        self,
                        "Ошибка",
                        "Нужна ровно одна корректная IPv4 подсеть.",
                    )
                    return False
                corrected = str(networks[0])
                cidr_changed = corrected != current_cidr
                if cidr_changed:
                    scene = self.view.network_scene
                    # CIDR исправлен: перенести сохраненную подпись со
                    # старой подсети на новую (если была).
                    old_name = self._node_names.get(current_cidr)
                    if old_name:
                        self._node_names.pop(current_cidr, None)
                        self._node_names[corrected] = old_name
                    old_node = scene.nodes.get(current_cidr)
                    if old_node is not None and old_node.node_type == "network":
                        scene.rename_node(current_cidr, corrected)
                        old_node.ip = corrected
                        old_node.update_label(
                            name=old_name or old_node.name, ip=corrected
                        )
                        for ip, net in list(self.host_network.items()):
                            if net == current_cidr:
                                self.host_network[ip] = corrected
                        scene.remove_owners(
                            [
                                o
                                for o in scene.owner_edges
                                if o.startswith(f"hypothesis:{current_cidr}:")
                            ]
                        )
                        self.network_routes.pop(current_cidr, None)
                    node = scene.nodes.get(corrected) or node
                # Подпись применяется только если поле правилось: иначе
                # предзаполненный текст затер бы перенос подписи.
                if name_edit.isModified():
                    self._set_node_name(node, name_edit.text())
                self._persist_settings()
                self.node_selected()
                if cidr_changed:
                    current_cidr = corrected
                    self.rescan_subnet(networks[0])
                return True
            # Не-сеть: подпись (если правилась) + опциональный доскан.
            if name_edit.isModified():
                self._set_node_name(node, name_edit.text())
                self._persist_settings()
                self.node_selected()
            if scan_check is not None and scan_check.isChecked():
                scan_check.setChecked(False)
                self.rescan_subnet(scan_target)
            return True

        apply_btn.clicked.connect(_apply)
        close_btn.clicked.connect(dialog.reject)
        dialog.exec()

    def rescan_subnet(self, network) -> bool:
        """Досканировать подсеть поверх текущей карты. False если занято."""
        if self.worker.is_running():
            QMessageBox.information(self, "Карта сети", "Дождитесь конца сканирования.")
            return False
        try:
            self._tcp_port_values()
        except ValueError as exc:
            self._render_status(f"Ошибка портов TCP: {exc}")
            return False
        network = ipaddress.ip_network(network, strict=False)
        self._rescan_network = str(network)
        new_text, changed = update_subnets_text(self.subnets.toPlainText(), network)
        if changed:
            self.subnets.setPlainText(new_text)
        self._persist_settings()
        self._setup_run_state()
        started = self.worker.start(
            networks=[network],
            known_networks=parse_networks(self.subnets.toPlainText()),
            ping_workers=self.threads.value(),
            trace_workers=self.trace_threads.value(),
            max_hops=self.max_hops.value(),
            traceroute_timeout=self.trace_timeout.value(),
            trace_retries=self.trace_retries.value(),
            scan_found_subnets=self.scan_found.isChecked(),
            trace_exact_routes=self.exact_routes.isChecked(),
            discovery_mode=self.discovery_mode.currentData(),
            tcp_ports=self._tcp_port_values(),
        )
        if not started:
            self.scan_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self._set_inputs_enabled(True)
            self._clock.stop()
            return False
        self._render_status(f"Досканирование {network}…")
        return True

    def _delete_selected(self) -> None:
        """Клавиша Delete на графе: удалить все выделенные узлы."""
        selected = [
            n
            for n in self.view.network_scene.selectedItems()
            if isinstance(n, NetworkNode)
        ]
        self._delete_nodes(selected)

    def _delete_nodes(self, nodes: list) -> None:
        """Удалить узлы с карты после подтверждения.

        Роутеры и подсети - с предупреждением о последствиях (маршруты
        через роутер пропадут; подсеть уносит связь хостов с сетью).
        Для подсетей предлагается галка «Также удалить хосты подсети».
        Данные скана удаляемых хостов чистятся, иначе тумблер
        «Объединить неизвестные» и ресканы воскресят узлы. Узел
        «Этот компьютер» не удаляется никогда.
        """
        if not nodes:
            return
        if self.worker.is_running():
            QMessageBox.information(self, "Карта сети", "Дождитесь конца сканирования.")
            return
        nodes = [n for n in nodes if n.node_type != "local"]
        if not nodes:
            return
        scene = self.view.network_scene
        routers = [n for n in nodes if n.node_type == "router"]
        networks = [n for n in nodes if n.node_type == "network"]
        # Живые хостов удаляемых подсетей (для текста и галки).
        network_hosts: dict = {}
        for net_node in networks:
            network_hosts[net_node.node_id] = [
                ip for ip, net in self.host_network.items() if net == net_node.node_id
            ]
        hosts_total = sum(len(v) for v in network_hosts.values())

        # Подтверждение: текст зависит от состава выделения.
        title = "Удаление узлов"
        if len(nodes) == 1:
            node = nodes[0]
            if node.node_type == "network":
                header = f"Удалить подсеть {node.node_id} с карты?"
            elif node.node_type == "router":
                header = f"Удалить маршрутизатор {node.ip} с карты?"
            elif node.node_type == "unknown":
                header = "Удалить узел «Без ответа» с карты?"
            else:
                header = f"Удалить хост {node.ip} с карты?"
        else:
            header = f"Удалить выделенные узлы ({len(nodes)}) с карты?"
        lines = [header]
        if routers:
            lines.append(
                "Маршрутизатор - транзитный узел: вместе с ним с карты "
                "пропадут построенные через него маршруты. При повторном "
                "сканировании узел появится снова."
            )
        if networks:
            lines.append(
                "Подсеть удаляется вместе со связями: её хосты останутся "
                "на карте, но потеряют связь со своей сетью."
            )
        message = "\n\n".join(lines)
        box = QMessageBox(QMessageBox.Icon.Question, title, message, parent=self)
        confirm = box.addButton("Удалить", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        remove_hosts_check = None
        if networks and hosts_total:
            remove_hosts_check = QCheckBox(
                f"Также удалить хосты подсетей ({hosts_total})"
            )
            remove_hosts_check.setToolTip(
                "Иначе хосты подсети останутся на карте без связи с сетью."
            )
            box.setCheckBox(remove_hosts_check)
        box.setDefaultButton(confirm)
        box.exec()
        if box.clickedButton() is not confirm:
            return

        # Подсети с галкой: расшить выбор на хосты этих подсетей.
        if remove_hosts_check is not None and remove_hosts_check.isChecked():
            hosts_extra: list = []
            for net_node in networks:
                for ip in network_hosts[net_node.node_id]:
                    node = scene.nodes.get(ip)
                    if node is not None and node not in nodes:
                        hosts_extra.append(node)
            nodes.extend(hosts_extra)

        # Удаление: сначала ребра (паттерн _rebuild_unknown_nodes),
        # затем узлы; чистка данных скана удаленных хостов. Подписи
        # (_node_names) не трогаем - переименования переживают ресканы.
        removed_ids = {n.node_id for n in nodes}
        self._invalidate_jobs()
        self._remove_data_nodes(removed_ids)

        scene.clearSelection()
        scene.clear_highlights()
        scene.update_edges()
        self.node_selected()
        self._refresh_retrace_button()
        self._render_status(f"Удалено узлов: {len(nodes)}.")

    def _remove_data_nodes(self, removed_ids: set[str]) -> None:
        scene = self.view.network_scene
        affected = {
            dest
            for dest, route in self.routes.items()
            if dest in removed_ids
            or any(h.get("ip") in removed_ids for h in route)
            or any(
                self._hop_id_for(dest, h, i) in removed_ids for i, h in enumerate(route)
            )
        }
        for destination in affected:
            scene.remove_owners([f"route:{destination}"])
            self.routes.pop(destination, None)
            self.route_source.pop(destination, None)
        for net, info in list(self.network_routes.items()):
            if net in removed_ids or any(
                h.get("ip") in removed_ids for h in info["hops"]
            ):
                scene.remove_owners(
                    [o for o in scene.owner_edges if o.startswith(f"hypothesis:{net}:")]
                )
                self.network_routes.pop(net, None)
        for key in removed_ids:
            net = self.host_network.get(key, "")
            scene.remove_owners([f"hypothesis:{net}:{key}"])
            self.host_network.pop(key, None)
            self.hostnames.pop(key, None)
            self.probes.pop(key, None)
            self.device_facts.pop(key, None)
        for ip, net in list(self.host_network.items()):
            if net in removed_ids:
                self.host_network.pop(ip)
        scene.remove_nodes(removed_ids)

    def clear_graph(self) -> None:
        self.worker.stop()
        self._invalidate_jobs()
        self._rescan_network = None
        self.probes.clear()
        self.device_facts.clear()
        self.host_network.clear()
        self.hostnames.clear()
        self.routes.clear()
        self.network_routes.clear()
        self.route_source.clear()
        self.view.network_scene.clear_graph()
        self.info.clear()
        self._phase_done = 0
        self._phase_total = 0
        self._routes_done = 0
        self._routes_total = 0
        self._refresh_retrace_button()
        self._render_status("Готов. Введите подсети и нажмите «Сканировать».")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        try:
            self._closed = True
            self._invalidate_jobs()
            self._persist_settings()
            self.worker.stop()
            if self._single_trace is not None:
                self._single_trace.stop()
            self._clock.stop()
        except Exception:
            pass
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Слоты воркера
    # ------------------------------------------------------------------

    def _on_worker_status(self, text: str) -> None:
        self._render_status(text)

    def _on_phase_changed(self, phase: str) -> None:
        self._phase = phase if phase in _PHASE_NAMES else "idle"
        if phase == "trace":
            # Счетчик теперь по подсетям - приедет первым progress(0, N).
            self._routes_total = 0
            self._routes_done = 0
        self._render_status()

    def update_progress(self, current: int, total: int) -> None:
        if total <= 0:
            return
        if self._phase == "trace":
            self._routes_done = current
            self._routes_total = total
        else:
            self._phase_done = current
            self._phase_total = total
        pct = int(current / total * 100)
        self.progress.setValue(pct)
        self.progress.setFormat(f"{current}/{total} ({pct}%)")
        self._render_status()

    def _render_status(self, text: str | None = None) -> None:
        if text is not None:
            self.status.setText(text)
        self.phase_label.setText(f"Этап: {_PHASE_NAMES.get(self._phase, 'Ожидание')}")
        if self._phase == "trace":
            self.checked_label.setText("")
            self.routes_label.setText(
                f"Маршруты: {self._routes_done}/{self._routes_total}"
            )
        elif self._phase_total:
            self.checked_label.setText(
                f"Проверено: {self._phase_done}/{self._phase_total}"
            )
            self.routes_label.setText("")
        else:
            self.checked_label.setText("")
            self.routes_label.setText("")
        self.found_label.setText(f"Найдено хостов: {len(self.host_network)}")
        if self.worker.is_running() or self._phase != "idle":
            self.time_label.setText(
                f"Время: {_format_elapsed(time.monotonic() - self._started_at)}"
            )
        elif self._started_at:
            self.time_label.setText(
                f"Время: {_format_elapsed(time.monotonic() - self._started_at)}"
            )
        else:
            self.time_label.setText("")

    def _tick_clock(self) -> None:
        if self.worker.is_running():
            self.time_label.setText(
                f"Время: {_format_elapsed(time.monotonic() - self._started_at)}"
            )

    def _saved_name_for(self, node_id: str, default: str) -> str:
        """Сохраненная подпись узла или default (если не переименовывали)."""
        return self._node_names.get(node_id) or default

    def on_host_found(self, network: str, ip: str, hostname: str) -> None:
        if hostname == ip:
            hostname = ""
        hostname = hostname or self.hostnames.get(ip, "")
        old_network = self.host_network.get(ip)
        if (
            old_network
            and ipaddress.ip_network(old_network).prefixlen
            > ipaddress.ip_network(network).prefixlen
        ):
            network = old_network
        self.host_network[ip] = network
        self.hostnames[ip] = hostname
        scene = self.view.network_scene
        if network not in scene.nodes:
            network_index = sum(
                1 for n in scene.nodes.values() if n.node_type == "network"
            )
            scene.add_node(
                network,
                self._saved_name_for(network, "Сеть"),
                network,
                "network",
                ((network_index % 3) * 450, (network_index // 3) * 350),
                icon_key="topo_network",
            )
        if ip not in scene.nodes:
            name, key, _, _ = self._device_presentation(ip, "host")
            scene.add_node(ip, name, ip, "host", (0, 0), icon_key=key)
        scene.add_edge(network, ip)
        self._refresh_device_node(ip)
        if ip in self.routes:
            # Маршрут пришел раньше хоста: достроить ребра цепочки.
            self._link_route_edges(ip)
        if scene.nodes[ip].isSelected():
            self.node_selected()
        self._render_status()

    def _link_chain(self, host: str, chain: list, src: str) -> None:
        """Связи принадлежат маршруту; неизвестный хвост не равен одному переходу."""
        scene = self.view.network_scene
        owner = f"route:{host}"
        pairs = list(zip(chain, chain[1:]))
        if src and chain and chain[0] != src:
            pairs.insert(0, (src, chain[0]))
        for first, second in pairs:
            kind = (
                "unknown"
                if first.startswith("unknown:") or second.startswith("unknown:")
                else "observed"
            )
            scene.add_edge(first, second, kind, owner)
        if chain and chain[-1] != host:
            scene.add_edge(chain[-1], host, "unknown", owner)

    def _link_route_edges(self, destination: str) -> None:
        """Достроить ребра цепочки известного маршрута.

        Нужен когда route_found пришел раньше host_found (в воркере так
        не бывает - пинг всегда первый, но защита дешевая).
        add_edge дубли игнорирует.
        """
        route = self.routes.get(destination) or []
        if not route:
            return
        chain = [self._hop_id_for(destination, hop, i) for i, hop in enumerate(route)]
        self._link_chain(destination, chain, self.route_source.get(destination) or "")

    def _ensure_local(self, src_ip: str) -> None:
        """??????? ??? ???????? ???? ????????? ? ?????? ?????????????? ????????."""
        scene = self.view.network_scene
        existing = scene.nodes.get(src_ip)
        if existing is None:
            name, key, _, _ = self._device_presentation(src_ip, "local")
            scene.add_node(src_ip, name, src_ip, "local", (0, 0), icon_key=key)
        elif existing.node_type != "local":
            existing.node_type = "local"
            existing.refresh_theme()
        self._refresh_device_node(src_ip)

    def _ensure_route_nodes(self, owner: str, route: list) -> list:
        """??????? ????, ???????? ?????? ????????????? ???????????? ? ??????????? TTL."""
        scene = self.view.network_scene
        chain: list = []
        for index, hop in enumerate(route):
            hop_ip = hop.get("ip", "")
            node_id = self._hop_id_for(owner, hop, index)
            if hop_ip:
                hostname = hop.get("hostname", "")
                if hostname and hostname != hop_ip and not self.hostnames.get(hop_ip):
                    self.hostnames[hop_ip] = hostname
                transit = hop_ip != owner and any(h.get("ip") for h in route[index + 1:])
                node_type = "router" if transit else "host"
                name, icon_key, _, _ = self._device_presentation(hop_ip, node_type)
                label = hop_ip
            else:
                node_type, name, icon_key = "unknown", "??? ??????", "topo_unknown"
                label = f"??? {node_id.rsplit(':', 1)[-1]}"
            node = scene.nodes.get(node_id)
            if node is None:
                scene.add_node(node_id, name, label, node_type, (index * 220, 0), icon_key=icon_key)
            elif node_type == "router" and node.node_type not in ("router", "local"):
                # ??????? ???????????? ????; ???????????? ? ??????? ?????????? ????????.
                node.node_type = "router"
                node.refresh_theme()
            if hop_ip:
                self._refresh_device_node(hop_ip)
            chain.append(node_id)
        return chain

    def _src_for(self, ip: str) -> str:
        """Источник берется из измерения, без повторного выбора маршрута ОС."""
        return self.route_source.get(ip, "")

    def on_route_found(
        self, network: str, representative: str, route: TraceResult, shared: bool
    ) -> None:
        """Измерения и гипотезы имеют разные типы и владельцев."""
        scene = self.view.network_scene
        if shared:
            owner = f"hypothesis:{network}"
            scene.remove_owners(
                [o for o in scene.owner_edges if o.startswith(owner + ":")]
            )
            chain = self._ensure_route_nodes(representative, route)
            self.network_routes[network] = {
                "representative": representative,
                "prefix": chain,
                "hops": route.hops,
                "src": route.source,
                "result": route,
            }
            if not chain:
                return
            for ip, net in self.host_network.items():
                if net != network or ip in self.routes:
                    continue
                self.route_source[ip] = route.source or ""
                if route.source:
                    self._ensure_local(route.source)
                full = ([route.source] if route.source else []) + chain + [ip]
                for first, second in zip(full, full[1:]):
                    scene.add_edge(
                        first, second, "hypothesis", f"hypothesis:{network}:{ip}"
                    )
        else:
            self._apply_personal_route(representative, route, network)
        self._render_status()

    def _apply_personal_route(
        self, destination: str, route: TraceResult, network: str = ""
    ) -> None:
        """Заменить владельца пути, сохранив связи остальных измерений."""
        scene = self.view.network_scene
        previous = self.routes.get(destination)
        old_nodes = {
            self._hop_id_for(destination, hop, index)
            for index, hop in enumerate(previous or [])
        }
        scene.remove_owners(
            [f"route:{destination}", f"hypothesis:{network}:{destination}"]
        )
        for hop in route:
            hop["measurement_id"] = route.measurement_id
        self.routes[destination] = route
        self.route_source[destination] = route.source or ""
        chain = self._ensure_route_nodes(destination, route)
        if route.source:
            self._ensure_local(route.source)
        self._link_chain(destination, chain, route.source or "")
        if network:
            scene.add_edge(network, destination)
        # Удалить неиспользуемые узлы предыдущего измерения, сохранив найденные хосты.
        orphaned = {
            nid for nid in old_nodes
            if nid not in self.host_network and not scene.incident.get(nid)
            and nid in scene.nodes and scene.nodes[nid].node_type != "local"
        }
        scene.remove_nodes(orphaned)
        if self.merge_unknown.isChecked():
            self._rebuild_unknown_nodes()
        affected = set(chain) | {destination, route.source}
        if any(isinstance(node, NetworkNode) and node.node_id in affected for node in scene.selectedItems()):
            self.node_selected()

    def _chain_for_host(self, ip: str) -> tuple:
        """Полная цепочка [src, ...хопы, хост] и src. Пусто = нет данных."""
        if ip in self.routes:
            route = self.routes[ip]
            chain = [self._hop_id_for(ip, hop, i) for i, hop in enumerate(route)]
        else:
            network = self.host_network.get(ip)
            info = self.network_routes.get(network) if network else None
            if not info or not info["prefix"]:
                return [], "", []
            chain = list(info["prefix"])
        src = self.route_source.get(ip)
        if not src:
            return chain + ([ip] if chain and chain[-1] != ip else []), "", chain
        full = [src] + chain
        if not chain or chain[-1] != ip:
            # Прямой сосед (цепочка пуста): путь только [оператор, хост].
            full.append(ip)
        return full, src, chain

    def _refresh_retrace_button(self) -> None:
        """Кнопка активна только на единственном выделенном хосте."""
        if self._single_trace is not None or self.worker.is_running():
            self.retrace_button.setEnabled(False)
            return
        selected = [
            n
            for n in self.view.network_scene.selectedItems()
            if isinstance(n, NetworkNode)
        ]
        enabled = len(selected) == 1 and selected[0].node_type in ("host", "router")
        self.retrace_button.setEnabled(enabled)

    def _retrace_selected_host(self) -> None:
        """Персонально перетрассировать выбранный хост (поверх общего)."""
        selected = [
            n
            for n in self.view.network_scene.selectedItems()
            if isinstance(n, NetworkNode)
        ]
        if (
            self._single_trace is not None
            or self.worker.is_running()
            or len(selected) != 1
            or selected[0].node_type not in ("host", "router")
        ):
            return
        host = selected[0].ip
        self.retrace_button.setEnabled(False)
        self._render_status(f"Трассировка {host}…")
        worker = SingleTraceWorker(
            host,
            max_hops=self.max_hops.value(),
            timeout_ms=self.trace_timeout.value(),
            retries=self.trace_retries.value(),
            parent=self,
        )
        worker.generation = self._generation
        self._single_jobs.add(worker)
        worker.finished.connect(self._on_single_trace_done)
        self._single_trace = worker
        worker.start()

    def _on_single_trace_done(self, host: str, route: TraceResult) -> None:
        """Позднее завершение не сбрасывает ссылку на новое задание."""
        worker = self.sender()
        self._single_jobs.discard(worker)
        current = worker is self._single_trace
        if current:
            self._single_trace = None
        valid = (
            current
            and worker.generation == self._generation
            and not self._closed
            and host in self.view.network_scene.nodes
        )
        worker.deleteLater()
        if valid:
            self._apply_personal_route(host, route, self.host_network.get(host, ""))
            self.node_selected()
            self._render_status(f"{host}: {route.method}, {route.reason}")
        self._refresh_retrace_button()

    def on_error(self, message: str) -> None:
        self._scan_error = message
        self._render_status(f"Ошибка: {message}")

    def scan_finished(self) -> None:
        self._clock.stop()
        self._phase = "idle"
        self.scan_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._set_inputs_enabled(True)
        self.progress.setVisible(False)
        if self._rescan_network is None:
            self.layout_graph()
            self._fit_view()
        else:
            scene = self.view.network_scene
            stale = {
                ip
                for ip, net in self.host_network.items()
                if net == self._rescan_network
                and ip in self.worker.checked_hosts
                and ip not in self.worker.found_hosts
            }
            if not self.worker._stop_event.is_set() and not self._scan_error:
                self._remove_data_nodes(stale)
            self._rescan_network = None
        scene = self.view.network_scene
        counts = {"network": 0, "router": 0, "host": 0, "unknown": 0}
        for node in scene.nodes.values():
            if node.node_type in counts:
                counts[node.node_type] += 1
        elapsed = _format_elapsed(time.monotonic() - self._started_at)
        status = (
            f"Готово за {elapsed} | "
            f"Сети: {counts['network']} | "
            f"Маршрутизаторы: {counts['router']} | "
            f"Хосты: {counts['host']}"
        )
        if counts["unknown"]:
            status += f" | Без ответа: {counts['unknown']}"
        failures = sum(
            r.reason
            in ("missing_utility", "launch_error", "utility_error", "parse_error")
            for r in self.routes.values()
        )
        if failures:
            status += f" | Ошибки измерения маршрутов: {failures} (см. детали узла)"
        plan = self.worker.plan
        status += f" | Проверено {plan.checked} из {plan.available} адресов"
        if self.worker._stop_event.is_set():
            status = "Отменено | " + status
        if self._scan_error:
            status = f"Ошибка: {self._scan_error} | " + status
        self._render_status(status)

    # ------------------------------------------------------------------
    # Граф
    # ------------------------------------------------------------------

    def layout_graph(self) -> None:
        # Два стиля: круги (один круг на сеть, по умолчанию) и кольца
        # (6 углов без востока/запада). Разброс - множитель расстояний.
        # Компактно: узлы 160x54, дуга круга ~150px на узел, кольца/ячейки
        # сжаты по сравнению с прежними 230px-дугами и шагами 240x140.
        SLOT_ANGLES = [45, 90, 135, 225, 270, 315]
        RING_BASE = 190.0
        RING_STEP = 80.0
        scene = self.view.network_scene
        networks = [n for n in scene.nodes.values() if n.node_type == "network"]
        routers = [
            n for n in scene.nodes.values() if n.node_type in ("router", "unknown")
        ]
        locals_ = [n for n in scene.nodes.values() if n.node_type == "local"]
        hosts = [n for n in scene.nodes.values() if n.node_type == "host"]
        hosts_by_network: dict = defaultdict(list)
        orphans: list = []
        for host in hosts:
            network = self.host_network.get(host.ip)
            if network:
                hosts_by_network[network].append(host)
            else:
                orphans.append(host)
        mode = str(self.layout_combo.currentData() or "circles")
        spread = max(40, min(150, self.spread_slider.value())) / 100.0

        def circle_radius(count: int) -> float:
            """Радиус круга для count хостов (дуга 150px на узел)."""
            if count <= 0:
                return 0.0
            return max(160.0, count * 150.0 / (2 * math.pi)) * spread

        def rings_outer(count: int) -> float:
            """Внешний радиус колец для count хостов."""
            if count <= 0:
                return 0.0
            rings = (count + len(SLOT_ANGLES) - 1) // len(SLOT_ANGLES)
            return (RING_BASE + (rings - 1) * RING_STEP) * spread

        radius_of = circle_radius if mode == "circles" else rings_outer
        outer_max = max([radius_of(len(v)) for v in hosts_by_network.values()] + [0.0])
        # Пакетное позиционирование: без промежуточных update_edges на setPos.
        scene.set_edge_updates_suspended(True)
        try:
            # Этот компьютер - строкой выше сетей, по центру.
            local_y = -(outer_max + 140) if networks else -110
            for index, node in enumerate(locals_):
                node.setPos(index * 190 - (len(locals_) - 1) * 95, local_y)
            cell_w = 2 * outer_max + 210
            cell_h = 2 * outer_max + 150
            for index, node in enumerate(networks):
                node.setPos((index % 3) * cell_w, (index // 3) * cell_h)
            net_rows = max(1, (len(networks) + 2) // 3)
            for network_name, host_nodes in hosts_by_network.items():
                network_node = scene.nodes.get(network_name)
                if not network_node:
                    continue
                center = network_node.scenePos()
                count = len(host_nodes)
                if mode == "circles":
                    radius = circle_radius(count)
                    for index, host in enumerate(host_nodes):
                        angle = 2 * math.pi * index / max(1, count)
                        host.setPos(
                            center.x() + math.cos(angle) * radius,
                            center.y() + math.sin(angle) * radius,
                        )
                else:
                    for pos, host in enumerate(host_nodes):
                        ring, slot = divmod(pos, len(SLOT_ANGLES))
                        radius = (RING_BASE + ring * RING_STEP) * spread
                        angle = math.radians(SLOT_ANGLES[slot])
                        host.setPos(
                            center.x() + math.cos(angle) * radius,
                            center.y() + math.sin(angle) * radius,
                        )
            # Роутеры - отдельным блоком ниже сетей, шаг 190x84.
            router_y0 = net_rows * cell_h + 90 if networks else 90
            for index, node in enumerate(routers):
                node.setPos((index % 5) * 190 + 50, (index // 5) * 84 + router_y0)
            router_rows = max(1, (len(routers) + 4) // 5)
            orphan_y0 = (
                router_y0 + (router_rows - 1) * 84 + 130
                if routers
                else (router_y0 + 60 if networks else 90)
            )
            for index, host in enumerate(orphans):
                host.setPos((index % 8) * 175, (index // 8) * 82 + orphan_y0)
        finally:
            scene.set_edge_updates_suspended(False)
        if self.merge_unknown.isChecked():
            for node in scene.nodes.values():
                if node.node_type == "unknown" and hasattr(node, "_ungrouped_position"):
                    del node._ungrouped_position
            self._rebuild_unknown_nodes()
        try:
            scene.setSceneRect(scene.itemsBoundingRect().adjusted(-300, -300, 300, 300))
        except Exception:
            pass

    def reset_map(self) -> None:
        """Разложить карту: стандартная раскладка + вписать в окно.

        Ручные перемещения узлов теряются, данные скана (маршруты,
        переименования) сохраняются. Работает и во время скана -
        раскладка просто пересчитается по текущим данным.
        """
        self.layout_graph()
        self._fit_view()
        self._render_status("Карта разложена по стандартной раскладке.")

    def _fit_view(self) -> None:
        try:
            self.view.fit_view()
        except Exception as e:
            logger.debug(f"TopologyMapDialog: fit_view failed: {e}")

    def _find_node(self) -> None:
        query = self.search.text().strip().lower()
        if not query:
            return
        scene = self.view.network_scene
        for node_id, node in scene.nodes.items():
            if query in node_id.lower() or query in node.name.lower():
                scene.clearSelection()
                node.setSelected(True)
                try:
                    self.view.centerOn(node)
                except Exception:
                    pass
                return
        self._render_status(f"Не найдено: {query}")

    def _route_prefix_to(self, router_ip: str) -> list:
        """Путь [src, ...узлы до роутера] из первого маршрута, где он есть."""
        for destination, route in self.routes.items():
            chain = []
            found = False
            for index, hop in enumerate(route or []):
                node_id = self._hop_id_for(destination, hop, index)
                chain.append(node_id)
                if hop.get("ip", "") == router_ip:
                    found = True
                    break
            if found:
                src = self.route_source.get(destination)
                if not src:
                    continue
                return [src] + chain
        for _network, info in self.network_routes.items():
            if info.get("representative") == router_ip:
                # Сам представитель (часто шлюз): его путь - префикс + он.
                src = info.get("src")
                if src:
                    return [src] + list(info.get("prefix", [])) + [router_ip]
        for network, info in self.network_routes.items():
            chain = info.get("prefix", [])
            if router_ip in chain:
                src = info.get("src")
                if not src:
                    continue
                return [src] + chain[: chain.index(router_ip) + 1]
        return []

    def highlight_path_to(self, node: NetworkNode) -> int:
        """Подсветить путь от хоста оператора до выделенного узла.

        Хост: общий маршрут подсети (или персональный при откате).
        Роутер: префикс первого маршрута, где встречается. Без данных -
        хост и его сеть, сеть - примыкающие ребра. Возвращает число ребер.
        """
        scene = self.view.network_scene
        scene.clear_highlights()
        node_ids: list = [node.node_id]
        edge_pairs: list = []

        def add_incident() -> None:
            for edge in scene.edges:
                if edge.source is node or edge.target is node:
                    edge_pairs.append((edge.source.node_id, edge.target.node_id))
                    node_ids.append(edge.source.node_id)
                    node_ids.append(edge.target.node_id)

        if node.node_type == "host":
            full, _src, chain = self._chain_for_host(node.ip)
            if full:
                node_ids.extend(full)
                edge_pairs.extend(zip(full, full[1:]))
            else:
                node_ids.extend(chain)
                edge_pairs.extend(zip(chain, chain[1:]))
                network = self.host_network.get(node.ip)
                if network:
                    node_ids.append(network)
                    edge_pairs.append((network, node.node_id))
        elif node.node_type == "network":
            # Путь оператора до подсети - через ее представителя
            # (или персональный маршрут), плюс сама сеть и дети.
            rep = None
            info = self.network_routes.get(node.node_id)
            if info:
                rep = info.get("representative")
            if rep is None:
                for ip, net in self.host_network.items():
                    if net == node.node_id and ip in self.routes:
                        rep = ip
                        break
            if rep is not None:
                full, _src, _chain = self._chain_for_host(rep)
                if full:
                    node_ids.extend(full)
                    edge_pairs.extend(zip(full, full[1:]))
            node_ids.append(node.node_id)
            for edge in scene.edges:
                if edge.source is node or edge.target is node:
                    edge_pairs.append((edge.source.node_id, edge.target.node_id))
                    node_ids.append(edge.source.node_id)
                    node_ids.append(edge.target.node_id)
        elif node.node_type == "router":
            prefix = self._route_prefix_to(node.ip)
            if prefix:
                node_ids.extend(prefix)
                edge_pairs.extend(zip(prefix, prefix[1:]))
            else:
                add_incident()
        else:
            add_incident()
        return scene.highlight_path(node_ids, edge_pairs)

    def _hop_line(self, hop: dict, index: int) -> str:
        """Строка хопа для панели деталей."""
        hop_ip = hop.get("ip", "")
        hostname = hop.get("hostname", "")
        if not hop_ip:
            hop_no = hop.get("hop", "") or index
            return f"{index:2}. * (без ответа, TTL {hop_no})"
        if hostname:
            return f"{index:2}. {hostname} ({hop_ip})"
        return f"{index:2}. {hop_ip}"

    def node_selected(self) -> None:
        scene = self.view.network_scene
        if not isValid(scene):
            return
        selected = scene.selectedItems()
        if not selected:
            self.info.clear()
            scene.clear_highlights()
            self._refresh_retrace_button()
            return
        selected_nodes = [n for n in selected if isinstance(n, NetworkNode)]
        if len(selected_nodes) > 1:
            # Мультивыделение: сводка вместо деталей одного узла,
            # подсветка пути не имеет смысла - снята.
            scene.clear_highlights()
            counts: dict = {}
            for node in selected_nodes:
                counts[node.node_type] = counts.get(node.node_type, 0) + 1
            type_names = {
                "network": "сетей",
                "router": "маршрутизаторов",
                "host": "хостов",
                "local": "локальных",
                "unknown": "без ответа",
            }
            lines = [f"Выделено узлов: {len(selected_nodes)}"]
            for key, count in counts.items():
                lines.append(f"  {type_names.get(key, key)}: {count}")
            lines.append("")
            lines.append("Удалить выделенное - правый клик или клавиша Delete.")
            self.info.setPlainText("\n".join(lines))
            self._refresh_retrace_button()
            return
        node = selected_nodes[0] if selected_nodes else selected[0]
        if not isinstance(node, NetworkNode):
            return
        lit = self.highlight_path_to(node)
        type_names = {
            "network": "Сеть",
            "router": "Маршрутизатор",
            "host": "Хост",
            "local": "Этот компьютер",
            "unknown": "Без ответа",
        }
        lines = [
            f"Роль на карте: {type_names.get(node.node_type, node.node_type)}",
            f"Имя: {node.name}",
            f"IP: {node.ip}",
        ]
        if node.node_type in ("host", "router", "local"):
            facts = self.device_facts.get(node.ip, DeviceFacts(hostname=self.hostnames.get(node.ip, "")))
            device_type, reason = facts.classify()
            lines.append(f"Тип оборудования: {device_type}")
            if reason:
                lines.append(f"Основание: {reason}")
            if facts.mac_address:
                lines.append(f"MAC: {facts.mac_address}")
            if facts.ttl is not None:
                lines.append(f"TTL ответа: {facts.ttl}")
            if facts.open_ports:
                lines.append("Открытые TCP-порты: " + ", ".join(map(str, facts.open_ports)))
        if lit:
            lines.append(f"Путь подсвечен: ребер {lit}")
        if node.node_type == "host":
            network = self.host_network.get(node.ip)
            if network:
                lines.append(f"Сеть: {network}")
            if node.ip in self.routes:
                route = self.routes[node.ip]
                lines.append("")
                lines.append("Маршрут (персональный):")
                src = self.route_source.get(node.ip)
                if src:
                    lines.append(f" 0. Этот компьютер ({src})")
                for index, hop in enumerate(route, start=1):
                    lines.append(self._hop_line(hop, index))
                chain = [
                    self._hop_id_for(node.ip, hop, index)
                    for index, hop in enumerate(route)
                ]
                if chain and chain[-1] != node.ip:
                    lines.append(
                        f"Дальше {_hop_display(chain[-1])} маршрут неизвестен,"
                    )
                    lines.append("адрес обнаружен отдельной пробой.")
            else:
                info = self.network_routes.get(network) if network else None
                if info:
                    lines.append("")
                    lines.append(
                        f"Гипотеза общего префикса, представитель {info['representative']}):"
                    )
                    src = self.route_source.get(node.ip)
                    if src:
                        lines.append(f" 0. Этот компьютер ({src})")
                    hops = info.get("hops", [])
                    for index, hop in enumerate(hops, start=1):
                        lines.append(self._hop_line(hop, index))
                    prefix = info.get("prefix", [])
                    if (
                        prefix
                        and prefix[-1] != node.ip
                        and node.ip != info.get("representative")
                    ):
                        # Общий путь до хоста не дотянулся: хост не печатаем
                        # номерным хопом, честно пишем где конец маршрута.
                        lines.append(
                            "Общий маршрут заканчивается на "
                            f"{_hop_display(prefix[-1])},"
                        )
                        lines.append(
                            "дальше путь неизвестен, адрес обнаружен отдельной пробой."
                        )
                    else:
                        lines.append(f"{len(hops) + 1:2}. {node.ip}")
        elif node.node_type == "router":
            prefix = self._route_prefix_to(node.ip)
            if prefix:
                lines.append("")
                lines.append("Маршрут:")
                lines.append(f" 0. Этот компьютер ({prefix[0]})")
                for index, hop_id in enumerate(prefix[1:], start=1):
                    lines.append(f"{index:2}. {_hop_display(hop_id)}")
        result = self.routes.get(node.ip)
        if node.node_type == "network":
            summary = self.network_routes.get(node.node_id)
            lines.append(
                "CIDR — группа поиска; членство не подтверждает сетевое соединение."
            )
            if summary:
                result_summary = summary["result"]
                lines.append(
                    f"Представителей: {len(result_summary.attempts)}; "
                    + (
                        "общий префикс — гипотеза"
                        if summary["prefix"]
                        else "недостаточная выборка / частичные или разные пути"
                    )
                )
                for sample in result_summary.attempts:
                    lines.append(
                        f"{sample.destination}: {sample.method}, источник {sample.source or 'не установлен'}, {sample.reason}"
                    )
        if node.node_type in ("host", "router") and result is None:
            lines.append(
                "Персональный путь не измерен. Доступна точная проверка маршрута."
            )
        if result is not None:
            lines.extend(
                [
                    "",
                    f"Метод: {result.method}",
                    f"Источник: {result.source or 'не установлен'}",
                    f"Завершение: {result.reason}; цель достигнута: {result.reached}",
                    f"Измерение: {result.measurement_id}",
                ]
            )
            if result.diagnostic:
                lines.append(f"Диагностика утилиты: {result.diagnostic}")
            for number, attempt in enumerate(result.attempts or (result,), 1):
                lines.append(
                    f"Попытка {number}: {attempt.reason}, {attempt.method}, время {attempt.started_at:.3f}"
                )
                lines.extend(
                    self._hop_line(h, i) + (" " + h.get("terminal", ""))
                    for i, h in enumerate(attempt, 1)
                )
        for probe in self.probes.get(node.ip, ()):
            lines.append(f"Проба {probe.protocol}/{probe.port}: {probe.state}")
        self.info.setPlainText("\n".join(lines))
        self._refresh_retrace_button()
