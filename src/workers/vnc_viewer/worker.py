"""Рабочий поток окна VNC: соединение, декодирование кадра и отправка ввода.

Кадр хранится в ``QImage`` (формат совпадает с форматом пикселей RFB) и
меняется только под ``lock``; окно рисует его под той же блокировкой. Ввод из
GUI-потока кладётся в очередь и будит поток через пару сокетов, поэтому GUI
никогда не ждёт сеть.
"""

from __future__ import annotations

import selectors
import socket
import threading
import time
from collections import deque

from PySide6.QtCore import QRect, QThread, Signal
from PySide6.QtGui import QImage, QPainter

from src.logger import logger
from src.workers.vnc_viewer.protocol import (
    PIXEL_FORMAT_16,
    PIXEL_FORMAT_32,
    Bell,
    ClipboardText,
    CopyRect,
    CursorShape,
    ExtClipboard,
    ExtClipboardState,
    PixelFormat,
    PointerPos,
    RectData,
    Resize,
    RfbAuthError,
    RfbStreamParser,
    RfbViewerError,
    Screens,
    UpdateDone,
    default_encodings,
    handshake,
    msg_client_cut_text,
    msg_key,
    msg_pointer,
    msg_set_encodings,
    msg_set_pixel_format,
    msg_update_request,
    open_socket,
)

CONNECT_TIMEOUT_SEC = 8.0
HANDSHAKE_TIMEOUT_SEC = 15.0
SEND_TIMEOUT_SEC = 15.0
SELECT_TIMEOUT_SEC = 0.1
RECV_CHUNK = 262_144
STATS_INTERVAL_SEC = 1.0

_POINTER = "pointer"


class VncViewerWorker(QThread):
    """Одно подключение к VNC-серверу; поток завершается при обрыве или stop()."""

    connected = Signal(str, int, int)  # имя рабочего стола, ширина, высота
    resized = Signal(int, int)
    frame_updated = Signal(QRect)
    screens_changed = Signal(object)  # tuple[Screen, ...]
    cursor_changed = Signal(object, int, int)  # QImage | None, hotspot
    pointer_moved = Signal(int, int)
    clipboard_received = Signal(object)  # str
    bell = Signal()
    stats = Signal(float, float)  # кадров/с, Кбит/с
    failed = Signal(str, bool)  # сообщение, нужен пароль

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        *,
        low_color: bool = False,
        cursor_shape: bool = True,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self._password = password
        self.pixel_format: PixelFormat = PIXEL_FORMAT_16 if low_color else PIXEL_FORMAT_32
        self.image_format = (
            QImage.Format.Format_RGB16 if low_color else QImage.Format.Format_RGB32
        )
        self.lock = threading.Lock()
        self.image = QImage()
        self._out: deque[tuple[str, bytes]] = deque()
        self._out_lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._sock_lock = threading.Lock()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._clipboard = ExtClipboardState()
        self._full_update = True
        self._cursor_shape = cursor_shape
        self._bytes = 0
        self._frames = 0

    # ---------- API для GUI-потока ----------

    def stop(self) -> None:
        """Завершить соединение; не ждёт поток."""
        self._stop.set()
        self.requestInterruption()
        self._wake()
        with self._sock_lock:
            sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def send_key(self, keysym: int, down: bool) -> None:
        self._enqueue("key", msg_key(keysym, down))

    def send_pointer(self, mask: int, x: int, y: int) -> None:
        """Движение без смены кнопок заменяет предыдущее неотправленное."""
        data = msg_pointer(mask, x, y)
        with self._out_lock:
            if self._out and self._out[-1][0] == _POINTER and self._out[-1][1][1] == data[1]:
                self._out[-1] = (_POINTER, data)
            else:
                self._out.append((_POINTER, data))
        self._wake()

    def send_clipboard(self, text: str) -> None:
        self._enqueue("clipboard", text.encode("utf-8"))

    def set_cursor_shape(self, enabled: bool) -> None:
        """Форма курсора отдельно (управление) или в кадре (просмотр)."""
        if enabled == self._cursor_shape:
            return
        self._cursor_shape = enabled
        self._enqueue("encodings", msg_set_encodings(default_encodings(cursor_shape=enabled)))

    def request_full_update(self) -> None:
        self._enqueue("full", b"")

    def _enqueue(self, kind: str, data: bytes) -> None:
        with self._out_lock:
            self._out.append((kind, data))
        self._wake()

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"\x00")
        except OSError:
            pass

    # ---------- Поток ----------

    def _stopped(self) -> bool:
        return self._stop.is_set() or self.isInterruptionRequested()

    def run(self) -> None:
        try:
            self._session()
        except RfbAuthError as error:
            if not self._stopped():
                self.failed.emit(str(error), True)
        except RfbViewerError as error:
            if not self._stopped():
                self.failed.emit(str(error), False)
        except OSError as error:
            if not self._stopped():
                logger.debug(f"VncViewer: {self.host}:{self.port}: {error}")
                self.failed.emit(f"Соединение прервано: {error.strerror or error}", False)
        except Exception as error:  # noqa: BLE001 - ошибка показывается в окне
            logger.exception(f"VncViewer: {self.host}:{self.port}: внутренняя ошибка")
            if not self._stopped():
                self.failed.emit(f"Внутренняя ошибка: {error}", False)
        finally:
            with self._sock_lock:
                sock, self._sock = self._sock, None
            if sock is not None:
                sock.close()
            self._wake_r.close()
            self._wake_w.close()

    def _session(self) -> None:
        sock = open_socket(self.host, self.port, CONNECT_TIMEOUT_SEC)
        with self._sock_lock:
            self._sock = sock
        if self._stopped():
            return
        sock.settimeout(HANDSHAKE_TIMEOUT_SEC)
        init = handshake(sock, self._password)
        self._password = ""
        if self._stopped():
            return
        sock.settimeout(SEND_TIMEOUT_SEC)
        sock.sendall(msg_set_pixel_format(self.pixel_format))
        sock.sendall(msg_set_encodings(default_encodings(cursor_shape=self._cursor_shape)))
        self._resize(init.width, init.height)
        parser = RfbStreamParser(init.width, init.height, self.pixel_format)
        self.connected.emit(init.name, init.width, init.height)
        self._request_update(sock)

        selector = selectors.DefaultSelector()
        selector.register(sock, selectors.EVENT_READ, "sock")
        selector.register(self._wake_r, selectors.EVENT_READ, "wake")
        last_stats = time.monotonic()
        try:
            while not self._stopped():
                for key, _ in selector.select(SELECT_TIMEOUT_SEC):
                    if key.data == "wake":
                        self._drain_wake()
                        continue
                    data = sock.recv(RECV_CHUNK)
                    if not data:
                        raise RfbViewerError("Сервер VNC закрыл соединение")
                    self._bytes += len(data)
                    self._handle(sock, parser.feed(data))
                if self._stopped():
                    break
                self._flush(sock)
                now = time.monotonic()
                if now - last_stats >= STATS_INTERVAL_SEC:
                    elapsed = now - last_stats
                    self.stats.emit(self._frames / elapsed, self._bytes * 8 / 1000 / elapsed)
                    self._frames = 0
                    self._bytes = 0
                    last_stats = now
        finally:
            selector.close()

    def _drain_wake(self) -> None:
        try:
            while self._wake_r.recv(4096):
                pass
        except (BlockingIOError, InterruptedError):
            pass

    def _flush(self, sock: socket.socket) -> None:
        with self._out_lock:
            items = list(self._out)
            self._out.clear()
        for kind, data in items:
            if kind == "clipboard":
                text = data.decode("utf-8")
                if self._clipboard.enabled:
                    for message in self._clipboard.announce(text):
                        sock.sendall(message)
                else:
                    self._clipboard.local_text = text
                    sock.sendall(msg_client_cut_text(text))
            elif kind == "full":
                self._full_update = True
            else:
                sock.sendall(data)

    def _request_update(self, sock: socket.socket) -> None:
        image = self.image
        sock.sendall(
            msg_update_request(not self._full_update, 0, 0, image.width(), image.height())
        )
        self._full_update = False

    def _resize(self, width: int, height: int) -> None:
        image = QImage(width, height, self.image_format)
        image.fill(0)
        with self.lock:
            self.image = image
        self._full_update = True
        self.resized.emit(width, height)

    def _handle(self, sock: socket.socket, events: list[object]) -> None:
        dirty = QRect()
        for event in events:
            if isinstance(event, RectData):
                with self.lock:
                    self._write_rect(event)
                dirty = dirty.united(QRect(event.x, event.y, event.width, event.height))
            elif isinstance(event, CopyRect):
                with self.lock:
                    piece = self.image.copy(event.src_x, event.src_y, event.width, event.height)
                    painter = QPainter(self.image)
                    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
                    painter.drawImage(event.x, event.y, piece)
                    painter.end()
                dirty = dirty.united(QRect(event.x, event.y, event.width, event.height))
            elif isinstance(event, UpdateDone):
                if not dirty.isEmpty():
                    self.frame_updated.emit(dirty)
                    self._frames += 1
                dirty = QRect()
                self._request_update(sock)
            elif isinstance(event, Resize):
                self._resize(event.width, event.height)
                dirty = QRect()
            elif isinstance(event, Screens):
                self.screens_changed.emit(event.screens)
            elif isinstance(event, CursorShape):
                image = self._cursor_image(event)
                self.cursor_changed.emit(image, event.hot_x, event.hot_y)
            elif isinstance(event, PointerPos):
                self.pointer_moved.emit(event.x, event.y)
            elif isinstance(event, ClipboardText):
                self.clipboard_received.emit(event.text)
            elif isinstance(event, ExtClipboard):
                replies, text = self._clipboard.on_message(event)
                for message in replies:
                    sock.sendall(message)
                if text is not None:
                    self.clipboard_received.emit(text)
            elif isinstance(event, Bell):
                self.bell.emit()
        if not dirty.isEmpty():
            self.frame_updated.emit(dirty)

    def _write_rect(self, event: RectData) -> None:
        """Скопировать строки области в кадр; вызывать под lock."""
        image = self.image
        bpp = self.pixel_format.bytes_per_pixel
        stride = image.bytesPerLine()
        row_bytes = event.width * bpp
        bits = image.bits()
        data = event.data
        if event.x == 0 and row_bytes == stride:
            start = event.y * stride
            bits[start : start + len(data)] = data
            return
        offset = event.y * stride + event.x * bpp
        for row in range(event.height):
            bits[offset : offset + row_bytes] = data[row * row_bytes : (row + 1) * row_bytes]
            offset += stride

    def _cursor_image(self, event: CursorShape) -> QImage | None:
        """Курсор ARGB32 из пикселей и битовой маски; пустой курсор - None."""
        if not event.width or not event.height:
            return None
        bpp = self.pixel_format.bytes_per_pixel
        source = QImage(
            event.pixels, event.width, event.height, event.width * bpp, self.image_format
        ).convertToFormat(QImage.Format.Format_ARGB32)
        argb = bytearray(source.constBits().tobytes()[: event.width * event.height * 4])
        mask_stride = (event.width + 7) // 8
        for y in range(event.height):
            row = event.mask[y * mask_stride : (y + 1) * mask_stride]
            base = y * event.width * 4
            for x in range(event.width):
                visible = row[x >> 3] & (0x80 >> (x & 7))
                argb[base + x * 4 + 3] = 0xFF if visible else 0
        return QImage(
            bytes(argb), event.width, event.height, event.width * 4, QImage.Format.Format_ARGB32
        ).copy()
