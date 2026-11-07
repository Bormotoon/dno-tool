"""Выезжающая панель окна VNC (как в окне удалённого доступа Veyon).

Панель лежит поверх изображения в левом верхнем углу и обычно спрятана: на
её месте виден только маленький язычок. Наведение мыши на угол (с короткой
задержкой, чтобы случайный проход курсора не мешал работе) выдвигает панель,
уход мыши - прячет. Кнопка с булавкой оставляет панель открытой. Пока нет
соединения, панель не прячется: нужны «Переподключить» и «Закрыть».

Кнопки не берут фокус клавиатуры, поэтому нажатия остаются у удалённого экрана.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QActionGroup, QCursor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.icons import _icon_from_key
from src.workers.vnc_viewer.keymap import KEY_COMBOS, KEYBOARD_PHYSICAL, KEYBOARD_SYMBOL

HOT_ZONE = QSize(56, 28)  # угол, наведение на который выдвигает панель
SHOW_DELAY_MS = 180
HIDE_DELAY_MS = 900
HIDE_MARGIN = 16
POLL_MS = 60
ANIMATION_MS = 160
INTRO_MS = 3500  # после подключения панель видна, чтобы о ней узнали

STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_ERROR = "error"
_STATE_COLORS = {STATE_CONNECTING: "#e0a800", STATE_CONNECTED: "#2fb35a", STATE_ERROR: "#d9534f"}

_STYLE = """
#vncPanel {
    background: palette(window);
    border: 1px solid palette(mid);
    border-top: none;
    border-left: none;
    border-bottom-right-radius: 10px;
}
#vncPanel QToolButton {
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 3px 6px;
}
#vncPanel QToolButton:hover { background: palette(midlight); border-color: palette(mid); }
#vncPanel QToolButton:checked { background: palette(highlight); color: palette(highlighted-text); }
#vncPanel QToolButton::menu-indicator { image: none; width: 0; }
#vncPanel #vncSeparator { background: palette(mid); }
#vncHandle {
    background: palette(window);
    border: 1px solid palette(mid);
    border-top: none;
    border-left: none;
    border-bottom-right-radius: 7px;
    padding: 0 6px 1px 6px;
}
"""


def _separator() -> QFrame:
    line = QFrame()
    line.setObjectName("vncSeparator")
    line.setFixedWidth(1)
    line.setMinimumHeight(36)
    return line


class VncControlPanel(QFrame):
    """Панель управления окном VNC; сама прячется и выдвигается."""

    mode_changed = Signal(str)  # "view" | "control"
    screen_selected = Signal(object)  # QRect | None
    fit_changed = Signal(bool)
    combo_requested = Signal(object)  # tuple[int, ...]
    clipboard_toggled = Signal(bool)
    screenshot_requested = Signal()
    fullscreen_toggled = Signal(bool)
    keyboard_mode_changed = Signal(str)
    low_color_changed = Signal(bool)
    reconnect_requested = Signal()
    close_requested = Signal()

    def __init__(self, container: QWidget) -> None:
        super().__init__(container)
        self.setObjectName("vncPanel")
        self.setStyleSheet(_STYLE)
        self.setAutoFillBackground(True)
        self._container = container
        self._shown = False
        self._sticky = True  # пока нет соединения - не прятать
        self._compact = False
        self._buttons: list[QToolButton] = []
        self._hover_since: float | None = None
        self._away_ticks = 0
        self._build()

        self.handle = QToolButton(container)
        self.handle.setObjectName("vncHandle")
        self.handle.setStyleSheet(_STYLE)
        self.handle.setText("▾ VNC")
        self.handle.setToolTip("Панель управления: наведите мышь на угол")
        self.handle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.handle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.handle.clicked.connect(lambda: self.show_panel())
        self.handle.adjustSize()

        self._animation = QPropertyAnimation(self, b"pos", self)
        self._animation.setDuration(ANIMATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._on_animation_finished)

        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._tick)
        self._poll.start()
        self._intro = QTimer(self)
        self._intro.setSingleShot(True)

        self.relayout()
        self.move(0, 0)
        self._shown = True
        self.handle.hide()

    # ---------- Построение ----------

    def _button(
        self, icon_key: str, text: str, tooltip: str, *, checkable: bool = False
    ) -> QToolButton:
        button = QToolButton()
        button.setIcon(_icon_from_key(icon_key))
        button.setIconSize(QSize(24, 24))
        button.setText(text)
        button.setToolTip(tooltip)
        button.setCheckable(checkable)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setAutoRaise(True)
        self._buttons.append(button)
        return button

    def _menu_button(self, icon_key: str, text: str, tooltip: str, menu: QMenu) -> QToolButton:
        button = self._button(icon_key, text + " ▾", tooltip)
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        return button

    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 8, 6)
        layout.setSpacing(2)

        info = QVBoxLayout()
        info.setSpacing(0)
        self.title_label = QLabel()
        font = self.title_label.font()
        font.setBold(True)
        self.title_label.setFont(font)
        self.status_label = QLabel()
        small = self.status_label.font()
        small.setPointSizeF(max(7.0, small.pointSizeF() * 0.9))
        self.status_label.setFont(small)
        self.info_label = QLabel()
        self.info_label.setFont(small)
        info.addWidget(self.title_label)
        info.addWidget(self.status_label)
        info.addWidget(self.info_label)
        info_widget = QWidget()
        info_widget.setLayout(info)
        info_widget.setMinimumWidth(150)
        info_widget.setMaximumWidth(260)
        layout.addWidget(info_widget)
        layout.addWidget(_separator())

        # Режим: две взаимоисключающие кнопки понятнее одного переключателя.
        self.view_button = self._button(
            "menu_veyon_view", "Просмотр", "Только смотреть: мышь и клавиатура не передаются", checkable=True
        )
        self.control_button = self._button(
            "menu_veyon_control", "Управление", "Управлять мышью и клавиатурой", checkable=True
        )
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self.view_button)
        self._mode_group.addButton(self.control_button)
        self.view_button.clicked.connect(lambda: self.mode_changed.emit("view"))
        self.control_button.clicked.connect(lambda: self.mode_changed.emit("control"))
        layout.addWidget(self.view_button)
        layout.addWidget(self.control_button)
        layout.addWidget(_separator())

        self.screen_menu = QMenu(self)
        self._screen_group = QActionGroup(self)
        self.screen_button = self._menu_button(
            "info_computer", "Экран", "Какой монитор удалённого компьютера показывать", self.screen_menu
        )
        layout.addWidget(self.screen_button)

        scale_menu = QMenu(self)
        scale_group = QActionGroup(self)
        self.fit_action = QAction("Вписать в окно", self)
        self.actual_action = QAction("Исходный размер (1:1), с прокруткой", self)
        for action, fit in ((self.fit_action, True), (self.actual_action, False)):
            action.setCheckable(True)
            scale_group.addAction(action)
            scale_menu.addAction(action)
            action.triggered.connect(lambda _=False, v=fit: self.fit_changed.emit(v))
        self.scale_button = self._menu_button(
            "ve_fit_project", "Масштаб", "Вписать экран в окно или показать в исходном размере", scale_menu
        )
        layout.addWidget(self.scale_button)

        keys_menu = QMenu(self)
        for label, keysyms in KEY_COMBOS:
            action = keys_menu.addAction(label)
            action.triggered.connect(lambda _=False, k=keysyms: self.combo_requested.emit(k))
        self.keys_button = self._menu_button(
            "veyon_unlock_input",
            "Клавиши",
            "Отправить сочетание клавиш, которое этот компьютер перехватывает сам",
            keys_menu,
        )
        layout.addWidget(self.keys_button)

        self.clipboard_button = self._button(
            "menu_sync",
            "Буфер обмена",
            "Общий буфер обмена с удалённым компьютером (текст)",
            checkable=True,
        )
        self.clipboard_button.toggled.connect(self.clipboard_toggled.emit)
        layout.addWidget(self.clipboard_button)

        self.screenshot_button = self._button("camera", "Снимок", "Сохранить снимок экрана в файл")
        self.screenshot_button.clicked.connect(self.screenshot_requested.emit)
        layout.addWidget(self.screenshot_button)

        self.fullscreen_button = self._button(
            "camera_view_1", "Во весь экран", "Развернуть окно на весь экран", checkable=True
        )
        self.fullscreen_button.toggled.connect(self._on_fullscreen_button)
        layout.addWidget(self.fullscreen_button)
        layout.addWidget(_separator())

        settings_menu = QMenu(self)
        settings_menu.addSection("Ввод с клавиатуры")
        keyboard_group = QActionGroup(self)
        self.physical_action = QAction("По клавишам — раскладка удалённого компьютера", self)
        self.symbol_action = QAction("По символам — раскладка этого компьютера", self)
        for action, mode in ((self.physical_action, KEYBOARD_PHYSICAL), (self.symbol_action, KEYBOARD_SYMBOL)):
            action.setCheckable(True)
            keyboard_group.addAction(action)
            settings_menu.addAction(action)
            action.triggered.connect(lambda _=False, v=mode: self.keyboard_mode_changed.emit(v))
        settings_menu.addSection("Качество изображения")
        quality_group = QActionGroup(self)
        self.full_color_action = QAction("Полный цвет", self)
        self.low_color_action = QAction("Экономия трафика (16 бит)", self)
        for action, low in ((self.full_color_action, False), (self.low_color_action, True)):
            action.setCheckable(True)
            quality_group.addAction(action)
            settings_menu.addAction(action)
            action.triggered.connect(lambda _=False, v=low: self.low_color_changed.emit(v))
        self.settings_button = self._menu_button(
            "menu_settings", "Настройки", "Раскладка клавиатуры и качество изображения", settings_menu
        )
        layout.addWidget(self.settings_button)

        self.reconnect_button = self._button("menu_refresh", "Переподключить", "Подключиться заново")
        self.reconnect_button.clicked.connect(self.reconnect_requested.emit)
        layout.addWidget(self.reconnect_button)

        self.pin_button = self._button(
            "menu_list_toolbar", "Закрепить", "Не прятать панель", checkable=True
        )
        self.pin_button.toggled.connect(lambda checked: checked and self.show_panel())
        layout.addWidget(self.pin_button)

        self.close_button = self._button("menu_abort", "Закрыть", "Закрыть окно")
        self.close_button.clicked.connect(self.close_requested.emit)
        layout.addWidget(self.close_button)

    # ---------- Состояние кнопок ----------

    def set_title(self, text: str) -> None:
        self.title_label.setText(text)

    def set_status(self, text: str, state: str) -> None:
        color = _STATE_COLORS.get(state, "#888888")
        self.status_label.setText(f'<span style="color:{color}">●</span> {text}')
        self.status_label.setToolTip(text)
        if state != STATE_CONNECTED:
            self.info_label.clear()
        sticky = state != STATE_CONNECTED
        if sticky:
            self._sticky = True
            self.show_panel()
        elif self._sticky:
            # Подключились: показать панель ещё немного и дать ей спрятаться.
            self._sticky = False
            self.show_panel()
            self._intro.start(INTRO_MS)
        self.relayout()

    def set_info(self, text: str) -> None:
        self.info_label.setText(text)

    def set_mode(self, mode: str) -> None:
        (self.view_button if mode == "view" else self.control_button).setChecked(True)
        self.keys_button.setEnabled(mode != "view")

    def set_fit(self, fit: bool) -> None:
        (self.fit_action if fit else self.actual_action).setChecked(True)

    def set_keyboard_mode(self, mode: str) -> None:
        (self.symbol_action if mode == KEYBOARD_SYMBOL else self.physical_action).setChecked(True)

    def set_low_color(self, low: bool) -> None:
        (self.low_color_action if low else self.full_color_action).setChecked(True)

    def set_clipboard(self, enabled: bool) -> None:
        self.clipboard_button.blockSignals(True)
        self.clipboard_button.setChecked(enabled)
        self.clipboard_button.blockSignals(False)

    def set_fullscreen(self, full: bool) -> None:
        self.fullscreen_button.blockSignals(True)
        self.fullscreen_button.setChecked(full)
        self.fullscreen_button.blockSignals(False)
        self._update_fullscreen_text()

    def _on_fullscreen_button(self, checked: bool) -> None:
        self._update_fullscreen_text()
        self.fullscreen_toggled.emit(checked)

    def _update_fullscreen_text(self) -> None:
        full = self.fullscreen_button.isChecked()
        text = "Обычный размер" if full else "Во весь экран"
        self.fullscreen_button.setText("" if self._compact else text)
        self.fullscreen_button.setToolTip(
            "Вернуть обычное окно" if full else "Развернуть окно на весь экран"
        )

    def set_screens(self, screens: list[QRect], current: QRect | None) -> None:
        """Пункты меню «Экран»; кнопка видна, только если мониторов больше одного."""
        self.screen_menu.clear()
        for action in self._screen_group.actions():
            self._screen_group.removeAction(action)
        entries: list[tuple[str, QRect | None]] = [("Все экраны", None)]
        entries += [
            (f"Экран {number} — {rect.width()}×{rect.height()}", rect)
            for number, rect in enumerate(screens, 1)
        ]
        for text, rect in entries:
            action = QAction(text, self)
            action.setCheckable(True)
            action.setData(rect)
            action.setChecked(rect == current if rect is not None else current is None)
            action.triggered.connect(lambda _=False, r=rect: self.screen_selected.emit(r))
            self._screen_group.addAction(action)
            self.screen_menu.addAction(action)
        self.screen_button.setVisible(len(screens) > 1)
        self.relayout()

    def screen_actions(self) -> list[QAction]:
        return list(self._screen_group.actions())

    # ---------- Размещение и показ ----------

    def relayout(self) -> None:
        """Подогнать размер под окно; узкое окно - кнопки без подписей."""
        available = max(200, self._container.width())
        self._set_compact(False)
        if self.sizeHint().width() > available:
            self._set_compact(True)
        hint = self.sizeHint()
        self.resize(min(hint.width(), available), hint.height())
        if not self._shown and self._animation.state() != QPropertyAnimation.State.Running:
            self.move(0, -self.height())
        self.handle.move(0, 0)
        self.raise_()
        self.handle.raise_()

    def _set_compact(self, compact: bool) -> None:
        self._compact = compact
        style = (
            Qt.ToolButtonStyle.ToolButtonIconOnly if compact else Qt.ToolButtonStyle.ToolButtonTextUnderIcon
        )
        for button in self._buttons:
            button.setToolButtonStyle(style)
        self._update_fullscreen_text()

    def is_shown(self) -> bool:
        return self._shown

    def show_panel(self) -> None:
        self._away_ticks = 0
        self.setVisible(True)
        self.handle.hide()
        self.raise_()
        running = self._animation.state() == QPropertyAnimation.State.Running
        if self._shown and not running and self.pos() == QPoint(0, 0):
            return
        self._shown = True
        self._animate_to(QPoint(0, 0))

    def hide_panel(self) -> None:
        if not self._shown or self._sticky or self.pin_button.isChecked():
            return
        self._shown = False
        self._animate_to(QPoint(0, -self.height()))

    def _animate_to(self, point: QPoint) -> None:
        self._animation.stop()
        self._animation.setStartValue(self.pos())
        self._animation.setEndValue(point)
        self._animation.start()

    def _on_animation_finished(self) -> None:
        if not self._shown:
            self.setVisible(False)
            self.handle.show()
            self.handle.raise_()

    def _menu_open(self) -> bool:
        return QApplication.activePopupWidget() is not None

    def _tick(self) -> None:
        window = self._container.window()
        if not window.isVisible() or self._menu_open():
            self._hover_since = None
            return
        pos = self._container.mapFromGlobal(QCursor.pos())
        inside_window = self._container.rect().contains(pos)
        if not self._shown:
            hot = QRect(QPoint(0, 0), HOT_ZONE)
            if inside_window and hot.contains(pos) and window.isActiveWindow():
                if self._hover_since is None:
                    self._hover_since = 0
                self._hover_since += POLL_MS
                if self._hover_since >= SHOW_DELAY_MS:
                    self._hover_since = None
                    self.show_panel()
            else:
                self._hover_since = None
            return
        if self._sticky or self.pin_button.isChecked() or self._intro.isActive():
            self._away_ticks = 0
            return
        zone = self.geometry().adjusted(0, 0, HIDE_MARGIN, HIDE_MARGIN)
        if inside_window and zone.contains(pos):
            self._away_ticks = 0
            return
        self._away_ticks += 1
        if self._away_ticks * POLL_MS >= HIDE_DELAY_MS:
            self._away_ticks = 0
            self.hide_panel()
