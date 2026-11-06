"""Независимые от интерфейса состояния и отмена диагностики."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class CheckState(str, Enum):
    PENDING = "ожидание"
    RUNNING = "выполняется"
    SUCCESS = "успешно"
    WARNING = "предупреждение"
    ERROR = "ошибка"
    UNAVAILABLE = "не удалось проверить"
    SKIPPED = "пропущено"
    CANCELLED = "отменено"


@dataclass(frozen=True)
class CheckResult:
    run_id: str
    device: object
    key: str
    state: CheckState
    icon: str
    text: str
    detail: str
    duration: float
    timestamp: float


CHECKS = {
    "ssh": ("ssh.ping", "ssh.port", "ssh.auth", "ssh.echo", "ssh.sudo"),
    "veyon": (
        "veyon.server_port",
        "veyon.service",
        "veyon.autostart",
        "veyon.handshake",
    ),
    "vnc": (
        "vnc.backend",
        "vnc.port",
        "vnc.rfb",
        "vnc.activity",
        "vnc.autostart",
        "vnc.auth",
    ),
}


class OperationControl:
    """Закрывает только ресурсы своего запуска при отмене или дедлайне."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.RLock()
        self._closers: set[Callable[[], None]] = set()

    def add(self, closer: Callable[[], None]) -> None:
        with self._lock:
            if self.cancelled.is_set():
                closer()
                raise InterruptedError("Отменено")
            self._closers.add(closer)

    def remove(self, closer: Callable[[], None]) -> None:
        with self._lock:
            self._closers.discard(closer)

    def abort(self) -> None:
        with self._lock:
            self.cancelled.set()
            closers = tuple(self._closers)
        for closer in closers:
            try:
                closer()
            except Exception:
                pass


class Exchange:
    """Общий срок обмена и прерываемое чтение фрагментов протокола."""

    def __init__(
        self, sock: object, timeout: float, control: OperationControl | None
    ) -> None:
        self.sock = sock
        self.deadline = time.monotonic() + timeout
        self.control = control
        if control:
            control.add(self.close)

    def close(self) -> None:
        import socket

        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        self.sock.close()

    def finish(self) -> None:
        if self.control:
            self.control.remove(self.close)
        self.close()

    def read(self, size: int) -> bytes:
        import socket

        data = bytearray()
        while len(data) < size:
            if self.control and self.control.cancelled.is_set():
                raise InterruptedError("Отменено")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Истёк общий срок RFB-обмена")
            self.sock.settimeout(min(0.2, remaining))
            try:
                chunk = self.sock.recv(size - len(data))
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("Сервер закрыл неполный RFB-ответ")
            data.extend(chunk)
        return bytes(data)
