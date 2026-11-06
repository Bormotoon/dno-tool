"""
Worker Bridge - мост для интеграции workers с EventBus.

Предоставляет централизованную обработку событий от workers,
абстрагируя UI компоненты от прямой работы с сигналами workers.
"""

import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from src.logger import logger

if TYPE_CHECKING:
    from .event_bus import EventBus
    from .interfaces import IWorkerClient


@dataclass
class WorkerContext:
    """Контекст выполнения worker."""

    worker_id: str
    worker_type: str
    client: Optional["IWorkerClient"] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


class WorkerEventAdapter(QObject):
    """
    Адаптер для преобразования сигналов worker в события EventBus.

    Позволяет автоматически транслировать сигналы worker в события,
    которые могут быть обработаны любыми компонентами приложения.
    """

    def __init__(self, event_bus: Optional["EventBus"] = None):
        super().__init__()
        self._event_bus = event_bus
        self._worker_mappings: Dict[str, Dict[str, str]] = {}

    def adapt_worker(
        self,
        worker: QObject,
        worker_id: str,
        signal_mappings: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Адаптировать worker для отправки событий.

        Args:
            worker: Экземпляр worker
            worker_id: Уникальный ID worker
            signal_mappings: Сопоставление сигналов -> типы событий
                {'progress_update': 'WORKER_PROGRESS', ...}
        """
        default_mappings = {
            "started": "WORKER_STARTED",
            "finished": "WORKER_FINISHED",
            "error": "WORKER_ERROR",
            "result_ready": "WORKER_RESULT",
        }

        mappings = signal_mappings or default_mappings
        self._worker_mappings[worker_id] = mappings

        for signal_name, event_type_name in mappings.items():
            signal = getattr(worker, signal_name, None)
            if signal and hasattr(signal, "connect"):
                # Создаем обработчик для этого сигнала
                handler = self._create_handler(worker_id, event_type_name, signal_name)
                signal.connect(handler)
                logger.debug(
                    f"WorkerEventAdapter: Connected {signal_name} -> {event_type_name}"
                )

    def _create_handler(
        self, worker_id: str, event_type_name: str, signal_name: str
    ) -> Callable[..., None]:
        """Создать обработчик сигнала."""

        def handler(*args):
            if not self._event_bus:
                return

            try:
                from .event_bus import EventType

                event_type = getattr(EventType, event_type_name, EventType.CUSTOM)

                # Формируем данные события
                data = {"worker_id": worker_id, "signal": signal_name, "args": args}

                # Если есть один аргумент, добавляем его как 'data'
                if len(args) == 1:
                    data["data"] = args[0]
                elif len(args) > 1:
                    data["data"] = args

                self._event_bus.publish_typed(
                    event_type=event_type, source=f"worker_{worker_id}", data=data
                )

            except Exception as e:
                logger.error(
                    f"WorkerEventAdapter: Error handling signal {signal_name}: {e}"
                )

        return handler


class WorkerBridge(QObject):
    """
    Мост для управления workers и их интеграции с системой событий.

    Предоставляет централизованное управление жизненным циклом workers
    и обработку их событий.
    """

    # Сигналы для внутреннего использования
    _worker_started = Signal(str, dict)  # worker_id, context
    _worker_progress = Signal(str, object)  # worker_id, progress
    _worker_finished = Signal(str, object)  # worker_id, result
    _worker_error = Signal(str, str)  # worker_id, error

    def __init__(self, event_bus: Optional["EventBus"] = None):
        super().__init__()
        self._event_bus = event_bus
        self._workers: Dict[str, QObject] = {}
        self._threads: Dict[str, QThread] = {}
        self._contexts: Dict[str, WorkerContext] = {}
        self._clients: Dict[str, weakref.ref] = {}
        self._event_adapter = WorkerEventAdapter(event_bus)
        self._lock = threading.Lock()

        self._worker_started.connect(self._on_worker_started_internal)
        self._worker_progress.connect(self._on_worker_progress_internal)
        self._worker_finished.connect(self._on_worker_finished_internal)
        self._worker_error.connect(self._on_worker_error_internal)

    def register_worker(
        self,
        worker: QObject,
        worker_id: str,
        client: Optional["IWorkerClient"] = None,
        context: Optional[Dict[str, Any]] = None,
        auto_adapt: bool = True,
    ) -> WorkerContext:
        """
        Зарегистрировать worker для управления.

        Args:
            worker: Экземпляр worker
            worker_id: Уникальный ID
            client: Клиент для обратных вызовов
            context: Контекст выполнения
            auto_adapt: Автоматически адаптировать сигналы

        Returns:
            Контекст worker
        """
        if worker_id in self._workers:
            if worker_id in self._threads:
                raise ValueError(f"Worker {worker_id} already registered")
            logger.warning(
                f"WorkerBridge: Worker {worker_id} already registered, replacing"
            )
            self.unregister_worker(worker_id)

        worker_type = type(worker).__name__

        ctx = WorkerContext(
            worker_id=worker_id,
            worker_type=worker_type,
            client=client,
            metadata=context or {},
        )

        self._workers[worker_id] = worker
        self._contexts[worker_id] = ctx

        if client:
            self._clients[worker_id] = weakref.ref(client)

        # Адаптируем сигналы
        if auto_adapt:
            self._event_adapter.adapt_worker(worker, worker_id)

        # Подключаем стандартные сигналы
        self._connect_worker_signals(
            worker,
            worker_id,
            forward_progress=getattr(client, "wants_worker_progress", False),
        )

        logger.debug(f"WorkerBridge: Registered worker {worker_id} ({worker_type})")
        return ctx

    def register_threaded_worker(
        self,
        worker: QObject,
        worker_id: str,
        client: Optional["IWorkerClient"] = None,
        context: Optional[Dict[str, Any]] = None,
        auto_start: bool = True,
    ) -> WorkerContext:
        """
        Зарегистрировать worker с выделенным потоком.

        Args:
            worker: Экземпляр worker
            worker_id: Уникальный ID
            client: Клиент для обратных вызовов
            context: Контекст выполнения
            auto_start: Автоматически запустить поток

        Returns:
            Контекст worker
        """
        # Валидация: worker должен иметь метод execute
        execute = getattr(worker, "execute", None)
        if not callable(execute):
            logger.error(
                f"WorkerBridge: Worker {worker_id} has no execute method, "
                "cannot start thread"
            )
            raise ValueError(  # noqa: TRY004 - preserve the existing validation API
                f"Worker {worker_id} must have an execute() method"
            )

        finished = getattr(worker, "finished", None)
        if finished is None or not hasattr(finished, "connect"):
            raise TypeError(f"Worker {worker_id} must have a finished signal")

        ctx = self.register_worker(worker, worker_id, client, context, auto_adapt=True)

        # Создаем поток
        thread = QThread()
        self._threads[worker_id] = thread

        # Перемещаем worker в поток
        worker.moveToThread(thread)

        # Подключаем сигналы потока
        thread.started.connect(execute)
        # quit() is thread-safe. A queued call would deadlock a GUI-thread wait().
        finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        # Qt processes deferred deletion in the finishing worker thread, even
        # after its event loop stops. A worker's finished signal is NOT proof
        # that execute() has returned or that its QThread has stopped.
        thread.finished.connect(worker.deleteLater, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(
            self._on_thread_finished, Qt.ConnectionType.QueuedConnection
        )

        if auto_start:
            thread.start()
            ctx.started_at = time.time()
            self._worker_started.emit(worker_id, context or {})

        return ctx

    def unregister_worker(self, worker_id: str) -> bool:
        """Request cancellation without waiting or releasing a live QThread.

        Call lifecycle methods from the bridge's owning thread. Threaded
        registrations remain owned until QThread.finished is delivered. The
        bridge itself must remain alive while has_pending_workers() is true.
        Worker abort() methods must be thread-safe, prompt cancellation hooks.
        """
        with self._lock:
            if worker_id not in self._workers:
                return False

            thread = self._threads.get(worker_id)
            worker = self._workers[worker_id]

        if thread is not None:
            if thread.isFinished():
                self._release_finished_thread(worker_id, thread)
                return True
            self.abort_worker(worker_id)
            thread.requestInterruption()
            if not thread.isRunning() and not thread.isFinished():
                # A worker already moved to a never-started thread cannot be
                # destroyed on the GUI thread. Start a teardown-only event loop
                # so finished -> deleteLater runs with the correct affinity.
                thread.started.disconnect(getattr(worker, "execute", None))
                thread.started.connect(thread.quit, Qt.ConnectionType.DirectConnection)
                thread.start()
            else:
                thread.quit()
            return True

        self.abort_worker(worker_id)
        with self._lock:
            self._forget_worker(worker_id)
        worker.deleteLater()
        logger.debug(f"WorkerBridge: Unregistered worker {worker_id}")
        return True

    def _forget_worker(self, worker_id: str) -> None:
        """Remove registration metadata while holding _lock."""
        self._workers.pop(worker_id, None)
        self._contexts.pop(worker_id, None)
        self._clients.pop(worker_id, None)
        self._event_adapter._worker_mappings.pop(worker_id, None)

    def request_stop_all(self) -> int:
        """Request cooperative cancellation of all registrations without waiting.

        Return the number of registrations asked to stop. Keep the Qt event
        loop and this bridge alive until has_pending_workers() becomes false.
        """
        with self._lock:
            worker_ids = list(self._workers)
        return sum(self.unregister_worker(worker_id) for worker_id in worker_ids)

    def has_pending_workers(self) -> bool:
        """Return whether any worker is still owned, including one stopping."""
        with self._lock:
            return bool(self._workers)

    def abort_worker(self, worker_id: str) -> bool:
        with self._lock:
            if worker_id not in self._workers:
                logger.warning(f"WorkerBridge: Worker {worker_id} not found for abort")
                return False

            worker = self._workers[worker_id]

        logger.debug(f"WorkerBridge: Aborting worker {worker_id}")

        abort = getattr(worker, "abort", None)
        if callable(abort):
            try:
                abort()
                logger.info(f"WorkerBridge: Aborted worker {worker_id}")
            except Exception as e:
                logger.error(
                    f"WorkerBridge: Error calling abort on worker {worker_id}: {e}"
                )

        return True

    def get_worker(self, worker_id: str) -> Optional[QObject]:
        """Получить экземпляр worker."""
        return self._workers.get(worker_id)

    def get_context(self, worker_id: str) -> Optional[WorkerContext]:
        """Получить контекст worker."""
        return self._contexts.get(worker_id)

    def is_running(self, worker_id: str) -> bool:
        """Проверить, выполняется ли worker."""
        if worker_id in self._threads:
            try:
                return self._threads[worker_id].isRunning()
            except RuntimeError:
                # Thread already deleted
                return False
        return worker_id in self._workers

    def abort_all(self) -> int:
        """
        Прервать все активные workers.

        Returns:
            Количество прерванных workers
        """
        aborted = 0
        for worker_id in list(self._workers.keys()):
            if self.abort_worker(worker_id):
                aborted += 1
        return aborted

    def _connect_worker_signals(
        self, worker: QObject, worker_id: str, *, forward_progress: bool = False
    ) -> None:
        finished = getattr(worker, "finished", None)
        if finished is not None and hasattr(finished, "connect"):

            def make_finished_handler(wid):
                def handler():
                    self._worker_finished.emit(wid, None)

                return handler

            finished.connect(make_finished_handler(worker_id))

        error = getattr(worker, "error", None)
        if error is not None and hasattr(error, "connect"):

            def make_error_handler(wid):
                def handler(msg):
                    self._worker_error.emit(wid, msg)

                return handler

            error.connect(make_error_handler(worker_id))

        # Прогресс частый: лишняя доставка только клиенту, который её запросил.
        progress = getattr(worker, "progress_update", None)
        if forward_progress and progress is not None and hasattr(progress, "connect"):

            def make_progress_handler(wid):
                def handler(*args):
                    self._worker_progress.emit(wid, args[0] if len(args) == 1 else args)

                return handler

            progress.connect(make_progress_handler(worker_id))

    def _on_worker_started_internal(
        self, worker_id: str, context: Dict[str, Any]
    ) -> None:
        """Внутренний обработчик запуска worker."""
        ctx = self._contexts.get(worker_id)
        if ctx:
            ctx.started_at = time.time()

        client_ref = self._clients.get(worker_id)
        if client_ref:
            client = client_ref()
            if client and hasattr(client, "on_worker_started"):
                try:
                    client.on_worker_started(worker_id, context)
                except Exception as e:
                    logger.error(f"WorkerBridge: Error in on_worker_started: {e}")
            elif not client:
                del self._clients[worker_id]

        # Публикуем событие
        if self._event_bus:
            try:
                from .event_bus import EventType

                self._event_bus.publish_typed(
                    event_type=EventType.WORKER_STARTED,
                    source="WorkerBridge",
                    data={"worker_id": worker_id, "context": context},
                )
            except Exception as e:
                logger.error(f"WorkerBridge: Error publishing event: {e}")

    def _on_worker_progress_internal(self, worker_id: str, progress: Any) -> None:
        client_ref = self._clients.get(worker_id)
        if client_ref:
            client = client_ref()
            if client and hasattr(client, "on_worker_progress"):
                try:
                    client.on_worker_progress(worker_id, progress)
                except Exception as e:
                    logger.error(f"WorkerBridge: Error in on_worker_progress: {e}")
            elif not client:
                self._clients.pop(worker_id, None)

    def _on_worker_finished_internal(self, worker_id: str, result: Any) -> None:
        with self._lock:
            ctx = self._contexts.get(worker_id)
            if ctx:
                ctx.finished_at = time.time()

        client_ref = self._clients.get(worker_id)
        if client_ref:
            client = client_ref()
            if client and hasattr(client, "on_worker_finished"):
                try:
                    client.on_worker_finished(worker_id, result)
                except Exception as e:
                    logger.error(f"WorkerBridge: Error in on_worker_finished: {e}")
            elif not client:
                self._clients.pop(worker_id, None)

        # Публикуем событие
        if self._event_bus:
            try:
                from .event_bus import EventType

                self._event_bus.publish_typed(
                    event_type=EventType.WORKER_FINISHED,
                    source="WorkerBridge",
                    data={"worker_id": worker_id, "result": result},
                )
            except Exception as e:
                logger.error(f"WorkerBridge: Error publishing event: {e}")

    def _on_worker_error_internal(self, worker_id: str, error: str) -> None:
        client_ref = self._clients.get(worker_id)
        if client_ref:
            client = client_ref()
            if client and hasattr(client, "on_worker_error"):
                try:
                    client.on_worker_error(worker_id, error)
                except Exception as e:
                    logger.error(f"WorkerBridge: Error in on_worker_error: {e}")
            elif not client:
                self._clients.pop(worker_id, None)

        # Публикуем событие
        if self._event_bus:
            try:
                from .event_bus import EventType

                self._event_bus.publish_typed(
                    event_type=EventType.WORKER_ERROR,
                    source="WorkerBridge",
                    data={"worker_id": worker_id, "error": error},
                )
            except Exception as e:
                logger.error(f"WorkerBridge: Error publishing event: {e}")

    @Slot()
    def _on_thread_finished(self) -> None:
        """Release ownership only for the actual QThread.finished sender."""
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        with self._lock:
            worker_id = next(
                (wid for wid, owned in self._threads.items() if owned is thread), None
            )
        if worker_id is not None:
            self._release_finished_thread(worker_id, thread)

    def _release_finished_thread(self, worker_id: str, thread: QThread) -> None:
        """Finalize on the bridge thread after a real finish signal or wait()."""
        with self._lock:
            if self._threads.get(worker_id) is not thread or not thread.isFinished():
                return
            self._threads.pop(worker_id)
            self._forget_worker(worker_id)
        # The worker's deferred deletion was posted by QThread.finished on its
        # own thread. Only the GUI-affine QThread wrapper is deleted here.
        thread.deleteLater()
        logger.debug(f"WorkerBridge: Thread finished for worker {worker_id}")
