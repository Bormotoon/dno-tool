"""Диалог мониторинга пинга нескольких хостов с редактором списка и графиком."""

import ipaddress
import math
import random
import re
import sys
import time
from collections import deque
from datetime import datetime

import pyqtgraph as pg
from PySide6.QtCore import QEvent, Qt, QTimer, Slot
from PySide6.QtGui import QBrush, QCloseEvent, QColor, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from src.logger import logger
from src.workers.network.pinger import PingThread

# Роли данных строки дерева: цвет кривой и текущая задержка
HOST_ROLE = Qt.ItemDataRole.UserRole + 1
DELAY_ROLE = Qt.ItemDataRole.UserRole + 2
LOSS_ROLE = Qt.ItemDataRole.UserRole + 3


def format_elapsed(seconds: float) -> str:
    """Форматировать длительность для оси X: секунды, мм:сс, чч:мм:сс."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}с"
    minutes, secs = divmod(total, 60)
    if total < 3600:
        return f"{minutes:02d}:{secs:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class TimeAxisItem(pg.AxisItem):
    """Ось X с человекочитаемыми метками времени."""

    def tickStrings(self, values, scale, spacing):
        return [format_elapsed(v) for v in values]


class PingMonitorDialog(QDialog):
    """Диалог мониторинга пинга нескольких хостов"""

    def __init__(self, parent=None, target: str = "8.8.8.8"):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Ping-мониторинг")
        self.resize(980, 620)

        # Состояние
        self._is_running = False
        self._is_paused = False
        self._pause_started: float | None = None
        self._session_start: float | None = None
        # Флаг программной мутации дерева (не триггерить _on_item_changed)
        self._tree_mutation = False

        # Per-host состояние (ключи - имена хостов)
        self._threads: dict[str, PingThread] = {}
        self._retiring: set[PingThread] = set()
        self._pending_done: int | None = None
        self._sample_count: dict[str, int] = {}
        self._dirty_hosts: set[str] = set()
        self._elapsed = 0.0
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._colors: dict[str, QColor] = {}
        self._data_times: dict[str, deque[float]] = {}
        self._data_delays: dict[str, deque[float]] = {}
        self._loss_count: dict[str, int] = {}
        # Вид графика: ширина следящего окна хвоста в секундах.
        # История ограничена 10 000 отсчетами на хост, статистика — за всю сессию.
        self._view_window = 300.0
        # Кэш максимума задержки для O(1) автомасштаба Y на долгой сессии
        self._y_max = 0.0

        self._init_ui()
        self._load_hosts(target)
        self._plot_timer = QTimer(self)
        self._plot_timer.setInterval(200)
        self._plot_timer.timeout.connect(self._refresh_plot)
        self._plot_timer.start()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _init_ui(self):
        """Инициализация интерфейса: слева редактор хостов, справа график."""
        layout = QVBoxLayout()

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # === Левая панель: редактор хостов ===
        hosts_panel = QFrame()
        hosts_layout = QVBoxLayout(hosts_panel)
        hosts_layout.setContentsMargins(0, 0, 0, 0)

        # Дерево хостов: чекбокс, цвет, задержка, потери
        self.hosts_tree = QTreeWidget()
        self.hosts_tree.setColumnCount(3)
        self.hosts_tree.setHeaderLabels(["Хост", "Задержка", "Потери"])
        self.hosts_tree.setRootIsDecorated(False)
        self.hosts_tree.setUniformRowHeights(True)
        self.hosts_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.hosts_tree.itemChanged.connect(self._on_item_changed)
        # Контекстное меню: Поменять цвет / Отметить все / Снять отметки / Удалить
        self.hosts_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.hosts_tree.customContextMenuRequested.connect(self._show_context_menu)
        header = self.hosts_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, header.ResizeMode.Stretch)
        header.setSectionResizeMode(1, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, header.ResizeMode.ResizeToContents)

        # Ряд добавления хоста
        add_row = QHBoxLayout()
        self.new_host_input = QLineEdit()
        self.new_host_input.setPlaceholderText("IP или имя хоста")
        self.new_host_input.returnPressed.connect(self._add_host)
        self.add_host_button = QPushButton("Добавить")
        self.add_host_button.setAutoDefault(False)
        self.add_host_button.clicked.connect(self._add_host)
        add_row.addWidget(self.new_host_input, 1)
        add_row.addWidget(self.add_host_button)

        hosts_layout.addWidget(self.hosts_tree, 1)
        hosts_layout.addLayout(add_row)

        # === Правая панель: график + управление ===
        plot_panel = QFrame()
        plot_layout = QVBoxLayout(plot_panel)
        plot_layout.setContentsMargins(0, 0, 0, 0)

        # Настройка графика
        self.plot_widget = pg.PlotWidget(
            axisItems={"bottom": TimeAxisItem(orientation="bottom")}
        )
        self.plot_widget.setLabel("left", "Задержка (мс)")
        self.plot_widget.setLabel("bottom", "Время")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self.plot_widget.setYRange(0, 200)
        self.plot_widget.addLegend()
        self._apply_plot_theme()

        plot_layout.addWidget(self.plot_widget, 1)

        # Кнопки Старт/Стоп и Пауза/Продолжить внизу окна
        # (под графиком и панелью хостов)
        self.pause_button = QPushButton("Пауза")
        self.pause_button.setAutoDefault(False)
        self.pause_button.setToolTip("Заморозить опрос, сохранив данные и графики")
        self.pause_button.setCheckable(True)
        self.pause_button.setChecked(False)
        self.pause_button.setEnabled(False)
        self.pause_button.toggled.connect(self._on_toggle_paused)
        self.toggle_button = QPushButton("Старт")
        self.toggle_button.setAutoDefault(False)
        self.toggle_button.setToolTip("Начать опрос отмеченных хостов / остановить все")
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(False)
        self.toggle_button.toggled.connect(self._on_toggle_running)
        bottom_row = QHBoxLayout()
        self.status_label = QLabel("Отметьте хосты и нажмите «Старт»")
        self.status_label.setWordWrap(True)
        self.follow_checkbox = QCheckBox("Следить за графиком")
        self.follow_checkbox.setChecked(True)
        self.follow_checkbox.setToolTip(
            "Окно последних 5 минут; снимите отметку для просмотра истории"
        )
        clear_button = QPushButton("Очистить")
        clear_button.setAutoDefault(False)
        clear_button.clicked.connect(self._clear_results)
        self.plot_widget.setToolTip(
            "До 10 000 отсчетов на хост. Разрыв линии означает отсутствие ответа."
        )
        bottom_row.addWidget(self.status_label, 1)
        bottom_row.addWidget(self.follow_checkbox)
        bottom_row.addWidget(clear_button)
        bottom_row.addWidget(self.pause_button)
        bottom_row.addWidget(self.toggle_button)

        splitter.addWidget(hosts_panel)
        splitter.addWidget(plot_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 700])

        layout.addWidget(splitter, 1)
        layout.addLayout(bottom_row)
        self.setLayout(layout)

        # Delete удаляет выбранные хосты из списка
        self._del_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.hosts_tree)
        self._del_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._del_shortcut.activated.connect(self._remove_selected_hosts)

    def _apply_plot_theme(self) -> None:
        """Применить палитру темы к фону, осям, сетке и легенде диаграммы."""
        palette = self.palette()
        text = palette.color(QPalette.ColorRole.Text)
        self.plot_widget.setBackground(palette.color(QPalette.ColorRole.Base))
        for name in ("left", "bottom", "right", "top"):
            axis = self.plot_widget.getAxis(name)
            axis.setPen(pg.mkPen(text))
            axis.setTextPen(pg.mkPen(text))
            axis.setLabel(color=text.name())
        legend = self.plot_widget.plotItem.legend
        if legend is not None:
            legend.setLabelTextColor(text)
            # setLabelTextColor меняет настройки, но не HTML уже созданных подписей.
            for _, label in legend.items:
                label.setText(label.text, color=text)
            legend.setBrush(palette.color(QPalette.ColorRole.Window))
            legend.setPen(palette.color(QPalette.ColorRole.Mid))

    def changeEvent(self, event: QEvent) -> None:
        """Обновить открытую диаграмму при смене темы приложения."""
        super().changeEvent(event)
        if hasattr(self, "plot_widget") and event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.StyleChange,
        ):
            self._apply_plot_theme()

    def _on_toggle_running(self, checked: bool):
        """Единая кнопка Старт/Стоп: checked = опрос идет."""
        if checked:
            self._start_ping()
            # Старт мог не состояться (нет отмеченных хостов) - вернуть кнопку
            if not self._is_running:
                self.toggle_button.blockSignals(True)
                try:
                    self.toggle_button.setChecked(False)
                finally:
                    self.toggle_button.blockSignals(False)
        else:
            self._stop_ping()

    def _on_toggle_paused(self, checked: bool):
        """Кнопка Пауза/Продолжить: checked = опрос заморожен."""
        if checked:
            self._pause_ping()
            if not self._is_paused:
                self.pause_button.blockSignals(True)
                try:
                    self.pause_button.setChecked(False)
                finally:
                    self.pause_button.blockSignals(False)
        else:
            self._resume_ping()
            if self._is_paused:
                self.pause_button.blockSignals(True)
                try:
                    self.pause_button.setChecked(True)
                finally:
                    self.pause_button.blockSignals(False)

    def _pause_ping(self):
        """Заморозить опрос: остановить потоки, сохранить данные и кривые."""
        if not self._is_running or self._is_paused:
            return
        logger.info("PingMonitorDialog: Pausing ping")
        self._is_paused = True
        self._pause_started = time.monotonic()
        for host in list(self._threads):
            self._stop_host(host, clear_view=False)
        self.pause_button.setText("Продолжить")
        self._update_status("Пауза")

    def _resume_ping(self):
        """Продолжить опрос после паузы: дописать в ту же сессию."""
        if not self._is_running or not self._is_paused:
            return
        logger.info("PingMonitorDialog: Resuming ping")
        # Сдвиг старта сессии на длительность паузы - без дыры на оси X
        if self._session_start is not None and self._pause_started is not None:
            self._session_start += time.monotonic() - self._pause_started
        self._is_paused = False
        self._pause_started = None
        self.pause_button.setText("Пауза")
        hosts = [
            self.hosts_tree.topLevelItem(i).data(0, HOST_ROLE)
            for i in range(self.hosts_tree.topLevelItemCount())
            if self.hosts_tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked
        ]
        for host in hosts:
            self._start_host(host, reset_data=False)
        if not hosts:
            self._update_status("Отметьте хотя бы один хост")
        else:
            self._update_status()

    # ------------------------------------------------------------------
    # Список хостов: загрузка/сохранение/мутации
    # ------------------------------------------------------------------

    def _load_hosts(self, target: str):
        """Заполнить список из конфига (+ target при отсутствии)."""
        checked_saved = None
        try:
            from src.config import config

            saved = getattr(config.app.network, "pinger_hosts", None)
            hosts = (
                [str(h).strip() for h in saved if str(h or "").strip()]
                if isinstance(saved, list)
                else []
            )
            checked_saved = getattr(config.app.network, "pinger_checked", None)
            checked = (
                {str(h).strip() for h in checked_saved if str(h or "").strip()}
                if isinstance(checked_saved, list)
                else set()
            )
            colors_saved = getattr(config.app.network, "pinger_colors", None)
            colors = (
                {
                    str(h).strip(): str(c).strip()
                    for h, c in dict(colors_saved).items()
                    if str(h or "").strip() and str(c or "").strip()
                }
                if isinstance(colors_saved, dict)
                else {}
            )
        except Exception as e:  # noqa: BLE001 — поврежденный конфиг не должен мешать открытию
            logger.debug(f"PingMonitorDialog: load hosts failed: {e}")
            hosts, checked, colors = [], set(), {}
        self._saved_colors: dict[str, str] = dict(colors)
        if target and target.strip() and target.strip() not in hosts:
            hosts.append(target.strip())
        if not hosts:
            hosts = ["8.8.8.8"]
        for h in hosts:
            self._append_host_item(
                h, checked=(h in checked or (checked_saved is None and h == target))
            )
        # Восстановить сохраненные цвета строк
        if self._saved_colors:
            self.hosts_tree.blockSignals(True)
            try:
                for h, hexcolor in self._saved_colors.items():
                    it = self._find_host_item(h)
                    if it is not None:
                        it.setForeground(0, QBrush(QColor(hexcolor)))
            finally:
                self.hosts_tree.blockSignals(False)

    def _append_host_item(self, host: str, checked: bool):
        """Добавить строку хоста в редактор (без дубликатов)."""
        if self._find_host_item(host) is not None:
            return
        item = QTreeWidgetItem([host, "", ""])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )
        item.setData(0, HOST_ROLE, host)
        item.setTextAlignment(
            1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        item.setTextAlignment(
            2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.hosts_tree.addTopLevelItem(item)

    def _find_host_item(self, host: str) -> QTreeWidgetItem | None:
        """Найти строку по имени хоста."""
        for i in range(self.hosts_tree.topLevelItemCount()):
            it = self.hosts_tree.topLevelItem(i)
            if it.data(0, HOST_ROLE) == host:
                return it
        return None

    def _persist_hosts(self):
        """Сохранить список и отметки хостов в settings.json."""
        try:
            from src.config import config

            hosts = [
                self.hosts_tree.topLevelItem(i).data(0, HOST_ROLE)
                for i in range(self.hosts_tree.topLevelItemCount())
            ]
            checked = []
            for i in range(self.hosts_tree.topLevelItemCount()):
                it = self.hosts_tree.topLevelItem(i)
                if it.checkState(0) == Qt.CheckState.Checked:
                    checked.append(it.data(0, HOST_ROLE))
            config.app.network.pinger_hosts = hosts
            config.app.network.pinger_checked = checked
            config.app.network.pinger_colors = dict(self._saved_colors)
            config.save()
        except Exception as e:  # noqa: BLE001 — ошибка сохранения не должна прерывать опрос
            logger.debug(f"PingMonitorDialog: persist hosts failed: {e}")

    def _add_host(self):
        """Добавить хост из поля ввода (горячо, если опрос идет)."""
        text = self.new_host_input.text().strip()
        if not text:
            return
        try:
            ipaddress.ip_address(text)
            valid = True
        except ValueError:
            valid = len(text) <= 253 and all(
                re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", part)
                for part in text.rstrip(".").split(".")
            )
        if not valid:
            self._update_status(f"Некорректный адрес: {text}")
            return
        existing = self._find_host_item(text)
        if existing is not None:
            existing.setCheckState(0, Qt.CheckState.Checked)
            self.new_host_input.clear()
            return
        # Новый хост: отмечен и (если опрос идет) сразу опрашивается
        self._append_host_item(text, checked=True)
        self.new_host_input.clear()
        item = self._find_host_item(text)
        self._apply_item_color(item)
        self._persist_hosts()
        logger.info(f"PingMonitorDialog: Added host {text}")
        if self._is_running and not self._is_paused:
            self._start_host(text, reset_data=True)
        elif self._is_paused:
            self._update_status("Пауза")
        else:
            self._update_status()

    def _remove_selected_hosts(self):
        """Удалить только явно выделенные строки."""
        items = list(self.hosts_tree.selectedItems())
        for item in items:
            host = item.data(0, HOST_ROLE)
            self._stop_host(host)
            idx = self.hosts_tree.indexOfTopLevelItem(item)
            self.hosts_tree.takeTopLevelItem(idx)
            # Цвет удаленного хоста больше не нужен
            self._saved_colors.pop(host, None)
        if items:
            self._persist_hosts()
            self._update_status()

    # ------------------------------------------------------------------
    # Контекстное меню списка хостов
    # ------------------------------------------------------------------

    def _show_context_menu(self, pos):
        """Меню по правому клику на списке хостов."""
        menu = QMenu(self.hosts_tree)
        item = self.hosts_tree.itemAt(pos)
        selected = list(self.hosts_tree.selectedItems())
        if item is not None and item not in selected:
            # Кликнули по невыделенной строке - считаем ее целью
            self.hosts_tree.clearSelection()
            item.setSelected(True)
            selected = [item]

        # Поменять цвет: для выбранных (или строки под курсором)
        if selected:
            color_targets = selected
            act_color = menu.addAction("Поменять цвет")
            act_color.triggered.connect(lambda: self._change_color(color_targets))
        menu.addSeparator()

        act_check_all = menu.addAction("Отметить все")
        act_check_all.triggered.connect(self._check_all_hosts)
        act_uncheck_all = menu.addAction("Снять отметки")
        act_uncheck_all.triggered.connect(self._uncheck_all_hosts)
        menu.addSeparator()

        act_remove = menu.addAction("Удалить")
        act_remove.setEnabled(bool(selected))
        act_remove.triggered.connect(self._remove_selected_hosts)

        act_clear = menu.addAction("Очистить графики")
        act_clear.setToolTip("Остановить опрос и сбросить графики")
        act_clear.triggered.connect(self._clear_results)

        menu.exec(self.hosts_tree.viewport().mapToGlobal(pos))

    def _change_color(self, items: list):
        """Диалог выбора цвета для выбранных хостов, сохранение и перерисовка."""
        if not items:
            return
        # Стартовый цвет - от первого целевого хоста
        first_host = items[0].data(0, HOST_ROLE)
        initial = self._saved_colors.get(first_host) or self._colors.get(first_host)
        color = QColorDialog.getColor(
            QColor(initial)
            if initial
            else QColor.fromHsv(random.randint(0, 359), 220, 255),
            self,
            "Цвет кривой",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if not color.isValid():
            return
        hexcolor = color.name()
        self.hosts_tree.blockSignals(True)
        try:
            for it in items:
                host = it.data(0, HOST_ROLE)
                self._saved_colors[host] = hexcolor
                self._colors[host] = QColor(color)
                # Обновить кривую на лету, если хост опрашивается
                curve = self._curves.get(host)
                if curve is not None:
                    curve.setPen(pg.mkPen(color, width=2))
                    curve.setSymbolBrush(pg.mkBrush(color))
                # Строка: окрашиваем все колонки, даже без активного опроса
                it.setForeground(0, QBrush(color))
                it.setForeground(1, QBrush(color))
                it.setForeground(2, QBrush(color))
        finally:
            self.hosts_tree.blockSignals(False)
        self._persist_hosts()
        logger.info(
            f"PingMonitorDialog: color for {[it.data(0, HOST_ROLE) for it in items]} -> {hexcolor}"
        )

    def _check_all_hosts(self):
        """Отметить все хосты (горячо запускает новых)."""
        for i in range(self.hosts_tree.topLevelItemCount()):
            it = self.hosts_tree.topLevelItem(i)
            if it.checkState(0) != Qt.CheckState.Checked:
                it.setCheckState(0, Qt.CheckState.Checked)

    def _uncheck_all_hosts(self):
        """Снять отметки (горячо останавливает опрос выбранных)."""
        for i in range(self.hosts_tree.topLevelItemCount()):
            it = self.hosts_tree.topLevelItem(i)
            if it.checkState(0) != Qt.CheckState.Unchecked:
                it.setCheckState(0, Qt.CheckState.Unchecked)
        self._update_status()

    def _clear_results(self):
        """Очистить данные графиков (и остановить опрос, если идет)."""
        if self._is_running:
            self._stop_ping()
        self._data_times.clear()
        self._data_delays.clear()
        self._loss_count.clear()
        self._y_max = 0.0
        self.hosts_tree.blockSignals(True)
        try:
            for i in range(self.hosts_tree.topLevelItemCount()):
                it = self.hosts_tree.topLevelItem(i)
                it.setText(1, "")
                it.setText(2, "")
                it.setData(1, DELAY_ROLE, None)
                it.setData(2, LOSS_ROLE, None)
                it.setToolTip(1, "")
        finally:
            self.hosts_tree.blockSignals(False)
        self.plot_widget.clearPlots()
        self._curves.clear()
        self._dirty_hosts.clear()
        self._sample_count.clear()
        self._elapsed = 0.0
        if self.plot_widget.plotItem.legend is not None:
            self.plot_widget.plotItem.legend.clear()
        self.plot_widget.setXRange(0, 60)
        self.plot_widget.setYRange(0, 200)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        """Чекбокс строки = горячее включение/исключение хоста."""
        if column != 0:
            return
        if self._tree_mutation:
            # Программное изменение (персист/цвет/очистка) - не реакция юзера
            return
        host = item.data(0, HOST_ROLE)
        checked = item.checkState(0) == Qt.CheckState.Checked
        self._persist_hosts()
        if self._is_running and not self._is_paused:
            if checked:
                self._start_host(host, reset_data=True)
            else:
                self._stop_host(host)
        elif self._is_paused:
            # На паузе потоки остановлены: только список, без запуска
            self._update_status("Пауза")
        else:
            self._update_status()

    # ------------------------------------------------------------------
    # Управление опросом
    # ------------------------------------------------------------------

    def _pick_color(self, host: str = "") -> QColor:
        """Цвет хоста: сохраненный (pinger_colors) или случайный.

        Случайный - яркий, с шагом по золотому сечению против повторов.
        """
        saved = self._saved_colors.get(host)
        if saved:
            return QColor(saved)
        base = random.randint(0, 359)
        hue = (base + 137 * len(self._colors)) % 360
        return QColor.fromHsv(hue, 220, 255)

    def _apply_item_color(self, item: QTreeWidgetItem):
        """Цветовой маркер строки = цвет кривой хоста."""
        if item is None:
            return
        self.hosts_tree.blockSignals(True)
        try:
            host = item.data(0, HOST_ROLE)
            color = self._colors.get(host)
            if color is not None:
                item.setForeground(0, QBrush(color))
                item.setForeground(1, QBrush(color))
                item.setForeground(2, QBrush(color))
        finally:
            self.hosts_tree.blockSignals(False)

    def _reset_host_data(self, host: str):
        """Сбросить накопленные точки хоста (чистый старт, не пауза)."""
        self._data_times[host] = deque(maxlen=10_000)
        self._data_delays[host] = deque(maxlen=10_000)
        self._loss_count[host] = 0
        self._sample_count[host] = 0

    def _recompute_y_max(self):
        """Пересчитать кэш Y-максимума по оставшимся данным."""
        self._y_max = 0.0
        for delays in self._data_delays.values():
            if delays:
                peak = max((v for v in delays if math.isfinite(v)), default=0.0)
                self._y_max = max(self._y_max, peak)

    def _start_host(self, host: str, reset_data: bool = True):
        """Запустить опрос одного хоста (идемпотентно).

        reset_data=True - чистый старт (сброс точек хоста).
        reset_data=False - продолжение (пауза): старые точки сохраняются.
        """
        if host in self._threads:
            return
        color = self._colors.get(host)
        if color is None:
            color = self._pick_color(host)
            self._colors[host] = color
        if reset_data or host not in self._data_times:
            self._reset_host_data(host)
        else:
            self._data_times.setdefault(host, deque(maxlen=10_000))
            self._data_delays.setdefault(host, deque(maxlen=10_000))
            self._loss_count.setdefault(host, 0)
        if host not in self._curves:
            self._curves[host] = self.plot_widget.plot(
                pen=pg.mkPen(color, width=2),
                symbol="o",
                symbolSize=5,
                symbolBrush=pg.mkBrush(color),
                name=host,
                connect="finite",
            )
        item = self._find_host_item(host)
        self._apply_item_color(item)
        thread = PingThread(host, interval=1.0, parent=self)
        thread.ping_result.connect(self._on_ping_result)
        thread.error.connect(self._on_worker_error)
        thread.finished.connect(self._on_thread_finished)
        self._threads[host] = thread
        thread.start()
        logger.info(f"PingMonitorDialog: Started ping to {host}")
        self._update_status()

    def _stop_host(self, host: str, clear_view: bool = True):
        """Остановить опрос одного хоста.

        clear_view=True - убрать кривую и цвет (снятие отметки/удаление).
        clear_view=False - только остановить поток, данные и кривую
        сохранить (пауза).
        """
        thread = self._threads.pop(host, None)
        if thread is not None:
            self._retiring.add(thread)
            thread.stop()
        if not clear_view:
            logger.info(f"PingMonitorDialog: Suspended ping to {host}")
            self._update_status("Пауза")
            return
        curve = self._curves.pop(host, None)
        if curve is not None:
            legend = self.plot_widget.plotItem.legend
            if legend is not None:
                legend.removeItem(curve)
            self.plot_widget.removeItem(curve)
        self._colors.pop(host, None)
        self._data_times.pop(host, None)
        self._data_delays.pop(host, None)
        self._loss_count.pop(host, None)
        self._sample_count.pop(host, None)
        self._dirty_hosts.discard(host)
        self._recompute_y_max()
        item = self._find_host_item(host)
        if item is not None:
            # Возврат к сохраненному цвету хоста (если задан), иначе сброс
            saved = self._saved_colors.get(host)
            self.hosts_tree.blockSignals(True)
            try:
                brush = QBrush(QColor(saved)) if saved else QBrush()
                item.setForeground(0, brush)
                item.setForeground(1, brush)
                item.setForeground(2, brush)
            finally:
                self.hosts_tree.blockSignals(False)
        logger.info(f"PingMonitorDialog: Stopped ping to {host}")
        self._update_status()

    def _start_ping(self):
        """Запуск мониторинга отмеченных хостов"""
        if self._is_running:
            logger.warning("PingMonitorDialog: Already running")
            return
        hosts = [
            self.hosts_tree.topLevelItem(i).data(0, HOST_ROLE)
            for i in range(self.hosts_tree.topLevelItemCount())
            if self.hosts_tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked
        ]
        if not hosts:
            self._update_status("Отметьте хотя бы один хост")
            logger.warning("PingMonitorDialog: No hosts selected")
            return

        logger.info(f"PingMonitorDialog: Starting ping to {hosts}")

        self._clear_results()
        self._is_running = True
        self._is_paused = False
        self._pause_started = None
        self._session_start = time.monotonic()
        self._y_max = 0.0

        # UI обновления: кнопка уже в checked-состоянии (_on_toggle_running)
        self.toggle_button.setText("Стоп")
        self.pause_button.setEnabled(True)
        self.pause_button.blockSignals(True)
        try:
            self.pause_button.setChecked(False)
        finally:
            self.pause_button.blockSignals(False)
        self.pause_button.setText("Пауза")

        for host in hosts:
            self._start_host(host, reset_data=True)

    def _stop_ping(self):
        """Остановка мониторинга всех хостов"""
        if not self._is_running:
            return

        logger.info("PingMonitorDialog: Stopping ping")

        # Останавливаем опрос, сохраняя графики для анализа результатов.
        for host in list(set(self._threads) | set(self._curves)):
            self._stop_host(host, clear_view=False)

        self._is_running = False
        self._is_paused = False
        self._pause_started = None

        # UI обновления
        self.toggle_button.blockSignals(True)
        self.toggle_button.setChecked(False)
        self.toggle_button.blockSignals(False)
        self.toggle_button.setText("Старт")
        self.pause_button.blockSignals(True)
        try:
            self.pause_button.setChecked(False)
        finally:
            self.pause_button.blockSignals(False)
        self.pause_button.setText("Пауза")
        self.pause_button.setEnabled(False)
        self._update_status("Остановлено")

    # ------------------------------------------------------------------
    # Данные и отображение
    # ------------------------------------------------------------------

    @Slot(float, datetime, bool)
    def _on_ping_result(
        self, delay: float, timestamp: datetime, upper_bound: bool
    ) -> None:
        """Отбросить результаты замененного или остановленного потока."""
        thread = self.sender()
        if (
            isinstance(thread, PingThread)
            and self._threads.get(thread.target) is thread
        ):
            self._update_host(thread.target, delay, timestamp, upper_bound)

    @Slot(str)
    def _on_worker_error(self, message: str) -> None:
        """Показать системную ошибку отдельно от потери ICMP-ответа."""
        thread = self.sender()
        if (
            isinstance(thread, PingThread)
            and self._threads.get(thread.target) is thread
        ):
            self._update_status(f"{thread.target}: {message}")
            item = self._find_host_item(thread.target)
            if item is not None:
                item.setText(1, "ошибка")
                item.setToolTip(1, message)

    @Slot()
    def _on_thread_finished(self) -> None:
        """Удерживать QThread до настоящего finished, затем разрешить закрытие."""
        thread = self.sender()
        if not isinstance(thread, PingThread):
            return
        self._retiring.discard(thread)
        if self._threads.get(thread.target) is thread:
            self._threads.pop(thread.target)
        thread.deleteLater()
        if self._pending_done is not None and not self._retiring and not self._threads:
            result = self._pending_done
            self._pending_done = None
            super().done(result)

    def _update_host(
        self, host: str, delay: float, timestamp: datetime, upper_bound: bool = False
    ) -> None:
        """Накопить отсчет за O(1); отрисовку выполняет общий GUI-таймер."""
        if host not in self._threads or self._is_paused or self._session_start is None:
            return
        self._elapsed = max(0.0, time.monotonic() - self._session_start)
        self._sample_count[host] = self._sample_count.get(host, 0) + 1
        lost = delay < 0
        if lost:
            self._loss_count[host] += 1
        self._data_times[host].append(self._elapsed)
        self._data_delays[host].append(float("nan") if lost else delay)
        self._dirty_hosts.add(host)
        if not lost:
            self._y_max = max(self._y_max, delay)
        item = self._find_host_item(host)
        if item is not None:
            losses = self._loss_count[host]
            count = self._sample_count[host]
            value_text = f"<{delay:g} мс" if upper_bound else f"{delay:.1f} мс"
            item.setText(1, "нет ответа" if lost else value_text)
            item.setToolTip(
                1,
                "На графике показана верхняя граница задержки ответа"
                if upper_bound
                else "",
            )
            item.setData(1, DELAY_ROLE, None if lost else delay)
            item.setText(2, f"{losses}/{count} ({100 * losses / count:.0f}%)")
            item.setData(2, LOSS_ROLE, losses)
            item.setToolTip(2, "Потери / завершенные запросы за текущую сессию")

    def _refresh_plot(self) -> None:
        """Обновить все измененные кривые и оси одним пакетом."""
        if not self._dirty_hosts:
            return
        for host in self._dirty_hosts:
            curve = self._curves.get(host)
            if curve is not None:
                curve.setData(
                    list(self._data_times[host]), list(self._data_delays[host])
                )
        self._dirty_hosts.clear()
        if self.follow_checkbox.isChecked():
            self.plot_widget.setYRange(0, max(100.0, self._y_max * 1.2))
            self.plot_widget.setXRange(
                max(0.0, self._elapsed - self._view_window), max(60.0, self._elapsed)
            )

    def _update_status(self, message: str = ""):
        """Показать состояние в постоянной строке под графиком."""
        if not message:
            message = (
                f"Опрос: {len(self._threads)} хостов" if self._is_running else "Готово"
            )
        self.status_label.setText(message)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Отложить закрытие до освобождения дочерних потоков."""
        event.ignore()
        self.reject()

    def reject(self) -> None:
        """Закрыть по Escape с тем же протоколом завершения."""
        self.done(QDialog.DialogCode.Rejected)

    def done(self, result: int) -> None:
        """Не уничтожать диалог до фактического завершения всех QThread."""
        self._stop_ping()
        if self._retiring or self._threads:
            self._pending_done = result
            self.setEnabled(False)
            self._update_status("Завершение опроса…")
            return
        super().done(result)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    dialog = PingMonitorDialog()
    dialog.show()
    sys.exit(app.exec())
