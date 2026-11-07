"""Постоянные VNC-сессии миниатюр с лимитами, независимые от Veyon и виджетов.

Фасад владеет подписками и освобождает только неиспользуемые сессии.
Сессии и задержки авторизации привязаны к ключу поколения без секретов.
Захваты ожидают свободного места; вытеснение пропускает активные сессии.
Резервирование учитывает framebuffer, декодер, масштабирование и кодирование,
а не только готовые PNG/JPEG. QPixmap и удалённый ввод здесь не используются.
"""

from __future__ import annotations

import struct
import threading
import time
import zlib
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage

from .rfb import RfbError, RfbLimits, RfbSession
from .types import ThumbnailRequest, ThumbnailResult, ThumbnailStatus

# Operational defaults; live instances may be configured anywhere inside the
# finite safety ranges below (approved upper bounds support 8K screens and
# larger host fleets). The defaults are never treated as absolute maxima.
MAX_SESSIONS = 32
MAX_MEMORY_BYTES = 256_000_000
MAX_THUMBNAIL_DIMENSION = 2048
MAX_IMAGE_VARIANTS = 4
MAX_SCREEN_PIXELS = 16_000_000

# Finite absolute safety bounds, deliberately distinct from the defaults above.
# "capture_timeout"/"poll_timeout"/"auth_backoff" accept int or float seconds.
_LIMIT_RANGES = {
    "max_sessions": (1, 256, int),
    "max_memory_bytes": (1, 4 * 1024**3, int),
    "capture_timeout": (0.1, 120.0, float),
    "poll_timeout": (0.01, 120.0, float),
    "auth_backoff": (0.1, 300.0, float),
    "max_image_variants": (1, 32, int),
    "max_screen_pixels": (1, 64_000_000, int),
    "max_thumbnail_dimension": (1, 4096, int),
}


def _validate_limits(**values: Any) -> None:
    """Reject unknown, mistyped (including bool) and out-of-range limits."""
    if not values:
        return
    for key, value in values.items():
        if key not in _LIMIT_RANGES:
            raise ValueError("vnc_limits")
        minimum, maximum, kind = _LIMIT_RANGES[key]
        if kind is float:
            numeric = type(value) in (int, float)
        else:
            numeric = type(value) is int
        if not numeric or not minimum <= value <= maximum:
            raise ValueError("vnc_limits")
    capture = values.get("capture_timeout")
    poll = values.get("poll_timeout")
    if capture is not None and poll is not None and poll > capture:
        raise ValueError("vnc_limits")


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(data, zlib.crc32(kind)) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def encode_png(image: QImage, compression: int) -> bytes:
    """Encode RGB PNG using the exact configured zlib level 0 through 9."""
    if type(compression) is not int or not 0 <= compression <= 9:
        raise ValueError("png_compression")
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    if image.isNull():
        raise RfbError("image_encoding")
    pixels = image.constBits()
    stride = image.bytesPerLine()
    row_bytes = image.width() * 3
    raw = bytearray((row_bytes + 1) * image.height())
    for row in range(image.height()):
        offset = row * (row_bytes + 1)
        raw[offset + 1 : offset + 1 + row_bytes] = pixels[
            row * stride : row * stride + row_bytes
        ]
    header = struct.pack(">IIBBBBB", image.width(), image.height(), 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, compression))
        + _png_chunk(b"IEND", b"")
    )


@dataclass
class _Session:
    key: tuple[object, ...]
    last_used: float
    interval_sec: int
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    client: RfbSession | None = field(default=None, repr=False)
    image_version: int = 0
    frame_time: float | None = None
    cache: OrderedDict[tuple[object, ...], tuple[bytes, str]] = field(
        default_factory=OrderedDict, repr=False
    )
    reserved: int = 0
    image_work: int = 0
    failures: int = 0
    retry_at: float = 0.0
    disconnected_at: float | None = None
    status: ThumbnailStatus = ThumbnailStatus.UNAVAILABLE
    error_code: str | None = None
    retiring: bool = False
    # Размеры запросов к сеансу и время последнего запроса каждого: по ним
    # выбирается размер кадра, который присылает сервер с расширением миниатюр.
    size_requests: dict[tuple[int, int], float] = field(
        default_factory=dict, repr=False
    )

    @property
    def memory_bytes(self) -> int:
        frame = self.client.memory_bytes if self.client else 0
        return frame + sum(len(value[0]) for value in self.cache.values())


class VncBackend:
    """Capture read-only RFB frames with finite deadlines, cache and backoff."""

    def __init__(
        self,
        *,
        max_sessions: int = MAX_SESSIONS,
        max_memory_bytes: int = MAX_MEMORY_BYTES,
        capture_timeout: float = 10.0,
        poll_timeout: float = 0.2,
        auth_backoff: float = 30.0,
        max_image_variants: int = MAX_IMAGE_VARIANTS,
        max_screen_pixels: int = MAX_SCREEN_PIXELS,
        max_thumbnail_dimension: int = MAX_THUMBNAIL_DIMENSION,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _validate_limits(
            max_sessions=max_sessions,
            max_memory_bytes=max_memory_bytes,
            capture_timeout=capture_timeout,
            poll_timeout=poll_timeout,
            auth_backoff=auth_backoff,
            max_image_variants=max_image_variants,
            max_screen_pixels=max_screen_pixels,
            max_thumbnail_dimension=max_thumbnail_dimension,
        )
        self._max_sessions = max_sessions
        self._max_memory_bytes = max_memory_bytes
        self._capture_timeout = capture_timeout
        self._poll_timeout = poll_timeout
        self._auth_backoff = auth_backoff
        self._max_image_variants = max_image_variants
        self._max_thumbnail_dimension = max_thumbnail_dimension
        self._rfb_limits = RfbLimits(max_screen_pixels=max_screen_pixels)
        self._clock = clock
        self._lock = threading.RLock()
        self._admission = threading.Condition(self._lock)
        self._waiting: deque[object] = deque()
        self._sessions: OrderedDict[tuple[object, ...], _Session] = OrderedDict()
        self._endpoints: set[tuple[str, int]] = set()
        self._shutdown = False
        self._metrics: dict[str, int | float] = {
            "captures": 0,
            "cache_hits": 0,
            "busy": 0,
            "evictions": 0,
            "errors": 0,
            "auth_failures": 0,
            "timeouts": 0,
            "protocol_errors": 0,
            "connections": 0,
            "reconnects": 0,
            "scale_requests": 0,
            "bytes_received": 0,
            "bytes_sent": 0,
            "capture_seconds": 0.0,
            "encode_seconds": 0.0,
            "last_capture_seconds": 0.0,
            "memory_peak_bytes": 0,
        }

    def is_available(self) -> bool:
        """Report dependency readiness, not remote authentication or reachability."""
        return not self._shutdown

    def _server_size(
        self, entry: _Session, request: ThumbnailRequest
    ) -> tuple[int, int]:
        """Размер кадра, который сеанс просит у сервера.

        Сеанс обслуживает несколько вариантов (список, веб, ноды), поэтому
        берётся наибольший размер среди запросов за последние
        ``3 × интервал`` (не меньше 30 с): больший вариант применяется сразу,
        а к меньшему сеанс возвращается только после выдержки, иначе варианты
        гоняли бы размер кадра туда и обратно с полной пересылкой каждый раз.
        Вызывается под ``entry.lock``.
        """
        now = self._clock()
        window = max(30.0, 3.0 * entry.interval_sec)
        entry.size_requests[(request.width, request.height)] = now
        for size, seen in list(entry.size_requests.items()):
            if now - seen > window:
                del entry.size_requests[size]
        return (
            max(width for width, _ in entry.size_requests),
            max(height for _, height in entry.size_requests),
        )

    def _request_server_size(
        self,
        entry: _Session,
        client: RfbSession,
        request: ThumbnailRequest,
        deadline: float,
    ) -> None:
        """Передать серверу размер миниатюры; без расширения ничего не делает."""
        if not client.scaling_supported:
            return
        if client.request_size(*self._server_size(entry, request), deadline):
            with self._lock:
                self._metrics["scale_requests"] += 1

    @staticmethod
    def _result(
        request: ThumbnailRequest,
        status: ThumbnailStatus,
        code: str | None = None,
        *,
        data: bytes | None = None,
        mime: str | None = None,
        from_cache: bool = False,
        frame_time: float | None = None,
    ) -> ThumbnailResult:
        return ThumbnailResult(
            data=data,
            mime=mime,
            backend="vnc",
            status=status,
            from_cache=from_cache,
            frame_time=frame_time,
            config_version=request.config_version,
            error_code=code,
        )

    def _valid(self, request: ThumbnailRequest) -> bool:
        return (
            request.backend == "vnc"
            and bool(request.host)
            and type(request.port) is int
            and 1 <= request.port <= 65535
            and all(
                type(size) is int and 1 <= size <= self._max_thumbnail_dimension
                for size in (request.width, request.height)
            )
            and request.format in ("png", "jpeg")
            and type(request.quality) is int
            and 0 <= request.quality <= 100
            and type(request.compression) is int
            and 0 <= request.compression <= 9
            and type(request.interval_sec) is int
            and 1 <= request.interval_sec <= 60
        )

    def _drop(self, entry: _Session, *, eviction: bool = False) -> None:
        """Drop an inactive session while holding the registry lock."""
        self._sessions.pop(entry.key, None)
        if entry.client is not None:
            entry.client.close()
            entry.client = None
        entry.cache.clear()
        entry.reserved = 0
        if eviction:
            self._metrics["evictions"] += 1

    def _evict_one(self, exclude: _Session | None = None) -> bool:
        for entry in list(self._sessions.values()):
            if entry is not exclude and entry.lock.acquire(blocking=False):
                try:
                    self._drop(entry, eviction=True)
                finally:
                    entry.lock.release()
                return True
        return False

    def configure(self, **values: Any) -> None:
        """Apply new limits without restarting captures or reconnecting.

        Budget and admission limits apply immediately; deadline/backoff changes
        affect the next operations only. Reconciliation releases only sessions
        whose entry lock is free (never an in-flight decoder) and defers the
        remaining over-budget usage until active captures converge.
        """
        if not values:
            return
        _validate_limits(**values)
        with self._lock:
            for key, value in values.items():
                setattr(self, f"_{key}", value)
            if "max_screen_pixels" in values:
                # A new immutable object makes existing clients switch at the
                # next safe point (their lock is held by one capture at a time).
                self._rfb_limits = RfbLimits(
                    max_screen_pixels=values["max_screen_pixels"]
                )
            self._reconcile_policy_locked()
            self._admission.notify_all()

    def _reconcile_policy_locked(self) -> None:
        """Converge idle sessions to current limits; skip locked sessions."""
        # Session-count reductions release the least recently used idle entries.
        excess = len(self._sessions) - self._max_sessions
        if excess > 0:
            dropped = 0
            for entry in list(self._sessions.values()):
                if dropped >= excess:
                    break
                if entry.lock.acquire(blocking=False):
                    try:
                        self._drop(entry, eviction=True)
                        dropped += 1
                    finally:
                        entry.lock.release()
        # Memory reductions release idle sessions until within the new budget.
        while (
            sum(value.reserved for value in self._sessions.values())
            > self._max_memory_bytes
        ):
            victim = None
            for entry in self._sessions.values():
                if entry.lock.acquire(blocking=False):
                    victim = entry
                    break
            if victim is None:
                break
            try:
                self._drop(victim, eviction=True)
            finally:
                victim.lock.release()
        for entry in self._sessions.values():
            if not entry.lock.acquire(blocking=False):
                continue
            try:
                while len(entry.cache) > self._max_image_variants:
                    entry.cache.popitem(last=False)
                    self._metrics["evictions"] += 1
                if (
                    entry.client is not None
                    and entry.client.limits is not self._rfb_limits
                ):
                    # Decoder policy changed: stop obtaining the old screen and
                    # drop its stale images instead of serving them.
                    entry.client.close()
                    entry.client = None
                    entry.cache.clear()
                    entry.image_version = -1
            finally:
                entry.lock.release()

    def _reserve(
        self, entry: _Session, amount: int, *, continuation: bool = False
    ) -> None:
        """Gate memory reservations: new allocations must fit, adjustments may not.

        A reservation decrease or an in-flight operation's workspace adjustment
        (continuation) never destroys the running decoder, even while retained
        sessions hold more than a newly reduced budget. Genuinely new
        allocations first evict idle sessions and otherwise report BUSY.
        """
        with self._lock:
            if self._shutdown or entry.retiring:
                raise RfbError("session_released", "cancelled")
            if amount > entry.reserved and not continuation:
                total = sum(value.reserved for value in self._sessions.values())
                while total - entry.reserved + amount > self._max_memory_bytes:
                    if not self._evict_one(entry):
                        raise RfbError("memory_limit", "busy")
                    total = sum(value.reserved for value in self._sessions.values())
            entry.reserved = amount
            total = sum(value.reserved for value in self._sessions.values())
            self._metrics["memory_peak_bytes"] = max(
                self._metrics["memory_peak_bytes"], total
            )

    def _cached(
        self, entry: _Session, request: ThumbnailRequest
    ) -> ThumbnailResult | None:
        if (
            entry.client is None
            or entry.disconnected_at is not None
            or entry.status != ThumbnailStatus.OK
        ):
            return None
        cached = entry.cache.get(request.image_key)
        if cached is None:
            return None
        self._metrics["cache_hits"] += 1
        return self._result(
            request,
            ThumbnailStatus.OK,
            data=cached[0],
            mime=cached[1],
            from_cache=True,
            frame_time=entry.frame_time,
        )

    @staticmethod
    def _read_current_screen(
        client: RfbSession, deadline: float, poll_timeout: float
    ) -> bool:
        """Дочитать экран до текущего состояния; True, если получено обновление.

        Сервер отвечает на открытый запрос при первом изменении экрана и копит
        следующие до нового запроса. Поэтому ответ на запрос прошлого такта устарел:
        за ним сразу идёт ещё один запрос. Большего числа проходов не нужно, а при
        постоянно меняющемся экране поток не должен ждать дедлайна.

        Пока данные поступают, чтение продолжается: иначе большой кадр растягивается
        на несколько тактов опроса и всё это время показывается старая картинка.
        """
        updated = False
        for _ in range(2):
            finished = False
            while time.monotonic() < deadline:
                received = client.bytes_received
                finished = client.poll(min(deadline, time.monotonic() + poll_timeout))
                if finished or client.bytes_received == received:
                    break
            if not finished:
                break
            updated = True
        return updated

    def capture(self, device: object, request: ThumbnailRequest) -> ThumbnailResult:
        """Capture one endpoint, waiting fairly for bounded session capacity.

        Queue time is not a network failure and does not spend the capture
        deadline. A single session can therefore refresh an entire host list.
        """
        if not self._valid(request):
            return self._result(
                request, ThumbnailStatus.PROTOCOL_ERROR, "invalid_request"
            )
        endpoint = (request.host, request.port)
        with self._lock:
            if self._shutdown:
                return self._result(request, ThumbnailStatus.CANCELLED, "shutdown")
            entry = self._sessions.get(request.session_key)
            if entry is None:
                ticket = object()
                self._waiting.append(ticket)
                try:
                    while True:
                        if self._shutdown:
                            return self._result(
                                request, ThumbnailStatus.CANCELLED, "shutdown"
                            )
                        if self._waiting[0] is ticket:
                            entry = self._sessions.get(request.session_key)
                            if entry is not None:
                                break
                            if len(self._sessions) < self._max_sessions:
                                break
                            if self._evict_one():
                                continue
                        self._admission.wait()
                finally:
                    self._waiting.remove(ticket)
                    self._admission.notify_all()
            if entry is None:
                entry = _Session(
                    request.session_key, self._clock(), request.interval_sec
                )
                self._sessions[entry.key] = entry
            entry.last_used = self._clock()
            entry.interval_sec = request.interval_sec
            self._sessions.move_to_end(entry.key)
            if entry.retiring:
                return self._result(
                    request, ThumbnailStatus.CANCELLED, "session_released"
                )
            if endpoint in self._endpoints or not entry.lock.acquire(blocking=False):
                cached = self._cached(entry, request)
                if cached is not None:
                    return cached
                self._metrics["busy"] += 1
                return self._result(request, ThumbnailStatus.BUSY)
            self._endpoints.add(endpoint)
            # Snapshot operation timing once: concurrent configure() calls must
            # not change the deadline of an already running capture.
            capture_timeout = self._capture_timeout
            poll_timeout = self._poll_timeout
            auth_backoff = self._auth_backoff
        started = time.monotonic()
        before_received = entry.client.bytes_received if entry.client else 0
        before_sent = entry.client.bytes_sent if entry.client else 0
        client = entry.client
        if client is not None and client.limits is not self._rfb_limits:
            # Decoder policy changed: this lock-held point is the safe boundary.
            client.close()
            entry.client = None
            entry.cache.clear()
            entry.image_version = -1
            client = None
        try:
            if entry.retiring:
                return self._result(
                    request, ThumbnailStatus.CANCELLED, "session_released"
                )
            if entry.retry_at > self._clock():
                return self._result(
                    request,
                    entry.status,
                    "authentication_backoff"
                    if entry.status == ThumbnailStatus.AUTH_FAILED
                    else entry.error_code,
                )
            deadline = started + capture_timeout
            image_work = 32 * request.width * request.height + 524_288
            entry.image_work = image_work
            if client is None:
                self._reserve(entry, entry.memory_bytes + image_work)

                def reserve_frame(amount: int) -> None:
                    cached_bytes = sum(len(value[0]) for value in entry.cache.values())
                    self._reserve(
                        entry,
                        amount + cached_bytes + entry.image_work,
                        continuation=True,
                    )

                client = RfbSession(
                    request.host,
                    request.port,
                    request.password,
                    reserve=reserve_frame,
                    limits=self._rfb_limits,
                    server_scaling=True,
                )
                entry.image_version = -1  # Frame versions restart on a new socket.
                entry.client = client
                client.connect(deadline)
                with self._lock:
                    self._metrics["connections"] += 1
                    if entry.failures:
                        self._metrics["reconnects"] += 1
            else:
                self._reserve(
                    entry,
                    max(
                        13 * client.width * client.height + 262_144,
                        client.memory_bytes,
                    )
                    + image_work
                    + sum(len(value[0]) for value in entry.cache.values()),
                )
            # До чтения кадра: сервер с расширением сразу пришлёт кадр нужного
            # размера, без лишней пересылки полного.
            self._request_server_size(entry, client, request, deadline)
            if not client.complete:
                while not client.complete and time.monotonic() < deadline:
                    if not client.poll(deadline):
                        break
                    # Подтверждение расширения могло прийти позже подключения.
                    self._request_server_size(entry, client, request, deadline)
                if self._shutdown or entry.retiring:
                    return self._result(
                        request, ThumbnailStatus.CANCELLED, "session_released"
                    )
                if not client.complete:
                    with self._lock:
                        self._metrics["timeouts"] += 1
                    return self._result(
                        request, ThumbnailStatus.TIMEOUT, "initial_frame"
                    )
                updated = True
            else:
                updated = self._read_current_screen(client, deadline, poll_timeout)
                if not client.complete:
                    # Сервер сменил размер кадра (запрошен другой размер миниатюры
                    # или изменился экран): дочитать новый кадр в этом же захвате.
                    while (
                        not client.complete
                        and time.monotonic() < deadline
                        and not self._shutdown
                        and not entry.retiring
                    ):
                        if not client.poll(deadline):
                            break
                    updated = updated or client.complete
            if self._shutdown or entry.retiring:
                return self._result(
                    request, ThumbnailStatus.CANCELLED, "session_released"
                )
            if (
                not updated
                and client.complete
                and entry.image_version != client.frame_version
            ):
                # Частичный ответ уже изменил пиксели, но старые закодированные
                # варианты ещё пригодны. Новый вариант требует полного ответа.
                with self._lock:
                    cached = self._cached(entry, request)
                if cached is not None:
                    return cached
                while (
                    not updated
                    and time.monotonic() < deadline
                    and not self._shutdown
                    and not entry.retiring
                ):
                    updated = client.poll(deadline)
                if self._shutdown or entry.retiring:
                    return self._result(
                        request, ThumbnailStatus.CANCELLED, "session_released"
                    )
                if not updated:
                    with self._lock:
                        self._metrics["busy"] += 1
                    return self._result(request, ThumbnailStatus.BUSY, "frame_pending")
            if updated and client.complete:
                entry.frame_time = client.frame_time
            if (
                updated
                and client.complete
                and entry.image_version != client.frame_version
            ):
                # Полный кадр остаётся только в RFB framebuffer; закодированные
                # варианты относятся к конкретной версии кадра.
                self._reserve(
                    entry,
                    max(
                        13 * client.width * client.height + 262_144,
                        client.memory_bytes,
                    )
                    + image_work
                    + sum(len(value[0]) for value in entry.cache.values()),
                    continuation=True,
                )
                entry.image_version = client.frame_version
                entry.frame_time = client.frame_time
                with self._lock:
                    entry.cache.clear()
            if not client.complete:
                # DesktopSize invalidates all previous dimensions and pixels.
                entry.image_version = -1
                with self._lock:
                    entry.cache.clear()
                return self._result(request, ThumbnailStatus.TIMEOUT, "resize_frame")
            entry.status = ThumbnailStatus.OK
            entry.error_code = None
            entry.failures = 0
            entry.retry_at = 0
            entry.disconnected_at = None
            with self._lock:
                if self._shutdown or entry.retiring:
                    return self._result(
                        request, ThumbnailStatus.CANCELLED, "session_released"
                    )
                cached = self._cached(entry, request)
            if cached is not None:
                return cached
            if entry.image_version != client.frame_version:
                return self._result(request, ThumbnailStatus.TIMEOUT, "initial_frame")
            encode_started = time.monotonic()
            # QImage оборачивает framebuffer без копирования. Блокировка entry
            # удерживается до конца масштабирования и кодирования, поэтому RFB
            # не может изменить буфер, пока Qt его читает.
            image = QImage(
                client.framebuffer,
                client.width,
                client.height,
                client.width * 4,
                QImage.Format.Format_RGB32,
            )
            if image.isNull():
                raise RfbError("image_encoding")
            image = image.scaled(
                request.width,
                request.height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            if request.format == "png":
                data, mime = encode_png(image, request.compression), "image/png"
            else:
                buffer = QBuffer()
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                # PySide's stub says bytes, but the tested binding requires str.
                if not image.save(buffer, "JPEG", request.quality):  # pyright: ignore[reportCallIssue, reportArgumentType]
                    raise RfbError("image_encoding")
                data, mime = bytes(buffer.data().data()), "image/jpeg"
                buffer.close()
            if time.monotonic() > deadline:
                raise RfbError("encode_deadline", "timeout")
            with self._lock:
                if self._shutdown or entry.retiring:
                    return self._result(
                        request, ThumbnailStatus.CANCELLED, "session_released"
                    )
                self._metrics["encode_seconds"] += time.monotonic() - encode_started
                self._metrics["captures"] += 1
                entry.cache[request.image_key] = (data, mime)
                while len(entry.cache) > self._max_image_variants:
                    entry.cache.popitem(last=False)
                    self._metrics["evictions"] += 1
            return self._result(
                request,
                ThumbnailStatus.OK,
                data=data,
                mime=mime,
                frame_time=entry.frame_time,
            )
        except (RfbError, OSError, MemoryError) as error:
            if isinstance(error, RfbError):
                code, status = error.code, ThumbnailStatus(error.status)
            elif isinstance(error, TimeoutError):
                code, status = "network_timeout", ThumbnailStatus.TIMEOUT
            elif isinstance(error, MemoryError):
                code, status = "memory_limit", ThumbnailStatus.BUSY
            elif isinstance(
                error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
            ):
                code, status = "connection_closed", ThumbnailStatus.UNAVAILABLE
            else:
                code, status = "connection_failed", ThumbnailStatus.UNAVAILABLE
            if client is not None:
                client.close()
            entry.client = None
            # A disconnected source has no displayable image. Keep only retry
            # metadata; a new socket must provide a complete new framebuffer.
            entry.image_version = -1
            entry.frame_time = None
            now = self._clock()
            if entry.disconnected_at is None:
                entry.disconnected_at = now
            entry.status, entry.error_code = status, code
            entry.failures += 1
            delay = auth_backoff if status == ThumbnailStatus.AUTH_FAILED else 1.0
            entry.retry_at = now + min(300, delay * 2 ** min(entry.failures - 1, 8))
            with self._lock:
                if self._shutdown or entry.retiring:
                    return self._result(
                        request, ThumbnailStatus.CANCELLED, "session_released"
                    )
                self._metrics["errors"] += 1
                entry.cache.clear()
                if status == ThumbnailStatus.AUTH_FAILED:
                    self._metrics["auth_failures"] += 1
                elif status == ThumbnailStatus.TIMEOUT:
                    self._metrics["timeouts"] += 1
                elif status in (
                    ThumbnailStatus.PROTOCOL_ERROR,
                    ThumbnailStatus.UNSUPPORTED,
                ):
                    self._metrics["protocol_errors"] += 1
            return self._result(request, status, code)
        finally:
            elapsed = time.monotonic() - started
            with self._lock:
                if client is not None:
                    self._metrics["bytes_received"] += max(
                        0, client.bytes_received - before_received
                    )
                    self._metrics["bytes_sent"] += max(
                        0, client.bytes_sent - before_sent
                    )
                self._metrics["capture_seconds"] += elapsed
                self._metrics["last_capture_seconds"] = elapsed
                # A suspended parser can retain decoder payloads, so its full
                # workspace stays reserved until the update is complete.
                if entry.client is None or not entry.client.pending:
                    entry.reserved = entry.memory_bytes
                self._endpoints.discard(endpoint)
                if self._shutdown or entry.retiring:
                    self._drop(entry)
                entry.lock.release()
                self._admission.notify_all()

    def release(self, session_key: tuple[object, ...]) -> None:
        """Release an unused facade session, deferring closure during capture."""
        with self._lock:
            entry = self._sessions.get(session_key)
            if entry is None:
                return
            entry.retiring = True
            if entry.lock.acquire(blocking=False):
                try:
                    self._drop(entry)
                finally:
                    entry.lock.release()
            self._admission.notify_all()

    def close_idle_sessions(self, now: float) -> None:
        """Evict idle sessions using a monotonic timestamp; skip active workers."""
        with self._lock:
            for entry in list(self._sessions.values()):
                if now - entry.last_used <= max(90, 3 * entry.interval_sec):
                    continue
                if entry.lock.acquire(blocking=False):
                    try:
                        self._drop(entry)
                    finally:
                        entry.lock.release()

    def get_metrics(self) -> dict[str, int | float]:
        """Return aggregate numbers only, never endpoints, passwords or pixels."""
        with self._lock:
            reserved = sum(entry.reserved for entry in self._sessions.values())
            session_count = len(self._sessions)
            # Separate causes: a memory overage blocks new allocations
            # (_reserve raises memory_limit/busy), while a session-count
            # overage only rejects new sessions (session_limit/busy).
            memory_over_limit = reserved > self._max_memory_bytes
            sessions_over_limit = session_count > self._max_sessions
            return dict(
                self._metrics,
                sessions=session_count,
                active_captures=len(self._endpoints),
                memory_bytes=sum(
                    entry.memory_bytes for entry in self._sessions.values()
                ),
                framebuffer_bytes=sum(
                    len(entry.client.framebuffer) if entry.client else 0
                    for entry in self._sessions.values()
                ),
                encoded_cache_bytes=sum(
                    len(value[0])
                    for entry in self._sessions.values()
                    for value in entry.cache.values()
                ),
                memory_reserved_bytes=reserved,
                server_scaled_sessions=sum(
                    1
                    for entry in self._sessions.values()
                    if entry.client is not None and entry.client.server_scaled
                ),
                max_sessions=self._max_sessions,
                max_memory_bytes=self._max_memory_bytes,
                max_screen_pixels=self._rfb_limits.max_screen_pixels,
                max_thumbnail_dimension=self._max_thumbnail_dimension,
                max_image_variants=self._max_image_variants,
                # Combined flag kept for consumers that only care that some
                # admission limit is currently exceeded.
                over_budget=memory_over_limit or sessions_over_limit,
                # While retained usage exceeds a reduced budget, new
                # allocations are rejected until active captures converge.
                allocations_blocked=memory_over_limit,
                memory_over_limit=memory_over_limit,
                sessions_over_limit=sessions_over_limit,
            )

    def shutdown(self) -> None:
        """Cancel sessions without waiting for network deadlines on the caller."""
        with self._lock:
            self._shutdown = True
            self._admission.notify_all()
            for entry in list(self._sessions.values()):
                entry.retiring = True
                if entry.lock.acquire(blocking=False):
                    try:
                        self._drop(entry)
                    finally:
                        entry.lock.release()
                elif entry.client is not None:
                    # Захват выполняется: без этого он дождётся своего дедлайна.
                    entry.client.interrupt()
