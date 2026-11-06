"""Resolve screen sources and coordinate captures across Qt, web and nodes."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Optional, Protocol

from src.architecture import EventBus, EventType
from src.config import config
from src.config import settings as settings_module
from src.config.settings import (
    ABSOLUTE_MAX_THUMBNAIL_DIMENSION,
    AppConfig,
    ThumbnailConfig,
)
from src.domain.models.device import DeviceModel
from src.logger import logger
from src.services.hosts.device_service import DeviceService
from src.services.remote.veyon_service import VeyonService
from src.services.thumbnails.types import (
    ThumbnailBackend,
    ThumbnailRequest,
    ThumbnailResult,
    ThumbnailStatus,
)


class _Configuration(Protocol):
    app: AppConfig


def configured_max_thumbnail_dimension() -> int:
    """Return the local instance's configured thumbnail side maximum."""
    with settings_module.THUMBNAIL_CONFIG_LOCK:
        return config.app.thumbnails.resources.max_thumbnail_dimension


def clamp_thumbnail_dimensions(
    width: int, height: int, maximum: int | None = None
) -> tuple[int, int]:
    """Clamp endpoint default sizes to the serving instance's own policy."""
    if maximum is None:
        maximum = configured_max_thumbnail_dimension()
    maximum = min(maximum, ABSOLUTE_MAX_THUMBNAIL_DIMENSION)
    return min(width, maximum), min(height, maximum)


def validate_thumbnail_dimensions(
    width: int, height: int, maximum: int | None = None
) -> tuple[int, int]:
    """Reject unbounded or full-resolution HTTP capture requests.

    Without an explicit ``maximum`` the local configured limit applies, so web
    and node requests are validated against the serving instance's own policy.
    """
    if maximum is None:
        maximum = configured_max_thumbnail_dimension()
    maximum = min(maximum, ABSOLUTE_MAX_THUMBNAIL_DIMENSION)
    if any(type(v) is not int or not 1 <= v <= maximum for v in (width, height)):
        raise ValueError(f"Размер миниатюры должен быть от 1 до {maximum} пикселей")
    return width, height


@dataclass
class _Usage:
    last_request: float
    http_until: float = 0
    devices: set[str] = field(default_factory=set)


class ThumbnailService:
    """Provide one non-GUI capture boundary with idempotent consumer ownership."""

    def __init__(
        self,
        # Optional syntax is required by the existing DI resolver on Python 3.10-3.13.
        veyon_service: Optional[VeyonService] = None,  # noqa: UP045 - legacy DI
        event_bus: Optional[EventBus] = None,  # noqa: UP045 - legacy DI
        device_service: Optional[DeviceService] = None,  # noqa: UP045 - legacy DI
        *,
        backends: dict[str, ThumbnailBackend] | None = None,
        configuration: _Configuration | None = None,
    ) -> None:
        self._config = configuration or config
        self._device_service = device_service
        if backends is None:
            from src.services.thumbnails.veyon import VeyonBackend
            from src.services.thumbnails.vnc import VncBackend

            if veyon_service is None:
                veyon_service = VeyonService(event_bus=event_bus)
            backends = {"veyon": VeyonBackend(veyon_service), "vnc": VncBackend()}
        self._backends = backends
        self._lock = threading.RLock()
        self._inflight: set[tuple] = set()
        self._sessions: dict[tuple, _Usage] = {}
        self._device_sessions: dict[str, tuple] = {}
        self._signatures: dict[str, tuple] = {}
        self._versions: dict[str, int] = {}
        self._devices: dict[str, DeviceModel] = {}
        self._subscriptions: dict[tuple[str, str], float] = {}
        self._consumer_versions: dict[tuple[str, str], int] = {}
        self._cache: OrderedDict[tuple, tuple[ThumbnailResult, float]] = OrderedDict()
        self._cache_bytes = 0
        self._metrics = {"captures": 0, "cache_hits": 0, "busy": 0, "evictions": 0}
        self._errors: dict[str, int] = {}
        self._last_error_log: dict[str, float] = {}
        self._stop = threading.Event()
        # Импорт здесь: пакет src.workers сам зависит от src.services.
        from src.workers.tracked_pool import TrackedThreadPoolExecutor

        self._cleanup = TrackedThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="thumbnail-release",
            label="Очистка миниатюр",
        )
        self._closing: set[tuple] = set()
        self._housekeeping_pending = False
        self._applied_policy: tuple | None = None
        self._sweeper: threading.Thread | None = None
        if event_bus is not None:
            event_bus.subscribe(EventType.DATA_SAVED, self._on_change)
            event_bus.subscribe(EventType.DATA_CHANGED, self._on_change)
            event_bus.subscribe(EventType.DEVICE_UPDATED, self._on_change)
        # Install the persisted resource policy without waiting for a save.
        self._apply_resource_policy()

    def effective_backend(self, device: DeviceModel) -> str:
        return device.thumbnail_backend or self._config.app.thumbnails.backend

    def is_available(self, devices: list[DeviceModel] | None = None) -> bool:
        """Report local backend readiness, not authentication of every server."""
        sources = {self._config.app.thumbnails.backend}
        for dev in devices or []:
            nid = getattr(dev, "node_iid", None)
            if nid and nid != "local":
                return True
            sources.add(self.effective_backend(dev))
        return any(self._backends[source].is_available() for source in sources)

    @property
    def supported_backends(self) -> list[str]:
        return list(self._backends)

    def _snapshot(
        self, device: DeviceModel, width: int | None, height: int | None
    ) -> tuple:
        with settings_module.THUMBNAIL_CONFIG_LOCK:
            app = self._config.app
            current = app.thumbnails
            # Deep snapshot: nested resource groups must stay immutable for the
            # lifetime of an operation even if the config object is replaced.
            settings = replace(
                current,
                resources=replace(current.resources),
                vnc=replace(current.vnc),
            )
            integration = replace(app.veyon)
            model = device.to_dict()
            generation = (
                getattr(self._config, "_thumbnail_backend_revision", 0)
                if model.get("thumbnail_backend") is None
                else 0,
                getattr(self._config, "_thumbnail_image_revision", 0),
            )
            connection_generation = (
                getattr(self._config, "_vnc_password_revision", 0),
                getattr(self._config, "_vnc_port_revision", 0)
                if not model.get("vnc_port")
                else 0,
            )
        backend = model.get("thumbnail_backend") or settings.backend
        max_dimension = min(
            settings.resources.max_thumbnail_dimension,
            ABSOLUTE_MAX_THUMBNAIL_DIMENSION,
        )
        width = (
            min(max_dimension, settings.width * settings.scale)
            if width is None
            else width
        )
        height = max(1, width * 9 // 16) if height is None else height
        validate_thumbnail_dimensions(width, height, maximum=max_dimension)
        port = (
            (model.get("vnc_port") or integration.vnc_port or 5900)
            if backend == "vnc"
            else integration.server_port
        )
        password = integration.vnc_password if backend == "vnc" else ""
        credentials = (
            password
            if backend == "vnc"
            else f"{integration.webapi_url}\0{integration.key_name}"
        )
        digest = hashlib.sha256(credentials.encode("utf-8")).digest()
        key = (
            backend,
            model["host"],
            port,
            digest,
            connection_generation if backend == "vnc" else (),
        )
        signature = (
            key,
            width,
            height,
            settings.format,
            settings.quality,
            settings.compression,
            settings.require_online,
            device.online_revision if settings.require_online else None,
            generation,
        )
        snapshot = SimpleNamespace(**model, is_online=device.is_online)
        return settings, snapshot, key, signature, password

    def revision(
        self, device: DeviceModel, width: int | None = None, height: int | None = None
    ) -> tuple:
        """Return a secret-free comparable token for rejecting delayed UI responses."""
        return self._snapshot(device, width, height)[3]

    @staticmethod
    def _configuration_signature(
        settings: ThumbnailConfig, key: tuple, generation: tuple = (0, 0)
    ) -> tuple:
        """Separate configuration revisions from concurrent image-size variants."""
        return (
            key,
            settings.width,
            settings.scale,
            settings.format,
            settings.quality,
            settings.compression,
            settings.require_online,
            generation,
        )

    def subscribe(self, device: DeviceModel, consumer: str) -> None:
        with self._lock:
            self._subscriptions[(device.iid, consumer)] = time.monotonic()

    def get_thumbnail(
        self,
        device: DeviceModel,
        width: int | None = None,
        height: int | None = None,
        *,
        online: bool | None = None,
        consumer: str | None = None,
    ) -> ThumbnailResult:
        """Capture only the chosen source; never perform transport fallback."""
        nid = getattr(device, "node_iid", None)
        if nid and nid != "local":
            return ThumbnailResult(
                status=ThumbnailStatus.UNSUPPORTED, error_code="owner_node_required"
            )
        original_width, original_height = width, height
        settings, snapshot, key, signature, password = self._snapshot(
            device, width, height
        )
        backend, host, port = key[:3]
        if self._stop.is_set():
            return ThumbnailResult(backend=backend, status=ThumbnailStatus.CANCELLED)
        if settings.require_online and not (
            device.is_online if online is None else online
        ):
            with self._lock:
                self._invalidate_cached_endpoint(key)
            return ThumbnailResult(backend=backend, status=ThumbnailStatus.OFFLINE)
        now = time.monotonic()
        iid = device.iid
        get_persisted_revision = getattr(
            self._device_service, "get_device_revision", None
        )
        persisted_revision = (
            get_persisted_revision(iid) if get_persisted_revision is not None else None
        )
        saved_at_start = (
            self._device_service.get_device_by_iid(iid)
            if self._device_service is not None
            else None
        )
        if (
            saved_at_start is not None
            and self.revision(saved_at_start, original_width, original_height)[:-2]
            != signature[:-2]
        ):
            return ThumbnailResult(backend=backend, status=ThumbnailStatus.CANCELLED)
        endpoint = key[:3]
        configuration_signature = self._configuration_signature(
            settings, key, signature[-1]
        )
        with self._lock:
            if self._signatures.get(iid) != configuration_signature:
                self._versions[iid] = self._versions.get(iid, 0) + 1
                self._signatures[iid] = configuration_signature
            version = self._versions[iid]
            old_key = self._device_sessions.get(iid)
            if old_key is not None and old_key != key:
                old_usage = self._sessions.get(old_key)
                if old_usage is not None:
                    old_usage.devices.discard(iid)
                self._queue_release(old_key)
            if key not in self._sessions and len(self._sessions) >= (
                settings.resources.max_session_records
            ):
                return ThumbnailResult(
                    backend=backend,
                    status=ThumbnailStatus.BUSY,
                    error_code="session_limit",
                )
            self._device_sessions[iid] = key
            self._devices[iid] = device
            usage = self._sessions.setdefault(key, _Usage(now))
            usage.last_request = now
            usage.devices.add(iid)
            if consumer is None:
                usage.http_until = now + max(5, 2 * settings.interval_sec)
            else:
                self.subscribe(device, consumer)
            consumer_key = (iid, consumer) if consumer is not None else None
            consumer_version = (
                self._consumer_versions.get(consumer_key, 0)
                if consumer_key is not None
                else 0
            )
            width, height = signature[1:3]
            request = ThumbnailRequest(
                width,
                height,
                settings.format,
                settings.quality,
                settings.compression,
                backend,
                host,
                port,
                password,
                key,
                version,
                settings.interval_sec,
            )
            # Cache hits must retain the original capture generation, not adopt
            # a newer configuration or a reconnected device's version number.
            image_key = (key, request.image_key, signature[6:])
            cached = self._cached(image_key, settings.interval_sec, now)
            busy = endpoint in self._inflight or key in self._closing
            if busy:
                self._metrics["busy"] += 1
            # Short-lived image reuse prevents simultaneous consumers from polling twice.
            entry = self._cache.get(image_key)
            if cached is not None and (
                busy or (entry is not None and now - entry[1] < 0.5)
            ):
                try:
                    current = self.revision(device, original_width, original_height)
                except ValueError:
                    current = None
                if current != signature or (
                    get_persisted_revision is not None
                    and get_persisted_revision(iid) != persisted_revision
                ):
                    return ThumbnailResult(
                        backend=backend, status=ThumbnailStatus.CANCELLED
                    )
                self._metrics["cache_hits"] += 1
                return replace(cached, config_version=version, from_cache=True)
            if busy:
                return ThumbnailResult(
                    backend=backend, status=ThumbnailStatus.BUSY, config_version=version
                )
            self._inflight.add(endpoint)
            self._start_sweeper()
        try:
            try:
                result = self._backends[backend].capture(snapshot, request)
            except Exception:  # noqa: BLE001 - contain backend errors without secrets
                with self._lock:
                    if time.monotonic() - self._last_error_log.get(iid, 0) > 60:
                        self._last_error_log[iid] = time.monotonic()
                        logger.warning(
                            f"ThumbnailService: capture failed iid={iid} backend={backend} code=internal_error"
                        )
                result = ThumbnailResult(backend=backend, error_code="internal_error")
            result = replace(result, backend=backend, config_version=version)
            if result.status != ThumbnailStatus.OK:
                result = replace(result, data=None, mime=None, from_cache=False)
            persisted_changed = False
            if saved_at_start is not None and self._device_service is not None:
                saved_now = self._device_service.get_device_by_iid(iid)
                try:
                    persisted_changed = (
                        saved_now is None
                        # Persisted copies have no live reachability history. That
                        # epoch and config generations are checked on device below.
                        or self.revision(saved_now, original_width, original_height)[
                            :-2
                        ]
                        != signature[:-2]
                        or (
                            get_persisted_revision is not None
                            and get_persisted_revision(iid) != persisted_revision
                        )
                    )
                except ValueError:
                    # A newly reduced limit may reject the old request size.
                    persisted_changed = True
            with self._lock:
                # Ошибка источника сбрасывает общий кэш даже при отмене подписки.
                # BUSY означает временную занятость, готовые варианты сохраняются.
                if result.status not in (
                    ThumbnailStatus.OK,
                    ThumbnailStatus.BUSY,
                    ThumbnailStatus.CANCELLED,
                ):
                    self._invalidate_cached_endpoint(key)
                try:
                    changed = (
                        self._snapshot(device, original_width, original_height)[3]
                        != signature
                        or self._versions.get(iid) != version
                    )
                except ValueError:
                    # A reduced dimension maximum rejects the old size: treat
                    # the delayed result as stale instead of leaking the error.
                    changed = True
                released = (
                    consumer_key is not None
                    and self._consumer_versions.get(consumer_key, 0) != consumer_version
                )
                if changed or persisted_changed or released or self._stop.is_set():
                    return ThumbnailResult(
                        backend=backend,
                        status=ThumbnailStatus.CANCELLED,
                        config_version=version,
                    )
                if result.data is not None and result.status == ThumbnailStatus.OK:
                    result = replace(
                        result, frame_time=result.frame_time or time.monotonic()
                    )
                    self._store(image_key, result, settings.resources)
                    self._metrics["captures"] += 1
                elif result.status not in (
                    ThumbnailStatus.BUSY,
                    ThumbnailStatus.CANCELLED,
                ):
                    self._errors[result.status.value] = (
                        self._errors.get(result.status.value, 0) + 1
                    )
                return result
        finally:
            with self._lock:
                self._inflight.discard(endpoint)
                self._queue_release(key)

    capture = get_thumbnail

    def _invalidate_cached_endpoint(self, key: tuple[object, ...]) -> None:
        """Discard every encoded size of a failed source while holding _lock."""
        for image_key in [item for item in self._cache if item[0] == key]:
            entry = self._cache.pop(image_key)
            self._cache_bytes -= len(entry[0].data or b"")

    def _cached(
        self, image_key: tuple, interval: int, now: float
    ) -> ThumbnailResult | None:
        entry = self._cache.get(image_key)
        if entry is None or now - entry[1] > max(5, 2 * interval):
            return None
        self._cache.move_to_end(image_key)
        return entry[0]

    def _store(
        self,
        image_key: tuple,
        result: ThumbnailResult,
        resources: object | None = None,
    ) -> None:
        if resources is None:
            resources = self._current_resources()
        old = self._cache.pop(image_key, None)
        if old:
            self._cache_bytes -= len(old[0].data or b"")
        self._cache[image_key] = (result, time.monotonic())
        self._cache_bytes += len(result.data or b"")
        self._trim_cache_locked(resources)

    def _current_resources(self):
        """Read the currently configured shared-cache limits."""
        with settings_module.THUMBNAIL_CONFIG_LOCK:
            return replace(self._config.app.thumbnails.resources)

    def _trim_cache_locked(self, resources) -> None:
        """Evict oldest entries until both byte and entry budgets hold."""
        while self._cache and (
            self._cache_bytes > resources.cache_bytes
            or len(self._cache) > resources.cache_entries
        ):
            _, entry = self._cache.popitem(last=False)
            self._cache_bytes -= len(entry[0].data or b"")
            self._metrics["evictions"] += 1

    def release(self, device_or_iid: DeviceModel | str, consumer: str = "qt") -> None:
        """Drop only this consumer's use; defer closure during captures/HTTP leases."""
        iid = device_or_iid if isinstance(device_or_iid, str) else device_or_iid.iid
        with self._lock:
            self._subscriptions.pop((iid, consumer), None)
            ck = (iid, consumer)
            self._consumer_versions[ck] = self._consumer_versions.get(ck, 0) + 1
            key = self._device_sessions.get(iid)
            if key is not None:
                self._queue_release(key)

    def release_consumer(self, consumer: str) -> None:
        with self._lock:
            iids = [iid for iid, owner in self._subscriptions if owner == consumer]
        for iid in iids:
            self.release(iid, consumer)

    def _unused(self, key: tuple) -> bool:
        usage = self._sessions.get(key)
        if usage is None or key[:3] in self._inflight:
            return False
        if time.monotonic() < usage.http_until:
            return False
        return not any(iid in usage.devices for iid, _ in self._subscriptions)

    def _queue_release(self, key: tuple) -> None:
        if key in self._closing or not self._unused(key):
            return
        self._closing.add(key)

        def close() -> None:
            try:
                # Recheck after asynchronous queuing so reacquisition cannot be closed.
                with self._lock:
                    if not self._unused(key):
                        return
                    self._sessions.pop(key, None)
                    for ck in [ck for ck in self._cache if ck[0] == key]:
                        entry = self._cache.pop(ck)
                        self._cache_bytes -= len(entry[0].data or b"")
                self._backends[key[0]].release(key)
            finally:
                with self._lock:
                    self._closing.discard(key)

        try:
            self._cleanup.submit(close, metadata={"type": "thumbnail_release"})
        except RuntimeError:
            self._closing.discard(key)

    def _start_sweeper(self) -> None:
        if self._sweeper is None:
            self._sweeper = threading.Thread(
                target=self._sweep_loop, name="thumbnail-idle", daemon=True
            )
            self._sweeper.start()

    def _sweep_loop(self) -> None:
        while not self._stop.wait(15):
            self.close_idle_sessions()

    def close_idle_sessions(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        ttl = max(90, 3 * self._config.app.thumbnails.interval_sec)
        with self._lock:
            for ck, last_seen in list(self._subscriptions.items()):
                if ck[1].startswith("node:") and now - last_seen > ttl:
                    self._subscriptions.pop(ck, None)
            for key, usage in list(self._sessions.items()):
                if now - usage.last_request > ttl or self._unused(key):
                    self._queue_release(key)
            active_iids = {
                iid for usage in self._sessions.values() for iid in usage.devices
            }
            active_iids.update(iid for iid, _ in self._subscriptions)
            for iid in list(self._devices):
                if iid not in active_iids:
                    self._devices.pop(iid, None)
                    self._device_sessions.pop(iid, None)
                    self._signatures.pop(iid, None)
                    self._versions.pop(iid, None)
                    self._last_error_log.pop(iid, None)
                    for ck in [ck for ck in self._consumer_versions if ck[0] == iid]:
                        self._consumer_versions.pop(ck, None)
        # Backends keep their own bounded sessions; reconcile them here so a
        # reduced limit converges without blocking the GUI thread.
        for backend in self._backends.values():
            try:
                backend.close_idle_sessions(now)
            except Exception:  # noqa: BLE001 - a backend failure must not stop sweeps
                logger.debug(
                    "ThumbnailService: backend idle sweep failed", exc_info=True
                )

    def _apply_resource_policy(self) -> None:
        """Install the current resource policy without restarting captures.

        Runs in the housekeeping/worker thread: cache and registry trimming are
        pure memory operations, and backend reconciliation may close sockets.
        """
        with settings_module.THUMBNAIL_CONFIG_LOCK:
            current = self._config.app.thumbnails
            resources = replace(current.resources)
            vnc = replace(current.vnc)
        policy = (resources, vnc)
        with self._lock:
            if self._applied_policy == policy:
                return
            self._trim_cache_locked(resources)
            # Release unused records beyond the new registry limit first;
            # owned or active records survive and block new admissions.
            ordered = sorted(
                self._sessions.items(), key=lambda item: item[1].last_request
            )
            for key, _usage in ordered[resources.max_session_records :]:
                self._queue_release(key)
            self._applied_policy = policy
        for backend in self._backends.values():
            apply_policy = getattr(backend, "configure", None)
            if callable(apply_policy):
                apply_policy(
                    max_sessions=vnc.max_sessions,
                    max_memory_bytes=vnc.memory_budget_bytes,
                    capture_timeout=vnc.capture_timeout_sec,
                    poll_timeout=vnc.poll_timeout_sec,
                    auth_backoff=vnc.auth_backoff_sec,
                    max_image_variants=vnc.max_image_variants,
                    max_screen_pixels=vnc.max_screen_pixels,
                    max_thumbnail_dimension=resources.max_thumbnail_dimension,
                )

    def _on_change(self, event: object) -> None:
        """Wake housekeeping; current snapshots enforce freshness synchronously."""
        # This callback runs in GUI thread: never release a network resource here.
        data = getattr(event, "data", {})
        if data.get("type") not in (
            "config",
            "thumbnails_config",
            "vnc_config",
            "veyon_config",
            "hosts",
            "devices",
            "wizard_finished",
        ):
            return
        with self._lock:
            for iid, device in list(self._devices.items()):
                signature = self._signatures.get(iid)
                if signature is None:
                    continue
                settings, _snapshot, key, _signature, _password = self._snapshot(
                    device, None, None
                )
                current = self._configuration_signature(settings, key, _signature[-1])
                if signature != current:
                    self._versions[iid] = self._versions.get(iid, 0) + 1
                    self._signatures[iid] = current
                    key = self._device_sessions.get(iid)
                    if key is not None and key != current[0]:
                        usage = self._sessions.get(key)
                        if usage is not None:
                            usage.devices.discard(iid)
                        self._queue_release(key)
        if not self._stop.is_set():
            with self._lock:
                if self._housekeeping_pending:
                    return
                self._housekeeping_pending = True

            def housekeeping() -> None:
                try:
                    self._apply_resource_policy()
                    self.close_idle_sessions()
                finally:
                    with self._lock:
                        self._housekeeping_pending = False

            try:
                self._cleanup.submit(
                    housekeeping, metadata={"type": "thumbnail_housekeeping"}
                )
            except RuntimeError:
                with self._lock:
                    self._housekeeping_pending = False

    def get_metrics(self) -> dict:
        with self._lock:
            resources = self._current_resources()
            over_budget = len(self._sessions) > resources.max_session_records
            metrics: dict[str, object] = dict(
                self._metrics,
                sessions=len(self._sessions),
                captures_active=len(self._inflight),
                subscriptions=len(self._subscriptions),
                cache_bytes=self._cache_bytes,
                cache_entries=len(self._cache),
                cache_bytes_limit=resources.cache_bytes,
                cache_entries_limit=resources.cache_entries,
                max_session_records=resources.max_session_records,
                over_budget=over_budget,
                allocations_blocked=over_budget,
                errors=dict(self._errors),
            )
        metrics["backends"] = {
            name: backend.get_metrics() for name, backend in self._backends.items()
        }
        return metrics

    def shutdown(self) -> None:
        """Stop accepting captures and close backends once the process shuts down."""
        with self._lock:
            if self._stop.is_set():
                return
            self._stop.set()
        if self._sweeper is not None:
            self._sweeper.join(timeout=1)
        self._cleanup.shutdown(wait=False, cancel_futures=True)
        for backend in self._backends.values():
            backend.shutdown()
        with self._lock:
            self._subscriptions.clear()
            self._cache.clear()
            self._cache_bytes = 0
