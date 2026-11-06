"""Shared host-info collectors for web and node tooltips.

The async API probes TCP in the tracked status pool, schedules SSH collection
in the service pool, and awaits its first snapshot without holding a worker.
The synchronous API remains available for existing blocking callers.
Both bypass device.is_online after their own TCP gate and serialize the same
live fields and ordered, localized rows. Cache TTL and dedup belong to the service.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from src.config import config
from src.logger import logger

if TYPE_CHECKING:
    from src.domain.models.device import DeviceModel
    from src.services.hosts.host_info_service import HostInfoService, HostInfoSnapshot


def _field_labels() -> dict[str, str]:
    """Метки живых полей (импорт ленивый — модуль Qt-free для тестов)."""
    from src.services.hosts.host_info_service import _FIELD_LABELS

    return _FIELD_LABELS


def _static_keys() -> set[str]:
    """Ключи статических полей (импорт ленивый — Qt-free)."""
    from src.services.hosts.host_info_service import _STATIC_KEYS

    return _STATIC_KEYS


def _unavailable_response(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "fields": {}, "rows": []}


def _snapshot_response(
    snap: HostInfoSnapshot | None,
    service: HostInfoService,
    fields: list[str] | None,
) -> dict[str, Any]:
    """Serialize both collectors identically, including field order and fallback."""
    if snap is None:
        return _unavailable_response("no_data")
    out: dict[str, Any] = {"ok": True, "fields": {}, "rows": []}
    labels = _field_labels()
    if fields:
        known_live = {key for key in labels if key not in _static_keys()}
        enabled = [key for key in fields if key in known_live]
    else:
        enabled = service.get_enabled_fields()
    for key in enabled:
        value = snap.field(key)
        if value:
            out["fields"][key] = value
            out["rows"].append([labels.get(key, key), value])
    return out


async def collect_host_info_fields_async(
    dev: DeviceModel | None,
    wait_sec: float = 4.0,
    fields: list[str] | None = None,
    *,
    source: str | None = None,
) -> dict[str, Any]:
    """Collect host fields without blocking the event loop or a polling worker.

    wait_sec limits only first-snapshot waiting, not queue/probe time. Empty
    fields uses the owner's enabled fields, just like the synchronous API.
    Cancellation stops this waiter, not the shared service fetch or its cache.
    """
    if dev is None:
        return _unavailable_response("not_found")

    from src.utils.network.reachability import get_reachability_prober
    from src.workers.task_metadata import operation_source

    source = source or operation_source.get()
    ssh_port = getattr(dev, "port", None) or config.app.ssh.port or 22
    host_alive = await asyncio.wrap_future(
        get_reachability_prober().probe(
            dev.host,
            ssh_port,
            1.0,
            metadata={"host": dev.host, "type": "host_info_ping", "source": source},
        )
    )
    if not host_alive:
        return _unavailable_response("offline")

    from src.services.hosts.host_info_service import get_host_info_service

    service = get_host_info_service()
    iid = getattr(dev, "iid", "") or ""
    if not iid:
        return _unavailable_response("no_data")

    # This only schedules work, preserving the service's TTL and in-flight dedup.
    service._start_local_fetch(dev, source=source)
    deadline = time.monotonic() + max(0.0, float(wait_sec))
    snap = service.get_info(iid)
    while snap is None and service.is_fetching(iid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(0.05, remaining))
        snap = service.get_info(iid)
    # Re-read once if the fetch completed between cache and in-flight reads.
    return _snapshot_response(service.get_info(iid), service, fields)


def collect_host_info_fields(
    dev: DeviceModel | None,
    wait_sec: float = 4.0,
    fields: list[str] | None = None,
    *,
    source: str | None = None,
) -> dict[str, Any]:
    """Собрать live-данные хоста (блокирующе, для run_in_threadpool).

    Args:
        dev: DeviceModel (объект ноды-владельца — SSH-сбор идёт с этой машины).
        wait_sec: окно ожидания первого асинхронного сбора (сек).
        fields: включённые живые поля от запрашивающего (оператора).
            Пустой/None → берутся настройки тултипа самой ноды
            (his.get_enabled_fields()). Позволяет оператору видеть свои
            поля в тултипах нод-хостов независимо от настроек ноды.

    Returns:
        {"ok": True, "fields": {ключ: значение}, "rows": [[подпись, значение]]}
        или {"ok": False, "reason": "offline"|"no_data", "fields": {}, "rows": []}.
    """
    from src.utils.network.net_utils import tcp_ping

    out: dict[str, Any] = {"ok": False, "fields": {}, "rows": []}
    if dev is None:
        out["reason"] = "not_found"
        return out

    ssh_port = getattr(dev, "port", None) or config.app.ssh.port or 22
    try:
        host_alive = tcp_ping(dev.host, ssh_port, 1.0)
    except Exception as e:  # noqa: BLE001 - probe failures share the offline response
        logger.debug(f"host_info_api: tcp_ping {dev.host} failed: {e}")
        host_alive = False
    if not host_alive:
        out["reason"] = "offline"
        return out

    from src.services.hosts.host_info_service import get_host_info_service

    his = get_host_info_service()
    iid = getattr(dev, "iid", "") or ""
    if not iid:
        out["reason"] = "no_data"
        return out

    # Запрашиваем свежие данные (кэш TTL 60 сек). _start_local_fetch без
    # гейта is_online: доступность уже проверена tcp_ping'ом выше.
    his._start_local_fetch(dev, source=source)

    def _wait_snapshot():
        # Первый сбор идёт в SSH-пуле — ждём его завершения (0.2с-поллинг).
        if his.get_info(iid) is None and his.is_fetching(iid):
            import time as _time

            deadline = _time.time() + max(0.0, float(wait_sec))
            while _time.time() < deadline:
                _time.sleep(0.2)
                if his.get_info(iid) is not None or not his.is_fetching(iid):
                    break
        return his.get_info(iid)

    return _snapshot_response(_wait_snapshot(), his, fields)
