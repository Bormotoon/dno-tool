"""Потоковый доступ веба к тому же алгоритму, что использует Qt-сканер."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import queue
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.config import PORTS, config
from src.utils.network.net_utils import get_default_ipv4_interface
from src.utils.scanner.device_classifier import (
    device_type_icon_key,
    scanner_state_icon_key,
)
from src.utils.scanner.engine import ScannerEngine
from src.utils.scanner.scanner_icmp import check_icmplib
from src.utils.scanner.scanner_range import ScannerAddressRange

from .auth import RequestUser, require_auth

User = Annotated[RequestUser, Depends(require_auth)]


class DiscoveryRequest(BaseModel):
    """Параметры соответствуют переключателям настольного сканера."""

    model_config = ConfigDict(extra="forbid", strict=True)
    target: str = Field(min_length=1, max_length=100)
    mode: Literal["icmp", "tcp", "icmp_tcp"] = "icmp"
    ports: list[int] = Field(default_factory=lambda: [22, 80, 443], max_length=256)
    threads: int = Field(default=32, ge=1, le=128)
    timeout: float = Field(default=1.0, ge=0.1, le=10, allow_inf_nan=False)
    ping_backend: Literal["system", "icmplib"] = "system"
    resolve_names: bool = True
    resolve_mac: bool = True
    classify_enabled: bool = True
    use_port_signatures: bool = True
    udp_probes: bool = True

    @field_validator("target")
    @classmethod
    def target_range(cls, value: str) -> str:
        """Разрешить те же IPv4-диапазоны и предел, что в Qt."""
        ScannerAddressRange(value)
        return value.strip()

    @field_validator("ports")
    @classmethod
    def port_range(cls, value: list[int]) -> list[int]:
        """Проверить номера TCP-портов без неявного приведения."""
        if any(not 1 <= port <= 65535 for port in value):
            raise ValueError("Порты должны быть от 1 до 65535")
        return list(dict.fromkeys(value))


class ImportRequest(BaseModel):
    """Импортировать только результаты собственного завершённого сканирования."""

    model_config = ConfigDict(extra="forbid", strict=True)
    scan_id: str = Field(max_length=64)
    hosts: list[str] = Field(min_length=1, max_length=4096)
    folder: str = Field(default="Сканер", min_length=1, max_length=128)


def install_scanner_routes(
    app: FastAPI, device_getter: Callable[[], Any]
) -> Callable[[], None]:
    """Подключить сканер и вернуть отмену веб-запусков при остановке сервера."""
    lock = threading.Lock()
    active: dict[str, ScannerEngine] = {}
    history: dict[str, tuple[str, float, dict[str, Any]]] = {}

    def shutdown() -> None:
        """Отменить сетевые операции, сохраняя владение до завершения потоков."""
        with lock:
            for engine in active.values():
                engine.abort()
            history.clear()

    @app.get("/api/tools/scanner/config")
    def defaults(user: User) -> dict[str, Any]:
        """Вернуть настройки сканера приложения и фактический интерфейс сервера."""
        interface = get_default_ipv4_interface()
        network = interface.network if interface is not None else None
        if interface is not None and network is not None and network.prefixlen == 32:
            # Как в Qt: интерфейс VPN /32 не ограничивает поиск одним адресом.
            network = ipaddress.IPv4Network(f"{interface.ip}/24", strict=False)
        availability = check_icmplib()
        return {
            "ports": dict(PORTS),
            "threads": config.app.network.scanner_thread_count,
            "timeout": config.app.network.scanner_ping_timeout_ms / 1000,
            "ping_backend": config.app.network.scanner_ping_backend,
            "icmplib_available": availability.available,
            "icmplib_reason": availability.reason,
            "interface": str(interface) if interface is not None else None,
            "network": str(network) if network is not None else None,
            "max_addresses": 65536,
        }

    @app.post("/api/tools/scanner/run")
    async def run(
        payload: DiscoveryRequest, request: Request, user: User
    ) -> StreamingResponse:
        """Передать браузеру фазы и результаты общего движка с обратным давлением."""
        if payload.mode != "icmp" and not payload.ports:
            raise HTTPException(400, "Выберите хотя бы один TCP-порт")
        engine = ScannerEngine(
            ScannerAddressRange(payload.target),
            icmp=payload.mode != "tcp",
            combined=payload.mode == "icmp_tcp",
            resolve_mac=payload.resolve_mac,
            resolve_names=payload.resolve_names,
            max_workers=payload.threads,
            classify_enabled=payload.classify_enabled,
            use_port_signatures=payload.use_port_signatures,
            udp_probes=payload.udp_probes,
            ping_backend=payload.ping_backend,
        )
        engine.enabled_ports = tuple(payload.ports)
        engine._ping_timeout_ms = round(payload.timeout * 1000)
        engine._port_timeout = payload.timeout
        messages: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=256)
        results: dict[str, Any] = {}
        stopped = threading.Event()

        def emit(kind: str, value: dict[str, Any]) -> None:
            while not engine.is_aborting:
                try:
                    messages.put((kind, value), timeout=0.05)
                    return
                except queue.Full:
                    continue

        def result(value: Any) -> None:
            data = asdict(value)
            data["icon_key"] = scanner_state_icon_key(
                value.online, value.state, value.device_type
            )
            data["type_icon_key"] = device_type_icon_key(value.device_type)
            results[value.host] = data
            emit("host", data)

        engine.result_ready.connect(result)
        engine.phase.connect(
            lambda phase, done, total, found: emit(
                "phase", {"phase": phase, "done": done, "total": total, "found": found}
            )
        )
        with lock:
            if active:
                raise HTTPException(409, "Веб-сканер ещё выполняет предыдущий запуск")
            active[engine.scan_id] = engine

        def coordinate() -> None:
            try:
                engine.execute()
            finally:
                with lock:
                    active.pop(engine.scan_id, None)
                    expired = [
                        key
                        for key, value in history.items()
                        if time.monotonic() - value[1] > 1800 or value[0] == user.owner
                    ]
                    for key in expired:
                        history.pop(key, None)
                    history[engine.scan_id] = (user.owner, time.monotonic(), results)
                    while len(history) > 16:
                        history.pop(next(iter(history)))
                stopped.set()

        thread = threading.Thread(
            target=coordinate, name="web-scanner-coordinator", daemon=True
        )
        thread.start()

        async def events() -> AsyncIterator[str]:
            def event(kind: str, data: dict[str, Any]) -> str:
                return (
                    f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                )

            try:
                yield event(
                    "start", {"scan_id": engine.scan_id, "total": len(engine.ips)}
                )
                while not stopped.is_set() or not messages.empty():
                    if await request.is_disconnected():
                        return
                    try:
                        kind, data = messages.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.03)
                        continue
                    yield event(kind, data)
                yield event(
                    "done",
                    {
                        "scan_id": engine.scan_id,
                        "status": engine.outcome,
                        "error": engine.failure,
                    },
                )
            finally:
                engine.abort()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/tools/scanner/import")
    def import_hosts(payload: ImportRequest, user: User) -> dict[str, Any]:
        """Сохранить отмеченные хосты через DeviceService, сохраняя правила дубликатов."""
        with lock:
            record = history.get(payload.scan_id)
            if (
                record is None
                or record[0] != user.owner
                or time.monotonic() - record[1] > 1800
            ):
                raise HTTPException(
                    404, "Результаты сканирования истекли или недоступны"
                )
            selected = [record[2].get(host) for host in dict.fromkeys(payload.hosts)]
        if any(item is None or not item["online"] for item in selected):
            raise HTTPException(
                400, "Импортировать можно только найденные хосты этого сканирования"
            )
        service = device_getter()
        if service is None:
            raise HTTPException(503, "Сервис устройств недоступен")
        added, skipped = 0, 0
        errors: list[dict[str, str]] = []
        devices: list[dict[str, str]] = []
        existing: list[dict[str, str]] = []
        for item in selected:
            assert item is not None
            host = str(ipaddress.IPv4Address(item["host"]))
            previous = service.get_device(host)
            if previous is not None:
                skipped += 1
                existing.append({"iid": previous.iid, "host": host})
                continue
            ok, message, device = service.add_device(
                {
                    "host": host,
                    "name": item["hostname"] or host,
                    "mac_address": item["mac_address"],
                },
                payload.folder,
                persist=True,
            )
            if ok:
                added += 1
                if device is not None:
                    devices.append({"iid": device.iid, "host": host})
            else:
                errors.append({"host": host, "message": message})
        message = (
            f"Добавлено в папку «{payload.folder}»: {added}. Уже существуют: {skipped}"
        )
        if skipped:
            message += ". Повторные хосты не добавляются и не переносятся"
        if errors:
            message += f". Ошибок: {len(errors)}. " + "; ".join(
                f"{item['host']}: {item['message']}" for item in errors[:5]
            )
        return {
            "added": added,
            "skipped": skipped,
            "errors": errors,
            "devices": devices,
            "existing": existing,
            "folder": payload.folder,
            "message": message,
        }

    return shutdown
