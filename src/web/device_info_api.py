"""Информация об устройстве в вебе: потоковый сбор и отчёт из завершённого снимка."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Executor
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

from src.domain.device_info import (
    STATUS_LABELS,
    DeviceInfoSnapshot,
    InfoStatus,
    category_data,
    snapshot_data,
)
from src.logger import logger
from src.services.hosts.device_info.catalog import INFO_CATEGORIES
from src.services.hosts.device_info.collector import DeviceInfoCollector
from src.services.hosts.device_info.export import serialize_report
from src.services.hosts.device_info.service import DeviceInfoService
from src.workers.tracked_pool import run_in_tracked_executor

from .auth import RequestUser, require_auth

User = Annotated[RequestUser, Depends(require_auth)]

MAX_COLLECTIONS = 4
MAX_SNAPSHOTS = 32
SNAPSHOT_TTL_SEC = 1800
REPORT_MEDIA_TYPES = {
    "txt": "text/plain; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "tsv": "text/tab-separated-values; charset=utf-8",
}


class CollectRequest(BaseModel):
    """Профиль определяет список разделов; произвольные команды не принимаются."""

    model_config = ConfigDict(extra="forbid", strict=True)
    profile: Literal["brief", "full"] = "brief"


def _event(kind: str, data: dict[str, Any]) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _status_label(value: str) -> str:
    return STATUS_LABELS.get(InfoStatus(value), value)


def _with_labels(data: dict[str, Any]) -> dict[str, Any]:
    """Подписи состояний задаёт сервер, чтобы они совпадали с приложением."""
    data["status_label"] = _status_label(data["status"])
    for category in data.get("categories", {}).values():
        category["status_label"] = _status_label(category["status"])
    return data


def install_device_info_routes(
    app: FastAPI,
    pool_getter: Callable[[], Executor | None],
    device_getter: Callable[[str], Any],
) -> Callable[[], None]:
    """Подключить маршруты; возвращает отмену всех идущих сборов при остановке."""
    gate = threading.BoundedSemaphore(MAX_COLLECTIONS)
    lock = threading.Lock()
    active: set[DeviceInfoCollector] = set()
    finished: OrderedDict[str, tuple[str, float, DeviceInfoSnapshot]] = OrderedDict()
    # Снимки строит сервис без состояния; его кэш и задания принадлежат GUI-потоку.
    builder = DeviceInfoService()

    def shutdown() -> None:
        with lock:
            collectors = list(active)
            finished.clear()
        for collector in collectors:
            collector.abort()

    def remember(owner: str, snapshot: DeviceInfoSnapshot) -> None:
        now = time.monotonic()
        with lock:
            for key in [
                key
                for key, value in finished.items()
                if now - value[1] > SNAPSHOT_TTL_SEC
            ]:
                finished.pop(key)
            finished[snapshot.request_id] = (owner, now, snapshot)
            while len(finished) > MAX_SNAPSHOTS:
                finished.popitem(last=False)

    @app.post("/api/tools/device-info/{iid}")
    async def collect(
        iid: str, payload: CollectRequest, request: Request, user: User
    ) -> StreamingResponse:
        """Передавать разделы по мере готовности; отключение браузера отменяет сбор."""
        device = device_getter(iid)
        if device is None:
            raise HTTPException(404, "Устройство не найдено")
        if not gate.acquire(blocking=False):
            raise HTTPException(
                429, f"Одновременно выполняется не больше {MAX_COLLECTIONS} сборов"
            )
        loop = asyncio.get_running_loop()
        messages: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

        def post(kind: str, data: dict[str, Any]) -> None:
            try:
                loop.call_soon_threadsafe(messages.put_nowait, (kind, data))
            except RuntimeError:
                # Цикл сервера уже закрыт: получателя нет, сбор завершится сам.
                pass

        def on_category(_request_id: str, category: Any) -> None:
            data = category_data(category)
            data["name"] = category.name
            data["status_label"] = _status_label(data["status"])
            post("category", data)

        def on_progress(_request_id: str, done: int, total: int, text: str) -> None:
            post("progress", {"done": done, "total": total, "message": text})

        names = DeviceInfoService.names_for_profile(payload.profile)
        snapshot = builder.snapshot(device, payload.profile, names)
        collector = DeviceInfoCollector(
            device,
            {name: INFO_CATEGORIES[name] for name in names},
            snapshot,
            on_category,
            on_progress,
        )
        with lock:
            active.add(collector)

        def run() -> None:
            try:
                result = collector.run()
                remember(user.owner, result)
                post("done", _with_labels(snapshot_data(result)))
            except Exception as error:  # noqa: BLE001
                logger.error(f"Web device info: {type(error).__name__}")
                post("error", {"message": "Сбой сбора сведений об устройстве"})
            finally:
                with lock:
                    active.discard(collector)
                gate.release()

        def release_unstarted() -> None:
            with lock:
                active.discard(collector)
            gate.release()

        def on_future_done(future: Any) -> None:
            # Отменённая в очереди задача не выполнит run и не освободит лимит.
            if future.cancelled():
                release_unstarted()
                post("error", {"message": "Сервер останавливается"})

        pool = pool_getter()
        try:
            if pool is None:
                raise HTTPException(503, "Пул служебных операций недоступен")
            # Отказ пула становится ответом 503, а не оборванным потоком событий.
            future = pool.submit(
                run, metadata={"type": "web_device_info", "owner": user.owner}
            )
        except BaseException:
            release_unstarted()
            raise
        future.add_done_callback(on_future_done)

        async def events() -> AsyncIterator[str]:
            try:
                yield _event(
                    "start",
                    {
                        "request_id": snapshot.request_id,
                        "name": snapshot.name,
                        "expected": list(names),
                    },
                )
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        kind, data = await asyncio.wait_for(messages.get(), 0.25)
                    except TimeoutError:
                        continue
                    yield _event(kind, data)
                    if kind in {"done", "error"}:
                        return
            finally:
                collector.abort()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/tools/device-info/report/{request_id}")
    async def report(
        request_id: str,
        user: User,
        format: Literal["txt", "json", "csv", "tsv"] = "txt",
        category: str | None = None,
    ) -> Response:
        """Сформировать отчёт теми же правилами, что и окно приложения."""
        with lock:
            record = finished.get(request_id)
        if (
            record is None
            or record[0] != user.owner
            or time.monotonic() - record[1] > SNAPSHOT_TTL_SEC
        ):
            raise HTTPException(404, "Снимок устарел, запустите сбор заново")
        snapshot = record[2]
        names = {category} if category else None
        try:
            text = await run_in_tracked_executor(
                pool_getter(),
                lambda: serialize_report(snapshot, format=format, names=names),
                metadata={"type": "web_device_info", "owner": user.owner},
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        filename = f"{snapshot.name or snapshot.host}.{format}"
        return Response(
            text,
            media_type=REPORT_MEDIA_TYPES[format],
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
            },
        )

    return shutdown
