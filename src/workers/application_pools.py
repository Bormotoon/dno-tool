"""Process-wide, bounded worker budgets shared by GUI, web and node callers.

Only host operations run in these executors. Coordinators must never submit
work here and synchronously wait for another operation in the same pool.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from src.workers.task_metadata import (
    is_interactive_task,
    operation_source,
    task_metadata,
)
from src.workers.tracked_pool import TrackedTask

_POOL_LABELS = {
    "dno-status": "Доступность хостов",
    "dno-commands": "Команды и действия Veyon",
    "dno-thumbnails": "Миниатюры экранов",
    "dno-services": "Диагностика и установка сервисов",
    "dno-rfb": "Удалённый экран (VNC / Veyon)",
}


_INTERACTIVE_GROUP = "interactive"
# Нажатие кнопки не должно получать отказ из-за полной очереди пакетных задач.
INTERACTIVE_ADMISSION_MARGIN = 64


class PoolBusyError(RuntimeError):
    """A finite worker queue has no admission capacity left."""


@dataclass(eq=False)
class _Operation:
    future: Future[Any]
    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    metadata: dict[str, Any]
    group: str
    task: TrackedTask
    remote: bool = False
    interactive: bool = False


class ApplicationPool(Executor):
    """Dispatch fairly without blocking a worker on an admission semaphore."""

    accepts_metadata = True

    def __init__(
        self,
        max_workers: int,
        name: str,
        *,
        max_pending: int = 4096,
        remote_limit: int | None = None,
        interactive_reserved: int = 0,
    ) -> None:
        if max_workers < 1 or max_pending < 0:
            raise ValueError("Invalid application pool limits")
        self._max_workers = max_workers
        self._name = name
        self._label = _POOL_LABELS.get(name, name)
        self._remote_limit = max(1, remote_limit or max_workers)
        # Выполняющуюся задачу вытеснить нельзя: быстрый старт кнопки обеспечивает
        # только поток, который пакетным задачам недоступен. Резерв идёт сверх
        # настройки: пакетным задачам доступно ровно max_workers потоков.
        self._interactive_reserved = max(0, interactive_reserved)
        self._max_pending = max_pending
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers + self._interactive_reserved,
            thread_name_prefix=name,
        )
        self._condition = threading.Condition(threading.RLock())
        self._queues: OrderedDict[str, deque[_Operation]] = OrderedDict()
        self._pending: dict[Future[Any], _Operation] = {}
        self._operations: dict[Future[Any], _Operation] = {}
        self._stats = {"submitted": 0, "completed": 0, "failed": 0, "cancelled": 0}
        self._active = 0
        self._batch_active = 0
        self._remote_active = 0
        self._accepting = True
        self._dispatching = False
        self._cancel_pending = False
        self._executor_stopped = False

    @property
    def label(self) -> str:
        return self._label

    def lease(self, *, source: str | None = None) -> PoolLease:
        """Выдать независимую аренду с явным источником запросов владельца."""
        return PoolLease(self, source=source)

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Future[Any]:
        info = task_metadata(metadata)
        remote = info.get("type") in ("node_status", "node_thumbnail")
        interactive = not remote and is_interactive_task(info)
        if remote:
            group = f"node:{info.get('node_iid') or 'remote'}"
        elif interactive:
            group = _INTERACTIVE_GROUP
        else:
            # Задания и интерфейсы делят потоки поровну, а не в порядке поступления.
            owner = info.get("job_id") or info.get("batch_id") or info.get("source")
            group = f"batch:{owner or 'local'}"
        future = Future()
        operation = _Operation(
            future,
            fn,
            args,
            kwargs,
            info,
            group,
            TrackedTask(getattr(fn, "__name__", type(fn).__name__), info),
            remote,
            interactive,
        )
        with self._condition:
            if not self._accepting:
                raise RuntimeError("Application pool is stopping")
            limit = (
                self._max_workers + self._interactive_reserved + self._max_pending
            )
            if interactive:
                limit += INTERACTIVE_ADMISSION_MARGIN
            if self._active + len(self._pending) >= limit:
                raise PoolBusyError("Worker queue is full; retry later")
            self._pending[future] = operation
            self._operations[future] = operation
            self._stats["submitted"] += 1
            self._queues.setdefault(group, deque()).append(operation)
            future.add_done_callback(self._operation_done)
            self._dispatch_locked()
        return future

    def _operation_done(self, future: Future[Any]) -> None:
        """Учесть исход пользовательского Future и освободить завершённую работу."""
        with self._condition:
            if self._operations.pop(future, None) is None:
                return
            counter = (
                "cancelled"
                if future.cancelled()
                else "failed"
                if future.exception() is not None
                else "completed"
            )
            self._stats[counter] += 1
            operation = self._pending.pop(future, None)
            if operation is not None:
                queue = self._queues[operation.group]
                queue.remove(operation)
                if not queue:
                    del self._queues[operation.group]
                # Отменённая очередь не попадёт в исполнитель: уведомляем wait().
                future.set_running_or_notify_cancel()
            self._dispatch_locked()
            self._condition.notify_all()

    def _select_group_locked(self) -> str | None:
        # Интерактивные идут первыми и могут занять любой свободный поток.
        if _INTERACTIVE_GROUP in self._queues:
            return _INTERACTIVE_GROUP
        if self._batch_active >= self._max_workers:
            return None
        for group in self._queues:
            if group.startswith("node:") and self._remote_active >= self._remote_limit:
                continue
            return group
        return None

    def _account_locked(self, operation: _Operation, delta: int) -> None:
        self._active += delta
        if not operation.interactive:
            self._batch_active += delta
        if operation.remote:
            self._remote_active += delta

    def _dispatch_locked(self) -> None:
        if self._dispatching:
            return
        self._dispatching = True
        try:
            while (
                not self._cancel_pending
                and self._active < self._max_workers + self._interactive_reserved
                and self._pending
            ):
                selected = self._select_group_locked()
                if selected is None:
                    break
                queue = self._queues[selected]
                operation = queue.popleft()
                self._queues.move_to_end(selected)
                if not queue:
                    del self._queues[selected]
                self._pending.pop(operation.future)
                self._account_locked(operation, 1)
                try:
                    inner = self._executor.submit(self._run, operation)
                except RuntimeError as error:
                    self._account_locked(operation, -1)
                    if operation.future.set_running_or_notify_cancel():
                        operation.future.set_exception(error)
                else:
                    inner.add_done_callback(
                        lambda _done, done=operation: self._finished(done)
                    )
            if not self._accepting and not self._pending and not self._active:
                self._executor.shutdown(wait=False)
                self._executor_stopped = True
        finally:
            self._dispatching = False

    def _run(self, operation: _Operation) -> None:
        future = operation.future
        with self._condition:
            if not future.set_running_or_notify_cancel():
                return
            operation.task.state = "running"
            operation.task.started_at = time.time()
        token = operation_source.set(operation.metadata.get("source"))
        try:
            result = operation.fn(*operation.args, **operation.kwargs)
        except BaseException as error:  # noqa: BLE001 - a worker must resolve its Future
            future.set_exception(error)
        else:
            future.set_result(result)
        finally:
            operation_source.reset(token)

    def _finished(self, operation: _Operation) -> None:
        with self._condition:
            self._account_locked(operation, -1)
            self._dispatch_locked()
            self._condition.notify_all()

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._condition:
            self._accepting = False
            self._cancel_pending = self._cancel_pending or cancel_futures
            queued = list(self._pending) if cancel_futures else []
        # Future callbacks can acquire this pool and their owner's locks.
        for future in queued:
            future.cancel()
        with self._condition:
            self._dispatch_locked()
            if wait:
                self._condition.wait_for(lambda: not self._pending and not self._active)
        if wait:
            self._executor.shutdown(wait=True)

    def get_stats(self) -> dict[str, Any]:
        return self.get_snapshot()["stats"]

    def get_snapshot(self) -> dict[str, Any]:
        """Согласованный снимок всей очереди диспетчера, нагрузки и счётчиков."""
        with self._condition:
            now = time.time()
            tasks = [
                {
                    "task_id": str(id(future)),
                    "fn": operation.task.fn_name,
                    "metadata": dict(operation.metadata),
                    "state": operation.task.state,
                    "duration": max(
                        0.0,
                        now
                        - (
                            operation.task.started_at
                            if operation.task.started_at is not None
                            else operation.task.created_at
                        ),
                    ),
                }
                for future, operation in self._operations.items()
            ]
            tasks.sort(key=lambda task: (task["state"] != "running", -task["duration"]))
            stats = {
                "label": self._label,
                "max_workers": self._max_workers,
                "interactive_reserved": self._interactive_reserved,
                "active": sum(task["state"] == "running" for task in tasks),
                "pending": sum(task["state"] == "pending" for task in tasks),
                "total_tasks": self._stats["submitted"],
                **self._stats,
            }
            return {"stats": stats, "tasks": tasks}

    def get_all_tasks(self) -> list[dict[str, Any]]:
        return self.get_snapshot()["tasks"]


class PoolLease(Executor):
    """Track and cancel only operations submitted by this consumer."""

    accepts_metadata = True

    def __init__(self, pool: ApplicationPool, *, source: str | None = None) -> None:
        self._pool = pool
        self._source = source
        self._max_workers = pool._max_workers
        self._lock = threading.RLock()
        self._futures: set[Future[Any]] = set()
        self._closed = False

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Worker owner is stopping")
            metadata = task_metadata(kwargs.pop("metadata", None), self._source)
            future = self._pool.submit(fn, *args, metadata=metadata, **kwargs)
            self._futures.add(future)
        future.add_done_callback(self._forget)
        return future

    def _forget(self, future: Future[Any]) -> None:
        with self._lock:
            self._futures.discard(future)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            self._closed = True
            futures = tuple(self._futures)
        if cancel_futures:
            for future in futures:
                future.cancel()
        if wait and futures:
            wait_futures(futures)

    def __enter__(self) -> Self:
        return super().__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.shutdown(wait=False, cancel_futures=True)
        super().__exit__(exc_type, exc, traceback)


_LOCK = threading.Lock()
_POOLS: dict[str, ApplicationPool] = {}
_stopping = False


def get_created_application_pools() -> tuple[ApplicationPool, ...]:
    """Вернуть уже созданные общие пулы, не создавая новых при мониторинге."""
    with _LOCK:
        return tuple(_POOLS.values())


def get_application_pool(role: str) -> ApplicationPool:
    """Resolve a process-owned budget; settings apply on process restart."""
    from src.config import config

    with _LOCK:
        if _stopping:
            raise RuntimeError("Application workers are stopping")
        if role not in _POOLS:
            sizes = {
                "status": config.app.network.thread_count,
                "commands": config.app.execution.thread_count,
                "thumbnails": config.app.thumbnails.thread_count,
                "services": config.app.network.thread_count,
                "rfb": config.app.web.rfb_thread_count,
            }
            if role not in sizes:
                raise ValueError(f"Unknown worker role: {role}")
            size = max(1, min(64, sizes[role]))
            _POOLS[role] = ApplicationPool(
                size,
                f"dno-{role}",
                # Половина миниатюр сохраняется для локальных захватов. В status остались
                # только запросы к нодам: их лимит равен настройке.
                remote_limit=max(1, size // 2) if role == "thumbnails" else size,
                # Один поток на восемь сверх настройки: освободившиеся потоки и так
                # достаются интерактивным задачам первыми, резерв нужен для
                # мгновенного старта кнопки, пока пакет занимает все потоки.
                interactive_reserved=max(1, size // 8)
                if role in ("commands", "services")
                else 0,
            )
        return _POOLS[role]


def shutdown_application_pools(*, wait: bool = False) -> None:
    """Permanently stop admission and cancel queued operations at process exit."""
    global _stopping
    with _LOCK:
        _stopping = True
        pools = tuple(_POOLS.values())
    for pool in pools:
        pool.shutdown(wait=False, cancel_futures=True)
    if wait:
        for pool in pools:
            pool.shutdown(wait=True)
