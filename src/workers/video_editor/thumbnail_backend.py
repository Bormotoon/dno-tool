"""Ограниченный общий пул и неизменяемые производные медиакэша."""

from __future__ import annotations

import tempfile
import threading
import time
import uuid
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from src.domain.video.media_cache import MediaDiskCache
from src.domain.video.output_lease import OutputLease
from .probe_worker import shutdown_probe_pool
from src.services.media import get_media_service, OperationContext
from src.services.media.backend import frame_to_qimage
from src.utils.system.pool_registry import QtPoolSnapshot, register_pool
from src.workers.gui_relay import post_to_gui

WAVE_W, WAVE_H, WAVE_TIMEOUT = 600, 80, 300
STRIP_H, STRIP_N, STRIP_FRAME_TIMEOUT = 36, 6, 20
PREVIEW_PARALLEL, MAX_PREVIEW_TASKS = 2, 64
_preview_lock = threading.RLock()
_preview_pool: QThreadPool | None = None
_preview_tasks: dict[tuple[threading.Event, str, str], _ThumbTask] = {}
_preview_closing = False


def _get_preview_pool() -> QThreadPool | None:
    global _preview_pool
    with _preview_lock:
        if _preview_pool is None and not _preview_closing:
            _preview_pool = QThreadPool()
            _preview_pool.setMaxThreadCount(PREVIEW_PARALLEL)
            register_pool(QtPoolSnapshot(_preview_pool, "Превью FFmpeg"))
        return _preview_pool


def _preview_done(task: _ThumbTask) -> None:
    with _preview_lock:
        _preview_tasks.pop((task._owner_cancel, task._key, task._mode), None)


def shutdown_media_pools(wait_ms: int = 2000) -> bool:
    """Закрыть общие пулы с единым ограниченным временем ожидания."""
    global _preview_closing
    deadline = time.monotonic() + max(0, wait_ms) / 1000
    with _preview_lock:
        _preview_closing = True
        pool = _preview_pool
        for task in _preview_tasks.values():
            task._cancelled.set()
    probes_done = shutdown_probe_pool(max(0, int((deadline - time.monotonic()) * 1000)))
    previews_done = pool is None or pool.waitForDone(
        max(0, int((deadline - time.monotonic()) * 1000))
    )
    return probes_done and previews_done


class ThumbSignals(QObject):
    ready = Signal(str, str, str)
    failed = Signal(str, str, str)


@dataclass
class MediaRequest:
    token: str
    key: str
    source: str
    mode: str
    position: float
    extra: dict
    fingerprint: tuple | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)


class _Cancellation:
    def __init__(self, owner: threading.Event, request: threading.Event) -> None:
        self.owner, self.request = owner, request

    def is_set(self) -> bool:
        return self.owner.is_set() or self.request.is_set()

    def set(self) -> None:
        self.request.set()


class _ThumbTask(QRunnable):
    def __init__(
        self,
        key: str,
        src: str,
        t: float,
        signals: ThumbSignals,
        mode: str = "frame",
        extra: dict | None = None,
        cancelled: threading.Event | None = None,
        finished: Callable | None = None,
        request: MediaRequest | None = None,
        cache: MediaDiskCache | None = None,
        owner: str = "",
    ) -> None:
        super().__init__()
        self._key, self._src, self._t, self._signals = key, src, t, signals
        self._mode, self._extra, self._finished = mode, dict(extra or {}), finished
        self._owner_cancel = cancelled if cancelled is not None else threading.Event()
        self._request = request
        self._cancelled = (
            _Cancellation(self._owner_cancel, request.cancelled)
            if request
            else self._owner_cancel
        )
        self._cache = cache or MediaDiskCache()
        self._owner = owner or uuid.uuid4().hex
        self._lease_delivered = False

    def run(self) -> None:
        try:
            if not self._cancelled.is_set():
                self._run()
        except InterruptedError:
            pass
        except Exception as error:
            self._emit_failed(str(error))
        finally:
            try:
                if self._cancelled.is_set() or not self._lease_delivered:
                    self._cache.release(token=self._key)
            finally:
                if self._finished is not None:
                    self._finished(self)

    def _run(self) -> None:
        fingerprint = self._cache.fingerprint(self._src)
        service = get_media_service()
        key = self._cache.key(
            fingerprint, self._mode, self._t, self._extra, (service.identity,)
        )
        if self._request:
            self._request.fingerprint = fingerprint
        path = self._cache.borrow(key, self._key, self._owner)
        if path is None:
            self._cache.root.mkdir(parents=True, exist_ok=True)
            with OutputLease(self._cache.path(key)), tempfile.TemporaryDirectory(
                prefix=f".job-{os.getpid()}-{self._key}-", dir=self._cache.root
            ) as job:
                staging = Path(job) / "result.png"
                context = OperationContext.with_timeout(
                    WAVE_TIMEOUT if self._mode == "wave" else STRIP_FRAME_TIMEOUT,
                    self._cancelled,
                )
                if self._mode == "strip":
                    self._strip(staging, context)
                elif self._mode == "wave":
                    service.waveform(
                        self._src,
                        str(staging),
                        int(self._extra.get("stream", 0)),
                        context,
                    )
                else:
                    service.frame_png(
                        self._src,
                        self._t,
                        str(staging),
                        int(self._extra.get("stream", 0)),
                        context,
                        filter_chain="scale=160:160:force_original_aspect_ratio=decrease",
                    )
                OperationContext(cancelled=self._cancelled).check()
                if self._cache.fingerprint(self._src) != fingerprint:
                    raise ValueError("Исходник изменился при подготовке миниатюры.")
                path = self._cache.publish(key, staging, self._key, self._owner)
        OperationContext(cancelled=self._cancelled).check()
        if self._cache.fingerprint(self._src) != fingerprint:
            raise ValueError("Исходник изменился при подготовке миниатюры.")
        self._lease_delivered = True
        signals, token, mode, cache = self._signals, self._key, self._mode, self._cache
        # Окно могло закрыться: получателя проверит ретранслятор в GUI-потоке, а аренду,
        # которую некому подтвердить, вернёт отдельная задача.
        post_to_gui(
            signals,
            lambda: signals.ready.emit(token, path, mode),
            on_dropped=lambda: _release_lease(cache, token),
        )

    def _strip(self, output: Path, context: OperationContext) -> None:
        from PySide6.QtGui import QImage, QPainter

        times = [float(value) for value in self._extra.get("times", [self._t])][
            :STRIP_N
        ]
        stream = int(self._extra.get("stream", 0))
        orientation = str(self._extra.get("orientation", ""))
        frames = []
        for position in times:
            context.check()
            try:
                frame = get_media_service().frame(
                    self._src,
                    position,
                    stream,
                    context,
                    filter_chain=f"{orientation}scale=120:{STRIP_H}:force_original_aspect_ratio=decrease,pad=120:{STRIP_H}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                )
                frames.append(frame_to_qimage(frame))
            except InterruptedError:
                raise
            except Exception:
                continue
        if not frames:
            raise ValueError("Кадры ленты не извлеклись.")
        canvas = QImage(120 * len(frames), STRIP_H, QImage.Format.Format_RGB32)
        canvas.fill(0)
        painter = QPainter(canvas)
        try:
            for number, frame in enumerate(frames):
                painter.drawImage(number * 120, 0, frame)
        finally:
            painter.end()
        if not canvas.save(str(output)):
            raise ValueError("Не удалось сохранить ленту.")

    def _emit_failed(self, message: str) -> None:
        if not self._cancelled.is_set():
            signals, token, mode, text = (
                self._signals,
                self._key,
                self._mode,
                message[:300],
            )
            post_to_gui(signals, lambda: signals.failed.emit(token, mode, text))


class _ReleaseTask(QRunnable):
    def __init__(
        self, cache: MediaDiskCache, token: str, owner: str, finished: Callable
    ) -> None:
        super().__init__()
        self.cache, self.token, self.owner, self.finished = (
            cache,
            token,
            owner,
            finished,
        )

    def run(self) -> None:
        try:
            self.cache.release(token=self.token, owner=self.owner)
        finally:
            self.finished()


def _release_lease(cache: MediaDiskCache, token: str) -> None:
    """Вернуть аренду из GUI-потока: сама запись в базу кэша идёт в пуле."""
    pool = _get_preview_pool()
    if pool is None:
        # Пул не создавался и уже не будет создан: идёт завершение программы.
        cache.release(token=token)
        return
    pool.start(_ReleaseTask(cache, token, "", lambda: None), 20)


class ThumbnailPool(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.signals = ThumbSignals(self)
        self._pool = _get_preview_pool()
        self._cancelled = threading.Event()
        self._owner = uuid.uuid4().hex
        self._cache = MediaDiskCache()
        self._requests: dict[str, MediaRequest] = {}
        self._latest: dict[tuple[str, str], str] = {}
        self._cleanup = 0
        self.destroyed.connect(self._cancelled.set)

    def request(
        self,
        key: str,
        src: str,
        t: float = 0,
        mode: str = "frame",
        extra: dict | None = None,
    ) -> bool:
        with _preview_lock:
            if self._cancelled.is_set() or _preview_closing or self._pool is None:
                return False
            previous = self._requests.get(self._latest.get((key, mode), ""))
            if previous and (previous.source, previous.position, previous.extra) == (
                src,
                t,
                dict(extra or {}),
            ):
                return True
            if len(_preview_tasks) >= MAX_PREVIEW_TASKS or (
                mode == "wave"
                and any(task._mode == "wave" for task in _preview_tasks.values())
            ):
                return False
            if previous:
                previous.cancelled.set()
                self._requests.pop(previous.token, None)
                self._release(token=previous.token)
            token = uuid.uuid4().hex
            request = MediaRequest(token, key, src, mode, t, dict(extra or {}))
            self._latest[(key, mode)] = token
            self._requests[token] = request
            task = _ThumbTask(
                token,
                src,
                t,
                self.signals,
                mode,
                extra,
                self._cancelled,
                _preview_done,
                request,
                self._cache,
                self._owner,
            )
            _preview_tasks[(self._cancelled, token, mode)] = task
            self._pool.start(task, {"frame": 10, "strip": 5, "wave": -5}.get(mode, 0))
            return True

    def resolve(self, token: str, mode: str) -> MediaRequest | None:
        with _preview_lock:
            request = self._requests.get(token)
            return (
                request
                if request and self._latest.get((request.key, mode)) == token
                else None
            )

    def acknowledge(self, token: str) -> None:
        with _preview_lock:
            request = self._requests.pop(token, None)
            if request and self._latest.get((request.key, request.mode)) == token:
                self._latest.pop((request.key, request.mode), None)
            self._release(token=token)

    def invalidate(self, key: str) -> None:
        with _preview_lock:
            for request in list(self._requests.values()):
                if request.key == key:
                    request.cancelled.set()
                    self.acknowledge(request.token)

    def _release(self, token: str = "", owner: str = "") -> None:
        if self._pool is not None:
            self._cleanup += 1
            self._pool.start(
                _ReleaseTask(self._cache, token, owner, self._release_done), 20
            )

    def _release_done(self) -> None:
        with _preview_lock:
            self._cleanup -= 1

    def close(self) -> None:
        with _preview_lock:
            if self._cancelled.is_set():
                return
            self._cancelled.set()
            self._requests.clear()
            self._latest.clear()
            self._release(owner=self._owner)

    @property
    def pending_count(self) -> int:
        with _preview_lock:
            return (
                self._cleanup
                + sum(owner is self._cancelled for owner, _, _ in _preview_tasks)
                + len(self._requests)
            )


def thumb_path(key: str, mode: str = "frame") -> str:
    """Совместимый путь старого кэша; новые производные адресуются fingerprint."""
    return str(Path(tempfile.gettempdir()) / f"dno_{mode}_{key}.png")
