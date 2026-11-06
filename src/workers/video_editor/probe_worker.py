"""Фоновый анализ медиафайлов: общий пул PyAV, лениво.

Несколько файлов анализируются параллельно в общем пуле процесса,
сигналы идут в GUI-поток по мере готовности.
"""

from __future__ import annotations

from .metadata import ProbeMetadata

import threading
import weakref
from collections import OrderedDict
from concurrent.futures import Future

from PySide6.QtCore import QObject, Signal, Slot, QTimer, Qt

from src.logger import logger
from src.workers.tracked_pool import TrackedThreadPoolExecutor

from .ffmpeg_utils import probe_media

PARALLEL = 3
MAX_PROBE_SUBMITTED = 24
MAX_OWNER_PROBE_SUBMITTED = 8
MAX_PROBE_PENDING = 4096
_probe_lock = threading.RLock()
_probe_condition = threading.Condition(_probe_lock)
_probe_pool: TrackedThreadPoolExecutor | None = None
_probe_futures: set[Future[None]] = set()
_probe_closing = False
_probe_owners: weakref.WeakSet[ProbeWorker] = weakref.WeakSet()


def _get_probe_pool() -> TrackedThreadPoolExecutor | None:
    global _probe_pool
    with _probe_lock:
        if _probe_closing:
            return None
        if _probe_pool is None:
            _probe_pool = TrackedThreadPoolExecutor(
                max_workers=PARALLEL,
                thread_name_prefix="dno-probe",
                label="Анализ медиафайлов",
            )
        return _probe_pool


def shutdown_probe_pool(wait_ms: int = 2000) -> bool:
    """Stop submissions and cancel queued probes, waiting at most wait_ms."""
    global _probe_closing
    with _probe_lock:
        _probe_closing = True
        pool = _probe_pool
        owners = tuple(_probe_owners)
    for owner in owners:
        owner.close()
    # Cancellation invokes callbacks, so never hold the shared lock here.
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)
    with _probe_condition:
        return _probe_condition.wait_for(
            lambda: not _probe_futures, timeout=max(0, wait_ms) / 1000
        )


class ProbeWorker(QObject):
    """Очередь файлов, PARALLEL библиотечных заданий одновременно."""

    probed = Signal(str, object)  # iid, metadata
    failed = Signal(str, str)  # iid, error
    _queue_ready = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = _get_probe_pool()
        self._lock = threading.RLock()
        self._futures: set[Future[None]] = set()
        self._closing = False
        self._cancelled = threading.Event()
        self._pending: OrderedDict[str, str] = OrderedDict()
        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(100)
        self._retry_timer.timeout.connect(self._drain)
        self._queue_ready.connect(self._drain, Qt.ConnectionType.QueuedConnection)
        with _probe_lock:
            _probe_owners.add(self)

    @Slot(str, str)
    def probe(self, iid: str, path: str) -> None:
        with self._lock, _probe_lock:
            if self._closing or _probe_closing or self._pool is None:
                return
            if len(self._pending) >= MAX_PROBE_PENDING:
                self._emit_result(iid, None, "Слишком много файлов ожидают анализа.")
                return
            self._pending[iid] = path
        self._drain()

    @Slot()
    def _drain(self) -> None:
        with self._lock, _probe_lock:
            if self._closing or _probe_closing or self._pool is None:
                self._retry_timer.stop()
                return
            while (
                self._pending
                and len(self._futures) < MAX_OWNER_PROBE_SUBMITTED
                and len(_probe_futures) < MAX_PROBE_SUBMITTED
            ):
                iid, path = self._pending.popitem(last=False)
                future = self._pool.submit(
                    self._probe_one,
                    iid,
                    path,
                    self._cancelled,
                    metadata={"type": "media_probe"},
                )
                self._futures.add(future)
                _probe_futures.add(future)
                future.add_done_callback(self._probe_done)
            if self._pending:
                self._retry_timer.start()
            else:
                self._retry_timer.stop()

    def _probe_done(self, future: Future[None]) -> None:
        with self._lock:
            self._futures.discard(future)
        with _probe_condition:
            _probe_futures.discard(future)
            _probe_condition.notify_all()
        # close() берёт ту же блокировку, а объект удаляется только после него.
        with self._lock:
            if self._closing:
                return
            try:
                self._queue_ready.emit()
            except RuntimeError:
                pass

    def _emit_result(
        self, iid: str, meta: ProbeMetadata | None, error: str = ""
    ) -> None:
        with self._lock, _probe_lock:
            if self._closing or _probe_closing or self._pool is None:
                return
            try:
                if meta:
                    self.probed.emit(iid, meta)
                else:
                    self.failed.emit(iid, error)
            except RuntimeError:
                # Владелец QObject может исчезнуть во время анализа.
                pass

    def _probe_one(self, iid: str, path: str, cancelled: threading.Event) -> None:
        with self._lock, _probe_lock:
            if self._closing or _probe_closing or self._pool is None:
                return
        try:
            meta = probe_media(path, cancelled=cancelled)
        except InterruptedError:
            return
        except Exception as e:  # noqa: BLE001 - worker boundary reports all failures
            logger.exception(f"ProbeWorker {path}: {e}")
            self._emit_result(iid, None, str(e))
            return
        self._emit_result(iid, meta, "Не удалось прочитать метаданные")

    def close(self) -> None:
        """Cancel this owner's pending work without shutting down other owners."""
        with self._lock:
            self._closing = True
            self._cancelled.set()
            self._pending.clear()
            futures = tuple(self._futures)
        for future in futures:
            future.cancel()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._futures) + len(self._pending)
