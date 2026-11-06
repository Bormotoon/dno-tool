"""Backend-neutral thumbnail polling of owner nodes, with bounded pool work."""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import QObject, QTimer, Signal

from src.config import config
from src.config.settings import ABSOLUTE_MAX_THUMBNAIL_DIMENSION
from src.domain.models.device import DeviceModel
from src.domain.device_identity import DeviceKey, device_identity, device_key
from src.logger import logger
from src.services.thumbnails.types import ThumbnailResult, ThumbnailStatus
from src.workers.gui_relay import post_to_gui

LOCAL_NODE_IID = "local"

if TYPE_CHECKING:
    from src.services.remote.node_service import NodeService


class _ThumbnailItem(Protocol):
    device: DeviceModel


_CaptureToken = tuple[int, int, tuple[object, ...]]


def _configured_request_width() -> int:
    """Owner-bound request width; capped only by the absolute ceiling."""
    return max(
        1,
        min(
            ABSOLUTE_MAX_THUMBNAIL_DIMENSION,
            int(config.app.thumbnails.width) * int(config.app.thumbnails.scale),
        ),
    )


def _model_revision(device: DeviceModel) -> tuple[object, ...]:
    return (
        device.iid,
        getattr(device, "node_iid", None),
        device.host,
        getattr(device, "port", None),
        getattr(device, "vnc_port", None),
        getattr(device, "thumbnail_backend", None),
        config.app.thumbnails.width,
        config.app.thumbnails.scale,
    )


class NodeThumbnailTimer(QObject):
    """Schedule one request at a time and discard delayed model/stop responses."""

    MAX_CONCURRENT_PER_HOST = 1
    def __init__(
        self,
        device: DeviceModel,
        item: _ThumbnailItem,
        node_service: NodeService,
        on_result: Callable[[_ThumbnailItem, bytes | None, ThumbnailResult], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._device = device
        self._item = item
        self._svc = node_service
        self._node_iid = getattr(device, "node_iid", None) or ""
        self._model_revision = _model_revision(device)
        self._on_result = on_result
        self._interval_ms = max(1, int(config.app.thumbnails.interval_sec)) * 1000
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)
        self._initial_timer = QTimer(self)
        self._initial_timer.setSingleShot(True)
        self._initial_timer.timeout.connect(self._tick)
        self._is_running = False
        self._node_reachable = True
        self._pending = False
        self._generation = 0
        self._serial = 0
        self._pending_token: _CaptureToken | None = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    def _get_jittered_interval(self) -> int:
        jitter = int(self._interval_ms * 0.1)
        return max(1000, self._interval_ms + random.randint(-jitter, jitter))

    def _reschedule_with_jitter(self) -> None:
        if self._is_running and self._node_reachable:
            self._timer.start(self._get_jittered_interval())

    def set_node_reachable(self, reachable: bool) -> None:
        """Pause on loss, retaining in-flight ownership until completion."""
        if self._node_reachable == reachable:
            return
        self._node_reachable = reachable
        self._generation += 1
        self._timer.stop()
        self._initial_timer.stop()
        if reachable and self._is_running and not self._pending:
            self._initial_timer.start(0)

    def reset_timer(self) -> None:
        if self._is_running:
            self._reschedule_with_jitter()

    def update_device(
        self, device: DeviceModel, item: _ThumbnailItem | None = None
    ) -> None:
        changed = (
            device is not self._device
            or _model_revision(device) != self._model_revision
        )
        if item is not None and item is not self._item:
            changed = True
        if changed:
            self._generation += 1
        self._device = device
        self._model_revision = _model_revision(device)
        self._node_iid = getattr(device, "node_iid", None) or ""
        if item is not None:
            self._item = item

    def start(
        self, immediate: bool = True, initial_delay_ms: int | None = None
    ) -> None:
        if self._is_running:
            return
        self._generation += 1
        self._is_running = True
        self._reschedule_with_jitter()
        if immediate and self._node_reachable:
            delay = initial_delay_ms
            if delay is None:
                delay = random.randint(
                    0, max(0, int(config.app.thumbnails.stagger_sec * 1000))
                )
            self._initial_timer.start(max(0, delay))

    def stop(self) -> None:
        self._generation += 1
        self._is_running = False
        self._timer.stop()
        self._initial_timer.stop()
        # Do not clear a running operation. A restart must not queue another
        # request until its old bounded operation has completed.

    def _current(self, token: _CaptureToken, item: _ThumbnailItem) -> bool:
        device = getattr(item, "device", None)
        return (
            self._is_running
            and self._node_reachable
            and token[0] == self._generation
            and item is self._item
            and device is self._device
            and token[2] == _model_revision(self._device)
        )

    def _tick(self) -> None:
        if not self._is_running or not self._node_reachable or self._pending:
            return
        node_iid = getattr(self._device, "node_iid", None)
        if not node_iid or node_iid == LOCAL_NODE_IID:
            return
        self._timer.stop()
        self._initial_timer.stop()
        # The owner node applies require_online. The operator's setting or
        # delayed SSH status must never suppress a VNC-capable owner request.
        try:
            from src.workers.network import get_veyon_worker_pool

            pool = get_veyon_worker_pool()
        except (RuntimeError, ValueError, KeyError, OSError) as e:
            logger.debug(f"NodeThumbnailTimer: no pool: {e}")
            self._reschedule_with_jitter()
            return
        self._serial += 1
        token = (self._generation, self._serial, _model_revision(self._device))
        self._pending = True
        self._pending_token = token
        device, item = self._device, self._item
        # Owner nodes enforce their own resource policy; the operator side only
        # caps requests at the finite absolute dimension ceiling, never by the
        # local operator's own (smaller) policy.
        width = _configured_request_width()
        height = max(1, width * 9 // 16)

        def job() -> ThumbnailResult:
            if not self._current(token, item):
                return ThumbnailResult(status=ThumbnailStatus.CANCELLED)
            client = self._svc.get_client(node_iid)
            if client is None:
                return ThumbnailResult(
                    status=ThumbnailStatus.UNAVAILABLE, error_code="node_unavailable"
                )
            return client.get_thumbnail_result(device.iid, width, height)

        timer_ref = self
        handle_result = self._handle_result

        def on_complete(task_id: str, result: ThumbnailResult) -> None:
            post_to_gui(timer_ref, handle_result, token, item, result)

        def on_error(task_id: str, error: object) -> None:
            on_complete(
                task_id,
                ThumbnailResult(
                    status=ThumbnailStatus.UNAVAILABLE,
                    error_code="node_infrastructure_error",
                ),
            )

        try:
            pool.submit(
                job,
                task_id=f"node_thumb_{node_iid}_{device.iid}_{random.randint(0, 1_000_000)}",
                on_complete=on_complete,
                on_error=on_error,
                metadata={
                    "host": device.host,
                    "type": "node_thumbnail",
                    "node_iid": node_iid,
                    "device": device.name or device.host,
                },
            )
        except (RuntimeError, ValueError, KeyError, OSError) as e:
            self._pending = False
            self._pending_token = None
            logger.debug(f"NodeThumbnailTimer: submit failed: {e}")
            self._reschedule_with_jitter()

    def _handle_result(
        self, token: _CaptureToken, item: _ThumbnailItem, result: ThumbnailResult
    ) -> None:
        if token != self._pending_token:
            return
        self._pending = False
        self._pending_token = None
        if self._current(token, item) and result.status != ThumbnailStatus.CANCELLED:
            try:
                self._on_result(item, result.data, result)
            except RuntimeError:
                pass
        if self._is_running and self._node_reachable and token[0] != self._generation:
            # A reconnect/model change waited for this old operation. Replace
            # its discarded frame promptly, never by overlapping requests.
            self._initial_timer.start(0)
        else:
            self._reschedule_with_jitter()


class NodeThumbnailManager(QObject):
    """Keep idempotent per-view subscriptions across mode/node availability."""

    thumbnail_ready = Signal(object, object, object)  # item, data, ThumbnailResult
    host_offline = Signal(str)
    _initialized = False

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timers: dict[DeviceKey, NodeThumbnailTimer] = {}
        self._ref_counts: dict[DeviceKey, int] = {}
        self._items: dict[DeviceKey, _ThumbnailItem] = {}
        self._owners: dict[DeviceKey, dict[int, _ThumbnailItem]] = {}
        self._mode_enabled = False
        self._stopped_nodes: set[str] = set()
        self._disconnected_nodes: set[str] = set()
        self._request_width = _configured_request_width()

    def set_mode_enabled(self, enabled: bool) -> None:
        self._mode_enabled = bool(enabled)
        if not enabled:
            self.stop_all()
            return
        self._stopped_nodes.clear()
        for iid, item in list(self._items.items()):
            if iid not in self._timers:
                self._start_timer_for_iid(iid, item)

    def start_thumbnail(self, device: DeviceModel, item: _ThumbnailItem) -> None:
        node_iid = getattr(device, "node_iid", None)
        if not node_iid or node_iid == LOCAL_NODE_IID:
            return
        self._stopped_nodes.discard(node_iid)
        iid = device_identity(device)
        self._items[iid] = item
        owners = self._owners.setdefault(iid, {})
        owners[id(item)] = item
        self._ref_counts[iid] = len(owners)
        timer = self._timers.get(iid)
        if timer is not None:
            timer.update_device(device, item)
        elif self._mode_enabled and node_iid not in self._stopped_nodes:
            self._start_timer_for_iid(iid, item)
        if node_iid in self._disconnected_nodes:
            self.thumbnail_ready.emit(item, None, self._node_unavailable())

    def stop_thumbnail(
        self, device_iid: str, item: _ThumbnailItem | None = None, *, node_iid: str | None = None
    ) -> None:
        device_iid = device_key(device_iid, node_iid or getattr(getattr(item, "device", None), "node_iid", None))
        owners = self._owners.get(device_iid)
        if not owners:
            return
        if item is not None:
            owners.pop(id(item), None)
            if owners:
                self._ref_counts[device_iid] = len(owners)
                self._items[device_iid] = next(iter(owners.values()))
                self.update_device(self._items[device_iid].device)
                return
        representative = self._items.pop(device_iid, None)
        node_iid = getattr(getattr(representative, "device", None), "node_iid", None)
        self._owners.pop(device_iid, None)
        self._ref_counts.pop(device_iid, None)
        timer = self._timers.pop(device_iid, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        _release_node_connections_async(node_iid, [device_iid[1]])

    def stop_all(self) -> None:
        nodes: dict[str, list[str]] = {}
        for iid, item in self._items.items():
            node_iid = getattr(getattr(item, "device", None), "node_iid", None)
            if node_iid and node_iid != LOCAL_NODE_IID:
                nodes.setdefault(node_iid, []).append(iid[1])
        for timer in self._timers.values():
            timer.stop()
            timer.deleteLater()
        self._timers.clear()
        for node_iid, iids in nodes.items():
            _release_node_connections_async(node_iid, iids)

    def stop_node(self, node_iid: str) -> None:
        if not node_iid or node_iid == LOCAL_NODE_IID:
            return
        self._stopped_nodes.add(node_iid)
        self.on_node_online(node_iid, False)
        iids = [
            iid
            for iid, item in self._items.items()
            if getattr(item.device, "node_iid", None) == node_iid
        ]
        for iid in iids:
            timer = self._timers.pop(iid, None)
            if timer:
                timer.stop()
                timer.deleteLater()
        _release_node_connections_async(node_iid, [key[1] for key in iids])

    @staticmethod
    def _node_unavailable() -> ThumbnailResult:
        return ThumbnailResult(
            status=ThumbnailStatus.UNAVAILABLE, error_code="node_unavailable"
        )

    def on_node_online(self, node_iid: str, reachable: bool) -> None:
        """Invalidate all node owners immediately; network loss is not stop_node."""
        if not node_iid or node_iid == LOCAL_NODE_IID:
            return
        if reachable:
            self._disconnected_nodes.discard(node_iid)
        else:
            self._disconnected_nodes.add(node_iid)
        for iid, item in list(self._items.items()):
            if getattr(item.device, "node_iid", None) != node_iid:
                continue
            timer = self._timers.get(iid)
            if timer is not None:
                timer.set_node_reachable(reachable)
            elif (
                reachable and self._mode_enabled and node_iid not in self._stopped_nodes
            ):
                self._start_timer_for_iid(iid, item)
            if not reachable:
                # Identity-scoped notifications cannot erase a same-address
                # host from another owner node or the operator's local list.
                self._on_timer_result(item, None, self._node_unavailable())

    def update_device(self, device: DeviceModel) -> None:
        timer = self._timers.get(device_identity(device))
        if timer is not None:
            item = self._items.get(device_identity(device))
            timer.update_device(device, item)

    def on_host_online(self, device: DeviceModel, is_online: bool) -> None:
        node_iid = getattr(device, "node_iid", None)
        if not node_iid or node_iid == LOCAL_NODE_IID:
            return
        item = self._items.get(device_identity(device))
        if item is not None:
            self.update_device(item.device)
        if (
            self._mode_enabled
            and device_identity(device) in self._items
            and device_identity(device) not in self._timers
            and node_iid not in self._stopped_nodes
        ):
            self._start_timer_for_iid(device_identity(device), self._items[device_identity(device)])
        # SSH reachability is not the owner's thumbnail availability policy.
        # Only owner results or node connection loss may clear remote frames.

    def _start_timer_for_iid(self, iid: DeviceKey, item: _ThumbnailItem) -> None:
        device = getattr(item, "device", None)
        node_iid = getattr(device, "node_iid", None)
        if (
            device is None
            or not node_iid
            or node_iid == LOCAL_NODE_IID
            or node_iid in self._stopped_nodes
        ):
            return
        try:
            from src.di import get_container
            from src.services import NodeService

            svc = get_container().resolve(NodeService)
        except (RuntimeError, ValueError, KeyError, OSError) as e:
            logger.debug(f"NodeThumbnailManager: no NodeService: {e}")
            return
        timer = NodeThumbnailTimer(
            device, item, svc, self._on_timer_result, parent=self
        )
        timer.set_node_reachable(node_iid not in self._disconnected_nodes)
        self._timers[iid] = timer
        digest = hashlib.sha256(repr(iid).encode("utf-8")).digest()
        phase_ms = int.from_bytes(digest[:8], "big") % timer._interval_ms
        timer.start(immediate=True, initial_delay_ms=phase_ms)

    def set_interval(self, seconds: int) -> None:
        for timer in self._timers.values():
            timer._interval_ms = max(1, min(60, int(seconds))) * 1000
            timer.reset_timer()

    def refresh_settings(self) -> None:
        self.set_interval(config.app.thumbnails.interval_sec)
        # Only a request-size change invalidates remote subscriptions:
        # resource budgets and the operator's own limits never rescale or
        # drop owner-side images.
        request_width = _configured_request_width()
        if request_width != self._request_width:
            self._request_width = request_width
            for timer in self._timers.values():
                timer._generation += 1

    def _on_timer_result(
        self, item: _ThumbnailItem, data: bytes | None, result: ThumbnailResult
    ) -> None:
        for owner in list(self._owners.get(device_identity(item.device), {}).values()):
            self.thumbnail_ready.emit(owner, data, result)


_node_thumbnail_manager: NodeThumbnailManager | None = None


def _release_node_connections_async(node_iid: str | None, iids: list[str]) -> None:
    """Release only this operator on the owner node; bounded fire-and-forget."""
    if not node_iid or node_iid == LOCAL_NODE_IID or not iids:
        return
    # Resolve now: stop_client may remove it before the queued job executes.
    try:
        from src.di import get_container
        from src.services import NodeService
        from src.workers.network import get_veyon_worker_pool

        svc = get_container().resolve_optional(NodeService)
        client = svc.get_client(node_iid) if svc is not None else None
        if client is None:
            return
        get_veyon_worker_pool().submit(
            lambda: client.release_thumbnails(iids),
            task_id=f"node_release_{node_iid}_{random.randint(0, 1_000_000)}",
            metadata={"type": "node_release", "node": node_iid, "count": len(iids)},
        )
    except (RuntimeError, ValueError, KeyError, OSError) as e:
        logger.debug(f"NodeThumbnailManager: release submit failed: {e}")


def get_node_thumbnail_manager() -> NodeThumbnailManager:
    global _node_thumbnail_manager
    if _node_thumbnail_manager is None:
        _node_thumbnail_manager = NodeThumbnailManager()
    return _node_thumbnail_manager


def reset_node_thumbnail_manager() -> None:
    global _node_thumbnail_manager
    if _node_thumbnail_manager is not None:
        _node_thumbnail_manager.stop_all()
    _node_thumbnail_manager = None
    NodeThumbnailManager._initialized = False
