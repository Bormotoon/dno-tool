"""GUI-координатор поколения запроса и асинхронного завершения."""

from __future__ import annotations

import copy
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from src.domain.device_info import CategoryResult, DeviceInfoSnapshot, InfoStatus
from src.domain.models.device import DeviceModel
from src.services.hosts.device_info import DeviceInfoService


class DeviceInfoController(QObject):
    changed = Signal()
    ended = Signal()
    progress_changed = Signal(int, int, str)

    def __init__(
        self,
        device: DeviceModel,
        service: DeviceInfoService,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.device = device
        self.service = service
        self.worker: Any = None
        self.snapshot: DeviceInfoSnapshot | None = service.cached(device)
        self.previous: DeviceInfoSnapshot | None = None
        self._signature: tuple[Any, ...] = ()
        self._request_id = ""
        self.closing = False

    def start(self, profile: str, names: tuple[str, ...], owner_name: str = "") -> None:
        if self.closing or self.worker is not None:
            return
        self.previous = copy.deepcopy(self.snapshot)
        self._signature = self.service.signature(self.device)
        worker = self.service.request(self.device, profile, names, id(self), owner_name)
        self.worker = worker
        self.snapshot = worker.snapshot_copy()
        self.snapshot.status = InfoStatus.CONNECTING
        self._request_id = self.snapshot.request_id
        worker.category_ready.connect(self._category)
        worker.progress.connect(self._progress)
        worker.finished.connect(self._finished)
        self.changed.emit()
        if worker.isFinished():
            self._finished()
        elif not worker.isRunning():
            worker.start()

    def _current(self, request_id: str) -> bool:
        return (
            not self.closing
            and request_id == self._request_id
            and self.service.signature(self.device) == self._signature
        )

    @Slot(str, object)
    def _category(self, request_id: str, category: CategoryResult) -> None:
        if self.snapshot is not None and self._current(request_id):
            self.snapshot.categories[category.name] = copy.deepcopy(category)
            self.changed.emit()

    @Slot(str, int, int, str)
    def _progress(self, request_id: str, done: int, total: int, message: str) -> None:
        if self.snapshot is not None and self._current(request_id):
            self.snapshot.status = (
                InfoStatus.COLLECTING if done else InfoStatus.CONNECTING
            )
            self.progress_changed.emit(done, total, message)

    @Slot()
    def _finished(self) -> None:
        worker = self.worker
        if worker is None:
            return
        if self.snapshot is not None and (
            self.closing or self._current(self._request_id)
        ):
            self.snapshot = copy.deepcopy(worker.snapshot)
        elif self.snapshot is not None:
            self.snapshot.status = InfoStatus.FAILED
            self.snapshot.diagnostic = (
                "Устройство или настройки подключения изменились; запустите новый сбор"
            )
        for signal, handler in (
            (worker.category_ready, self._category),
            (worker.progress, self._progress),
            (worker.finished, self._finished),
        ):
            try:
                signal.disconnect(handler)
            except (RuntimeError, TypeError):
                pass
        self.worker = None
        self.changed.emit()
        self.ended.emit()

    def stop(self) -> None:
        if self.worker is not None:
            if self.snapshot is not None:
                self.snapshot.status = InfoStatus.CANCELLING
            self.service.release(self.worker, id(self))
            self.changed.emit()

    def close(self) -> None:
        self.closing = True
        self.stop()

    def invalidate(self) -> None:
        """Удалённое устройство не получает запоздалые данные."""
        self._request_id = ""
        self.stop()
