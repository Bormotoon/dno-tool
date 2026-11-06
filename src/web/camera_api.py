"""Просмотр камер и управление записью без доступа к настройкам приложения."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from src.domain.models.camera import CameraModel
from src.services.media.camera import open_camera
from src.services.remote.camera_service import CameraService
from src.utils.system.media_runtime import load_pyav
from src.workers.tracked_pool import TrackedThreadPoolExecutor

from .auth import RequestUser, require_auth

User = Annotated[RequestUser, Depends(require_auth)]


@dataclass
class Preview:
    """Только последний кадр; медленный браузер не накапливает очередь."""

    camera: CameraModel
    cancel: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    refs: int = 1
    sequence: int = 0
    jpeg: bytes = b""
    state: str = "Подключение…"


class CameraStreams:
    """Общий ограниченный пул веб-просмотра с одним декодером на камеру."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, Preview] = {}
        self._pool = TrackedThreadPoolExecutor(
            max_workers=16,
            label="Видеостена веба",
            thread_name_prefix="web-camera",
            ephemeral=False,
        )
        self._stopping = False

    def acquire(self, camera: CameraModel) -> Preview:
        """Подписать ещё один браузер на существующий поток либо открыть новый."""
        with self._lock:
            if self._stopping:
                raise HTTPException(503, "Видеостена останавливается")
            entry = self._entries.get(camera.iid)
            if entry is not None:
                if entry.cancel.is_set() or entry.camera.rtsp_url != camera.rtsp_url:
                    raise HTTPException(
                        409, "Поток камеры перезапускается. Повторите просмотр"
                    )
                entry.refs += 1
                return entry
            if len(self._entries) >= 16:
                raise HTTPException(429, "Одновременно можно просматривать до 16 камер")
            entry = Preview(camera)
            self._entries[camera.iid] = entry
            self._pool.submit(
                self._decode, entry, metadata={"type": "web_camera_preview"}
            )
            return entry

    def release(self, entry: Preview) -> None:
        """Закрыть RTSP после последнего потребителя, не затрагивая Qt и запись."""
        with self._lock:
            entry.refs -= 1
            if entry.refs <= 0:
                entry.cancel.set()

    def close(self) -> None:
        """Запросить остановку; владельцы контейнеров освободят их в своих потоках."""
        with self._lock:
            self._stopping = True
            for entry in self._entries.values():
                entry.cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _decode(self, entry: Preview) -> None:
        """Декодировать RTSP с переподключением, таймаутами и лимитом площади."""
        try:
            while not entry.cancel.is_set():
                try:
                    with open_camera(entry.camera.rtsp_url, 3) as container:
                        if not container.streams.video:
                            raise ValueError("Нет видеопотока")
                        stream = container.streams.video[0]
                        stream.codec_context.thread_count = 1
                        if stream.width * stream.height > 16_000_000:
                            raise ValueError("Слишком большой кадр")
                        codec = None
                        last = 0.0
                        activity = time.monotonic()
                        for packet in container.demux(stream):
                            if entry.cancel.is_set():
                                return
                            if time.monotonic() - activity > 10:
                                raise TimeoutError("Нет кадров")
                            for frame in packet.decode():
                                if entry.cancel.is_set():
                                    return
                                activity = time.monotonic()
                                if frame.width * frame.height > 16_000_000:
                                    raise ValueError("Слишком большой кадр")
                                if activity - last < 0.125:
                                    continue
                                last = activity
                                scale = min(1.0, 960 / frame.width, 540 / frame.height)
                                width, height = max(
                                    2, int(frame.width * scale) // 2 * 2
                                ), max(2, int(frame.height * scale) // 2 * 2)
                                frame = frame.reformat(
                                    width=width, height=height, format="yuvj420p"
                                )
                                if (
                                    codec is None
                                    or codec.width != width
                                    or codec.height != height
                                ):
                                    codec = load_pyav().CodecContext.create(
                                        "mjpeg", "w"
                                    )
                                    codec.width, codec.height, codec.pix_fmt = (
                                        width,
                                        height,
                                        "yuvj420p",
                                    )
                                    codec.thread_count = 1
                                jpeg = b"".join(
                                    bytes(value) for value in codec.encode(frame)
                                )
                                if len(jpeg) > 4 * 1024 * 1024:
                                    raise ValueError("Слишком большой JPEG")
                                with entry.lock:
                                    entry.jpeg = jpeg
                                    entry.sequence += 1
                                    entry.state = "Прямой эфир"
                        if not entry.cancel.is_set():
                            raise ConnectionError("Камера завершила поток")
                except Exception:  # noqa: BLE001
                    # Ошибка RTSP может включать пароль, поэтому её текст не передаётся.
                    with entry.lock:
                        entry.state = "Нет связи. Повтор подключения…"
                        entry.jpeg = b""
                    entry.cancel.wait(2)
        finally:
            with self._lock:
                if self._entries.get(entry.camera.iid) is entry:
                    self._entries.pop(entry.camera.iid, None)


class WallRequest(BaseModel):
    """Все ячейки доски идут через один HTTP-поток браузера."""

    model_config = ConfigDict(extra="forbid", strict=True)
    iids: list[str] = Field(min_length=1, max_length=16)


class RecordingCommand(BaseModel):
    """Запись текущей страницы либо общая остановка, как в Qt."""

    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["start", "stop_all"]
    iids: list[str] = Field(default_factory=list, max_length=16)


def install_camera_routes(
    app: FastAPI, getter: Callable[[], CameraService | None]
) -> Callable[[], None]:
    """Подключить просмотр и команды общего менеджера записи через GUI-мост."""
    streams = CameraStreams()

    def service() -> CameraService:
        value = getter()
        if value is None:
            raise HTTPException(503, "Сервис камер недоступен")
        return value

    async def recording_command(
        action: Literal["state", "start", "stop_all"], iids: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        """Ожидать GUI асинхронно, отменяя ещё не начатую команду по таймауту."""
        try:
            future = service().web_recording_bridge().submit(action, iids)
        except RuntimeError as error:
            raise HTTPException(503, "Сервис записи занят или недоступен") from error
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=5)
        except TimeoutError as error:
            future.cancel()
            raise HTTPException(
                503, "Нет ответа менеджера записи. Проверьте состояние перед повтором"
            ) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(503, "Сервис записи недоступен") from error

    @app.get("/api/tools/cameras/state")
    async def recording_state(user: User) -> dict[str, Any]:
        """Состояния Qt-записи без настроек, путей и секретов."""
        return await recording_command("state")

    @app.post("/api/tools/cameras/recording")
    async def record(payload: RecordingCommand, user: User) -> dict[str, Any]:
        """Записать текущие камеры доски или остановить все записи приложения."""
        if payload.action == "start" and not payload.iids:
            raise HTTPException(400, "На текущей странице нет камер")
        if payload.action == "stop_all" and payload.iids:
            raise HTTPException(400, "Общая остановка не принимает список камер")
        return await recording_command(
            payload.action, tuple(dict.fromkeys(payload.iids))
        )

    def subscribe(iids: list[str], request: Request) -> StreamingResponse:
        """Мультиплексировать доску, не занимая лимит HTTP-соединений браузера."""
        models = {item.iid: item for item in service().load_cameras()}
        if set(iids) - models.keys():
            raise HTTPException(404, "Камера не найдена")
        entries = []
        try:
            for iid in dict.fromkeys(iids):
                entries.append(streams.acquire(models[iid]))
        except HTTPException:
            for entry in entries:
                streams.release(entry)
            raise

        async def events() -> AsyncIterator[str]:
            sequences: dict[str, int] = {}
            states: dict[str, str] = {}
            heartbeat = time.monotonic()
            try:
                while not await request.is_disconnected() and not any(
                    entry.cancel.is_set() for entry in entries
                ):
                    for entry in entries:
                        iid = entry.camera.iid
                        with entry.lock:
                            seq, jpeg, message = entry.sequence, entry.jpeg, entry.state
                        if states.get(iid) != message:
                            states[iid] = message
                            yield f'event: state\ndata: {json.dumps({"iid": iid, "message": message}, ensure_ascii=False)}\n\n'
                        if sequences.get(iid) != seq and jpeg:
                            sequences[iid] = seq
                            yield f'event: frame\ndata: {json.dumps({"iid": iid, "jpeg": base64.b64encode(jpeg).decode("ascii")})}\n\n'
                    if time.monotonic() - heartbeat > 15:
                        heartbeat = time.monotonic()
                        yield ": heartbeat\n\n"
                    await asyncio.sleep(0.06)
            finally:
                for entry in entries:
                    streams.release(entry)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/tools/cameras/preview")
    async def wall(
        payload: WallRequest, request: Request, user: User
    ) -> StreamingResponse:
        """Подписать до 16 ячеек, оставляя соединения для команд и статусов."""
        return subscribe(payload.iids, request)

    @app.get("/api/tools/cameras/{iid}/preview")
    async def preview(iid: str, request: Request, user: User) -> StreamingResponse:
        """Совместимый маршрут одиночной камеры."""
        return subscribe([iid], request)

    return streams.close
