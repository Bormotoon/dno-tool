"""Окно VNC: просмотр и управление удалённым экраном без браузера.

Аналог окна Veyon для хостов с x11vnc, kmsvnc и KRFB. Возможности:
- просмотр и управление (переключается на лету);
- выбор монитора: раскладка от сервера (ExtendedDesktopSize) или по SSH;
- буфер обмена в обе стороны (Extended Clipboard UTF-8, иначе классический);
- масштаб «вписать в окно» или исходный размер с прокруткой, полный экран;
- панель управления выезжает из левого верхнего угла при наведении и сама
  прячется, как в окне Veyon (``VncControlPanel``);
- ввод по клавишам (раскладка хоста) или по символам (раскладка оператора),
  сочетания вроде Ctrl+Alt+Del из меню;
- снимок экрана, экономия трафика (16 бит), переподключение после обрыва.

Соединение держит ``VncViewerWorker`` (QThread на учёте ``track_qthread``),
окно рисует его кадр под блокировкой. Окна живут независимо от главного окна,
по одному на хост; ``close_all_vnc_viewers`` закрывает их при выходе.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QByteArray, QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QScrollArea,
    QWidget,
)

from src.logger import logger
from src.ui.dialogs.tools.vnc_viewer_panel import (
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_ERROR,
    VncControlPanel,
)
from src.ui.icons import _icon_from_key
from src.workers.vnc_viewer.keymap import KEYBOARD_PHYSICAL, KEYBOARD_SYMBOL, key_to_keysym
from src.workers.vnc_viewer.worker import VncViewerWorker

STATE_ID = "vnc_viewer"
SCREEN_DETECT_DELAY_MS = 1500
RECONNECT_DELAYS_SEC = (2, 5, 10, 20, 30)

_WINDOWS: dict[str, "VncViewerWindow"] = {}


class _ViewerStore:
    """Настройки окна в ``ViewState``: геометрия и выбранные режимы."""

    def __init__(self, view_state) -> None:
        self._view_state = view_state
        if view_state is not None:
            try:
                view_state.register(STATE_ID, persist=True)
            except Exception as error:  # noqa: BLE001 - настройки не мешают работе
                logger.debug(f"VncViewer: ViewState: {error}")
                self._view_state = None

    def get(self, key: str, default):
        if self._view_state is None:
            return default
        value = self._view_state.get(STATE_ID, key, default)
        return default if value is None else value

    def set(self, key: str, value) -> None:
        if self._view_state is None:
            return
        try:
            self._view_state.set(STATE_ID, key, value, track_change=False)
        except Exception as error:  # noqa: BLE001
            logger.debug(f"VncViewer: ViewState {key}: {error}")


class _VncView(QWidget):
    """Изображение удалённого экрана и перехват мыши/клавиатуры."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Поля - до вызовов Qt: сеттеры уже присылают события в event().
        self._worker: VncViewerWorker | None = None
        self.fit = True
        self.view_only = False
        self.keyboard_mode = KEYBOARD_PHYSICAL
        self._screen: QRect | None = None
        self._buttons = 0
        self._last_point: tuple[int, int] | None = None
        self._wheel = [0, 0]
        self._pressed: dict[int, int] = {}
        self._cursor_image: QImage | None = None
        self._cursor_hot = (0, 0)
        self._cursor_known = False
        self._cursor_scale = 0.0
        self.message = ""
        self._platform = QGuiApplication.platformName()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.setMinimumSize(160, 120)

    # ---------- Состояние ----------

    def set_worker(self, worker: VncViewerWorker | None) -> None:
        self.release_input()
        self._worker = worker
        self._last_point = None
        self._cursor_known = False
        self._cursor_image = None
        self._apply_cursor()
        self.update()

    def frame_size(self) -> QSize:
        worker = self._worker
        if worker is None:
            return QSize()
        with worker.lock:
            return worker.image.size()

    def set_screen(self, rect: QRect | None) -> None:
        self._screen = QRect(rect) if rect is not None else None
        self._cursor_scale = 0.0
        self._apply_cursor()
        self.updateGeometry()
        self.update()

    def source_rect(self) -> QRect:
        size = self.frame_size()
        full = QRect(0, 0, size.width(), size.height())
        if self._screen is not None and full.contains(self._screen):
            return self._screen
        return full

    def sizeHint(self) -> QSize:
        rect = self.source_rect()
        return rect.size() if not rect.isEmpty() else QSize(800, 600)

    def _layout(self) -> tuple[QRect, QRectF, float]:
        src = self.source_rect()
        if src.isEmpty():
            return src, QRectF(), 0.0
        if self.fit:
            scale = min(self.width() / src.width(), self.height() / src.height())
        else:
            scale = 1.0
        width, height = src.width() * scale, src.height() * scale
        x = max(0.0, (self.width() - width) / 2)
        y = max(0.0, (self.height() - height) / 2)
        return src, QRectF(x, y, width, height), scale

    def map_frame_rect(self, rect: QRect) -> QRect:
        src, target, scale = self._layout()
        if scale <= 0:
            return QRect()
        part = rect.intersected(src)
        if part.isEmpty():
            return QRect()
        mapped = QRectF(
            target.x() + (part.x() - src.x()) * scale,
            target.y() + (part.y() - src.y()) * scale,
            part.width() * scale,
            part.height() * scale,
        )
        return mapped.toAlignedRect().adjusted(-1, -1, 1, 1)

    def _to_frame(self, pos: QPointF) -> tuple[int, int] | None:
        src, target, scale = self._layout()
        if scale <= 0:
            return None
        x = src.x() + (pos.x() - target.x()) / scale
        y = src.y() + (pos.y() - target.y()) / scale
        x = min(max(int(x), src.left()), src.right())
        y = min(max(int(y), src.top()), src.bottom())
        return x, y

    # ---------- Курсор ----------

    def set_remote_cursor(self, image: QImage | None, hot_x: int, hot_y: int) -> None:
        self._cursor_known = True
        self._cursor_image = image
        self._cursor_hot = (hot_x, hot_y)
        self._cursor_scale = 0.0
        self._apply_cursor()

    def _apply_cursor(self) -> None:
        if self.view_only or not self._cursor_known:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        image = self._cursor_image
        if image is None or image.isNull():
            self.setCursor(Qt.CursorShape.BlankCursor)
            return
        _, _, scale = self._layout()
        scale = scale or 1.0
        if abs(scale - self._cursor_scale) < 0.01:
            return
        self._cursor_scale = scale
        if abs(scale - 1.0) >= 0.01:
            width = max(1, round(image.width() * scale))
            height = max(1, round(image.height() * scale))
            image = image.scaled(
                width, height,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        hot_x, hot_y = self._cursor_hot
        self.setCursor(
            QCursor(QPixmap.fromImage(image), round(hot_x * scale), round(hot_y * scale))
        )

    # ---------- Рисование ----------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_cursor()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))
        worker = self._worker
        src, target, scale = self._layout()
        if worker is not None and scale > 0:
            if scale < 0.99:  # при увеличении сглаживание только размывает текст
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            with worker.lock:
                if not worker.image.isNull():
                    painter.drawImage(target, worker.image, QRectF(src))
        if self.message:
            font = painter.font()
            font.setPointSizeF(max(font.pointSizeF(), 10.0) * 1.2)
            painter.setFont(font)
            box = self.rect().adjusted(40, 40, -40, -40)
            bounds = painter.boundingRect(
                box, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.message
            ).adjusted(-16, -10, 16, 10)
            painter.fillRect(bounds, QColor(0, 0, 0, 190))
            painter.setPen(QColor(235, 235, 235))
            painter.drawText(
                box, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.message
            )
        painter.end()

    # ---------- Ввод ----------

    def _input_enabled(self) -> bool:
        return not self.view_only and self._worker is not None and not self.message

    def release_input(self) -> None:
        """Отпустить нажатые клавиши и кнопки (потеря фокуса, смена режима)."""
        worker = self._worker
        if worker is not None:
            for keysym in reversed(list(self._pressed.values())):
                worker.send_key(keysym, False)
            if self._buttons and self._last_point is not None:
                worker.send_pointer(0, *self._last_point)
        self._pressed.clear()
        self._buttons = 0
        self._wheel = [0, 0]

    def send_combo(self, keysyms: tuple[int, ...]) -> None:
        worker = self._worker
        if worker is None or self.view_only:
            return
        for keysym in keysyms:
            worker.send_key(keysym, True)
        for keysym in reversed(keysyms):
            worker.send_key(keysym, False)

    def event(self, event: QEvent) -> bool:
        kind = event.type()
        if self._input_enabled():
            if kind == QEvent.Type.ShortcutOverride:
                # Сочетания программы не перехватывают клавиши удалённого экрана.
                event.accept()
                return True
            if kind == QEvent.Type.KeyPress:
                self._key(event, True)
                return True
            if kind == QEvent.Type.KeyRelease:
                self._key(event, False)
                return True
        return super().event(event)

    def _key(self, event: QKeyEvent, down: bool) -> None:
        worker = self._worker
        if worker is None:
            return
        ident = event.nativeScanCode() or int(event.key())
        if not down:
            if event.isAutoRepeat():
                return
            keysym = self._pressed.pop(ident, None)
        else:
            keysym = self._pressed.get(ident) if event.isAutoRepeat() else None
        if keysym is None:
            keysym = key_to_keysym(
                event.key(),
                event.text(),
                event.modifiers(),
                event.nativeScanCode(),
                self._platform,
                self.keyboard_mode,
            )
        if keysym is None:
            return
        if down:
            self._pressed[ident] = keysym
        worker.send_key(keysym, down)

    def focusOutEvent(self, event) -> None:
        self.release_input()
        super().focusOutEvent(event)

    def _mouse_mask(self, buttons: Qt.MouseButton) -> int:
        mask = 0
        if buttons & Qt.MouseButton.LeftButton:
            mask |= 1
        if buttons & Qt.MouseButton.MiddleButton:
            mask |= 2
        if buttons & Qt.MouseButton.RightButton:
            mask |= 4
        return mask

    def _send_mouse(self, event: QMouseEvent) -> None:
        if not self._input_enabled():
            return
        point = self._to_frame(event.position())
        if point is None:
            return
        self._buttons = self._mouse_mask(event.buttons())
        self._last_point = point
        self._worker.send_pointer(self._buttons, *point)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._send_mouse(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._send_mouse(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._send_mouse(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self._send_mouse(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self._input_enabled():
            return super().wheelEvent(event)
        point = self._to_frame(event.position())
        if point is None:
            return
        delta = event.angleDelta()
        self._wheel[0] += delta.x()
        self._wheel[1] += delta.y()
        # Кнопки RFB: 4/5 - вертикальная прокрутка, 6/7 - горизонтальная.
        for axis, (positive, negative) in ((1, (8, 16)), (0, (32, 64))):
            while abs(self._wheel[axis]) >= 120:
                bit = positive if self._wheel[axis] > 0 else negative
                self._wheel[axis] -= 120 if self._wheel[axis] > 0 else -120
                self._worker.send_pointer(self._buttons | bit, *point)
                self._worker.send_pointer(self._buttons, *point)
        event.accept()


class _Stage(QWidget):
    """Область окна: изображение на всю площадь, панель поверх него."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scroll: QScrollArea | None = None
        self.panel = None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.scroll is not None:
            self.scroll.setGeometry(self.rect())
        if self.panel is not None:
            self.panel.relayout()


class VncViewerWindow(QMainWindow):
    """Окно одного хоста: соединение и выезжающая панель управления."""

    def __init__(self, device, mode: str, view_state=None) -> None:
        super().__init__(None)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowIcon(_icon_from_key("menu_vnc"))
        self._device = device
        self._iid = getattr(device, "iid", "") or device.host
        self._host = device.host
        self._title_name = (getattr(device, "name", "") or "").strip() or self._host
        from src.config import config
        from src.services.remote.krfb_service import KRFBService

        self._port = KRFBService.resolve_port(device)
        self._password = getattr(config.app.veyon, "vnc_password", "") or ""
        self._store = _ViewerStore(view_state)
        self._worker: VncViewerWorker | None = None
        self._control = mode != "view"
        self._connected = False
        self._ever_connected = False
        self._desktop_name = ""
        self._reconnect_attempt = 0
        self._reconnect_deadline = 0.0
        self._reconnect_message = ""
        self._server_screens: list[QRect] = []
        self._detected_screens: list[QRect] = []
        self._screen_detect_size: QSize | None = None
        self._current_screen: QRect | None = None
        self._last_remote_clip = ""
        self._last_sent_clip = ""
        self._clipboard = bool(self._store.get("clipboard", True))
        self._closing = False

        self._view = _VncView()
        self._view.fit = bool(self._store.get("fit", True))
        mode_value = self._store.get("keyboard", KEYBOARD_PHYSICAL)
        self._view.keyboard_mode = (
            mode_value if mode_value in (KEYBOARD_PHYSICAL, KEYBOARD_SYMBOL) else KEYBOARD_PHYSICAL
        )
        self._low_color = bool(self._store.get("low_color", False))

        stage = _Stage()
        self._scroll = QScrollArea(stage)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet("QScrollArea { background: black; }")
        self._scroll.setWidget(self._view)
        stage.scroll = self._scroll
        self.panel = VncControlPanel(stage)
        stage.panel = self.panel
        self.setCentralWidget(stage)
        self._connect_panel()

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(1000)
        self._reconnect_timer.timeout.connect(self._reconnect_tick)
        self._screen_timer = QTimer(self)
        self._screen_timer.setSingleShot(True)
        self._screen_timer.timeout.connect(self._detect_screens)

        QApplication.clipboard().dataChanged.connect(self._on_local_clipboard)

        self._apply_scale_mode()
        self.set_mode(mode)
        geometry = self._store.get("geometry", None)
        if isinstance(geometry, QByteArray) and not geometry.isEmpty():
            self.restoreGeometry(geometry)
        else:
            self.resize(1280, 820)
        self._connect()

    # ---------- Панель ----------

    def _connect_panel(self) -> None:
        panel = self.panel
        panel.set_title(self._title_name)
        panel.set_fit(self._view.fit)
        panel.set_keyboard_mode(self._view.keyboard_mode)
        panel.set_low_color(self._low_color)
        panel.set_clipboard(self._clipboard)
        panel.set_screens([], None)
        panel.mode_changed.connect(self._on_mode_requested)
        panel.screen_selected.connect(self._on_screen_selected)
        panel.fit_changed.connect(self._on_fit_changed)
        panel.combo_requested.connect(self._send_combo)
        panel.clipboard_toggled.connect(self._on_clipboard_toggled)
        panel.screenshot_requested.connect(self._save_screenshot)
        panel.fullscreen_toggled.connect(self._on_fullscreen_toggled)
        panel.keyboard_mode_changed.connect(self._set_keyboard_mode)
        panel.low_color_changed.connect(self._set_low_color)
        panel.reconnect_requested.connect(self._manual_reconnect)
        panel.close_requested.connect(self.close)

    def _focus_view(self) -> None:
        self._view.setFocus(Qt.FocusReason.OtherFocusReason)

    # ---------- Режимы ----------

    def set_mode(self, mode: str) -> None:
        """mode: "control" или "view"."""
        control = mode != "view"
        self._control = control
        self.panel.set_mode("control" if control else "view")
        self._view.release_input()
        self._view.view_only = not control
        self._view._cursor_scale = 0.0
        self._view._apply_cursor()
        if self._worker is not None and self._connected:
            self._worker.set_cursor_shape(control)
            if control:
                self._send_local_clipboard(force=True)
        self._update_title()

    def _on_mode_requested(self, mode: str) -> None:
        self.set_mode(mode)
        self._focus_view()

    def _on_fit_changed(self, fit: bool) -> None:
        self._view.fit = fit
        self._store.set("fit", fit)
        self.panel.set_fit(fit)
        self._apply_scale_mode()
        self._focus_view()

    def _apply_scale_mode(self) -> None:
        if self._view.fit:
            self._scroll.setWidgetResizable(True)
        else:
            self._scroll.setWidgetResizable(False)
            self._view.resize(self._view.sizeHint())
        self._view._cursor_scale = 0.0
        self._view._apply_cursor()
        self._view.update()

    def _set_keyboard_mode(self, mode: str) -> None:
        self._view.release_input()
        self._view.keyboard_mode = mode
        self._store.set("keyboard", mode)
        self.panel.set_keyboard_mode(mode)
        self._focus_view()

    def _set_low_color(self, low: bool) -> None:
        self.panel.set_low_color(low)
        if low == self._low_color:
            return
        self._low_color = low
        self._store.set("low_color", low)
        self._connect()

    def _on_clipboard_toggled(self, enabled: bool) -> None:
        self._clipboard = enabled
        self._store.set("clipboard", enabled)
        if enabled:
            self._send_local_clipboard(force=True)
        self._focus_view()

    def _on_fullscreen_toggled(self, checked: bool) -> None:
        if checked:
            self.showFullScreen()
        else:
            self.showNormal()
        self._focus_view()

    def _send_combo(self, keysyms) -> None:
        self._view.send_combo(tuple(keysyms))
        self._focus_view()

    def _update_title(self) -> None:
        suffix = "" if self._control else " — просмотр"
        self.setWindowTitle(f"VNC — {self._title_name}{suffix}")

    # ---------- Соединение ----------

    def _connect(self) -> None:
        from src.workers.qthread_registry import track_qthread

        self._stop_worker()
        self._reconnect_timer.stop()
        worker = VncViewerWorker(
            self._host,
            self._port,
            self._password,
            low_color=self._low_color,
            cursor_shape=self._control,
        )
        track_qthread(worker, f"Окно VNC: {self._host}")
        worker.connected.connect(self._on_connected)
        worker.resized.connect(self._on_resized)
        worker.frame_updated.connect(self._on_frame)
        worker.screens_changed.connect(self._on_server_screens)
        worker.cursor_changed.connect(self._on_cursor)
        worker.clipboard_received.connect(self._on_remote_clipboard)
        worker.bell.connect(QApplication.beep)
        worker.stats.connect(self._on_stats)
        worker.failed.connect(self._on_failed)
        self._worker = worker
        self._connected = False
        self._server_screens = []
        self._view.set_worker(worker)
        text = f"Подключение к {self._host}:{self._port}…"
        self._set_message(text)
        self.panel.set_status(text, STATE_CONNECTING)
        worker.start()

    def _stop_worker(self) -> None:
        worker, self._worker = self._worker, None
        self._connected = False
        if worker is not None:
            self._view.set_worker(None)
            worker.stop()

    def _manual_reconnect(self) -> None:
        self._reconnect_attempt = 0
        self._connect()

    def _is_current(self) -> bool:
        return self.sender() is not None and self.sender() is self._worker

    def _set_message(self, text: str) -> None:
        self._view.message = text
        self._view.update()

    def _on_connected(self, name: str, width: int, height: int) -> None:
        if not self._is_current():
            return
        self._connected = True
        self._ever_connected = True
        self._reconnect_attempt = 0
        self._desktop_name = name
        self._set_message("")
        self.panel.set_status(f"Подключено, {self._host}:{self._port}", STATE_CONNECTED)
        self.panel.set_info(f"{width}×{height}")
        self._worker.set_cursor_shape(self._control)
        self._send_local_clipboard(force=True)
        self._focus_view()
        if not self._server_screens:
            self._screen_timer.start(SCREEN_DETECT_DELAY_MS)

    def _on_resized(self, width: int, height: int) -> None:
        if not self._is_current():
            return
        if self._screen_detect_size is not None and self._screen_detect_size != QSize(width, height):
            self._detected_screens = []
            self._screen_detect_size = None
            if not self._server_screens:
                self._screen_timer.start(SCREEN_DETECT_DELAY_MS)
        self._refresh_screens()
        self._apply_scale_mode()

    def _on_frame(self, rect: QRect) -> None:
        if not self._is_current():
            return
        mapped = self._view.map_frame_rect(rect)
        if not mapped.isEmpty():
            self._view.update(mapped)

    def _on_cursor(self, image, hot_x: int, hot_y: int) -> None:
        if self._is_current():
            self._view.set_remote_cursor(image, hot_x, hot_y)

    def _on_stats(self, fps: float, kbps: float) -> None:
        if not self._is_current():
            return
        size = self._view.frame_size()
        traffic = f"{kbps / 1000:.1f} Мбит/с" if kbps >= 1000 else f"{kbps:.0f} Кбит/с"
        self.panel.set_info(f"{size.width()}×{size.height()} · {fps:.0f} к/с · {traffic}")

    def _on_failed(self, message: str, need_password: bool) -> None:
        if not self._is_current():
            return
        was_connected = self._connected
        self._stop_worker()
        self.panel.set_status(message, STATE_ERROR)
        if need_password:
            self._set_message(message)
            QTimer.singleShot(0, self, lambda: self._ask_password(message))
            return
        if was_connected or self._ever_connected:
            delay = RECONNECT_DELAYS_SEC[min(self._reconnect_attempt, len(RECONNECT_DELAYS_SEC) - 1)]
            self._reconnect_attempt += 1
            self._reconnect_deadline = time.monotonic() + delay
            self._reconnect_message = message
            self._reconnect_tick()
            self._reconnect_timer.start()
        else:
            self._set_message(f"{message}\n\nНажмите «Переподключить» на панели вверху.")

    def _reconnect_tick(self) -> None:
        remaining = self._reconnect_deadline - time.monotonic()
        if remaining <= 0:
            self._reconnect_timer.stop()
            self._connect()
            return
        self._set_message(
            f"{self._reconnect_message}\n\nПереподключение через {int(remaining + 0.99)} с…"
        )

    def _ask_password(self, message: str) -> None:
        if self._closing:
            return
        password, ok = QInputDialog.getText(
            self,
            "Пароль VNC",
            f"{message}.\nПароль VNC для {self._host}:",
            QLineEdit.EchoMode.Password,
        )
        if ok and password:
            self._password = password
            self._connect()
        else:
            self._set_message(f"{message}\n\nНажмите «Переподключить», чтобы ввести пароль.")

    # ---------- Экраны ----------

    def _on_server_screens(self, screens) -> None:
        if not self._is_current():
            return
        self._server_screens = [QRect(s.x, s.y, s.width, s.height) for s in screens]
        if len(self._server_screens) > 1:
            self._screen_timer.stop()
        self._refresh_screens()

    def _detect_screens(self) -> None:
        """Раскладка по SSH, если сервер не сообщил несколько мониторов."""
        from src.ui.widgets.device_tree import LOCAL_NODE_IID

        node_iid = getattr(self._device, "node_iid", None)
        if not self._connected or len(self._server_screens) > 1 or (node_iid and node_iid != LOCAL_NODE_IID):
            return
        size = self._view.frame_size()
        if size.isEmpty() or size == self._screen_detect_size:
            return
        self._screen_detect_size = QSize(size)
        from src.services.remote.vnc_screens import detect_screens
        from src.workers.gui_relay import post_to_gui
        from src.workers.network.worker_pool import get_service_worker_pool

        apply = self._apply_detected_screens
        device, host, width, height = self._device, self._host, size.width(), size.height()

        def complete(_task_id: str, rects: object) -> None:
            post_to_gui(self, apply, rects, width, height)

        def failed(_task_id: str, error: str) -> None:
            logger.debug(f"VncViewer: {host}: экраны не определены: {error}")

        try:
            get_service_worker_pool().submit(
                lambda: detect_screens(device, width, height),
                on_complete=complete,
                on_error=failed,
                metadata={"type": "vnc_screens"},
            )
        except RuntimeError as error:
            logger.debug(f"VncViewer: пул недоступен: {error}")

    def _apply_detected_screens(self, rects: object, width: int, height: int) -> None:
        if self._view.frame_size() != QSize(width, height):
            return
        self._detected_screens = [QRect(*rect) for rect in (rects or [])]
        self._refresh_screens()

    def available_screens(self) -> list[QRect]:
        screens = self._server_screens if len(self._server_screens) > 1 else self._detected_screens
        size = self._view.frame_size()
        full = QRect(0, 0, size.width(), size.height())
        screens = [rect for rect in screens if full.contains(rect)]
        return screens if len(screens) > 1 else []

    def _refresh_screens(self) -> None:
        screens = self.available_screens()
        if self._current_screen not in screens:
            self._current_screen = None
        self.panel.set_screens(screens, self._current_screen)
        self._view.set_screen(self._current_screen)
        self._apply_scale_mode()

    def _on_screen_selected(self, rect) -> None:
        self._current_screen = QRect(rect) if isinstance(rect, QRect) else None
        self._view.set_screen(self._current_screen)
        self._apply_scale_mode()
        self._focus_view()

    # ---------- Буфер обмена ----------

    def _on_remote_clipboard(self, text: str) -> None:
        if not self._is_current() or not self._clipboard or not text:
            return
        self._last_remote_clip = text
        self._last_sent_clip = text
        QApplication.clipboard().setText(text)

    def _on_local_clipboard(self) -> None:
        self._send_local_clipboard(force=False)

    def _send_local_clipboard(self, force: bool) -> None:
        if self._worker is None or not self._connected or not self._clipboard or not self._control:
            return
        text = QApplication.clipboard().text()
        if not text or (not force and text in (self._last_remote_clip, self._last_sent_clip)):
            return
        if force and text == self._last_remote_clip:
            return
        self._last_sent_clip = text
        self._worker.send_clipboard(text)

    # ---------- Снимок ----------

    def _save_screenshot(self) -> None:
        worker = self._worker
        if worker is None:
            return
        rect = self._view.source_rect()
        with worker.lock:
            if worker.image.isNull():
                return
            image = worker.image.copy(rect)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_host = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in self._host)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить снимок экрана",
            f"vnc-{safe_host}-{stamp}.png",
            "PNG (*.png);;JPEG (*.jpg *.jpeg)",
        )
        if path:
            self.panel.set_info(
                "Снимок сохранён" if image.save(path) else "Не удалось сохранить снимок"
            )
        self._focus_view()

    # ---------- Закрытие ----------

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.panel.set_fullscreen(bool(self.windowState() & Qt.WindowState.WindowFullScreen))

    def closeEvent(self, event) -> None:
        self._closing = True
        self._reconnect_timer.stop()
        self._screen_timer.stop()
        try:
            QApplication.clipboard().dataChanged.disconnect(self._on_local_clipboard)
        except (RuntimeError, TypeError):
            pass
        if not self.isFullScreen():
            self._store.set("geometry", self.saveGeometry())
        self._view.release_input()
        self._stop_worker()
        if _WINDOWS.get(self._iid) is self:
            _WINDOWS.pop(self._iid, None)
        super().closeEvent(event)


def open_vnc_viewer(device, mode: str = "control", view_state=None) -> VncViewerWindow:
    """Открыть окно VNC хоста или поднять уже открытое (с новым режимом)."""
    key = getattr(device, "iid", "") or device.host
    window = _WINDOWS.get(key)
    if window is not None:
        try:
            window.set_mode(mode)
            if window.isMinimized():
                window.showNormal()
            window.raise_()
            window.activateWindow()
            return window
        except RuntimeError:
            _WINDOWS.pop(key, None)
    window = VncViewerWindow(device, mode, view_state)
    _WINDOWS[key] = window
    window.show()
    return window


def close_all_vnc_viewers() -> None:
    """Закрыть все окна VNC (выход из программы)."""
    for window in list(_WINDOWS.values()):
        try:
            window.close()
        except RuntimeError:
            pass
    _WINDOWS.clear()
