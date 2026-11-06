"""Отслеживаемый ThreadPoolExecutor с метаданными задач.

TrackedThreadPoolExecutor — тонкая обёртка над ThreadPoolExecutor,
запоминающая fn/metadata/host каждой поставленной задачи и считающая
active/pending/completed/failed. Регистрируется в глобальном реестре
для мониторинга.

Для пулов вне общих бюджетов ApplicationPool: сканер, карта сети, камеры в
веб-интерфейсе, очистка миниатюр, анализ медиафайлов, разбор Ansible.

Потокобезопасный. Не порождает Qt-сигналов — монитор опрашивает через
get_snapshot().
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from src.workers.task_metadata import operation_source, task_metadata


@dataclass
class TrackedTask:
    """Информация о поставленной задаче для мониторинга."""

    fn_name: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    state: str = "pending"  # pending | running | completed | failed | cancelled


class TrackedThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor с трекингом active/pending задач и metadata.

    Реестр _REGISTRY хранит все живые экземпляры (включая эфемерные).
    Монитор опрашивает get_all_tracked_pools() каждые 500мс.
    """

    _REGISTRY_LOCK = threading.Lock()
    _REGISTRY: Set["TrackedThreadPoolExecutor"] = set()

    @classmethod
    def get_all_tracked_pools(cls) -> List["TrackedThreadPoolExecutor"]:
        """Вернуть список всех живых отслеживаемых пулов (снапшот)."""
        with cls._REGISTRY_LOCK:
            return list(cls._REGISTRY)

    @classmethod
    def get_persistent_pools(cls) -> List["TrackedThreadPoolExecutor"]:
        """Постоянные пулы (веб-сервер). Не ephemeral."""
        with cls._REGISTRY_LOCK:
            return [p for p in cls._REGISTRY if not p._ephemeral]

    def __init__(
        self,
        max_workers: Optional[int] = None,
        thread_name_prefix: str = "tracked",
        label: str = "",
        ephemeral: bool = False,
    ) -> None:
        super().__init__(
            max_workers=max_workers or 1,
            thread_name_prefix=thread_name_prefix,
        )
        self._label = label or thread_name_prefix
        self._ephemeral = ephemeral
        self._lock = threading.Lock()
        self._tasks: Dict[Future, TrackedTask] = {}
        self._stats = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        }
        with TrackedThreadPoolExecutor._REGISTRY_LOCK:
            TrackedThreadPoolExecutor._REGISTRY.add(self)

    @property
    def label(self) -> str:
        return self._label

    @property
    def ephemeral(self) -> bool:
        return self._ephemeral

    def submit(self, fn, *args, metadata=None, **kwargs) -> Future:  # type: ignore[override]
        fn_name = (
            getattr(fn, "__name__", None)
            or getattr(fn, "__qualname__", None)
            or repr(fn)
        )
        task = TrackedTask(
            fn_name=fn_name,
            metadata=task_metadata(metadata),
        )
        future = super().submit(self._wrap, fn, task, *args, **kwargs)
        with self._lock:
            self._tasks[future] = task
            self._stats["submitted"] += 1
        future.add_done_callback(self._on_done)
        return future

    def _wrap(self, fn, task: TrackedTask, *args, **kwargs):
        with self._lock:
            task.started_at = time.time()
            task.state = "running"
        token = operation_source.set(task.metadata.get("source"))
        try:
            return fn(*args, **kwargs)
        finally:
            operation_source.reset(token)
            with self._lock:
                task.completed_at = time.time()

    def _on_done(self, future: Future) -> None:
        with self._lock:
            task = self._tasks.get(future)
            if task is None:
                return
            task.completed_at = time.time()
            if future.cancelled():
                task.state = "cancelled"
                self._stats["cancelled"] += 1
            elif future.exception() is not None:
                task.state = "failed"
                self._stats["failed"] += 1
            else:
                task.state = "completed"
                self._stats["completed"] += 1
            # Counters retain the history, not Futures, images, command output or
            # exception tracebacks. Callers own results until their delivery.
            self._tasks.pop(future, None)
        self._unregister_if_drained()

    def _unregister_if_drained(self) -> None:
        # Never hold task and registry locks together. Nonblocking shutdown
        # does not release ownership of operations that still run.
        with self._lock:
            drained = self._shutdown and not self._tasks
        if drained:
            with TrackedThreadPoolExecutor._REGISTRY_LOCK:
                TrackedThreadPoolExecutor._REGISTRY.discard(self)

    def get_stats(self) -> Dict[str, Any]:
        return self.get_snapshot()["stats"]

    def get_snapshot(self) -> dict[str, Any]:
        """Получить нагрузку, счётчики и задания под одной блокировкой."""
        with self._lock:
            now = time.time()
            tasks = [
                {
                    "task_id": str(id(future)),
                    "fn": task.fn_name,
                    "metadata": dict(task.metadata),
                    "state": task.state,
                    "duration": max(0.0, now - (
                        task.started_at if task.started_at is not None else task.created_at
                    )),
                }
                for future, task in self._tasks.items()
                if task.state in ("running", "pending")
            ]
            tasks.sort(key=lambda task: (task["state"] != "running", -task["duration"]))
            stats = {
                "label": self._label,
                "max_workers": self._max_workers,
                "active": sum(t["state"] == "running" for t in tasks),
                "pending": sum(t["state"] == "pending" for t in tasks),
                **dict(self._stats),
                # total_tasks — всего за сессию; _tasks хранит только незавершённые.
                "total_tasks": self._stats["submitted"],
            }
            return {"stats": stats, "tasks": tasks}

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        """Все задачи (running + pending) для таблицы монитора."""
        return self.get_snapshot()["tasks"]

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        try:
            super().shutdown(wait=wait, cancel_futures=cancel_futures)
        finally:
            # Running operations are not cancelled by shutdown(wait=False).
            # Their done callbacks record the actual outcome when they exit.
            self._unregister_if_drained()

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            return super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            with TrackedThreadPoolExecutor._REGISTRY_LOCK:
                TrackedThreadPoolExecutor._REGISTRY.discard(self)


async def run_in_tracked_executor(
    pool: Executor | None,
    fn: Callable,
    *args,
    metadata: Optional[Dict[str, Any]] = None,
) -> Any:
    """Асинхронный запуск fn в TrackedThreadPoolExecutor с metadata.

    Замена ``loop.run_in_executor(pool, fn, *args)``, передаёт metadata
    в TrackedThreadPoolExecutor.submit для отображения в мониторе пулов.
    """
    if pool is None:
        raise RuntimeError("Worker pool is unavailable or stopping")
    future = pool.submit(fn, *args, metadata=metadata)
    return await asyncio.wrap_future(future)
