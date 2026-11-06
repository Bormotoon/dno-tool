"""Backend-independent Qt thumbnail scheduling, with legacy Veyon aliases."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import QObject, QTimer, Signal

from src.config import config
from src.config.settings import ABSOLUTE_MAX_THUMBNAIL_DIMENSION
from src.domain.models.device import DeviceModel
from src.logger import logger
from src.services.thumbnails.types import ThumbnailResult, ThumbnailStatus
from src.workers.gui_relay import post_to_gui

if TYPE_CHECKING:
    from src.services.thumbnails.thumbnail_service import ThumbnailService


def _get_thumbnail_service() -> ThumbnailService:
    from src.di import get_container
    from src.services.thumbnails.thumbnail_service import ThumbnailService

    return get_container().resolve(ThumbnailService)


class ThumbnailItem(Protocol):
    """Minimal item contract, without a dependency on GUI widgets."""

    device: DeviceModel
    _thumbnail_capture_revision: tuple | None


class ThumbnailTimer(QObject):
    """Schedule captures in the dedicated pool and reject obsolete responses."""

    def __init__(
        self,
        device: DeviceModel,
        item: ThumbnailItem,
        on_result: Callable[[ThumbnailItem, bytes | None, ThumbnailResult], None],
        interval: int | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._device = device
        self._host = device.host
        self._item = item
        self._on_result = on_result
        self._service = _get_thumbnail_service()
        self._consumer = ""
        self._interval = (interval or config.app.thumbnails.interval_sec) * 1000
        self._icon_width = config.app.thumbnails.width
        self._scale = config.app.thumbnails.scale
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._execute_capture)
        self._initial_timer = QTimer(self)
        self._initial_timer.setSingleShot(True)
        self._initial_timer.timeout.connect(self._execute_capture)
        self._is_running = False
        self._pending = False
        self._generation = 0
        self._capture_serial = 0
        self._pending_token: tuple | None = None
        self._revision: tuple | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def device(self) -> DeviceModel:
        return self._device

    @property
    def is_running(self) -> bool:
        return self._is_running

    def _dimensions(self) -> tuple[int, int]:
        width = max(
            1,
            min(
                min(
                    config.app.thumbnails.resources.max_thumbnail_dimension,
                    ABSOLUTE_MAX_THUMBNAIL_DIMENSION,
                ),
                self._icon_width * self._scale,
            ),
        )
        return width, max(1, width * 9 // 16)

    def invalidate(self) -> None:
        """Отбросить результат текущего запроса, не блокируя GUI сетевой операцией.

        Запрос остаётся незавершённым до настоящего ответа: новый запрос к тому же
        источнику получил бы BUSY. Отброшенный кадр заменяет _handle_capture_result.
        """
        self._generation += 1

    def update_device(
        self, device: DeviceModel, item: ThumbnailItem | None = None
    ) -> None:
        width, height = self._dimensions()
        revision = self._service.revision(device, width, height)
        if self._device is not device or self._revision != revision:
            self.invalidate()
        self._device = device
        self._host = device.host
        self._revision = revision
        if item is not None:
            self._item = item

    def update_interval(self, seconds: int) -> None:
        self._interval = max(1, min(60, seconds)) * 1000

    def update_icon_width(self, width: int) -> None:
        if self._icon_width != width:
            self._icon_width = width
            self.invalidate()

    def update_scale(self, scale: int) -> None:
        if self._scale != scale:
            self._scale = scale
            self.invalidate()

    def reset_timer(self) -> None:
        if self._is_running:
            self._reschedule_with_jitter()

    def _get_jittered_interval(self) -> int:
        jitter = int(self._interval * 0.1)
        return self._interval + random.randint(-jitter, jitter)

    def _reschedule_with_jitter(self) -> None:
        if self._is_running:
            self._timer.start(self._get_jittered_interval())

    def start(self, immediate: bool = True) -> None:
        if self._is_running:
            return
        self._is_running = True
        self.invalidate()
        self._consumer = f"qt:{id(self)}:{self._generation}"
        self._service.subscribe(self._device, self._consumer)
        self._reschedule_with_jitter()
        if immediate:
            stagger = max(0, int(config.app.thumbnails.stagger_sec * 1000))
            self._initial_timer.start(random.randint(0, stagger))

    def stop(self) -> None:
        was_running = self._is_running
        self._is_running = False
        self._timer.stop()
        self._initial_timer.stop()
        self.invalidate()
        if was_running:
            self._service.release(self._device.iid, self._consumer)

    def _execute_capture(self) -> None:
        if not self._is_running or self._pending:
            return
        self._initial_timer.stop()
        self._timer.stop()
        if config.app.thumbnails.require_online and not self._device.is_online:
            self._reschedule_with_jitter()
            return
        from src.workers.network import get_thumbnail_worker_pool

        try:
            service = self._service
            pool = get_thumbnail_worker_pool()
            device = self._device
            width, height = self._dimensions()
            revision = service.revision(device, width, height)
            self._revision = revision
            self._capture_serial += 1
            token = (self._generation, self._capture_serial, revision)
            consumer = self._consumer
            self._pending_token = token
            self._pending = True

            def capture() -> ThumbnailResult:
                # All readiness/port/authentication checks belong in this worker.
                # The facade creates an immutable request and guards its version.
                if (
                    token[0] != self._generation
                    or not self._is_running
                    or service.revision(device, width, height) != revision
                ):
                    return ThumbnailResult(status=ThumbnailStatus.CANCELLED)
                try:
                    return service.get_thumbnail(
                        device,
                        width=width,
                        height=height,
                        online=device.is_online,
                        consumer=consumer,
                    )
                finally:
                    # A queued/in-flight worker must not resurrect an old lease.
                    if consumer != self._consumer or not self._is_running:
                        service.release(device.iid, consumer)

            timer_ref = self
            handle_result = self._handle_capture_result

            def completed(task_id: str, result: ThumbnailResult) -> None:
                post_to_gui(timer_ref, handle_result, token, result)

            def failed(task_id: str, error: str) -> None:
                completed(task_id, ThumbnailResult(error_code="capture_failed"))

            pool.submit(
                capture,
                task_id=f"thumbnail_{device.iid}_{id(self)}_{self._capture_serial}",
                on_complete=completed,
                on_error=failed,
                metadata={
                    "host": device.host,
                    "type": "thumbnail",
                    "backend": device.thumbnail_backend
                    or config.app.thumbnails.backend,
                },
            )
        except (RuntimeError, ValueError, KeyError) as exc:
            self._pending = False
            self._pending_token = None
            logger.warning(f"ThumbnailTimer: capture submission failed: {exc}")
            self._on_result(
                self._item,
                None,
                ThumbnailResult(
                    backend=self._device.thumbnail_backend
                    or config.app.thumbnails.backend,
                    error_code="capture_submission_failed",
                ),
            )
            self._reschedule_with_jitter()

    def _handle_capture_result(self, token: tuple, result: ThumbnailResult) -> None:
        if token != self._pending_token:
            return
        self._pending = False
        self._pending_token = None
        if not self._is_running:
            return
        if token[0] != self._generation:
            # Модель или настройки сменились во время запроса: кадр устарел, новый
            # нужен сразу. Без этого следующего запроса не будет: таймер уже остановлен.
            self._initial_timer.start(0)
            return
        width, height = self._dimensions()
        if (
            self._service.revision(self._device, width, height) == token[2]
            and result.status != ThumbnailStatus.CANCELLED
        ):
            self._on_result(self._item, result.data, result)
        self._reschedule_with_jitter()


class ThumbnailTimerManager(QObject):
    """Own one timer per iid and idempotent subscriptions per list item."""

    thumbnail_ready = Signal(object, object, object)
    host_offline = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timers: dict[str, ThumbnailTimer] = {}
        self._owners: dict[str, dict[int, ThumbnailItem]] = {}
        self._ref_counts: dict[str, int] = {}
        self._items: dict[str, ThumbnailItem] = {}
        self._mode_enabled = False
        self._icon_width = config.app.thumbnails.width

    def set_mode_enabled(self, enabled: bool) -> None:
        self._mode_enabled = enabled
        if not enabled:
            self.stop_all()
        else:
            self.apply_online_gating()

    def start_thumbnail(self, device: DeviceModel, item: ThumbnailItem) -> None:
        owners = self._owners.setdefault(device.iid, {})
        owners[id(item)] = item
        self._items[device.iid] = item
        self._ref_counts[device.iid] = len(owners)
        self.update_device(device)

    def update_device(self, device: DeviceModel) -> None:
        """Refresh an in-place edit, including endpoint and backend changes."""
        iid = device.iid
        if iid not in self._items:
            return
        allowed = self._mode_enabled and (
            device.is_online or not config.app.thumbnails.require_online
        )
        if not allowed:
            self._remove_timer(iid)
            return
        timer = self._timers.get(iid)
        if timer is None:
            self._start_timer_for_iid(iid, self._items[iid])
        else:
            previous_revision = timer._revision
            timer.update_device(device, self._items[iid])
            # Repeated status notifications must not push the next capture
            # into the future forever, especially with a long interval.
            if timer._revision != previous_revision:
                timer.reset_timer()

    def _start_timer_for_iid(self, iid: str, item: ThumbnailItem) -> None:
        device = getattr(item, "device", None)
        if device is None:
            return
        timer = ThumbnailTimer(device, item, self._on_timer_result, parent=self)
        timer.update_icon_width(self._icon_width)
        timer.update_device(device)
        self._timers[iid] = timer
        timer.start()

    def _remove_timer(self, iid: str) -> None:
        timer = self._timers.pop(iid, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
            # release() schedules backend closure internally, never network I/O here.

    def stop_thumbnail(self, iid: str, item: ThumbnailItem | None = None) -> None:
        owners = self._owners.get(iid)
        if not owners:
            return
        if item is None:
            owners.clear()  # Legacy iid-only callers remove the whole device.
        else:
            owners.pop(id(item), None)
        if owners:
            self._ref_counts[iid] = len(owners)
            self._items[iid] = next(iter(owners.values()))
            timer = self._timers.get(iid)
            if timer is not None:
                timer.update_device(self._items[iid].device, self._items[iid])
            return
        self._remove_timer(iid)
        self._owners.pop(iid, None)
        self._ref_counts.pop(iid, None)
        self._items.pop(iid, None)

    def stop_all(self) -> None:
        """Suspend captures, retaining item ownership for the next mode toggle."""
        for iid in list(self._timers):
            self._remove_timer(iid)

    def set_interval(self, seconds: int) -> None:
        for timer in self._timers.values():
            timer.update_interval(seconds)
            timer.reset_timer()

    def apply_online_gating(self) -> None:
        for item in list(self._items.values()):
            self.update_device(item.device)

    def update_icon_width(self, width: int) -> None:
        self._icon_width = width
        for timer in self._timers.values():
            timer.update_icon_width(width)
            timer.reset_timer()

    def update_scale(self, scale: int) -> None:
        for timer in self._timers.values():
            timer.update_scale(scale)
            timer.reset_timer()

    def refresh_settings(self) -> None:
        self.set_interval(config.app.thumbnails.interval_sec)
        self.update_scale(config.app.thumbnails.scale)
        self.apply_online_gating()

    def get_subscription_revision(self, iid: str) -> tuple | None:
        """Return the current GUI-safe token for this active capture subscription."""
        timer = self._timers.get(iid)
        if timer is None:
            return None
        return timer._service.revision(timer.device, *timer._dimensions())

    def on_host_online(self, device: DeviceModel, is_online: bool) -> None:
        for item in list(self._items.values()):
            if item.device.iid == device.iid:
                self.update_device(item.device)
        if not is_online:
            self.host_offline.emit(device.host)

    def _on_timer_result(
        self, item: ThumbnailItem, data: bytes | None, result: ThumbnailResult
    ) -> None:
        for owner in list(self._owners.get(item.device.iid, {}).values()):
            timer = self._timers.get(item.device.iid)
            owner._thumbnail_capture_revision = timer._revision if timer else None
            self.thumbnail_ready.emit(owner, data, result)

    @property
    def active_count(self) -> int:
        return len(self._timers)

    def get_metrics(self, service: object | None = None) -> dict:
        metrics = {
            "active_timers": len(self._timers),
            "pending_captures": sum(timer._pending for timer in self._timers.values()),
        }
        try:
            metrics.update(_get_thumbnail_service().get_metrics())
        except (KeyError, RuntimeError, ValueError, OSError) as exc:
            logger.debug(f"ThumbnailTimerManager: metrics unavailable: {exc}")
        return metrics


# Compatibility aliases share the neutral implementation and factory instance.
VeyonThumbnailTimer = ThumbnailTimer
VeyonThumbnailTimerManager = ThumbnailTimerManager
_veyon_thumbnail_timer_manager: ThumbnailTimerManager | None = None


def get_thumbnail_timer_manager() -> ThumbnailTimerManager:
    global _veyon_thumbnail_timer_manager
    if _veyon_thumbnail_timer_manager is None:
        _veyon_thumbnail_timer_manager = ThumbnailTimerManager()
    return _veyon_thumbnail_timer_manager


def reset_thumbnail_timer_manager() -> None:
    global _veyon_thumbnail_timer_manager
    if _veyon_thumbnail_timer_manager is not None:
        _veyon_thumbnail_timer_manager.stop_all()
        _veyon_thumbnail_timer_manager.deleteLater()
    _veyon_thumbnail_timer_manager = None


get_veyon_thumbnail_timer_manager = get_thumbnail_timer_manager
reset_veyon_thumbnail_timer_manager = reset_thumbnail_timer_manager
