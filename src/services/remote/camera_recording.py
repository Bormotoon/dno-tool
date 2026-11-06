"""Менеджер библиотечных сеансов записи независимо от окна.

Запись идёт в потоке приложения и живёт, пока оно работает, поэтому состояние на диск
не сохраняется: после перезапуска нечего подхватывать и нечего считать осиротевшим.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from src.domain.models.camera import CameraModel
from src.workers.camera_workers import (
    CameraPingWorker,
    CameraRecordWorker,
    stop_worker_blocking,
)

if TYPE_CHECKING:
    from src.services.remote.camera_service import CameraService

_HISTORY_LIMIT = 10000


@dataclass
class Recording:
    """Состояние одной камеры и намерения продолжать запись."""

    camera: CameraModel
    worker: CameraRecordWorker | None = None
    intent: bool = False
    status: str = "idle"
    online: bool = False
    failures: int = 0
    retries: int = 0
    retry_at: float = 0.0
    started: float = 0.0
    limit_started: float = 0.0
    deadline: float = 0.0
    path: str = ""
    size: int = 0
    error: str = ""
    removed: bool = False
    segment_started: float = 0.0
    elapsed: float = 0.0


class StopRecordingThread(QThread):
    """Остановка с ожиданием истинного завершения QThread вне GUI."""

    def __init__(
        self,
        worker: CameraRecordWorker,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.worker = worker
        self.ok = False

    def run(self) -> None:
        try:
            self.ok = stop_worker_blocking(self.worker)
        except Exception:
            self.ok = False


class CameraRecordingManager(QObject):
    """Единственный владелец потоков записи и состояния камер сервиса."""

    changed = Signal()
    message = Signal(str)

    def __init__(self, service: CameraService, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.records: dict[str, Recording] = {}
        self._threads: set[QThread] = set()
        self._stoppers = {}
        self._ping = None
        self._shutdown = False
        self._next_ping = 0.0
        # Завершённые записи этого запуска: для открытия в видеоредакторе.
        self._history: deque[dict] = deque(maxlen=_HISTORY_LIMIT)
        self.reload()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    def reload(self) -> None:
        """Согласовать новые модели, сохранив активные и удаляемые записи."""
        self.policy = self.service.get_settings()
        models = {c.identifier: c for c in self.service.load_cameras()}
        for iid, record in list(self.records.items()):
            if iid in models:
                previous = record.camera
                record.camera = models[iid]
                record.removed = False
                record.deadline = (
                    time.monotonic()
                    + max(
                        0,
                        self.policy.max_record_hours * 3600
                        - max(0, time.time() - record.limit_started),
                    )
                    if self.policy.max_record_hours and record.limit_started
                    else 0
                )
                if record.worker:
                    record.worker._policy = self.policy
                    fields = (
                        "ip_address",
                        "login",
                        "password",
                        "output_path",
                        "path_mask",
                        "file_mask",
                    )
                    if (
                        any(
                            getattr(previous, key) != getattr(record.camera, key)
                            for key in fields
                        )
                        and record.intent
                    ):
                        record.worker.request_rotation()
                        record.status = "stopping"
            elif record.worker or record.intent:
                record.removed = True
                self.stop(iid)
            else:
                del self.records[iid]
        for iid, camera in models.items():
            if iid not in self.records:
                self.records[iid] = Recording(camera)
        self._next_ping = 0
        self._generation = getattr(self, "_generation", 0) + 1
        self.changed.emit()

    def _own(self, thread: QThread) -> None:
        self._threads.add(thread)
        thread.finished.connect(self._thread_finished)
        thread.start()

    @Slot()
    def _thread_finished(self) -> None:
        """Освобождать объекты только по нативному QThread.finished."""
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        self._threads.discard(thread)
        if isinstance(thread, StopRecordingThread):
            iid = self._stoppers.pop(thread, None)
            record = self.records.get(iid)
            if record:
                if not thread.ok and (
                    thread.worker.is_running or thread.worker.isRunning()
                ):
                    record.status = "error"
                    record.error = "Ожидается завершение записи камеры"
                elif record.worker is thread.worker:
                    self._finish_record(iid, thread.worker)
        elif thread is self._ping:
            self._ping = None
        else:
            for iid, record in list(self.records.items()):
                if record.worker is thread:
                    # Stopper ещё читает worker: сохраняем объект до его финиша.
                    if any(stop.worker is thread for stop in self._stoppers):
                        self.changed.emit()
                        return
                    self._finish_record(iid, thread)
                    break
        if isinstance(thread, StopRecordingThread):
            worker = thread.worker
            if (
                worker not in self._threads
                and not worker.isRunning()
                and all(r.worker is not worker for r in self.records.values())
            ):
                worker.deleteLater()
        # Ошибка остановки оставляет живой worker в records и _threads.
        if not any(r.worker is thread for r in self.records.values()) and not any(
            stop.worker is thread for stop in self._stoppers
        ):
            thread.deleteLater()
        self.changed.emit()

    def start(self, iid: str) -> None:
        if self._shutdown:
            raise ValueError("Приложение завершает работу")
        record = self.records[iid]
        if record.worker or record.intent:
            raise ValueError("Запись уже запущена или ожидает подключения")
        active = sum(bool(r.worker or r.intent) for r in self.records.values())
        if active >= self.policy.max_simultaneous:
            raise ValueError("Достигнут лимит одновременных записей")
        if not record.online:
            raise ValueError("Камера недоступна")
        record.intent = True
        record.limit_started = time.time()
        record.deadline = (
            time.monotonic() + self.policy.max_record_hours * 3600
            if self.policy.max_record_hours
            else 0
        )
        record.retries = 0
        self._launch(iid)

    def _launch(self, iid: str) -> None:
        record = self.records.get(iid)
        if (
            not record
            or self._shutdown
            or not record.intent
            or record.worker
            or record.removed
        ):
            return
        try:
            output = record.camera.record_file()
        except (ValueError, OSError):
            record.intent = False
            record.status, record.error = "error", "Некорректный путь или шаблон записи"
            self.changed.emit()
            return
        worker = CameraRecordWorker(
            iid,
            record.camera.rtsp_url,
            output,
            self,
            policy=self.policy,
            secrets=(record.camera.login, record.camera.password),
        )
        worker.started_rec.connect(self._started)
        worker.progress.connect(self._progress)
        record.worker = worker
        record.status, record.error = "starting", ""
        record.path, record.size = output, 0
        record.elapsed = 0
        record.segment_started = time.monotonic()
        record.started = time.time()
        self._own(worker)
        self.changed.emit()

    @Slot(str, object)
    def _started(self, iid: str, session_id: object) -> None:
        record = self.records.get(iid)
        if record and record.worker is self.sender():
            record.path = record.worker.output_file or record.path
            self.changed.emit()

    @Slot(str, object)
    def _progress(self, iid: str, size: int) -> None:
        record = self.records.get(iid)
        if record and record.worker is self.sender():
            record.path = record.worker.output_file or record.path
            record.size = size
            if record.status != "stopping":
                record.status = "recording"
            record.retries = 0
            self.changed.emit()

    def stop(self, iid: str) -> None:
        record = self.records.get(iid)
        if not record:
            return
        record.intent = False
        record.retry_at = 0
        if any(value == iid for value in self._stoppers.values()):
            return
        if record.worker:
            record.status = "stopping"
            # Запрос отмены ставится до передачи ожидания фоновому потоку.
            if isinstance(record.worker, CameraRecordWorker):
                record.worker.stop()
            stopper = StopRecordingThread(record.worker, self)
            self._stoppers[stopper] = iid
            self._own(stopper)
        else:
            record.status = "idle"
            if record.removed:
                del self.records[iid]
        self.changed.emit()

    def completed_recordings(self, camera_iids: list[str]) -> list[dict]:
        """Только завершённые локальные файлы; активный путь не выдаётся."""
        selected = set(camera_iids)
        active = {
            record.path for record in self.records.values() if record.worker is not None
        }
        items = [
            dict(item)
            for item in self._history
            if item.get("camera_iid") in selected and item.get("file") not in active
        ]
        known = {item["file"] for item in items}
        for iid in camera_iids:
            record = self.records.get(iid)
            if (
                record
                and record.path
                and record.worker is None
                and record.path not in active
                and record.path not in known
            ):
                items.append(
                    {
                        "file": record.path,
                        "caption": record.camera.caption,
                        "started": record.started,
                    }
                )
        available = []
        for item in items:
            try:
                if (
                    not Path(item["file"]).is_file()
                    or Path(item["file"]).stat().st_size <= 0
                ):
                    continue
            except OSError:
                continue
            started = item.get("started", 0)
            item["started"] = (
                started
                if type(started) in (int, float)
                and math.isfinite(started)
                and started > 0
                else 0
            )
            item["caption"] = (
                item.get("caption") if isinstance(item.get("caption"), str) else ""
            )
            available.append(item)
        return sorted(available, key=lambda item: (item["started"], item["file"]))

    def stop_all(self) -> None:
        for iid in list(self.records):
            self.stop(iid)

    def _finish_record(self, iid: str, worker: CameraRecordWorker) -> None:
        record = self.records.get(iid)
        if record is None or record.worker is not worker:
            return
        output = getattr(worker, "output_file", None)
        if isinstance(output, str) and output:
            record.path = output
        if record.path:
            self._history.append(
                {
                    "file": record.path,
                    "camera_iid": iid,
                    "caption": record.camera.caption,
                    "started": record.started,
                }
            )
        record.worker = None
        record.elapsed = max(0, time.time() - record.started) if record.started else 0
        if record.removed:
            del self.records[iid]
            return
        record.error = worker.reason
        if worker.permanent:
            record.intent = False
        if not record.intent or self._shutdown:
            record.status = "error" if record.error else "idle"
        else:
            record.status = "reconnecting"
            delay = (
                0
                if worker.rotate
                else min(self.policy.retry_max, 3 * 2 ** min(record.retries, 8))
            )
            record.retries = 0 if worker.rotate else record.retries + 1
            record.retry_at = time.monotonic() + delay

    @Slot(str, bool)
    def _ping_result(self, iid: str, online: bool) -> None:
        if self._shutdown:
            return
        record = self.records.get(iid)
        sender = self.sender()
        if (
            not record
            or record.removed
            or getattr(sender, "generation", None) != self._generation
        ):
            return
        record.failures = 0 if online else record.failures + 1
        if online or record.failures >= self.policy.offline_failures:
            record.online = online
        # Прогресс записи надёжнее отдельного TCP-пинга: запись контролирует worker.
        self.changed.emit()

    def _tick(self) -> None:
        now = time.monotonic()
        if self._shutdown:
            if not self._threads:
                self._timer.stop()
            return
        for iid, record in list(self.records.items()):
            if self.policy.max_record_hours and record.intent:
                elapsed = max(0, time.time() - record.limit_started)
                if not record.deadline:
                    record.deadline = now + max(
                        0, self.policy.max_record_hours * 3600 - elapsed
                    )
                if now >= record.deadline:
                    self.stop(iid)
                    record.error = "Достигнут лимит длительности записи"
            elif not self.policy.max_record_hours:
                record.deadline = 0
            if (
                record.intent
                and not record.worker
                and record.retry_at <= now
                and record.online
            ):
                self._launch(iid)
        if self._ping is None and now >= self._next_ping:
            self._generation = getattr(self, "_generation", 0) + 1
            self._ping = CameraPingWorker(
                [r.camera for r in self.records.values() if not r.removed],
                self.policy.timeout,
                self,
                max_workers=self.policy.ping_workers,
            )
            self._ping.generation = self._generation
            self._ping.result.connect(self._ping_result)
            self._next_ping = now + self.policy.ping_interval
            self._own(self._ping)
        self.changed.emit()

    def request_shutdown_recordings(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        for thread in list(self._threads):
            if isinstance(thread, CameraPingWorker):
                thread.requestInterruption()
        self.stop_all()

    def has_pending_workers(self) -> bool:
        return bool(self._threads or any(r.worker for r in self.records.values()))

    def shutdown_all_recordings(self, timeout: float = 8.0) -> None:
        """Совместимый неблокирующий запрос завершения; ожидание ведёт приложение."""
        self.request_shutdown_recordings()
