"""Файлы хоста в браузере: SFTP без промежуточной копии на сервере."""

from __future__ import annotations

import asyncio
import errno
import queue
import secrets
import stat
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Annotated, Any
from urllib.parse import quote

import paramiko
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import ClientDisconnect

from src.logger import logger
from src.utils.filesystem.sftp_files import (
    OperationControl,
    TransferCancelled,
    atomic_remote_writer,
    describe_error,
    describe_ssh_failure,
    list_remote_dir,
    remote_child,
    remote_stat,
    remove_remote_tree,
    validate_name,
)
from src.workers.tracked_pool import TrackedThreadPoolExecutor

from .auth import (
    INSECURE_TRANSPORT_MESSAGE,
    RequestUser,
    is_secure_transport,
    require_secure_auth,
)

User = Annotated[RequestUser, Depends(require_secure_auth)]
RemotePath = Annotated[str, Field(min_length=1, max_length=4096, pattern=r"^[^\x00]+$")]

POOL_SIZE = 8
MAX_TRANSFERS = 4
MAX_CONNECTIONS = 16
MAX_ENTRIES = 50_000
MAX_TICKETS = 256
TICKET_TTL_SEC = 60
IDLE_CONNECTION_SEC = 60
SHORT_DEADLINE_SEC = 60
DELETE_DEADLINE_SEC = 600
# Ожидание следующего куска тела или места у браузера, после которого передача обрывается.
STALL_TIMEOUT_SEC = 120
CHUNK_SIZE = 64 * 1024
QUEUE_CHUNKS = 16


class PathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: RemotePath


class RenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: RemotePath
    destination: RemotePath


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    paths: list[RemotePath] = Field(min_length=1, max_length=256)


@dataclass
class _Connection:
    """SSH-соединение пары пользователь/хост; у каждой операции свой SFTP-канал."""

    client: paramiko.SSHClient
    signature: tuple[Any, ...]
    users: int = 0
    last_used: float = field(default_factory=time.monotonic)


class _Connections:
    """Ограниченный набор соединений; простаивающие закрываются фоновым потоком."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], _Connection] = {}
        self._connecting: dict[tuple[str, str], threading.Lock] = {}
        self._stop = threading.Event()
        self._sweeper: threading.Thread | None = None

    @staticmethod
    def _signature(device: Any) -> tuple[Any, ...]:
        return (device.host, device.port, device.credential_bundle_iid)

    def acquire(self, owner: str, device: Any) -> _Connection:
        """Вернуть живое соединение, подключаясь вне общей блокировки."""
        from src.workers.command.executor_base import create_ssh_client

        key = (owner, device.iid)
        signature = self._signature(device)
        with self._lock:
            gate = self._connecting.setdefault(key, threading.Lock())
        with gate:
            with self._lock:
                if self._stop.is_set():
                    raise RuntimeError("Сервер останавливается")
                item = self._items.get(key)
                if item is not None and self._alive(item, signature):
                    item.users += 1
                    item.last_used = time.monotonic()
                    return item
                stale = self._items.pop(key, None)
                if len(self._items) >= MAX_CONNECTIONS and not self._evict_idle():
                    raise HTTPException(429, "Открыто слишком много SFTP-соединений")
            self._close(stale)
            client = create_ssh_client(device)
            item = _Connection(client, signature, users=1)
            with self._lock:
                if self._stop.is_set():
                    self._close(item)
                    raise RuntimeError("Сервер останавливается")
                self._items[key] = item
                self._start_sweeper()
            return item

    @staticmethod
    def _alive(item: _Connection, signature: tuple[Any, ...]) -> bool:
        transport = item.client.get_transport()
        return (
            item.signature == signature
            and transport is not None
            and transport.is_active()
        )

    def _evict_idle(self) -> bool:
        """Освободить место самым старым простаивающим соединением."""
        idle = [(item.last_used, key) for key, item in self._items.items() if not item.users]
        if not idle:
            return False
        _used, key = min(idle)
        self._close(self._items.pop(key))
        return True

    def release(self, item: _Connection) -> None:
        with self._lock:
            item.users -= 1
            item.last_used = time.monotonic()

    def _start_sweeper(self) -> None:
        if self._sweeper is None:
            self._sweeper = threading.Thread(
                target=self._sweep, name="web-files-idle", daemon=True
            )
            self._sweeper.start()

    def _sweep(self) -> None:
        while not self._stop.wait(5):
            self.sweep_once()

    def sweep_once(self, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        expired = []
        with self._lock:
            for key, item in list(self._items.items()):
                if not item.users and moment - item.last_used >= IDLE_CONNECTION_SEC:
                    expired.append(self._items.pop(key))
        for item in expired:
            self._close(item)

    @staticmethod
    def _close(item: _Connection | None) -> None:
        if item is not None:
            try:
                item.client.close()
            except (OSError, EOFError, paramiko.SSHException):
                pass

    def close(self) -> None:
        """Закрытие транспорта прерывает выполняющиеся операции."""
        self._stop.set()
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
        for item in items:
            self._close(item)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


@dataclass(frozen=True)
class _Ticket:
    owner: str
    iid: str
    path: str
    name: str
    expires: float


def _entry_type(mode: int | None) -> str:
    if mode is None:
        return "other"
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISDIR(mode):
        return "dir"
    return "file" if stat.S_ISREG(mode) else "other"


def _http_error(error: BaseException) -> HTTPException:
    """Превратить сбой операции в ответ; текст не содержит учётных данных."""
    if isinstance(error, HTTPException):
        return error
    if isinstance(error, TransferCancelled):
        return HTTPException(499, "Операция отменена")
    if isinstance(error, FileExistsError):
        return HTTPException(409, "Объект с таким именем уже существует")
    if isinstance(error, FileNotFoundError) or getattr(error, "errno", None) == errno.ENOENT:
        return HTTPException(404, "Файл или каталог не найден")
    if isinstance(error, PermissionError) or getattr(error, "errno", None) == errno.EACCES:
        return HTTPException(403, "Нет прав доступа к этому пути")
    if isinstance(error, ValueError):
        return HTTPException(400, str(error))
    if isinstance(error, TimeoutError):
        return HTTPException(504, "Истекло время операции SFTP")
    if error.__class__.__name__ == "SftpSubsystemMissing":
        return HTTPException(502, str(error))
    if isinstance(error, paramiko.SSHException | EOFError):
        return HTTPException(502, describe_ssh_failure(error))
    if isinstance(error, OSError):
        return HTTPException(502, describe_error(error))
    logger.error(f"Web files: {type(error).__name__}")
    return HTTPException(500, "Сбой файловой операции")


def install_files_routes(
    app: FastAPI, device_getter: Callable[[str], Any]
) -> Callable[[], None]:
    """Подключить маршруты; возвращает остановку всех операций и соединений."""
    pool = TrackedThreadPoolExecutor(
        max_workers=POOL_SIZE,
        label="Файлы веба",
        thread_name_prefix="web-files",
        ephemeral=False,
    )
    connections = _Connections()
    transfers = threading.BoundedSemaphore(MAX_TRANSFERS)
    stopping = threading.Event()
    tickets: dict[str, _Ticket] = {}
    tickets_lock = threading.Lock()

    def shutdown() -> None:
        stopping.set()
        connections.close()
        with tickets_lock:
            tickets.clear()
        pool.shutdown(wait=False, cancel_futures=True)

    def device_or_404(iid: str) -> Any:
        device = device_getter(iid)
        if device is None:
            raise HTTPException(404, "Устройство не найдено")
        return device

    def control_for(
        deadline_sec: float | None, cancelled: threading.Event | None = None
    ) -> OperationControl:
        return OperationControl(
            lambda: stopping.is_set() or bool(cancelled and cancelled.is_set()),
            time.monotonic() + deadline_sec if deadline_sec else None,
        )

    def with_sftp(
        owner: str,
        device: Any,
        control: OperationControl,
        action: Callable[[Any], Any],
    ) -> Any:
        """Выполнить действие в собственном SFTP-канале общего SSH-соединения."""
        from src.workers.command.sftp_utils import open_sftp_with_diagnosis

        connection = connections.acquire(owner, device)
        sftp = None
        try:
            sftp = open_sftp_with_diagnosis(
                connection.client, host=device.host, control=control
            )
            with control.watch(sftp.get_channel()):
                return action(sftp)
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except (OSError, EOFError, paramiko.SSHException):
                    pass
            connections.release(connection)

    async def short(
        user: RequestUser,
        device: Any,
        action: Callable[[Any], Any],
        deadline: float = SHORT_DEADLINE_SEC,
    ) -> Any:
        """Выполнить короткую операцию в пуле и перевести ошибку в HTTP-ответ."""
        control = control_for(deadline)
        try:
            future = pool.submit(
                with_sftp,
                user.owner,
                device,
                control,
                lambda sftp: action(sftp, control),
                metadata={"type": "web_files", "owner": user.owner},
            )
        except RuntimeError as error:
            raise HTTPException(503, "Сервер останавливается") from error
        try:
            return await asyncio.wrap_future(future)
        except Exception as error:  # noqa: BLE001 - перевод в ответ с понятным текстом
            raise _http_error(error) from None

    @app.get("/api/tools/files/{iid}/list")
    async def list_dir(iid: str, user: User, path: str = "") -> dict[str, Any]:
        """Каталог хоста; пустой путь означает домашний каталог учётной записи."""
        device = device_or_404(iid)
        if "\0" in path or len(path) > 4096:
            raise HTTPException(400, "Недопустимый путь")

        def action(sftp: Any, control: OperationControl) -> dict[str, Any]:
            normalized, entries = list_remote_dir(sftp, path, control)
            truncated = len(entries) > MAX_ENTRIES
            parent = str(PurePosixPath(normalized).parent)
            return {
                "path": normalized,
                "parent": None if parent == normalized else parent,
                "truncated": truncated,
                "entries": [
                    {
                        "name": entry.name,
                        "type": _entry_type(entry.mode),
                        "size": entry.size,
                        "modified": entry.modified,
                    }
                    for entry in entries[:MAX_ENTRIES]
                ],
            }

        return await short(user, device, action)

    @app.get("/api/tools/files/{iid}/stat")
    async def stat_path(iid: str, user: User, path: str) -> dict[str, Any]:
        """Проверка перед загрузкой: браузер спрашивает о замене до отправки файла."""
        device = device_or_404(iid)
        if not path or "\0" in path or len(path) > 4096:
            raise HTTPException(400, "Недопустимый путь")

        def action(sftp: Any, _control: OperationControl) -> dict[str, Any]:
            attrs = remote_stat(sftp, path)
            if attrs is None:
                return {"exists": False}
            return {
                "exists": True,
                "type": _entry_type(attrs.st_mode),
                "size": attrs.st_size or 0,
            }

        return await short(user, device, action)

    @app.post("/api/tools/files/{iid}/mkdir")
    async def mkdir(iid: str, payload: PathRequest, user: User) -> dict[str, Any]:
        device = device_or_404(iid)
        logger.info(f"Web files: {user.owner} создаёт каталог {device.host}:{payload.path}")
        try:
            validate_name(PurePosixPath(payload.path).name)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

        def action(sftp: Any, _control: OperationControl) -> None:
            if remote_stat(sftp, payload.path) is not None:
                raise FileExistsError(payload.path)
            sftp.mkdir(payload.path)

        await short(user, device, action)
        return {"success": True}

    @app.post("/api/tools/files/{iid}/rename")
    async def rename(iid: str, payload: RenameRequest, user: User) -> dict[str, Any]:
        device = device_or_404(iid)
        logger.info(
            f"Web files: {user.owner} переименовывает {device.host}:{payload.source} "
            f"-> {payload.destination}"
        )
        try:
            validate_name(PurePosixPath(payload.destination).name)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

        def action(sftp: Any, _control: OperationControl) -> None:
            if remote_stat(sftp, payload.destination) is not None:
                raise FileExistsError(payload.destination)
            sftp.rename(payload.source, payload.destination)

        await short(user, device, action)
        return {"success": True}

    @app.post("/api/tools/files/{iid}/delete")
    async def delete(iid: str, payload: DeleteRequest, user: User) -> dict[str, Any]:
        """Удалить объекты; ошибка одного не отменяет остальные, ссылки не обходятся."""
        device = device_or_404(iid)
        logger.info(
            f"Web files: {user.owner} удаляет на {device.host}: {len(payload.paths)} объектов, "
            f"первый {payload.paths[0]}"
        )

        def action(sftp: Any, control: OperationControl) -> dict[str, Any]:
            removed = 0
            errors: list[str] = []

            def count(_path: str) -> None:
                nonlocal removed
                removed += 1

            for path in payload.paths:
                control.check()
                try:
                    remove_remote_tree(sftp, path, control, count)
                except (TransferCancelled, TimeoutError):
                    raise
                except Exception as error:  # noqa: BLE001 - итог содержит все ошибки
                    if len(errors) < 100:
                        errors.append(f"{path}: {describe_error(error)}")
            return {"removed": removed, "errors": errors}

        return await short(user, device, action, DELETE_DEADLINE_SEC)

    @app.post("/api/tools/files/{iid}/upload")
    async def upload(
        iid: str,
        request: Request,
        user: User,
        directory: str,
        name: str,
        replace: bool = False,
    ) -> dict[str, Any]:
        """Принять тело потоком в удалённый временный файл и опубликовать его целиком."""
        device = device_or_404(iid)
        try:
            destination = remote_child(directory, name)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        declared = request.headers.get("content-length", "")
        expected = int(declared) if declared.isdigit() else None
        if not transfers.acquire(blocking=False):
            raise HTTPException(429, f"Одновременно до {MAX_TRANSFERS} передач файлов")
        chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=QUEUE_CHUNKS)
        cancelled = threading.Event()
        control = control_for(None, cancelled)

        def write(sftp: Any) -> int:
            if not replace and remote_stat(sftp, destination) is not None:
                raise FileExistsError(destination)
            total = 0
            with atomic_remote_writer(
                sftp, destination, control=control, replace=replace
            ) as temporary:
                with sftp.open(temporary, "wb") as remote:
                    remote.set_pipelined(True)
                    idle_since = time.monotonic()
                    while True:
                        control.check()
                        try:
                            chunk = chunks.get(timeout=0.25)
                        except queue.Empty:
                            if time.monotonic() - idle_since > STALL_TIMEOUT_SEC:
                                raise TimeoutError("Браузер перестал передавать файл")
                            continue
                        idle_since = time.monotonic()
                        if chunk is None:
                            break
                        remote.write(chunk)
                        total += len(chunk)
                if expected is not None and total != expected:
                    raise ValueError("Файл передан не полностью")
            return total

        def run() -> int:
            try:
                return with_sftp(user.owner, device, control, write)
            finally:
                transfers.release()

        logger.info(f"Web files: {user.owner} загружает {device.host}:{destination}")
        try:
            future = pool.submit(
                run, metadata={"type": "web_files_transfer", "owner": user.owner}
            )
        except RuntimeError as error:
            transfers.release()
            raise HTTPException(503, "Сервер останавливается") from error

        async def put(item: bytes | None) -> None:
            while not future.done():
                try:
                    chunks.put_nowait(item)
                    return
                except queue.Full:
                    await asyncio.sleep(0.005)

        try:
            async for chunk in request.stream():
                if future.done():
                    break
                if chunk:
                    await put(chunk)
            await put(None)
        except ClientDisconnect:
            # Без признака конца файл не будет опубликован.
            cancelled.set()
        except BaseException:
            cancelled.set()
            raise
        try:
            size = await asyncio.wrap_future(future)
        except Exception as error:  # noqa: BLE001
            raise _http_error(error) from None
        return {"success": True, "size": size, "path": destination}

    @app.post("/api/tools/files/{iid}/download-ticket")
    async def download_ticket(
        iid: str, payload: PathRequest, user: User
    ) -> dict[str, Any]:
        """Выдать одноразовую ссылку: браузер скачивает сам, пароль не попадает в URL."""
        device = device_or_404(iid)

        def action(sftp: Any, _control: OperationControl) -> None:
            attrs = sftp.lstat(payload.path)
            if stat.S_ISLNK(attrs.st_mode or 0):
                raise ValueError("Ссылки не скачиваются: откройте файл, на который она указывает")
            if not stat.S_ISREG(attrs.st_mode or 0):
                raise ValueError("Скачать можно только обычный файл")

        await short(user, device, action)
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with tickets_lock:
            for key in [key for key, item in tickets.items() if item.expires <= now]:
                tickets.pop(key)
            if len(tickets) >= MAX_TICKETS:
                raise HTTPException(429, "Слишком много незавершённых скачиваний")
            tickets[token] = _Ticket(
                user.owner,
                iid,
                payload.path,
                PurePosixPath(payload.path).name,
                now + TICKET_TTL_SEC,
            )
        return {"ticket": token}

    @app.get("/api/tools/files/download/{token}")
    async def download(token: str, request: Request) -> StreamingResponse:
        """Отдать файл потоком; билет заменяет авторизацию и работает один раз."""
        client = request.client.host if request.client else None
        if not is_secure_transport(request.url.scheme, client):
            raise HTTPException(403, INSECURE_TRANSPORT_MESSAGE)
        with tickets_lock:
            ticket = tickets.pop(token, None)
        if ticket is None or ticket.expires <= time.monotonic():
            raise HTTPException(404, "Ссылка на скачивание устарела")
        device = device_or_404(ticket.iid)
        if not transfers.acquire(blocking=False):
            raise HTTPException(429, f"Одновременно до {MAX_TRANSFERS} передач файлов")
        logger.info(f"Web files: {ticket.owner} скачивает {device.host}:{ticket.path}")
        chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=QUEUE_CHUNKS)
        cancelled = threading.Event()
        control = control_for(None, cancelled)
        loop = asyncio.get_running_loop()
        opened: asyncio.Future[int] = loop.create_future()

        def notify(size: int | None, error: BaseException | None) -> None:
            def apply() -> None:
                if opened.done():
                    return
                if error is not None:
                    opened.set_exception(error)
                else:
                    opened.set_result(size or 0)

            try:
                loop.call_soon_threadsafe(apply)
            except RuntimeError:
                pass

        def read(sftp: Any) -> None:
            attrs = sftp.lstat(ticket.path)
            if not stat.S_ISREG(attrs.st_mode or 0):
                raise ValueError("Скачать можно только обычный файл")
            size = attrs.st_size or 0
            with sftp.open(ticket.path, "rb") as remote:
                remote.prefetch(size, max_concurrent_requests=32)
                notify(size, None)
                while True:
                    control.check()
                    data = remote.read(CHUNK_SIZE)
                    if not data:
                        break
                    waited = time.monotonic()
                    while True:
                        try:
                            chunks.put(data, timeout=0.25)
                            break
                        except queue.Full:
                            control.check()
                            if time.monotonic() - waited > STALL_TIMEOUT_SEC:
                                raise TimeoutError("Браузер перестал принимать файл")

        def run() -> None:
            try:
                with_sftp(ticket.owner, device, control, read)
            except BaseException as error:  # noqa: BLE001 - причину получает ответ
                notify(None, error)
                logger.info(f"Web files: скачивание прервано: {type(error).__name__}")
                cancelled.set()
            finally:
                transfers.release()
                # Конец потока не должен зависеть от места в очереди.
                try:
                    chunks.put_nowait(None)
                except queue.Full:
                    pass

        try:
            future = pool.submit(
                run, metadata={"type": "web_files_transfer", "owner": ticket.owner}
            )
        except RuntimeError as error:
            transfers.release()
            raise HTTPException(503, "Сервер останавливается") from error
        # Отменённая в очереди задача не выполнит run и не сообщит результат.
        future.add_done_callback(
            lambda done: done.cancelled()
            and notify(None, RuntimeError("Сервер останавливается"))
        )
        try:
            size = await opened
        except Exception as error:  # noqa: BLE001
            raise _http_error(error) from None

        async def body() -> AsyncIterator[bytes]:
            try:
                while True:
                    try:
                        data = chunks.get_nowait()
                    except queue.Empty:
                        # Запасной выход на случай, если признак конца не поместился.
                        if future.done():
                            return
                        await asyncio.sleep(0.005)
                        continue
                    if data is None:
                        return
                    yield data
            finally:
                cancelled.set()

        return StreamingResponse(
            body(),
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "Content-Length": str(size),
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(ticket.name, safe='')}",
            },
        )

    return shutdown
