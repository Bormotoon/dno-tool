"""QThread-обёртка над общим сбором сведений об устройстве."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QThread, Signal

from src.domain.device_info import DeviceInfoSnapshot
from src.domain.models.device import DeviceModel
from src.services.hosts.device_info.collector import DeviceInfoCollector


class DeviceInfoWorker(QThread):
    """Сигнал QThread.finished сохраняет штатную семантику завершения потока."""

    category_ready = Signal(str, object)
    progress = Signal(str, int, int, str)
    result_ready = Signal(object)

    def __init__(
        self,
        device: DeviceModel,
        categories: dict[str, Any],
        snapshot: DeviceInfoSnapshot,
    ) -> None:
        super().__init__()
        self._collector = DeviceInfoCollector(
            device,
            categories,
            snapshot,
            on_category=self.category_ready.emit,
            on_progress=self.progress.emit,
        )

    @property
    def device(self) -> DeviceModel:
        return self._collector.device

    @property
    def categories(self) -> dict[str, Any]:
        return self._collector.categories

    @property
    def snapshot(self) -> DeviceInfoSnapshot:
        return self._collector.snapshot

    def snapshot_copy(self) -> DeviceInfoSnapshot:
        return self._collector.snapshot_copy()

    def abort(self) -> None:
        """GUI только выставляет флаг; закрытие сокета выполняется вне GUI."""
        self._collector.abort()

    def run(self) -> None:
        self.result_ready.emit(self._collector.run())


class NodeInfoFetcher(DeviceInfoWorker):
    """Совместимое имя адаптера; маршрут определяется снимком и владельцем."""
