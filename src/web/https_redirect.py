"""
Перенаправление HTTP на HTTPS на том же порту веб-сервера.

Пока HTTPS включён, порт понимает только TLS, и адрес http:// давал обрыв соединения.
Слушатель сам принимает соединения и по первому байту отличает TLS (запись handshake
0x16) от обычного HTTP. TLS-соединение передаётся протоколу uvicorn через
connect_accepted_socket, на HTTP отвечает 307 с тем же адресом по https://.

Первый байт читается с MSG_PEEK в отдельном потоке на selectors: у ProactorEventLoop
на Windows нет ожидания готовности сокета без чтения, а прочитанный байт TLS уже
нельзя вернуть в сокет.
"""

from __future__ import annotations

import asyncio
import os
import re
import selectors
import socket
import threading
import time
from typing import Callable, Optional

from src.logger import logger

# Первый байт записи TLS handshake (ClientHello).
TLS_HANDSHAKE_BYTE = 0x16
# Сколько ждать первых данных и заголовков HTTP-запроса.
FIRST_DATA_TIMEOUT_SEC = 10.0
# Соединений без первых данных одновременно; select на Windows держит до 512 сокетов.
MAX_PENDING = 256
MAX_REQUEST_HEAD = 8192

_HOST = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::(\d{1,5}))?")
_TARGET = re.compile(r"/[\x21-\x7e]*")


def bind_sockets(host: str | None, port: int, backlog: int) -> list[socket.socket]:
    """Открыть слушающие сокеты так же, как loop.create_server(host, port)."""
    infos = socket.getaddrinfo(
        host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    )
    sockets: list[socket.socket] = []
    seen = set()
    try:
        for family, kind, proto, _name, address in infos:
            if (family, address) in seen:
                continue
            seen.add((family, address))
            sock = socket.socket(family, kind, proto)
            sockets.append(sock)
            # Как в asyncio: на Windows SO_REUSEADDR позволил бы занять чужой порт.
            if os.name == "posix":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6 and hasattr(socket, "IPPROTO_IPV6"):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, True)
            sock.bind(address)
            sock.listen(backlog)
            sock.setblocking(False)
    except BaseException:
        for sock in sockets:
            sock.close()
        raise
    return sockets


def build_redirect(head: bytes, local_host: str) -> bytes:
    """Ответ на заголовок HTTP-запроса: 307 на https:// или 400 для мусора."""
    lines = head.split(b"\r\n")
    request = lines[0].split(b" ")
    if len(request) != 3 or not request[2].startswith(b"HTTP/"):
        return _response(400, "Bad Request")
    method = request[0]
    target = request[1].decode("latin-1")
    if not _TARGET.fullmatch(target):
        target = "/"
    host = ""
    for line in lines[1:]:
        if not line:
            break
        name, sep, value = line.partition(b":")
        if sep and name.strip().lower() == b"host":
            host = value.strip().decode("latin-1")
            break
    match = _HOST.fullmatch(host)
    if match is None:
        if host:
            return _response(400, "Bad Request")
        # HTTP/1.0 без Host: адрес, на который пришло соединение.
        name, port = local_host, None
    else:
        name, port = match.group(1), match.group(2)
    # Без порта клиент шёл на 80: TLS слушает тот же порт, что принял запрос.
    location = f"https://{name}:{port or 80}{target}"
    return _response(
        307, "Temporary Redirect", location, with_body=method != b"HEAD"
    )


def _response(
    status: int, reason: str, location: str = "", with_body: bool = True
) -> bytes:
    body = f"{location}\n".encode("ascii") if location and with_body else b""
    headers = [f"HTTP/1.1 {status} {reason}"]
    if location:
        headers.append(f"Location: {location}")
    headers += [
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        # Временное перенаправление: после выключения HTTPS браузер не уйдёт на https://.
        "Cache-Control: no-store",
        "Connection: close",
    ]
    return ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body


def _local_host(sock: socket.socket) -> str:
    try:
        address = sock.getsockname()[0]
    except OSError:
        return "localhost"
    if address in ("0.0.0.0", "::", ""):
        return "localhost"
    return f"[{address}]" if ":" in address else address


class _RedirectProtocol(asyncio.Protocol):
    """Прочитать заголовок HTTP-запроса, ответить перенаправлением и закрыть."""

    def __init__(self, local_host: str) -> None:
        self._local_host = local_host
        self._buffer = bytearray()
        self._transport: Optional[asyncio.Transport] = None
        self._timer: Optional[asyncio.TimerHandle] = None
        self._answered = False

    def connection_made(self, transport) -> None:
        self._transport = transport
        loop = asyncio.get_running_loop()
        self._timer = loop.call_later(FIRST_DATA_TIMEOUT_SEC, transport.abort)

    def data_received(self, data: bytes) -> None:
        if self._answered:
            return
        self._buffer += data
        end = self._buffer.find(b"\r\n\r\n")
        if end >= 0:
            self._answer(build_redirect(bytes(self._buffer[:end]), self._local_host))
        elif len(self._buffer) > MAX_REQUEST_HEAD:
            self._answer(_response(431, "Request Header Fields Too Large"))

    def eof_received(self) -> bool:
        if not self._answered:
            self._answer(_response(400, "Bad Request"))
        return False

    def connection_lost(self, exc) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def _answer(self, response: bytes) -> None:
        self._answered = True
        self._transport.write(response)
        self._transport.close()


class _FirstByteWatcher(threading.Thread):
    """Ждать первые данные принятых сокетов и сообщать, TLS это или HTTP."""

    def __init__(self, on_result: Callable[[socket.socket, Optional[str]], None]) -> None:
        super().__init__(name="dno-tool-web-sniffer", daemon=True)
        self._on_result = on_result
        self._selector = selectors.DefaultSelector()
        self._wake_read, self._wake_write = socket.socketpair()
        self._wake_read.setblocking(False)
        self._wake_write.setblocking(False)
        self._selector.register(self._wake_read, selectors.EVENT_READ)
        self._lock = threading.Lock()
        self._incoming: list[socket.socket] = []
        self._count = 0
        self._closed = False
        # Сокет -> срок ожидания; меняется только в этом потоке.
        self._deadlines: dict[socket.socket, float] = {}

    def submit(self, sock: socket.socket) -> bool:
        with self._lock:
            if self._closed or self._count >= MAX_PENDING:
                return False
            self._count += 1
            self._incoming.append(sock)
        self._wake()
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._wake()

    def _wake(self) -> None:
        try:
            self._wake_write.send(b"\0")
        except OSError:
            pass  # буфер уже полон или поток завершён: он и так проснётся

    def run(self) -> None:
        try:
            self._loop()
        except Exception as error:  # noqa: BLE001 - поток не должен падать молча
            logger.error(f"WebServer: ошибка ожидания первых данных: {error}")
        finally:
            for sock in list(self._deadlines):
                sock.close()
            with self._lock:
                self._closed = True
                for sock in self._incoming:
                    sock.close()
                self._incoming.clear()
            self._selector.close()
            self._wake_read.close()
            self._wake_write.close()

    def _loop(self) -> None:
        while True:
            with self._lock:
                closed = self._closed
                incoming, self._incoming = self._incoming, []
            if closed:
                for sock in incoming:
                    sock.close()
                return
            now = time.monotonic()
            for sock in incoming:
                self._deadlines[sock] = now + FIRST_DATA_TIMEOUT_SEC
                self._selector.register(sock, selectors.EVENT_READ)
            for sock, deadline in list(self._deadlines.items()):
                if deadline <= now:
                    self._finish(sock, None)
            timeout = None
            if self._deadlines:
                timeout = max(0.0, min(self._deadlines.values()) - now)
            for key, _events in self._selector.select(timeout):
                if key.fileobj is self._wake_read:
                    try:
                        while self._wake_read.recv(4096):
                            pass
                    except OSError:
                        pass
                    continue
                self._peek(key.fileobj)

    def _peek(self, sock: socket.socket) -> None:
        try:
            first = sock.recv(1, socket.MSG_PEEK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            first = b""
        if not first:
            self._finish(sock, None)
        elif first[0] == TLS_HANDSHAKE_BYTE:
            self._finish(sock, "tls")
        else:
            self._finish(sock, "http")

    def _finish(self, sock: socket.socket, kind: Optional[str]) -> None:
        self._selector.unregister(sock)
        del self._deadlines[sock]
        with self._lock:
            self._count -= 1
        if kind is None:
            sock.close()
            return
        self._on_result(sock, kind)


class HttpsRedirectListener:
    """Слушатель для uvicorn.Server.servers: TLS к uvicorn, HTTP на https://."""

    def __init__(
        self,
        sockets: list[socket.socket],
        protocol_factory: Callable[[], asyncio.Protocol],
        ssl_context,
    ) -> None:
        self._sockets = sockets
        self._factory = protocol_factory
        self._ssl = ssl_context
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._watcher = _FirstByteWatcher(self._deliver)
        self._accept_tasks: list[asyncio.Task] = []
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    @property
    def sockets(self) -> list[socket.socket]:
        return list(self._sockets)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._watcher.start()
        for sock in self._sockets:
            self._accept_tasks.append(loop.create_task(self._accept(sock)))

    async def _accept(self, listener: socket.socket) -> None:
        while not self._closing:
            try:
                conn, _address = await self._loop.sock_accept(listener)
            except asyncio.CancelledError:
                raise
            except OSError as error:
                if self._closing:
                    return
                # Например, исчерпаны дескрипторы: не крутить цикл вхолостую.
                logger.debug(f"WebServer: ошибка приёма соединения: {error}")
                await asyncio.sleep(0.1)
                continue
            conn.setblocking(False)
            if not self._watcher.submit(conn):
                conn.close()

    def _deliver(self, conn: socket.socket, kind: str) -> None:
        # Вызывается из потока ожидания.
        try:
            self._loop.call_soon_threadsafe(self._dispatch, conn, kind)
        except RuntimeError:
            conn.close()

    def _dispatch(self, conn: socket.socket, kind: str) -> None:
        if self._closing:
            conn.close()
            return
        task = self._loop.create_task(self._serve(conn, kind))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(self, conn: socket.socket, kind: str) -> None:
        try:
            if kind == "tls":
                await self._loop.connect_accepted_socket(
                    self._factory, conn, ssl=self._ssl
                )
            else:
                local_host = _local_host(conn)
                await self._loop.connect_accepted_socket(
                    lambda: _RedirectProtocol(local_host), conn
                )
        except asyncio.CancelledError:
            conn.close()
            raise
        except Exception as error:  # noqa: BLE001 - ошибка одного клиента
            logger.debug(f"WebServer: соединение не установлено: {type(error).__name__}")
            conn.close()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for task in self._accept_tasks:
            task.cancel()
        for task in list(self._tasks):
            task.cancel()
        for sock in self._sockets:
            sock.close()
        self._watcher.close()

    async def wait_closed(self) -> None:
        pending = [*self._accept_tasks, *self._tasks]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        deadline = time.monotonic() + 1.0
        while self._watcher.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
