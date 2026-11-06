"""
Контроллер системного трея.

Инкапсулирует создание QSystemTrayIcon, контекстного меню, рендер иконок
(online/busy) и переключение состояния по событиям EventBus
(COMMAND_STARTED/COMMAND_FINISHED/COMMAND_ABORTED).

Связь с главным окном — через сигналы ``show_requested`` и ``quit_requested``.
"""

from typing import Optional

from PySide6.QtCore import QObject, Signal, Qt, QByteArray
from PySide6.QtGui import QIcon, QAction, QPixmap, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QSystemTrayIcon, QMenu

from src.config import ICONS
from src.ui.icons import set_themed_icon
from src.logger import logger
from src.architecture import EventBus, EventType, Event


class TrayController(QObject):
    """Управление системным треем: иконка, меню, уведомления, busy-состояние."""

    show_requested = Signal()
    cameras_requested = Signal()
    quit_requested = Signal()

    def __init__(self, event_bus: EventBus, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._event_bus = event_bus
        self._busy_counter = 0
        self._tray: Optional[QSystemTrayIcon] = None
        self._icon_online: Optional[QIcon] = None
        self._icon_busy: Optional[QIcon] = None

        self._build_tray()
        self._subscribe_events()

    def _build_tray(self) -> None:
        """Создать QSystemTrayIcon, меню и подключить сигналы."""
        self._icon_online = self._render_icon(ICONS.get("online", ""))
        self._icon_busy = self._render_icon(ICONS.get("busy", ""))

        self._tray = QSystemTrayIcon(self._icon_online, None)
        self._tray.setToolTip("DNO Tool")
        self._tray.setIcon(self._icon_online)

        menu = QMenu()
        show_action = QAction("Открыть", menu)
        set_themed_icon(show_action, "icon")
        cameras_action = QAction("Камеры ЕЦХД", menu)
        set_themed_icon(cameras_action, "camera")
        quit_action = QAction("Выход", menu)
        set_themed_icon(quit_action, "menu_exit")
        menu.addAction(show_action)
        menu.addAction(cameras_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)

        show_action.triggered.connect(self.show_requested)
        cameras_action.triggered.connect(self.cameras_requested)
        quit_action.triggered.connect(self.quit_requested)
        self._tray.activated.connect(self._on_activated)

    def _subscribe_events(self) -> None:
        """Подписаться на события команд для переключения busy-состояния."""
        self._event_bus.subscribe(EventType.COMMAND_STARTED, self._on_command_started)
        self._event_bus.subscribe(EventType.COMMAND_FINISHED, self._on_command_finished)
        self._event_bus.subscribe(EventType.COMMAND_ABORTED, self._on_command_finished)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_requested.emit()

    def _on_command_started(self, event: Event) -> None:
        self._busy_counter += 1
        if self._busy_counter == 1:
            self.set_busy(True)

    def _on_command_finished(self, event: Event) -> None:
        if self._busy_counter > 0:
            self._busy_counter -= 1
        if self._busy_counter == 0:
            self.set_busy(False)

    def set_busy(self, busy: bool) -> None:
        """Переключить иконку/тултип трея в зависимости от занятости."""
        if self._tray is None:
            return
        if busy:
            self._tray.setIcon(self._icon_busy)
            self._tray.setToolTip("DNO Tool — выполнение заданий")
        else:
            self._tray.setIcon(self._icon_online)
            self._tray.setToolTip("DNO Tool")

    def is_active(self) -> bool:
        """Активен ли трей (привязан и виден)."""
        return self._tray is not None and self._tray.isVisible()

    def show(self) -> None:
        if self._tray is not None:
            self._tray.show()

    def hide(self) -> None:
        if self._tray is not None:
            self._tray.hide()

    def show_message(
        self,
        title: str,
        message: str,
        icon_type: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.MessageIcon.Information,
        msec: int = 2000,
    ) -> None:
        if self._tray is not None:
            self._tray.showMessage(title, message, icon_type, msec)

    @staticmethod
    def _render_icon(path_or_name: str) -> QIcon:
        """Растеризовать SVG в QIcon 32x32 для трея.

        Принимает абсолютный путь (из ICONS) или имя файла в icons/.
        Байты берутся из zip темы (на лету), fallback - файл с диска.
        """
        data = None
        if "/" not in path_or_name and "\\" not in path_or_name:
            name = path_or_name
        else:
            name = path_or_name.replace("\\", "/").split("/")[-1]
        if name:
            try:
                from src.config import get_themed_icon_bytes

                data = get_themed_icon_bytes(name)
            except Exception:  # noqa: BLE001
                data = None
        if data is None:
            if "/" not in path_or_name and "\\" not in path_or_name:
                from src.config import get_asset_path

                path = get_asset_path(f"icons/{path_or_name}")
            else:
                path = path_or_name
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                return QIcon(path)
        renderer = QSvgRenderer(QByteArray(data))
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        return QIcon(pixmap)

    def refresh_icons(self) -> None:
        """Перечитать иконки трея под текущую тему (с учётом busy)."""
        self._icon_online = self._render_icon(ICONS.get("online", ""))
        self._icon_busy = self._render_icon(ICONS.get("busy", ""))
        if self._tray is not None:
            self._tray.setIcon(
                self._icon_busy if self._busy_counter > 0 else self._icon_online
            )
