"""SSH-терминал браузера: WebSocket поверх интерактивного канала хоста."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import paramiko
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from src.config import config
from src.logger import logger
from src.utils.filesystem.sftp_files import OperationControl, describe_ssh_failure
from src.workers.tracked_pool import TrackedThreadPoolExecutor

from .auth import _decode_token, authenticate_async, is_secure_transport, resolve_session_token

MAX_SESSIONS = 8
MAX_DIMENSION = 500
READ_SIZE = 32768
# Чанков в очереди к браузеру: при заполнении поток чтения останавливается,
# окно SSH-канала закрывается и удалённая команда притормаживается.
OUTPUT_QUEUE_SIZE = 64
CONNECT_DEADLINE_SEC = 30
CLOSE_AUTH = 4401
CLOSE_INSECURE = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_LIMIT = 4429


class TerminalSession:
    """Канал одного окна; вывод читает поток пула и передаёт в цикл событий."""

    def __init__(
        self,
        device: Any,
        loop: asyncio.AbstractEventLoop,
        output: asyncio.Queue[bytes | None],
        cols: int,
        rows: int,
    ) -> None:
        self.device = device
        self._loop = loop
        self._output = output
        self._cols = cols
        self._rows = rows
        self._slots = threading.BoundedSemaphore(OUTPUT_QUEUE_SIZE)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._client: paramiko.SSHClient | None = None
        self._channel: paramiko.Channel | None = None
        self._ready = threading.Event()
        self.error = ""

    def released(self) -> None:
        """Браузер получил кадр: поток чтения может продолжать."""
        try:
            self._slots.release()
        except ValueError:
            pass

    def _post(self, item: bytes | None) -> bool:
        """Дождаться места в очереди, не теряя возможность остановиться."""
        if item is not None:
            while not self._slots.acquire(timeout=0.25):
                if self._stop.is_set():
                    return False
        try:
            self._loop.call_soon_threadsafe(self._output.put_nowait, item)
        except RuntimeError:
            return False
        return True

    def run(self) -> None:
        """Подключиться и читать вывод до закрытия любой из сторон."""
        from src.workers.command.executor_base import create_ssh_client

        client = paramiko.SSHClient()
        with self._lock:
            self._client = client
        try:
            if self._stop.is_set():
                return
            create_ssh_client(self.device, client=client)
            transport = client.get_transport()
            if transport is None:
                raise ConnectionError("Нет SSH-соединения")
            control = OperationControl(self._stop.is_set)
            channel = transport.open_session(timeout=10)
            with self._lock:
                self._channel = channel
            channel.settimeout(0.5)
            with control.watch(channel):
                channel.get_pty(
                    term="xterm-256color", width=self._cols, height=self._rows
                )
                channel.invoke_shell()
            self._ready.set()
            while not self._stop.is_set():
                try:
                    data = channel.recv(READ_SIZE)
                except TimeoutError:
                    continue
                if not data or not self._post(data):
                    break
        except Exception as error:  # noqa: BLE001
            if not self._stop.is_set():
                self.error = describe_ssh_failure(error)
                logger.info(
                    f"Web terminal: {self.device.host}: {type(error).__name__}"
                )
        finally:
            self.close()
            self._post(None)

    def send(self, data: bytes) -> None:
        """Передать ввод; при заполненном окне вызов ждёт, поэтому не в цикле событий."""
        # Первые символы могут прийти раньше готовности оболочки: порядок сохраняется,
        # потому что следующий ввод читается только после этого вызова.
        self._ready.wait(CONNECT_DEADLINE_SEC)
        channel = self._channel
        if channel is None or not self._ready.is_set():
            return
        try:
            channel.sendall(data)
        except (OSError, EOFError, paramiko.SSHException):
            self.close()

    def resize(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        channel = self._channel
        if channel is None or not self._ready.is_set():
            return
        try:
            channel.resize_pty(width=cols, height=rows)
        except (OSError, EOFError, paramiko.SSHException):
            pass

    def close(self) -> None:
        """Закрытие прерывает блокирующий ввод-вывод в других потоках."""
        self._stop.set()
        self._ready.set()
        with self._lock:
            channel, client = self._channel, self._client
            self._channel, self._client = None, None
        for resource in (channel, client):
            if resource is not None:
                try:
                    resource.close()
                except (OSError, EOFError, paramiko.SSHException):
                    pass


def _dimension(value: Any, default: int) -> int:
    """Размеры окна приходят от браузера: bool и выход за пределы отбрасываются."""
    if type(value) is not int or not 1 <= value <= MAX_DIMENSION:
        return default
    return value


def _same_origin(ws: WebSocket) -> bool:
    """Страница другого сайта не должна открыть шелл от имени пользователя."""
    origin = ws.headers.get("origin")
    if not origin:
        # Браузер присылает Origin всегда; без него это не браузерный клиент.
        return True
    try:
        netloc = urlsplit(origin).netloc.lower()
        # Reverse proxy может менять Host: его внешний адрес задан в настройках.
        public = urlsplit(getattr(config.app.web, "public_url", "") or "").netloc.lower()
    except ValueError:
        return False
    return netloc == (ws.headers.get("host") or "").lower() or (bool(public) and netloc == public)


def install_terminal_routes(
    app: FastAPI, device_getter: Callable[[str], Any]
) -> Callable[[], None]:
    """Подключить маршрут; возвращает закрытие всех сеансов при остановке сервера."""
    pool = TrackedThreadPoolExecutor(
        max_workers=MAX_SESSIONS,
        label="Терминалы веба",
        thread_name_prefix="web-terminal",
        ephemeral=False,
    )
    lock = threading.Lock()
    sessions: set[TerminalSession] = set()
    stopping = threading.Event()

    def shutdown() -> None:
        stopping.set()
        with lock:
            current = list(sessions)
        for session in current:
            session.close()
        pool.shutdown(wait=False, cancel_futures=True)

    @app.websocket("/ws/terminal/{iid}")
    async def terminal(ws: WebSocket, iid: str) -> None:
        token = ws.query_params.get("token")
        resolved = resolve_session_token(token)
        if resolved:
            owner, _ = resolved
        else:
            creds = _decode_token(token) if token else None
            owner = await authenticate_async(*creds) if creds else None
        if owner is None:
            await ws.close(code=CLOSE_AUTH)
            return
        if not _same_origin(ws):
            await ws.close(code=CLOSE_AUTH)
            return
        # Закрытие до accept браузер видит как код 1006, поэтому причину
        # авторизованному пользователю сообщаем после установки соединения.
        client_host = ws.client.host if ws.client else None
        if not is_secure_transport(ws.url.scheme, client_host):
            await ws.accept()
            await ws.close(code=CLOSE_INSECURE)
            return
        device = device_getter(iid)
        if device is None:
            await ws.accept()
            await ws.close(code=CLOSE_NOT_FOUND)
            return

        loop = asyncio.get_running_loop()
        output: asyncio.Queue[bytes | None] = asyncio.Queue()
        session = TerminalSession(
            device,
            loop,
            output,
            _dimension(_int(ws.query_params.get("cols")), 80),
            _dimension(_int(ws.query_params.get("rows")), 24),
        )
        with lock:
            allowed = not stopping.is_set() and len(sessions) < MAX_SESSIONS
            if allowed:
                sessions.add(session)
        await ws.accept()
        if not allowed:
            await ws.close(code=CLOSE_LIMIT)
            return

        logger.info(f"Web terminal: {owner} открыл сеанс на {device.host}")
        try:
            pool.submit(session.run, metadata={"type": "web_terminal", "owner": owner})
            sender = asyncio.create_task(_send_output(ws, output, session))
            receiver = asyncio.create_task(_receive_input(ws, session))
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.exception()
        except RuntimeError:
            # Пул уже остановлен: сеанс не запускался.
            logger.debug("Web terminal: пул остановлен")
        finally:
            session.close()
            with lock:
                sessions.discard(session)
            logger.info(f"Web terminal: {owner} закрыл сеанс на {device.host}")
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass

    return shutdown


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


async def _send_output(
    ws: WebSocket, output: asyncio.Queue[bytes | None], session: TerminalSession
) -> None:
    """Отправлять вывод бинарными кадрами: UTF-8 декодирует xterm.js."""
    while True:
        item = await output.get()
        if item is None:
            break
        await ws.send_bytes(item)
        session.released()
    message: dict[str, str] = {"type": "closed"}
    if session.error:
        message["error"] = session.error
    await ws.send_text(json.dumps(message, ensure_ascii=False))


async def _receive_input(ws: WebSocket, session: TerminalSession) -> None:
    """Принимать ввод и размер; ожидание записи притормаживает только этот сеанс."""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data:
            await asyncio.to_thread(session.send, data)
            continue
        text = message.get("text")
        if not text:
            continue
        try:
            control = json.loads(text)
        except ValueError:
            continue
        if isinstance(control, dict) and control.get("type") == "resize":
            cols = _dimension(control.get("cols"), 0)
            rows = _dimension(control.get("rows"), 0)
            if cols and rows:
                await asyncio.to_thread(session.resize, cols, rows)
