"""API базовых инструментов веб-рабочего стола без зависимости от Qt-виджетов."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Executor
from dataclasses import asdict
from typing import Annotated, Any, cast

import psutil
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.domain.models.camera import CameraModel
from src.logger import logger
from src.services.remote.camera_service import CameraService
from src.utils.scanner.scanner_icmp import system_probe
from src.utils.scanner.scanner_range import ScannerAddressRange
from src.utils.scanner.scanner_transport import tcp_probe
from src.utils.system.pool_registry import get_registered_pools
from src.utils.text.command_highlight import highlight_commands
from src.workers.application_pools import PoolBusyError, get_created_application_pools
from src.workers.task_presentation import task_label
from src.workers.tracked_pool import TrackedThreadPoolExecutor, run_in_tracked_executor

from .auth import RequestUser, require_auth

AuthenticatedUser = Annotated[RequestUser, Depends(require_auth)]


class HighlightStep(BaseModel):
    """Только текст для просмотра, без параметров выполнения."""

    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(max_length=131072)
    type: str = Field(default="ssh", pattern=r"^(ssh|local)$")


class HighlightRequest(BaseModel):
    """Ограничить память и время разбора одного окна информации."""

    model_config = ConfigDict(extra="forbid", strict=True)
    commands: list[HighlightStep] = Field(max_length=64)

    @field_validator("commands")
    @classmethod
    def total_size(cls, value: list[HighlightStep]) -> list[HighlightStep]:
        """Не принимать пакет больше 512 КиБ даже при допустимых отдельных шагах."""
        if sum(len(item.text.encode("utf-8")) for item in value) > 524288:
            raise ValueError("Слишком большой пакет для подсветки")
        return value


class ScanRequest(BaseModel):
    """Ограниченный запрос TCP-сканера."""

    model_config = ConfigDict(extra="forbid", strict=True)
    target: str = Field(min_length=1, max_length=100)
    ports: list[int] = Field(
        default_factory=lambda: [22, 80, 443], min_length=1, max_length=16
    )
    timeout: float = Field(default=0.5, ge=0.1, le=3.0, allow_inf_nan=False)

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        """Проверить размер диапазона до планирования сетевых операций."""
        addresses = ScannerAddressRange(value)
        if len(addresses) > 1024:
            raise ValueError("Максимум 1024 IPv4-адреса за один запуск")
        return value.strip()

    @field_validator("ports")
    @classmethod
    def validate_ports(cls, values: list[int]) -> list[int]:
        """Отбросить дубликаты и запретить недопустимые порты."""
        if any(not 1 <= port <= 65535 for port in values):
            raise ValueError("Порт должен быть от 1 до 65535")
        return list(dict.fromkeys(values))


class PingRequest(BaseModel):
    """Одна ICMP-проба; повторением и историей управляет браузер."""

    model_config = ConfigDict(extra="forbid", strict=True)
    target: str = Field(min_length=1, max_length=253)
    timeout: float = Field(default=1.0, ge=0.1, le=3.0, allow_inf_nan=False)

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        """Разрешить IPv4 или DNS-имя, исключая параметры системного ping."""
        import ipaddress
        import re

        try:
            return str(ipaddress.IPv4Address(value))
        except ValueError:
            name = value.encode("idna").decode("ascii").rstrip(".")
            if (
                not name
                or re.fullmatch(r"[0-9.]+", name)
                or not all(
                    re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                    for part in name.split(".")
                )
            ):
                raise ValueError("Укажите IPv4-адрес или DNS-имя хоста") from None
            return name


def _event(kind: str, data: dict[str, Any]) -> str:
    """Сериализовать одно событие SSE."""
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class _VeyonMonitor:
    """Показатели Veyon для монитора; опрос CLI не чаще раза в 5 с, как в Qt."""

    INTERVAL_SEC = 5.0

    def __init__(self, getter: Callable[[], Any]) -> None:
        self._getter = getter
        self._lock = threading.Lock()
        self._value: dict[str, Any] | None = None
        self._at = -float("inf")

    def snapshot(self) -> dict[str, Any] | None:
        """Вернуть `None`, если Veyon на этой машине не используется."""
        service = self._getter()
        if service is None:
            return None
        with self._lock:
            connections = service.get_webapi_client_count()
            if not connections and not service.is_cli_available():
                return None
            now = time.monotonic()
            if self._value is None or now - self._at >= self.INTERVAL_SEC:
                self._value = self._collect(service)
                self._at = now
            return {**self._value, "connections": connections}

    def invalidate(self) -> None:
        with self._lock:
            self._value = None

    @staticmethod
    def _collect(service: Any) -> dict[str, Any]:
        from src.utils.system.subprocess_env import is_running_elevated

        result: dict[str, Any] = {
            "status": "unknown",
            "limit": None,
            "rss_mb": None,
            "commit_mb": None,
            "webapi_established": None,
            "webapi_close_wait": None,
            "can_restart": False,
        }
        try:
            cli = service.is_cli_available()
            result["status"] = service.get_service_status() if cli else "cli_missing"
            stats = service.get_webapi_server_stats()
            for key in ("rss_mb", "commit_mb", "webapi_established", "webapi_close_wait"):
                result[key] = stats.get(key)
            if cli:
                result["limit"] = service.get_webapi_connection_limit()
            # Без повышенных прав UAC/pkexec спросил бы на экране сервера, а не в браузере.
            result["can_restart"] = bool(cli and is_running_elevated())
        except Exception:  # noqa: BLE001 - сбой диагностики не останавливает монитор
            logger.debug("Web monitor: Veyon stats unavailable", exc_info=True)
        return result


def _resources(
    thumbnails: Any, started: float, veyon: _VeyonMonitor | None = None
) -> dict[str, Any]:
    """Снять показатели сервера, не раскрывая метаданные команд."""
    memory = psutil.virtual_memory()
    process = psutil.Process()
    snapshots = [pool.get_snapshot() for pool in get_created_application_pools()]
    snapshots += [
        pool.get_snapshot() for pool in TrackedThreadPoolExecutor.get_persistent_pools()
    ]
    snapshots += [pool.get_snapshot() for pool in get_registered_pools()]
    tasks = [
        {
            "pool": snapshot["stats"]["label"],
            "operation": task_label(
                item["fn"],
                {
                    key: value
                    for key, value in item.get("metadata", {}).items()
                    if key in {"type", "feature", "backend", "port", "count"}
                },
            ),
            "state": item["state"],
            "duration": item["duration"],
        }
        for snapshot in snapshots
        for item in snapshot["tasks"]
    ]
    return {
        "timestamp": time.time(),
        "uptime_sec": time.monotonic() - started,
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "cpu_count": psutil.cpu_count(),
        "memory": {
            "total": memory.total,
            "used": memory.used,
            "percent": memory.percent,
        },
        "process": {
            "pid": process.pid,
            "rss": process.memory_info().rss,
            "threads": process.num_threads(),
        },
        "pools": [snapshot["stats"] for snapshot in snapshots],
        "tasks": tasks[:256],
        "thumbnails": thumbnails.get_metrics() if thumbnails is not None else {},
        "veyon": veyon.snapshot() if veyon is not None else None,
    }


def _camera_snapshot(camera: CameraModel, gate: threading.BoundedSemaphore) -> bytes:
    """Получить один ограниченный JPEG, освобождая RTSP-контейнер в его потоке."""
    from src.services.media.camera import open_camera
    from src.utils.system.media_runtime import load_pyav

    if not gate.acquire(blocking=False):
        raise PoolBusyError("Захват камер занят. Повторите запрос позже")
    try:
        deadline = time.monotonic() + 8
        with open_camera(camera.rtsp_url, 3.0) as container:
            if not container.streams.video:
                raise ValueError("Нет видеопотока")
            stream = container.streams.video[0]
            stream.codec_context.thread_count = 1
            if stream.width * stream.height > 16_000_000:
                raise ValueError("Слишком большой кадр")
            for packet in container.demux(stream):
                if time.monotonic() > deadline:
                    raise TimeoutError("Истекло время получения кадра")
                for frame in packet.decode():
                    if frame.width * frame.height > 16_000_000:
                        raise ValueError("Слишком большой кадр")
                    scale = min(1.0, 1280 / frame.width, 720 / frame.height)
                    width = max(2, int(frame.width * scale) // 2 * 2)
                    height = max(2, int(frame.height * scale) // 2 * 2)
                    frame = frame.reformat(
                        width=width, height=height, format="yuvj420p"
                    )
                    codec = load_pyav().CodecContext.create("mjpeg", "w")
                    codec.width, codec.height, codec.pix_fmt = width, height, "yuvj420p"
                    codec.thread_count = 1
                    return b"".join(bytes(encoded) for encoded in codec.encode(frame))
        raise ValueError("Камера не вернула кадр")
    finally:
        gate.release()


def install_desktop_routes(
    app: FastAPI,
    pool_getter: Callable[[], Executor | None],
    camera_getter: Callable[[], CameraService | None],
    thumbnail_getter: Callable[[], Any],
    veyon_getter: Callable[[], Any] = lambda: None,
) -> None:
    """Подключить инструменты к существующим авторизации, сервисам и пулам."""
    started = time.monotonic()
    camera_gate = threading.BoundedSemaphore(2)
    scan_gate = asyncio.Semaphore(2)
    veyon_monitor = _VeyonMonitor(veyon_getter)
    veyon_restart = asyncio.Lock()

    @app.post("/api/tools/command-highlight")
    async def command_highlight(
        payload: HighlightRequest, user: AuthenticatedUser
    ) -> dict[str, Any]:
        """Подсветить текст локально, не исполняя и не сохраняя команды."""
        return {
            "commands": await run_in_tracked_executor(
                pool_getter(),
                highlight_commands,
                [item.model_dump() for item in payload.commands],
                metadata={"type": "web_highlight", "source": "web"},
            )
        }

    @app.post("/api/tools/scan")
    async def scan(
        payload: ScanRequest,
        request: Request,
        user: AuthenticatedUser,
    ) -> StreamingResponse:
        """Выдать результаты ограниченными порциями, останавливаясь при отключении."""
        if scan_gate.locked():
            raise HTTPException(429, "Уже выполняются два сканирования")
        await scan_gate.acquire()
        targets = ScannerAddressRange(payload.target)
        total = len(targets) * len(payload.ports)

        async def events() -> AsyncIterator[str]:
            pending: set[asyncio.Task[Any]] = set()
            ready: set[asyncio.Task[Any]] = set()
            done = 0
            probes = iter((host, port) for host in targets for port in payload.ports)
            try:
                yield _event("start", {"total": total})
                while True:
                    if await request.is_disconnected():
                        break
                    while len(pending) < 8:
                        item = next(probes, None)
                        if item is None:
                            break
                        host, port = item
                        pending.add(
                            asyncio.create_task(
                                run_in_tracked_executor(
                                    pool_getter(),
                                    tcp_probe,
                                    host,
                                    port,
                                    payload.timeout,
                                    metadata={"type": "web_scan", "owner": user.owner},
                                )
                            )
                        )
                    if not pending:
                        break
                    ready, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    while ready:
                        task = ready.pop()
                        result = task.result()
                        done += 1
                        yield _event(
                            "result", {**asdict(result), "done": done, "total": total}
                        )
                yield _event("done", {"done": done, "total": total})
            except PoolBusyError:
                yield _event(
                    "error",
                    {"message": "Общий пул занят. Повторите сканирование позже"},
                )
            finally:
                pending |= ready
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                scan_gate.release()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/tools/ping")
    async def ping(payload: PingRequest, user: AuthenticatedUser) -> dict[str, Any]:
        """Запустить ICMP-пробу вне event loop и отменить её при отмене запроса."""
        cancelled = threading.Event()
        try:
            result = await run_in_tracked_executor(
                pool_getter(),
                system_probe,
                payload.target,
                round(payload.timeout * 1000),
                cancelled.is_set,
                metadata={"type": "web_ping", "owner": user.owner},
            )
            return {**asdict(result), "timestamp": time.time()}
        finally:
            cancelled.set()

    @app.get("/api/tools/resources")
    async def resources(user: AuthenticatedUser) -> dict[str, Any]:
        """Вернуть живые показатели машины, на которой работает веб-сервер."""
        return cast(
            dict[str, Any],
            await run_in_tracked_executor(
                pool_getter(),
                _resources,
                thumbnail_getter(),
                started,
                veyon_monitor,
                metadata={"type": "web_resources", "source": "web"},
            ),
        )

    @app.post("/api/tools/veyon/restart")
    async def restart_veyon(user: AuthenticatedUser) -> dict[str, Any]:
        """Перезапустить локальный сервис Veyon, только если не нужен запрос прав."""
        from src.utils.system.subprocess_env import is_running_elevated

        service = veyon_getter()
        if service is None or not service.is_cli_available():
            raise HTTPException(409, "Veyon на этом компьютере не найден")
        if not is_running_elevated():
            raise HTTPException(
                409,
                "Приложение запущено без прав администратора. Запрос прав появился "
                "бы на экране сервера: перезапустите сервис в самом приложении",
            )
        if veyon_restart.locked():
            raise HTTPException(409, "Сервис Veyon уже перезапускается")
        async with veyon_restart:
            logger.info(f"Web: перезапуск сервиса Veyon, пользователь {user.username}")
            ok = await run_in_tracked_executor(
                pool_getter(),
                service.restart_service,
                metadata={"type": "web_veyon_restart", "source": "web"},
            )
            service.invalidate_installed_cache()
            veyon_monitor.invalidate()
        if not ok:
            raise HTTPException(
                500, service.last_error() or "Не удалось перезапустить сервис Veyon"
            )
        return {"success": True}

    def cameras() -> list[CameraModel]:
        """Получить каталог из DI, не создавая второй сервис камер."""
        service = camera_getter()
        if service is None:
            raise HTTPException(503, "Сервис камер недоступен")
        return service.load_cameras()

    @app.get("/api/tools/cameras")
    async def camera_list(user: AuthenticatedUser) -> dict[str, Any]:
        """Отдать только публичные поля каталога."""
        return {
            "cameras": [
                {
                    "iid": camera.iid,
                    "name": camera.camera_name,
                    "address": camera.address,
                    "cabinet": camera.cabinet,
                    "host": camera.rtsp_host,
                    "endpoint": camera.ip_address,
                }
                for camera in cameras()
            ]
        }

    @app.get("/api/tools/cameras/{iid}/snapshot")
    async def snapshot(iid: str, user: AuthenticatedUser) -> Response:
        """Разрешить получение кадра исключительно камеры из сохранённого каталога."""
        camera = next((item for item in cameras() if item.iid == iid), None)
        if camera is None:
            raise HTTPException(404, "Камера не найдена")
        if camera.credential_error:
            raise HTTPException(409, camera.credential_error)
        try:
            data = await run_in_tracked_executor(
                pool_getter(),
                _camera_snapshot,
                camera,
                camera_gate,
                metadata={"type": "web_camera_snapshot", "source": "web"},
            )
        except PoolBusyError:
            raise
        except Exception:  # noqa: BLE001
            # Текст исключения медиастека может содержать авторизованный RTSP URL.
            raise HTTPException(
                502,
                "Не удалось получить кадр. Проверьте доступность и настройки камеры",
            ) from None
        return Response(
            data, media_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )
