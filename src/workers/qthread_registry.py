"""Учёт QThread без окна-владельца: удержание, завершение приложения и монитор пулов.

Поток, у которого нет родителя среди дочерних объектов главного окна, не виден
завершению приложения через ``findChildren`` и не должен зависеть от жизни окна,
которое его запустило: уничтожение работающего QThread роняет процесс.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, QTimer, Slot
from shiboken6 import isValid

from src.utils.system.pool_registry import register_pool

_LABEL = "Фоновые потоки Qt"
# finished приходит раньше освобождения потока ОС: удалять объект ещё рано.
_RETIRE_RETRY_MS = 10
_ThreadT = TypeVar("_ThreadT", bound=QThread)


@dataclass(eq=False)
class _Entry:
    label: str
    started_at: float = field(default_factory=time.time)


class _QThreadRegistry(QObject):
    """Держать QThread до настоящего завершения потока и удалять его в GUI-потоке."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._threads: dict[QThread, _Entry] = {}
        self._completed = 0
        application = QCoreApplication.instance()
        if application is not None and self.thread() is not application.thread():
            self.moveToThread(application.thread())

    @property
    def label(self) -> str:
        return _LABEL

    def track(self, thread: _ThreadT, label: str) -> _ThreadT:
        with self._lock:
            self._threads[thread] = _Entry(label)
        thread.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        return thread

    @Slot()
    def _on_finished(self) -> None:
        thread = self.sender()
        if isinstance(thread, QThread):
            self._retire(thread)

    def _retire(self, thread: QThread) -> None:
        if not isValid(thread):
            return
        if not thread.wait(0):
            QTimer.singleShot(_RETIRE_RETRY_MS, self, lambda: self._retire(thread))
            return
        with self._lock:
            if self._threads.pop(thread, None) is None:
                return
            self._completed += 1
        thread.deleteLater()

    def pending_labels(self) -> list[str]:
        with self._lock:
            items = list(self._threads.items())
        return [
            entry.label
            for thread, entry in items
            if isValid(thread) and thread.isRunning()
        ]

    def request_stop(self) -> None:
        """Попросить потоки завершиться; ответ зависит от их проверки прерывания."""
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            if isValid(thread) and thread.isRunning():
                thread.requestInterruption()

    def get_snapshot(self) -> dict[str, Any]:
        """Снимок в формате пулов; общего лимита у этих потоков нет."""
        now = time.time()
        with self._lock:
            items = list(self._threads.items())
            completed = self._completed
        tasks: list[dict[str, Any]] = [
            {
                "task_id": str(id(thread)),
                "fn": entry.label,
                "metadata": {"source": "application"},
                "state": "running",
                "duration": max(0.0, now - entry.started_at),
            }
            for thread, entry in items
            if isValid(thread) and thread.isRunning()
        ]
        tasks.sort(key=lambda task: -task["duration"])
        return {
            "stats": {
                "label": _LABEL,
                "max_workers": len(tasks),
                "active": len(tasks),
                "pending": 0,
                "submitted": completed + len(items),
                "completed": completed,
                "failed": 0,
                "cancelled": 0,
                "total_tasks": completed + len(items),
            },
            "tasks": tasks,
        }


_factory_lock = threading.Lock()
_registry: _QThreadRegistry | None = None


def _get_registry() -> _QThreadRegistry:
    global _registry
    with _factory_lock:
        if _registry is None:
            _registry = _QThreadRegistry()
            register_pool(_registry)
        return _registry


def track_qthread(thread: _ThreadT, label: str) -> _ThreadT:
    """Взять QThread на учёт; вызывать в GUI-потоке до ``start()``.

    Сильная ссылка держится до завершения потока ОС, после чего реестр сам вызывает
    ``deleteLater``. Подпись видит пользователь, если поток задержит выход.
    """
    return _get_registry().track(thread, label)


def pending_qthread_labels() -> list[str]:
    """Подписи учтённых потоков, которые ещё работают."""
    registry = _registry
    return registry.pending_labels() if registry is not None else []


def request_tracked_qthreads_stop() -> None:
    """Запросить прерывание учтённых потоков; не ждёт, вызывать можно из любого потока."""
    registry = _registry
    if registry is not None:
        registry.request_stop()
