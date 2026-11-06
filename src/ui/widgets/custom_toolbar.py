"""Пользовательские панели команд (кастомные тулбары).

Каждая панель - отдельный QToolBar с именем и кнопками-ссылками на команды
из commands.json (привязка по task_iid). Исполнение идет через
MainWindow.execute_command на тех же хостах, что кнопка "Выполнить".
"""

from PySide6.QtCore import Signal, Qt, QSize
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QLabel

from src.ui.widgets.device_list_toolbar import BaseDeviceToolbar, _TOOLBAR_QSS

#: Размер иконок кастомной панели: default (как у обычных панелей),
#: минимум и максимум для спинбокса редактора.
PANEL_ICON_SIZE_DEFAULT = 32
PANEL_ICON_SIZE_MIN = 16
PANEL_ICON_SIZE_MAX = 64


def sanitize_panel_icon_size(value) -> int:
    """Размер иконок панели в допустимых границах (иначе default)."""
    try:
        size = int(value)
    except (TypeError, ValueError):
        return PANEL_ICON_SIZE_DEFAULT
    if size < PANEL_ICON_SIZE_MIN or size > PANEL_ICON_SIZE_MAX:
        return PANEL_ICON_SIZE_DEFAULT
    return size


def custom_qicon(icon_spec: str) -> QIcon:
    """QIcon для спека: theme:<key> | pack:<base> | color:<base> | file:<путь>.

    pack/color читаются из zip (frozen без папок) с fallback на диск.
    """
    from src.config.custom_icons import (
        resolve_icon_spec,
        pack_icon_bytes,
        color_pack_icon_bytes,
        pack_icon_path,
        color_pack_icon_path,
    )
    from src.ui.icons import _icon_from_key, qicon_from_bytes, qicon_from_file

    kind, value = resolve_icon_spec(icon_spec)
    try:
        if kind == "theme" and value:
            return _icon_from_key(value)
        if kind == "pack" and value:
            data = pack_icon_bytes(value)
            if data:
                return qicon_from_bytes(data)
            p = pack_icon_path(value)
            if p:
                return qicon_from_file(p)
        if kind == "color" and value:
            data = color_pack_icon_bytes(value)
            if data:
                return qicon_from_bytes(data)
            p = color_pack_icon_path(value)
            if p:
                return qicon_from_file(p)
        if kind == "file" and value:
            return qicon_from_file(value)
    except Exception:
        pass
    try:
        return _icon_from_key("command")
    except Exception:
        return QIcon()


class CustomCommandToolbar(BaseDeviceToolbar):
    """Одна пользовательская панель: имя + кнопки команд."""

    commandRequested = Signal(str)

    def __init__(self, panel_iid: str = "", parent=None):
        super().__init__(parent)
        self.panel_iid = panel_iid
        self._task_iids: dict = {}  # QAction -> task_iid
        # Название создается лениво в rebuild() только при show_name=True.
        # Хранение action нужно чтобы отличать его в refresh_icons().
        self._name_label = None
        self._name_action = None

    def set_panel_icon_size(self, size) -> int:
        """Размер иконок всей панели. Вернуть примененный размер.

        Штатный QSS фиксирует кнопки 32x32 - для размеров больше 32
        бокс кнопок и высота тулбара растут (бокс = размер, высота +4).
        Меньшие иконки центрируются в штатном боксе 32.
        """
        size = sanitize_panel_icon_size(size)
        self.setIconSize(QSize(size, size))
        box = max(PANEL_ICON_SIZE_DEFAULT, size)
        if box == PANEL_ICON_SIZE_DEFAULT:
            self.setStyleSheet(_TOOLBAR_QSS)
        else:
            self.setStyleSheet(_TOOLBAR_QSS.replace("32px", f"{box}px"))
        self.setFixedHeight(box + 4)
        return size

    @staticmethod
    def _panel_show_name(panel) -> bool:
        """Рисовать ли nameLabel панели (старые конфиги без поля - True)."""
        return bool(getattr(panel, "show_name", True))

    def rebuild(self, panel, task_resolver=None) -> None:
        """Перестроить кнопки из CustomToolbarPanel.

        task_resolver(task_iid) -> (exists: bool, display: str).
        """
        old_label = getattr(self, "_name_label", None)
        self.clear()
        # clear() снимает actions, но виджет прошлого названия остается
        # ребенком тулбара - удаляем явно, иначе он висит поверх кнопок.
        if old_label is not None:
            try:
                old_label.hide()
            except Exception:
                pass
            try:
                old_label.deleteLater()
            except Exception:
                pass
        self._name_label = None
        self._name_action = None
        self._task_iids.clear()
        self.set_panel_icon_size(getattr(panel, "icon_size", None))
        if self._panel_show_name(panel):
            label = QLabel(getattr(panel, "name", ""), self)
            label.setObjectName("custom_toolbar_name")
            self._name_label = label
            self._name_action = self.addWidget(label)
            self.addSeparator()
        for btn in getattr(panel, "buttons", []) or []:
            exists, display = (
                (True, "")
                if task_resolver is None
                else task_resolver(getattr(btn, "task_iid", ""))
            )
            action = QAction(getattr(btn, "name", "") or display or "?", self)
            icon_spec = getattr(btn, "icon", "") or ""
            # Theme-иконки - через set_themed_icon (авто-refresh при смене темы)
            from src.config.custom_icons import resolve_icon_spec

            kind, value = resolve_icon_spec(icon_spec)
            if kind == "theme" and value:
                try:
                    from src.ui.icons import set_themed_icon

                    set_themed_icon(action, value)
                except Exception:
                    action.setIcon(custom_qicon(icon_spec))
            else:
                action.setIcon(custom_qicon(icon_spec))
            tip = getattr(btn, "name", "") or display
            if display and display != tip:
                tip = f"{tip} - {display}"
            if not exists:
                tip = (tip + " (команда удалена)").strip()
                try:
                    from src.ui.icons import _icon_from_key

                    if not icon_spec:
                        action.setIcon(_icon_from_key("result_warning"))
                except Exception:
                    pass
            action.setToolTip(tip)
            task_iid = getattr(btn, "task_iid", "")
            self._task_iids[action] = task_iid
            action.triggered.connect(
                lambda _checked=False, _t=task_iid: self.commandRequested.emit(_t)
            )
            self.addAction(action)
        # Контекстное меню панели - настройка (обрабатывает MainWindow)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def refresh_icons(self, panel) -> None:
        """Перечитать иконки pack/file после смены темы/пака."""
        from src.config.custom_icons import resolve_icon_spec

        spec_by_name = {
            getattr(b, "name", ""): getattr(b, "icon", "")
            for b in getattr(panel, "buttons", []) or []
        }
        for action in self.actions():
            if action.isSeparator():
                continue
            if action is getattr(self, "_name_action", None):
                continue
            try:
                if action.associatedWidgets():
                    continue
            except Exception:
                pass
            spec = spec_by_name.get(action.text(), "")
            kind, _ = resolve_icon_spec(spec)
            if kind in ("pack", "color", "file", "empty"):
                action.setIcon(custom_qicon(spec))
