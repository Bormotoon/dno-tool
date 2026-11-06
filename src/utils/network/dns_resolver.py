"""Ограниченное число неотменяемых вызовов DNS операционной системы."""

from __future__ import annotations

import ipaddress
import queue
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

Address = tuple[socket.AddressFamily, socket.SocketKind, int, str, Any]
Resolution = tuple[list[Address] | None, OSError | None]
Reply = Callable[[list[Address] | None, OSError | None], None]


class DnsBusyError(OSError):
    """Очередь разрешения имён заполнена."""


class DnsTimeoutError(OSError):
    """Срок разрешения имени истёк."""


def numeric_endpoint(host: str, port: int) -> list[Address] | None:
    """Вернуть адрес для IP-литерала; для имени нужен DNS, результат None."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv4Address):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, port))]
    scope = address.scope_id
    scope_id = (
        int(scope)
        if scope and scope.isdigit()
        else (socket.if_nametoindex(scope) if scope else 0)
    )
    endpoint = (str(address).split("%", 1)[0], port, 0, scope_id)
    return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", endpoint)]


class DnsResolver:
    """Bound non-cancellable OS DNS calls to lazy daemon workers and a finite queue.

    Numeric endpoints bypass DNS. An expired queued job is skipped, and a hung
    OS resolver cannot create unbounded workers or extend a capture deadline.
    Daemon workers do not hold process shutdown waiting for an OS DNS call.
    """

    def __init__(self, name: str, workers: int = 2, queue_size: int = 32) -> None:
        self._name = name
        self._workers = workers
        self._queue: queue.Queue[tuple[str, int, float, Reply]] = queue.Queue(
            maxsize=queue_size
        )
        self._lock = threading.Lock()
        self._started = False

    def _worker(self) -> None:
        while True:
            host, port, deadline, reply = self._queue.get()
            try:
                if time.monotonic() >= deadline:
                    reply(None, DnsTimeoutError("DNS deadline expired"))
                    continue
                try:
                    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                except OSError as error:
                    reply(None, error)
                else:
                    reply(addresses, None)
            finally:
                self._queue.task_done()

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._started:
                return
            for index in range(self._workers):
                threading.Thread(
                    target=self._worker, name=f"{self._name}-{index}", daemon=True
                ).start()
            self._started = True

    def resolve_async(
        self, host: str, port: int, deadline: float, reply: Reply
    ) -> None:
        """Разрешить имя в рабочем потоке; reply выполняется в том же потоке.

        Переполненная очередь сообщается исключением DnsBusyError.
        """
        self._ensure_workers()
        try:
            self._queue.put_nowait((host, port, deadline, reply))
        except queue.Full as error:
            raise DnsBusyError("DNS queue is full") from error

    def resolve(self, host: str, port: int, deadline: float) -> list[Address]:
        numeric = numeric_endpoint(host, port)
        if numeric is not None:
            return numeric
        results: queue.Queue[Resolution] = queue.Queue(maxsize=1)
        self.resolve_async(
            host,
            port,
            deadline,
            lambda found, error: results.put_nowait((found, error)),
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DnsTimeoutError("DNS deadline expired")
        try:
            addresses, resolution_error = results.get(timeout=remaining)
        except queue.Empty as error:
            raise DnsTimeoutError("DNS deadline expired") from error
        if resolution_error is not None:
            raise resolution_error
        return addresses or []
