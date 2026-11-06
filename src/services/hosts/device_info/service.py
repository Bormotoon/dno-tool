"""Координатор запросов, ограниченный кэш и независимые снимки по владельцу."""

from __future__ import annotations

import copy
import hashlib
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from src.config import config
from src.domain.device_identity import device_identity
from src.domain.device_info import GOOD_STATUSES, DeviceInfoSnapshot, InfoStatus
from src.domain.models.device import DeviceModel
from src.services.base import BaseService

from .catalog import BRIEF_CATEGORIES, INFO_CATEGORIES


class DeviceInfoService(BaseService):
    """Не QObject: потоки удерживаются до настоящего finished, GUI их не ждёт."""

    def __init__(self) -> None:
        super().__init__()
        self._jobs: dict[tuple[Any, ...], Any] = {}
        self._consumers: dict[str, set[int]] = {}
        self._cache: OrderedDict[tuple[Any, ...], DeviceInfoSnapshot] = OrderedDict()
        self._windows: dict[tuple[str, str], Any] = {}

    def signature(self, device: DeviceModel) -> tuple[Any, ...]:
        """В ключах нет секретов; чужой владелец не использует SSH-профиль оператора."""
        owner, iid = device_identity(device)
        if owner != "local":
            record = next(
                (node for node in config.app.nodes.nodes if node.iid == owner), None
            )
            digest = hashlib.sha256(repr(record).encode()).hexdigest()
            return owner, iid, device.host, device.port, digest
        settings = (
            config.app.ssh,
            config.app.credentials,
            device.credential_bundle_iid,
            device.use_sudo,
            device.locale,
        )
        digest = hashlib.sha256(repr(settings).encode()).hexdigest()
        return owner, iid, device.host, device.port or config.app.ssh.port or 22, digest

    def snapshot(
        self,
        device: DeviceModel,
        profile: str,
        names: tuple[str, ...],
        owner_name: str = "",
    ) -> DeviceInfoSnapshot:
        owner, _ = device_identity(device)
        return DeviceInfoSnapshot(
            device_identity(device),
            device.name,
            device.host,
            owner_name or ("Локальный экземпляр" if owner == "local" else owner),
            "ssh" if owner == "local" else "node",
            (
                f"{device.host}:{device.port or config.app.ssh.port or 22}"
                if owner == "local"
                else "Неизвестен (настройки владельца)"
            ),
            profile=profile,
            expected_categories=names,
        )

    @staticmethod
    def names_for_profile(profile: str) -> tuple[str, ...]:
        return tuple(INFO_CATEGORIES) if profile == "full" else tuple(BRIEF_CATEGORIES)

    def cached(self, device: DeviceModel) -> DeviceInfoSnapshot | None:
        key = self.signature(device)
        snapshot = self._cache.get(key)
        if snapshot is None:
            return None
        self._cache.move_to_end(key)
        return copy.deepcopy(snapshot)

    def fresh(self, snapshot: DeviceInfoSnapshot, names: tuple[str, ...]) -> bool:
        from .catalog import HARDWARE_CATEGORIES

        now = datetime.now(timezone.utc)
        for name in names:
            category = snapshot.categories.get(name)
            if category is None or category.status not in GOOD_STATUSES:
                return False
            age = (now - datetime.fromisoformat(category.timestamp)).total_seconds()
            if age > (300 if name in HARDWARE_CATEGORIES else 30):
                return False
        return True

    def request(
        self,
        device: DeviceModel,
        profile: str,
        names: tuple[str, ...],
        consumer: int,
        owner_name: str = "",
    ) -> Any:
        from src.workers.device_info import DeviceInfoWorker

        key = (*self.signature(device), names, profile)
        worker: Any = self._jobs.get(key)
        if worker is None:
            snapshot = self.snapshot(device, profile, names, owner_name)
            worker = DeviceInfoWorker(
                device, {name: INFO_CATEGORIES[name] for name in names}, snapshot
            )
            self._jobs[key] = worker
            self._consumers[snapshot.request_id] = set()
            # Подключение из GUI-потока обеспечивает доставку после result_ready.
            worker.finished.connect(
                lambda key=key, worker=worker: self._retire(key, worker)
            )
        self._consumers[worker.snapshot.request_id].add(consumer)
        return worker

    def _retire(self, key: tuple[Any, ...], worker: Any) -> None:
        assert worker is not None
        if self._jobs.get(key) is not worker:
            return
        assert worker is not None
        self._jobs.pop(key, None)
        self._consumers.pop(worker.snapshot.request_id, None)
        snapshot = worker.snapshot
        if snapshot.finished_at and snapshot.status != InfoStatus.CANCELLED:
            # Снимки не объединяются: новый неполный запрос не делает старые данные свежими.
            safe = copy.deepcopy(snapshot)
            safe.categories = {
                name: replace(
                    cat,
                    commands=tuple(
                        replace(cmd, stdout="", stderr="") for cmd in cat.commands
                    ),
                )
                for name, cat in safe.categories.items()
            }
            self._cache[key[:-2]] = safe
            self._cache.move_to_end(key[:-2])
            while len(self._cache) > 32:
                self._cache.popitem(last=False)
        worker.deleteLater()

    def release(self, worker: Any, consumer: int) -> None:
        consumers = self._consumers.get(worker.snapshot.request_id)
        if consumers is not None:
            consumers.discard(consumer)
            if not consumers:
                worker.abort()

    def has_pending_workers(self) -> bool:
        return bool(self._jobs)

    def available_snapshots(self) -> list[DeviceInfoSnapshot]:
        snapshots = {snapshot.request_id: snapshot for snapshot in self._cache.values()}
        for window in self._windows.values():
            snapshot = window.controller.snapshot
            if snapshot is not None:
                snapshots[snapshot.request_id] = snapshot
        return copy.deepcopy(list(snapshots.values()))

    def shutdown(self) -> None:
        for worker in list(self._jobs.values()):
            worker.abort()

    def dialog(self, device: DeviceModel, parent: Any = None) -> Any:
        """Одна немодальная карточка на устойчивую пару владелец/iid."""
        from src.ui.dialogs.device.device_info import DeviceInfoDialog

        key = device_identity(device)
        existing = self._windows.get(key)
        if existing is not None and not existing._closing:
            return existing
        dialog = DeviceInfoDialog(device, parent, service=self)
        self._windows[key] = dialog
        from PySide6.QtCore import Qt

        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(
            lambda _code, key=key, dialog=dialog: self._forget_window(key, dialog)
        )
        return dialog

    def open_dialog(self, device: DeviceModel, parent: Any = None) -> Any:
        dialog = self.dialog(device, parent)
        dialog.showNormal()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def _forget_window(self, key: tuple[str, str], dialog: Any) -> None:
        if self._windows.get(key) is dialog:
            self._windows.pop(key, None)


def get_device_info_service() -> DeviceInfoService:
    """Резолвить зарегистрированный singleton, в изолированном UI — локальный экземпляр."""
    from src.di import get_container

    container = get_container()
    service: DeviceInfoService | None = container.resolve_optional(DeviceInfoService)
    if service is None:
        service = DeviceInfoService()
        container.register_instance(DeviceInfoService, service)
    return service
