"""Обёртка над общими пулами процесса для вызовов с колбэками завершения.

WorkerPoolManager отправляет задачу в исполнитель и доставляет результат через
on_complete/on_error. Колбэки выполняются в потоке, завершившем задачу, поэтому
виджеты из них трогать нельзя: только сигналы. Модуль не зависит от Qt.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from src.logger import logger
from src.workers.task_metadata import task_metadata


@dataclass
class _Task:
    """Незавершённая задача; запись удаляется сразу после доставки результата."""

    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    on_complete: Callable[..., Any] | None
    on_error: Callable[..., Any] | None
    done: threading.Event = field(default_factory=threading.Event)
    future: Future[Any] | None = None


class WorkerPoolManager:
    """Отправка задач с колбэками и владение своими незавершёнными задачами.

    НЕ синглтон - можно создавать несколько независимых менеджеров.
    """

    def __init__(
        self,
        max_workers: int = 8,
        thread_name_prefix: str = "pyktool",
        *,
        executor: Executor | None = None,
        source: str | None = None,
    ) -> None:
        self._source = source
        self._owns_executor = executor is None
        self._executor = (
            executor
            if executor is not None
            else ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix=thread_name_prefix
            )
        )
        self._shutdown = False
        self._tasks: dict[str, _Task] = {}
        self._task_counter = 0
        self._cancelling: set[Future[Any]] = set()
        self._delivery_threads: dict[str, int] = {}
        self._lock = threading.Lock()

        if self._owns_executor:
            logger.info(f"WorkerPoolManager: собственный пул, потоков {max_workers}")

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        task_id: str | None = None,
        on_complete: Callable[..., Any] | None = None,
        on_error: Callable[..., Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """
        Отправить задачу в пул.

        Args:
            fn: Функция для выполнения
            *args: Аргументы функции
            task_id: Уникальный ID задачи (генерируется если не указан)
            on_complete: Callback при успешном завершении
            on_error: Callback при ошибке
            metadata: Дополнительные данные
            **kwargs: Именованные аргументы функции

        Returns:
            task_id: ID отправленной задачи
        """
        with self._lock:
            if self._shutdown:
                raise RuntimeError("Cannot submit tasks after manager shutdown")
            if task_id is None or task_id in self._tasks:
                self._task_counter += 1
                task_id = f"task_{self._task_counter}_{int(time.time() * 1000)}"
            task = _Task(fn, args, kwargs, on_complete, on_error)
            # Публикуется до того, как рабочий поток сможет взять блокировку.
            self._tasks[task_id] = task
            executor_kwargs = (
                {"metadata": task_metadata(metadata, self._source)}
                if getattr(self._executor, "accepts_metadata", False)
                else {}
            )
            try:
                future = self._executor.submit(
                    self._execute_with_tracking, task_id, **executor_kwargs
                )
            except Exception:
                self._tasks.pop(task_id, None)
                raise
            task.future = future

        # Завершённый или отменённый Future вызывает колбэк синхронно прямо здесь.
        # Регистрировать и отменять Future под блокировкой менеджера нельзя.
        future.add_done_callback(lambda done: self._on_future_done(task_id, done))
        return task_id

    def cancel(self, task_id: str, wait: bool = True, timeout: float = 5.0) -> bool:
        """Отменить ожидающую задачу, при необходимости дождаться выполняющейся.

        True означает только отмену, а не естественное завершение или таймаут.
        Выполняющуюся функцию прервать нельзя. Ожидание идёт без блокировки.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            future = task.future
            attempt = future is not None and future not in self._cancelling
            if attempt and future is not None:
                self._cancelling.add(future)
        cancelled = False
        if attempt and future is not None:
            try:
                cancelled = future.cancel()
            finally:
                with self._lock:
                    self._cancelling.discard(future)
        if wait:
            task.done.wait(timeout=max(0.0, timeout))
        return cancelled

    def cancel_all(self, wait: bool = True, timeout: float = 5.0) -> int:
        """Отменить все незавершённые задачи; вернуть число отменённых."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            tasks = list(self._tasks.items())
        cancelled = sum(self.cancel(task_id, wait=False) for task_id, _ in tasks)
        if wait:
            for _, task in tasks:
                task.done.wait(timeout=max(0.0, deadline - time.monotonic()))
        return cancelled

    def shutdown(self, wait: bool = True, cancel_futures: bool = True) -> None:
        """
        Остановить приём задач.

        Args:
            wait: Ждать завершения задач
            cancel_futures: Отменить незавершенные задачи

        Внешний исполнитель остаётся у своего владельца. Из колбэка завершения
        вызывается только с wait=False: ожидание включало бы сам колбэк.
        """
        logger.info("WorkerPoolManager: Shutting down...")
        with self._lock:
            if wait and threading.get_ident() in self._delivery_threads.values():
                raise RuntimeError(
                    "Cannot wait for shutdown from a completion callback; use wait=False"
                )
            self._shutdown = True
            tasks = list(self._tasks.values())

        if cancel_futures:
            self.cancel_all(wait=False)

        if self._owns_executor:
            self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        if wait:
            for task in tasks:
                task.done.wait()
        logger.info("WorkerPoolManager: Shutdown complete")

    def _execute_with_tracking(self, task_id: str) -> Any:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise RuntimeError(f"Task {task_id} not found")
            fn, args, kwargs = task.fn, task.args, task.kwargs
        return fn(*args, **kwargs)

    def _on_future_done(self, task_id: str, future: Future[Any]) -> None:
        """Доставить исход один раз, включая отмену самим исполнителем."""
        result = None
        error = None
        if not future.cancelled():
            try:
                result = future.result()
            except BaseException as exc:  # noqa: BLE001 - учитывается и SystemExit в задаче
                error = str(exc)
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.future is not future:
                return
            self._delivery_threads[task_id] = threading.get_ident()

        # Колбэки не влияют на исход задачи и не выполняются под блокировкой.
        try:
            if future.cancelled():
                pass
            elif error is not None:
                logger.info(f"WorkerPoolManager: Task {task_id} failed: {error}")
                self._invoke(task_id, task.on_error, error)
            else:
                self._invoke(task_id, task.on_complete, result)
        finally:
            with self._lock:
                # Результаты, функции и аргументы освобождаются сразу после доставки.
                self._tasks.pop(task_id, None)
                self._delivery_threads.pop(task_id, None)
            task.done.set()

    @staticmethod
    def _invoke(task_id: str, callback: Callable[..., Any] | None, value: Any) -> None:
        if callback is None:
            return
        try:
            callback(task_id, value)
        except Exception as e:  # noqa: BLE001 - изолировать пользовательские колбэки
            logger.error(f"WorkerPoolManager: Error in callback for {task_id}: {e}")


# Глобальные экземпляры - РАЗДЕЛЬНЫЕ для пингов, команд и Veyon
_ping_worker_pool: WorkerPoolManager | None = None
_command_worker_pool: WorkerPoolManager | None = None
_veyon_worker_pool: WorkerPoolManager | None = None
_service_worker_pool: WorkerPoolManager | None = None
_POOL_LOCK = threading.Lock()


def get_ping_worker_pool() -> WorkerPoolManager:
    """Получить или создать WorkerPoolManager для пингов"""
    global _ping_worker_pool
    with _POOL_LOCK:
        if _ping_worker_pool is None:
            from src.workers.application_pools import get_application_pool

            _ping_worker_pool = WorkerPoolManager(
                executor=get_application_pool("status"), source="application"
            )
    return _ping_worker_pool


def get_command_worker_pool() -> WorkerPoolManager:
    """Получить или создать WorkerPoolManager для команд"""
    global _command_worker_pool
    with _POOL_LOCK:
        if _command_worker_pool is None:
            from src.workers.application_pools import get_application_pool

            _command_worker_pool = WorkerPoolManager(
                executor=get_application_pool("commands"), source="application"
            )
    return _command_worker_pool


def get_veyon_worker_pool() -> WorkerPoolManager:
    """Получить или создать WorkerPoolManager для миниатюр Veyon"""
    global _veyon_worker_pool
    with _POOL_LOCK:
        if _veyon_worker_pool is None:
            from src.workers.application_pools import get_application_pool

            _veyon_worker_pool = WorkerPoolManager(
                executor=get_application_pool("thumbnails"), source="application"
            )
    return _veyon_worker_pool


def get_service_worker_pool() -> WorkerPoolManager:
    """Share diagnostics/installation capacity with background host information."""
    global _service_worker_pool
    with _POOL_LOCK:
        if _service_worker_pool is None:
            from src.workers.application_pools import get_application_pool

            _service_worker_pool = WorkerPoolManager(
                executor=get_application_pool("services"), source="application"
            )
    return _service_worker_pool


def shutdown_gui_worker_pools() -> None:
    """Cancel GUI-owned queued tasks; process executors have a separate owner."""
    with _POOL_LOCK:
        pools = (
            _ping_worker_pool,
            _command_worker_pool,
            _veyon_worker_pool,
            _service_worker_pool,
        )
    for pool in pools:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


def get_thumbnail_worker_pool() -> WorkerPoolManager:
    """Return the existing dedicated pool for either thumbnail backend."""
    return get_veyon_worker_pool()
