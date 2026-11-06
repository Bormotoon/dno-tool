"""Координатор сессии и жизненного цикла потоков удалённой записи."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from src.domain.models import DeviceModel
from src.workers.remote_recording import RecordingSession, SessionOperation


class RecordingState(str, Enum):
    PREPARING = "Подготовка"
    STARTING = "Запуск"
    RUNNING = "Работа"
    STOPPING = "Остановка"
    COMPLETED = "Завершено"
    ERROR = "Ошибка"
    LOST = "Связь потеряна / остановка не подтверждена"


class RecordingCoordinator(QObject):
    changed = Signal()
    snapshot = Signal(object)
    log = Signal(str)
    drained = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.session: RecordingSession | None = None
        self.state = RecordingState.COMPLETED
        self.workers: set[QThread] = set()
        self.operation: SessionOperation | None = None
        self.closing = False
        self.stop_requested = False
        self.stopping = False
        self.downloadable = False
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll)
        self._last_log: list[str] = []
        self._drain_emitted = False

    @property
    def active(self) -> bool:
        return self.session is not None and self.state not in (
            RecordingState.COMPLETED,
            RecordingState.ERROR,
        )

    def own(self, worker: QThread, callback: Callable[[], None]) -> None:
        """Результат читается лишь после настоящего QThread.finished."""
        worker.setParent(self)
        self.workers.add(worker)

        def finished() -> None:
            self.workers.discard(worker)
            callback()
            worker.deleteLater()
            self.changed.emit()
            self._check_drained()

        worker.finished.connect(finished)
        worker.start()

    def start(
        self, device: DeviceModel, settings: dict, mode: str, output: str = ""
    ) -> None:
        if self.active or self.operation or self.closing:
            return
        self.state = RecordingState.PREPARING
        self.downloadable = False
        self.session = RecordingSession.create(device, settings, mode, output)
        self.stop_requested = False
        self.stopping = False
        self._last_log = []
        self.changed.emit()
        self._operate("start")

    def _operate(self, action: str) -> None:
        if self.operation is not None or self.session is None:
            return
        worker = SessionOperation(self.session, action)
        self.operation = worker
        if action == "start":
            self.state = RecordingState.STARTING
        elif action == "stop":
            self.state = RecordingState.STOPPING
        self.changed.emit()

        def apply() -> None:
            if worker.session is not self.session:
                return
            if self.operation is not worker:
                return
            self.operation = None
            if worker.error:
                self.state = (
                    RecordingState.ERROR
                    if action == "start" and not worker.dispatched
                    else RecordingState.LOST
                )
                self.log.emit("[ERROR] " + worker.error)
            else:
                result = worker.result
                self.snapshot.emit(result)
                state = result["state"]
                self.state = {
                    "starting": RecordingState.STARTING,
                    "running": RecordingState.RUNNING,
                    "completed": RecordingState.COMPLETED,
                    "error": RecordingState.ERROR,
                }.get(state, RecordingState.LOST)
                if self.stopping and state in ("running", "starting"):
                    self.state = RecordingState.LOST
                self.downloadable = (
                    self.session.mode == "record" and state == "completed"
                )
                if result.get("error"):
                    self.log.emit("[ERROR] " + result["error"])
                lines = result.get("log", [])
                if lines != self._last_log:
                    # Хвосты перекрываются; повторяющиеся строки не раздувают историю.
                    overlap = 0
                    for length in range(min(len(lines), len(self._last_log)), 0, -1):
                        if self._last_log[-length:] == lines[:length]:
                            overlap = length
                            break
                    for line in lines[overlap:]:
                        self.log.emit("[REMOTE] " + line)
                    self._last_log = lines
            if self.active and self.stop_requested:
                self.stop_requested = False
                if action != "stop":
                    self._operate("stop")
            if self.active and not self.closing:
                self.timer.start()
            else:
                self.timer.stop()
            # При неизвестном результате закрытие приостанавливается, доступен повтор.
            if self.closing and self.state == RecordingState.LOST:
                self.closing = False
            self.changed.emit()

        self.own(worker, apply)

    def poll(self) -> None:
        if self.active and not self.operation:
            self._operate("status")

    def stop(self) -> None:
        self.timer.stop()
        self.stopping = True
        if not self.active:
            self._check_drained()
            return
        if self.operation:
            self.stop_requested = True
            self.state = RecordingState.STOPPING
            self.changed.emit()
        else:
            self._operate("stop")

    @property
    def stop_unconfirmed(self) -> bool:
        """Остановка уже запрашивалась, но её результат неизвестен."""
        return (
            self.session is not None
            and self.stopping
            and self.operation is None
            and self.state == RecordingState.LOST
        )

    def abandon(self) -> None:
        """Отказаться от сессии без подтверждения: процесс на хосте мог остаться."""
        if self.operation is not None:
            return
        self.log.emit(
            "[WARN] Сессия оставлена без подтверждения остановки; "
            "процесс на удалённом хосте может продолжать работу"
        )
        self.session = None
        self.state = RecordingState.COMPLETED
        self.closing = True
        self.changed.emit()
        # Остальные потоки (загрузка) отменяются тем же путём, что и при закрытии.
        self.shutdown()

    def shutdown(self) -> None:
        self.closing = True
        self.timer.stop()
        for worker in list(self.workers):
            if worker is self.operation:
                # Сначала получить результат запуска, затем остановить тот же процесс.
                continue
            if hasattr(worker, "cancel"):
                worker.cancel()
            elif hasattr(worker, "cancelled"):
                worker.cancelled.set()
        self.stop()
        self._check_drained()

    def _check_drained(self) -> None:
        if (
            self.closing
            and not self.workers
            and not self.active
            and not self._drain_emitted
        ):
            self._drain_emitted = True
            self.drained.emit()
