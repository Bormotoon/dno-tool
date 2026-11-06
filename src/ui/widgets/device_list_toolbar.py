"""Панели инструментов списка устройств.

Три отдельные панели (тулбара) над списком устройств:
- ViewToolbar — фильтр онлайн, режим/стиль миниатюр, ползунок размера.
- VeyonToolbar — Veyon-зависимые фичи (lock/unlock/login/WebAPI/...). Скрывается
  целиком при отсутствии Veyon.
- ControlToolbar — power/session-операции через SSH/systemd/UDP/DBus
  (power_on/reboot/powerdown/login_ssh/logoff). Всегда виден.

Все три базируются на QToolBar — встроенная поддержка overflow: при нехватке
места справа появляется служебная кнопка с выпадающим меню (имя объекта
qt_toolbar_ext_button, создаётся Qt лениво при первой раскладке). Два дефекта
штатного пути, чинятся в BaseDeviceToolbar:
1. Qt ставит кнопке иконку стиля (стрелка) один раз при создании. На Fusion
   пиксмап стрелки рисуется чёрным независимо от палитры - на тёмных темах
   невидим. Вместо неё ставится кастомная SVG-иконка «три точки»
   (overflow_icon, перекрашивается палитрой темы).
2. Qt резервирует под кнопку ~11px ширины у правого края: наша кнопка 32px
   частично вылезает за границу тулбара, и её центральная иконка обрезается.
   Кнопка позиционируется в свободную полосу (_fit_ext_button): центр точек -
   середина зазора между последней видимой кнопкой и краем.

Кнопки панелей реализованы через QAction (не QToolButton напрямую) - это
обеспечивает корректную работу overflow: actions уходят в popup-меню с
сохранением иконки, tooltip и checked-состояния.
"""

from PySide6.QtCore import Signal, QTimer, Qt, QSize, QEvent
from PySide6.QtWidgets import (
    QToolBar,
    QToolButton,
    QSlider,
    QLabel,
    QWidget,
    QSizePolicy,
    QMenu,
    QApplication,
)
from PySide6.QtGui import QAction
from src.ui.icons import set_themed_icon, overflow_icon
from src.services.hosts.host_info_service import (
    TOOLTIP_MENU_FIELDS,
    STATIC_TOOLTIP_FIELDS,
    DEFAULT_FIELDS,
)


# Общий стиль для всех тулбаров: размеры и отступы плюс плоская подсветка
# состояний из ролей палитры (без хардкода цветов - работает на любой теме).
# Базовый border прозрачный 1px, чтобы hover/pressed/checked не прыгали
# на 1px при появлении рамки. Нажатие контурное (рамка highlight поверх
# фона hover): одинаково выглядит при любом размере иконки и не спорит
# с тонкими пак-контурами. Toggle-кнопки дополнительно меняют иконку
# на On-вариант (см. src/ui/icons.py), фон checked - поверх иконки.
_TOOLBAR_QSS = (
    "QToolBar { border: none; background: transparent; padding: 0; }"
    "QToolBar::separator { width: 1px; margin: 8px 4px; }"
    "QToolButton { padding: 0; margin: 0; border: 1px solid transparent; border-radius: 4px; "
    "min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; }"
    "QToolButton:hover { background: palette(midlight); border: 1px solid palette(mid); }"
    "QToolButton:pressed { background: palette(midlight); border: 1px solid palette(highlight); }"
    "QToolButton:checked { background: palette(midlight); border: 1px solid palette(highlight); }"
    "QToolButton:checked:hover { background: palette(midlight); border: 1px solid palette(highlight); }"
    "QToolButton:disabled { background: transparent; border: 1px solid transparent; }"
)


# Плоская кнопка для одиночных QToolButton вне тулбаров (видеоредактор,
# терминал и т.п.): те же состояния из ролей палитры, но без фиксации
# размера - размер задает сам виджет. Нажатие контурное (рамка highlight),
# а не заливка: одинаково выглядит при любом размере иконки.
FLAT_TOOLBUTTON_QSS = (
    "QToolButton { padding: 0; margin: 0; border: 1px solid transparent; "
    "border-radius: 4px; background: transparent; }"
    "QToolButton:hover { background: palette(midlight); border: 1px solid palette(mid); }"
    "QToolButton:pressed { background: palette(midlight); border: 1px solid palette(highlight); }"
    "QToolButton:checked { background: palette(midlight); border: 1px solid palette(highlight); }"
    "QToolButton:disabled { background: transparent; border: 1px solid transparent; }"
)

# Та же плоская кнопка с выпадающей частью (MenuButtonPopup): стрелка
# отделена чертой при наведении, чтобы было видно, что это отдельная кнопка.
FLAT_MENU_TOOLBUTTON_QSS = FLAT_TOOLBUTTON_QSS + (
    "QToolButton[popupMode=\"1\"] { padding-right: 18px; }"
    "QToolButton::menu-button { width: 18px; border: none; "
    "border-left: 1px solid transparent; "
    "border-top-right-radius: 4px; border-bottom-right-radius: 4px; }"
    "QToolButton::menu-button:hover { border-left: 1px solid palette(mid); "
    "background: palette(mid); }"
)


def _ext_button(toolbar: "QToolBar"):
    """Служебная overflow-кнопка тулбара или None (создаётся лениво Qt)."""
    return toolbar.findChild(QToolButton, "qt_toolbar_ext_button")


def _refresh_ext_button_icon(toolbar: "QToolBar") -> None:
    """Кастомная иконка «три точки» для служебной overflow-кнопки тулбара.

    Qt ставит кнопке иконку стиля (стрелка) один раз при создании кнопки;
    на Fusion её пиксмап чёрный - на тёмных палитрах невидим. Свежий QIcon
    ставится кнопке: движок иконки рисует точки цветом текущей палитры при
    отрисовке (перекраска при смене темы) - замена QIcon сбрасывает кэш
    пикмапов. Вызывается при появлении кнопки и на
    PaletteChange/StyleChange тулбара.
    """
    btn = _ext_button(toolbar)
    if btn is not None:
        btn.setIcon(overflow_icon())


class BaseDeviceToolbar(QToolBar):
    """Базовый класс тулбара списка устройств: общий стиль и размер.

    Предоставляет единый внешний вид (фиксированная высота 36px, иконки 32x32,
    margins 6px по бокам) для всех трёх тулбаров (View/Veyon/Control).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMovable(False)
        self.setFloatable(False)
        self.setIconSize(QSize(32, 32))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setFixedHeight(36)
        # Layout margins 6px по бокам, 0 сверху/снизу — иначе кнопки обрезаются
        self.layout().setContentsMargins(6, 0, 6, 0)
        self.layout().setSpacing(2)
        self.setStyleSheet(_TOOLBAR_QSS)
        # Служебная overflow-кнопка: следить за геометрией (Fusion режет её)
        self._ext_btn_filtering = False

    def childEvent(self, event):
        """Появление служебной overflow-кнопки (qt_toolbar_ext_button).

        Qt создаёт кнопку лениво при первой нехватке места и сразу ставит
        ей иконку стиля (стрелка). ChildAdded приходит с пустым
        objectName (имя ставится после добавления), поэтому ловим
        ChildPolished - имя там уже выставлено. Замена - после возврата в
        цикл событий: singleShot(0), иначе Qt перезапишет иконку в том же
        C++-стеке.
        """
        if event.type() == QEvent.Type.ChildPolished and event.child() is not None:
            child = event.child()
            if (
                isinstance(child, QToolButton)
                and child.objectName() == "qt_toolbar_ext_button"
            ):
                QTimer.singleShot(0, self._set_ext_icon)
        return super().childEvent(event)

    def _set_ext_icon(self):
        """Иконка «три точки» + геометрия служебной кнопки (см. module doc)."""
        try:
            btn = _ext_button(self)
        except RuntimeError:
            return  # тулбар удалён до срабатывания таймера
        if btn is None:
            return
        _refresh_ext_button_icon(self)
        self._install_ext_filter(btn)
        self._fit_ext_button(btn)

    def _install_ext_filter(self, btn):
        """Однократно поставить eventFilter на геометрию служебной кнопки."""
        if self._ext_btn_filtering:
            return
        self._ext_btn_filtering = True
        btn.installEventFilter(self)

    def eventFilter(self, obj, event):
        """Геометрия служебной кнопки после каждого relayout тулбара.

        Qt ставит кнопку частично за правым краем на каждой раскладке -
        после каждого Resize/Move/Show кнопки позиция пересчитывается
        (_fit_ext_button). Двигаем только при фактическом несовпадении,
        рекурсии нет.
        """
        if obj is _ext_button(self) and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Move,
            QEvent.Type.Show,
        ):
            self._fit_ext_button(obj)
        return super().eventFilter(obj, event)

    def _fit_ext_button(self, btn):
        """Позиционировать служебную кнопку в свободную полосу тулбара.

        Раскладчик Qt резервирует под кнопку ~11px у правого края и ставит
        её частично за границей тулбара (x = width - 11): наша кнопка 32px
        вылезает, и её иконка (по центру кнопки) оказывается за пределами
        и обрезается. Побочный эффект наивной привязки «x = width - 32» -
        точки наезжают на последнюю видимую кнопку, пока Qt не спрятал её
        в overflow.

        Правило: центр точек - середина свободной полосы между концом
        последней видимой кнопки и правым краем тулбара. Фон кнопки
        прозрачный, так что частичный вылет прямоугольника кнопки за
        границу безопасен - внутри остаются точки. Если видимых кнопок
        нет - прижать к правому краю (x = width - 32).
        """
        try:
            if btn.isHidden() or not btn.isVisible():
                return
            tbw = self.width()
            vis = [
                b
                for b in self.findChildren(QToolButton)
                if b.objectName() != "qt_toolbar_ext_button" and b.isVisible()
            ]
            if vis:
                last_end = max(b.x() + b.width() for b in vis)
                x = round((last_end + tbw) / 2 - btn.width() / 2)
            else:
                x = tbw - btn.width()
            x = max(0, x)
            if btn.x() != x:
                btn.move(x, btn.y())
        except RuntimeError:
            pass

    def event(self, event):
        # Смена темы/палитры: перекрасить точки уже созданной overflow-кнопки
        # (движок иконки читает палитру в момент отрисовки, но QIcon кнопки
        # нужно заменить, чтобы сбросить кэш пикмапов).
        et = event.type()
        if et in (
            QEvent.Type.StyleChange,
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
        ):
            _refresh_ext_button_icon(self)
        return super().event(event)

    def _add_feature_action(self, token, icon_key, tip, label, target_map, signal):
        """Хелпер: создать QAction фичи, подключить к signal.emit(token).

        Args:
            token: строковый идентификатор фичи (lock/login_ssh/...).
            icon_key: ключ в ICONS.
            tip: tooltip.
            label: текст для overflow-меню.
            target_map: dict куда сохранить action (self._vactions).
            signal: Signal(str) для эмиссии token при triggered.
        """
        act = QAction(label, self)
        set_themed_icon(act, icon_key)
        act.setToolTip(tip)
        act.triggered.connect(lambda _, t=token: signal.emit(t))
        self.addAction(act)
        target_map[token] = act
        return act


class ViewToolbar(BaseDeviceToolbar):
    """Панель «Вид»: фильтр онлайн, режим/стиль миниатюр, тултип-инфо, ползунок.

    Содержит:
    - Переключатель «Только онлайн» (QAction, checkable)
    - Переключатель режима «Иконки/Миниатюры Veyon» (QAction, checkable)
    - Переключатель стиля миниатюр (QAction, checkable)
    - Кнопка «Информация в тултипе хоста» (QToolButton + InstantPopup меню)
    - Ползунок размера (QSlider, 32–512, шаг 8, debounce 50 мс)
    """

    online_only_changed = Signal(bool)
    thumbnail_mode_changed = Signal(bool)
    thumbnail_style_changed = Signal(bool)
    icon_size_changed = Signal(int)
    tooltip_fields_changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        # Переключатель «Только онлайн»
        self._online_action = QAction("Только онлайн", self)
        set_themed_icon(self._online_action, "toolbar_online_only", toggle=True)
        self._online_action.setCheckable(True)
        self._online_action.setToolTip("Показывать только онлайн-устройства")
        self._online_action.toggled.connect(self._on_online_toggled)
        self.addAction(self._online_action)

        self.addSeparator()

        # Переключатель режима «Иконки/Миниатюры»
        self._thumb_action = QAction("Миниатюры экранов", self)
        set_themed_icon(self._thumb_action, "toolbar_thumbnails", toggle=True)
        self._thumb_action.setCheckable(True)
        self._thumb_action.setToolTip("Показывать миниатюры экранов (вместо иконок)")
        self._thumb_action.toggled.connect(self._on_thumb_toggled)
        self.addAction(self._thumb_action)

        self.addSeparator()

        # Переключатель стиля миниатюр «В рамке / Одна картинка»
        self._style_action = QAction("Одна картинка (без рамки)", self)
        set_themed_icon(self._style_action, "toolbar_thumbnail_style", toggle=True)
        self._style_action.setCheckable(True)
        self._style_action.setToolTip(
            "Отображение миниатюр: в рамке монитора (выкл) / одна картинка (вкл)"
        )
        self._style_action.toggled.connect(self._on_style_toggled)
        self.addAction(self._style_action)

        self.addSeparator()

        # Кнопка «Данные в тултипе» — меню выбора информации, показываемой
        # в тултипе при наведении на онлайн-хост (QToolButton + InstantPopup).
        # Статические поля (Имя/Хост/Порт/Нода) + разделитель + живые поля
        # (SSH/HostInfo-опрос).
        self._info_button = QToolButton(self)
        set_themed_icon(self._info_button, "toolbar_host_info")
        self._info_button.setToolTip("Информация в тултипе хоста")
        self._info_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._info_menu = QMenu(self._info_button)
        self._info_field_actions = {}
        for key, label in TOOLTIP_MENU_FIELDS:
            act = self._info_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(key in DEFAULT_FIELDS)
            act.toggled.connect(self._emit_tooltip_fields)
            self._info_field_actions[key] = act
            if key == STATIC_TOOLTIP_FIELDS[-1][0]:
                self._info_menu.addSeparator()  # граница статика/живые
        self._info_button.setMenu(self._info_menu)
        self.addWidget(self._info_button)

        self.addSeparator()

        # Ползунок размера (виджеты не могут быть QAction — добавляем через addWidget)
        size_label = QLabel("Размер:", self)
        self.addWidget(size_label)

        self._slider = QSlider(Qt.Horizontal, self)
        self._slider.setRange(32, 512)
        self._slider.setSingleStep(8)
        self._slider.setValue(64)
        self._slider.setFixedWidth(150)
        self._slider.setToolTip("Размер миниатюр устройств")
        self.addWidget(self._slider)

        self._value_label = QLabel("64px", self)
        self._value_label.setFixedWidth(36)
        self.addWidget(self._value_label)

        # Debounce-таймер для ползунка размера
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(50)
        self._debounce_timer.timeout.connect(self._emit_size)
        self._slider.valueChanged.connect(self._on_slider_changed)

        # Фиксированная ширина тулбара — чтобы QToolBar не сжимал layout
        # и не trigger overflow (ползунок не пропадает, межиконочные
        # промежутки стабильны при любом размере окна).
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def showEvent(self, event):
        """Пересчитать фиксированную ширину после показа (метрики готовы)."""
        super().showEvent(event)
        self._apply_fixed_width()

    def _apply_fixed_width(self) -> None:
        """Зафиксировать ширину тулбара по sizeHint layout-а.

        QToolBar при нехватке ширины переносит QAction в выпадающее меню «»,
        а виджеты (ползунок/метки), добавленные через addWidget, скрываются
        и не попадают в меню — ползунок просто пропадает. Фиксация ширины
        отключает этот механизм: layout никогда не сжимается.
        """
        w = self.layout().sizeHint().width()
        if w > 0:
            self.setFixedWidth(w)

    def _on_online_toggled(self, checked: bool):
        self.online_only_changed.emit(checked)

    def _on_thumb_toggled(self, checked: bool):
        self.thumbnail_mode_changed.emit(checked)

    def _on_style_toggled(self, checked: bool):
        # checked = True  → plain (одна картинка)
        # checked = False → monitor (в рамке)
        self.thumbnail_style_changed.emit(checked)

    def _on_slider_changed(self, val: int):
        self._value_label.setText(f"{val}px")
        self._debounce_timer.start()

    def _emit_size(self):
        self.icon_size_changed.emit(self._slider.value())

    def _emit_tooltip_fields(self):
        """Меню тултипа: отправить список включённых полей (в порядке TOOLTIP_MENU_FIELDS)."""
        fields = [
            key
            for key, _label in TOOLTIP_MENU_FIELDS
            if self._info_field_actions[key].isChecked()
        ]
        self.tooltip_fields_changed.emit(fields)

    def set_tooltip_fields(self, fields):
        """Программная установка выбранных полей (без эмиссии сигнала)."""
        wanted = set(fields or [])
        for key, _label in TOOLTIP_MENU_FIELDS:
            act = self._info_field_actions[key]
            act.blockSignals(True)
            act.setChecked(key in wanted)
            act.blockSignals(False)

    # Программная установка (без эмиссии сигнала)
    def set_online_only(self, value: bool):
        self._online_action.blockSignals(True)
        self._online_action.setChecked(value)
        self._online_action.blockSignals(False)

    def set_thumbnail_mode(self, value: bool):
        self._thumb_action.blockSignals(True)
        self._thumb_action.setChecked(value)
        self._thumb_action.blockSignals(False)

    def set_thumbnail_style(self, plain: bool):
        """Установить стиль миниатюр: plain=True → одна картинка, False → в рамке."""
        self._style_action.blockSignals(True)
        self._style_action.setChecked(plain)
        self._style_action.blockSignals(False)

    def set_icon_size(self, value: int):
        self._slider.blockSignals(True)
        self._slider.setValue(value)
        self._value_label.setText(f"{value}px")
        self._slider.blockSignals(False)

    def set_thumbnails_available(self, available: bool):
        """Explain readiness without hiding or resetting the user's thumbnail mode."""
        # A transient backend failure must not hide the user's saved mode.
        self._thumb_action.setVisible(True)
        self._style_action.setVisible(True)
        self._thumb_action.setToolTip(
            "Показывать миниатюры экранов (вместо иконок)"
            if available
            else "Выбранный источник сейчас недоступен. Настройки и режим сохранены."
        )
        # Пересчитать фиксированную ширину после инвалидации layout-а
        # (setVisible меняет sizeHint асинхронно).
        QTimer.singleShot(0, self._apply_fixed_width)


class VeyonToolbar(BaseDeviceToolbar):
    """Панель «Veyon»: Veyon-зависимые фичи (WebAPI/RFB).

    Кнопки (QAction): lock, unlock, lock_input, unlock_input, login (WebAPI),
    logoff_webapi (WebAPI), website, start_app, message, restart_service.
    Сигнал veyonFeatureRequested.emit(token) при нажатии.

    Весь тулбар Veyon-зависимый — при отсутствии Veyon скрывается целиком
    (управляется через setVisible из MainWindow._refresh_veyon_ui_visibility).
    """

    veyonFeatureRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vactions = {}
        self._setup_ui()

    def _setup_ui(self):
        # Группы Veyon-фичей (Veyon-зависимые; HostControl-фичи в ControlToolbar).
        veyon_groups = [
            [  # screen/input lock
                ("lock", "veyon_lock", "Заблокировать экран", "Заблокировать экран"),
                (
                    "unlock",
                    "veyon_unlock",
                    "Разблокировать экран",
                    "Разблокировать экран",
                ),
                (
                    "lock_input",
                    "veyon_lock_input",
                    "Заблокировать ввод",
                    "Заблокировать ввод",
                ),
                (
                    "unlock_input",
                    "veyon_unlock_input",
                    "Разблокировать ввод",
                    "Разблокировать ввод",
                ),
            ],
            [  # session (Veyon WebAPI)
                (
                    "login",
                    "veyon_login",
                    "Вход пользователя (Veyon)",
                    "Вход пользователя (Veyon)",
                ),
                (
                    "logoff_webapi",
                    "veyon_logoff",
                    "Выход пользователя (Veyon)",
                    "Выход пользователя (Veyon)",
                ),
            ],
            [  # remote actions
                ("website", "veyon_website", "Открыть сайт", "Открыть сайт"),
                (
                    "start_app",
                    "veyon_start_app",
                    "Запустить программу",
                    "Запустить программу",
                ),
                (
                    "message",
                    "veyon_message",
                    "Отправить сообщение",
                    "Отправить сообщение",
                ),
            ],
            [  # service (рестарт Veyon на хосте, SSH-транспорт но Veyon-операция)
                (
                    "restart_service",
                    "veyon_restart_service",
                    "Перезагрузить сервис Veyon",
                    "Перезагрузить сервис Veyon",
                ),
            ],
        ]
        for gi, group in enumerate(veyon_groups):
            if gi > 0:
                self.addSeparator()
            for token, icon_key, tip, label in group:
                self._add_feature_action(
                    token,
                    icon_key,
                    tip,
                    label,
                    self._vactions,
                    self.veyonFeatureRequested,
                )

    def set_features_available(self, available: bool):
        """Скрыть/показать все кнопки панели.

        Весь тулбар Veyon-зависимый — при `available=False` скрываются все
        actions. Видимостью самого тулбара управляет MainWindow (через
        setVisible — с учётом галки пользователя и доступности Veyon).
        """
        for act in self._vactions.values():
            act.setVisible(available)


class ControlToolbar(BaseDeviceToolbar):
    """Панель «Управление»: power/session через SSH/systemd/UDP/DBus.

    Кнопки (QAction) в порядке панели Veyon:
    ssh_lock/ssh_unlock/ssh_lock_input/ssh_unlock_input (SSH, без Veyon),
    login_ssh (DBus org.mos.auth), logoff (SSH: KDE DBus / loginctl),
    power_on (WoL), reboot (SSH), powerdown (SSH).
    Сигнал veyonFeatureRequested.emit(token) при нажатии (имя сигнала
    сохранено для единообразия подключения).

    Все кнопки всегда видны — не зависят от Veyon (SSH/UDP/DBus).
    """

    veyonFeatureRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vactions = {}
        self._setup_ui()

    def _setup_ui(self):
        # Группы host-control-фичей (SSH/systemd/UDP/DBus, не Veyon).
        # Порядок групп как на панели Veyon: блокировка, сеанс, затем питание.
        control_groups = [
            [  # блокировка без Veyon (SSH + python3 на хосте)
                (
                    "ssh_lock",
                    "veyon_lock",
                    "Заблокировать экран (SSH)",
                    "Заблокировать экран (SSH)",
                ),
                (
                    "ssh_unlock",
                    "veyon_unlock",
                    "Разблокировать экран (SSH)",
                    "Разблокировать экран (SSH)",
                ),
                (
                    "ssh_lock_input",
                    "veyon_lock_input",
                    "Заблокировать ввод (SSH)",
                    "Заблокировать ввод (SSH)",
                ),
                (
                    "ssh_unlock_input",
                    "veyon_unlock_input",
                    "Разблокировать ввод (SSH)",
                    "Разблокировать ввод (SSH)",
                ),
            ],
            [  # session (SSH/DBus)
                (
                    "login_ssh",
                    "veyon_login",
                    "Вход в систему (SSH)",
                    "Вход в систему (SSH)",
                ),
                (
                    "logoff",
                    "veyon_logoff",
                    "Завершить сеанс (SSH)",
                    "Завершить сеанс (SSH)",
                ),
            ],
            [  # power
                (
                    "power_on",
                    "veyon_power_on",
                    "Включить (Wake-on-LAN)",
                    "Включить (Wake-on-LAN)",
                ),
                ("reboot", "veyon_reboot", "Перезагрузить", "Перезагрузить"),
                (
                    "powerdown",
                    "veyon_powerdown",
                    "Выключить питание",
                    "Выключить питание",
                ),
            ],
        ]
        for gi, group in enumerate(control_groups):
            if gi > 0:
                self.addSeparator()
            for token, icon_key, tip, label in group:
                self._add_feature_action(
                    token,
                    icon_key,
                    tip,
                    label,
                    self._vactions,
                    self.veyonFeatureRequested,
                )


# Обратная совместимость: DeviceListToolbar как псевдоним для ViewToolbar.
# Старый код, импортирующий DeviceListToolbar, получит панель «Вид»
# (online_only/thumbnails/style/slider). Veyon/Control-кнопки перенесены
# в отдельные тулбары, подключаемые в MainWindow.setup_list_view.
DeviceListToolbar = ViewToolbar


class DeviceTreeToolbar(BaseDeviceToolbar):
    """Панель инструментов дерева устройств.

    Кнопки (QAction):
    - «Добавить папку/устройство/диапазон» — те же действия, что пункты
      контекстного меню дерева (цель — по текущему выделению: папка —
      внутрь неё, хост — в его родителя, иначе в корень/секцию «Эта машина»).
    - «Сортировать» — сортировка текущей группы (тот же
      sort_current_group, что в контекстном меню).
    - «Сканер хостов» — открывает диалог сканера сети (сигнал
      scanner_requested, обработчик подключает MainWindow).
    - «Развернуть всё» — expandAll (секции нод тоже, но без их содержимого
      до первого обновления hosts).
    - «Свернуть всё» — collapseAll (сворачивает всё, кроме top-level строк).
    - «Ноды» (checkable) — показывать секции нод первым уровнем дерева
      (включая локальную ноду «Эта машина»). По умолчанию включено.
    """

    add_folder_requested = Signal()
    add_host_requested = Signal()
    add_range_requested = Signal()
    sort_requested = Signal()
    scanner_requested = Signal()
    expand_all_requested = Signal()
    collapse_all_requested = Signal()
    show_nodes_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        self._add_folder_action = QAction("Добавить папку", self)
        set_themed_icon(self._add_folder_action, "menu_add_folder")
        self._add_folder_action.setToolTip("Добавить папку")
        self._add_folder_action.triggered.connect(self.add_folder_requested.emit)
        self.addAction(self._add_folder_action)

        self._add_host_action = QAction("Добавить устройство", self)
        set_themed_icon(self._add_host_action, "menu_add_device")
        self._add_host_action.setToolTip("Добавить устройство")
        self._add_host_action.triggered.connect(self.add_host_requested.emit)
        self.addAction(self._add_host_action)

        self._add_range_action = QAction("Добавить диапазон IP", self)
        set_themed_icon(self._add_range_action, "menu_add_ip_range")
        self._add_range_action.setToolTip("Добавить диапазон IP")
        self._add_range_action.triggered.connect(self.add_range_requested.emit)
        self.addAction(self._add_range_action)

        self._sort_action = QAction("Сортировать", self)
        set_themed_icon(self._sort_action, "menu_sort")
        self._sort_action.setToolTip("Сортировать текущую группу")
        self._sort_action.triggered.connect(self.sort_requested.emit)
        self.addAction(self._sort_action)

        self.addSeparator()

        self._expand_action = QAction("Развернуть всё", self)
        set_themed_icon(self._expand_action, "toolbar_expand_all")
        self._expand_action.setToolTip("Развернуть всё дерево")
        self._expand_action.triggered.connect(self.expand_all_requested.emit)
        self.addAction(self._expand_action)

        self._collapse_action = QAction("Свернуть всё", self)
        set_themed_icon(self._collapse_action, "toolbar_collapse_all")
        self._collapse_action.setToolTip("Свернуть всё дерево")
        self._collapse_action.triggered.connect(self.collapse_all_requested.emit)
        self.addAction(self._collapse_action)

        self.addSeparator()

        self._nodes_action = QAction("Ноды", self)
        set_themed_icon(self._nodes_action, "toolbar_nodes", toggle=True)
        self._nodes_action.setCheckable(True)
        # По умолчанию выключено: ноды скрыты, клиенты нод не опрашиваются.
        self._nodes_action.setChecked(False)
        self._nodes_action.setToolTip(
            "Показывать ноды в дереве (первый уровень). "
            "Локальная машина — нода «Эта машина»"
        )
        self._nodes_action.toggled.connect(self.show_nodes_changed.emit)
        self.addAction(self._nodes_action)

        self.addSeparator()

        self._scanner_action = QAction("Сканер хостов", self)
        set_themed_icon(self._scanner_action, "menu_scanner")
        self._scanner_action.setToolTip("Открыть сканер хостов")
        self._scanner_action.triggered.connect(self.scanner_requested.emit)
        self.addAction(self._scanner_action)

    def set_show_nodes(self, checked: bool) -> None:
        """Программная установка переключателя нод (без эмиссии сигнала)."""
        self._nodes_action.blockSignals(True)
        self._nodes_action.setChecked(checked)
        self._nodes_action.blockSignals(False)
