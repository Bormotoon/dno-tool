"""ICMP-пробы сканера: системный ping и публичные сокеты icmplib."""

import os
import platform
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from itertools import count

from src.utils.scanner.scanner_types import ProbeResult, ProbeState

_WAIT_SLICE = 0.05
_request_counter = count(secrets.randbits(32))
_request_lock = threading.Lock()
_active_ids: set[int] = set()


@dataclass(frozen=True)
class IcmpAvailability:
    """Доступность Python-пинга и выбранный режим сокета на текущий запуск."""

    available: bool
    reason: str = ""
    privileged: bool = True


def check_icmplib() -> IcmpAvailability:
    """Проверить импорт и создание сокета, сразу освобождая его."""
    try:
        library = import_module("icmplib")
    except (ImportError, OSError) as exc:
        return IcmpAvailability(False, f"Библиотека icmplib недоступна: {exc}")

    reason = ""
    modes = (True,) if platform.system() == "Windows" else (False, True)
    for privileged in modes:
        try:
            with library.ICMPv4Socket(privileged=privileged):
                return IcmpAvailability(True, privileged=privileged)
        except (library.ICMPSocketError, OSError) as exc:
            if isinstance(exc, (library.SocketPermissionError, PermissionError)):
                reason = "Недостаточно прав для создания ICMP-сокета"
            else:
                reason = f"Не удалось создать ICMP-сокет: {exc}"
    return IcmpAvailability(False, reason)


@contextmanager
def _request_identity() -> Iterator[tuple[int, int]]:
    """Выдать уникальный среди активных проб 16-битный ID и номер запроса."""
    with _request_lock:
        while True:
            value = next(_request_counter)
            identifier = value & 0xFFFF
            if identifier not in _active_ids:
                _active_ids.add(identifier)
                break
    try:
        yield identifier, (value >> 16) & 0xFFFF
    finally:
        with _request_lock:
            _active_ids.remove(identifier)


def icmplib_probe(
    host: str,
    timeout_ms: int,
    cancelled: Callable[[], bool],
    *,
    privileged: bool,
) -> ProbeResult:
    """Отправить один Echo Request и ждать только его ответ до общего deadline."""
    started = time.monotonic()

    def result(state: ProbeState, diagnostic: str = "") -> ProbeResult:
        return ProbeResult(
            host, 0, "icmp", state, time.monotonic() - started, diagnostic
        )

    if cancelled():
        return result("cancelled")
    try:
        library = import_module("icmplib")
    except (ImportError, OSError) as exc:
        return result("error", f"Библиотека icmplib недоступна: {exc}")

    try:
        with (
            _request_identity() as (identifier, sequence),
            library.ICMPv4Socket(privileged=privileged) as sock,
        ):
            if cancelled():
                return result("cancelled")
            request = library.ICMPRequest(
                destination=host, id=identifier, sequence=sequence
            )
            deadline = started + timeout_ms / 1000
            sock.send(request)
            while not cancelled():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return result("timeout")
                try:
                    reply = sock.receive(request, timeout=min(_WAIT_SLICE, remaining))
                except library.TimeoutExceeded:
                    continue
                # Linux SOCK_DGRAM меняет ID при send; проверяем обновлённый request.
                if reply.id != request.id or reply.sequence != request.sequence:
                    continue
                if reply.type == 0 and reply.code == 0 and reply.source == host:
                    return result("open")
                if reply.type in (3, 4, 5, 11, 12):
                    return result(
                        "unreachable", f"ICMP type={reply.type}, code={reply.code}"
                    )
            return result("cancelled")
    except library.ICMPError as exc:
        return result("unreachable", str(exc))
    except (library.ICMPSocketError, OSError) as exc:
        return result("error", f"Ошибка ICMP-сокета: {exc}")


def system_probe(
    host: str, timeout_ms: int, cancelled: Callable[[], bool]
) -> ProbeResult:
    """Выполнить один системный ping с TTL, отменой и освобождением процесса."""
    started = time.monotonic()

    def result(
        state: ProbeState, diagnostic: str = "", ttl: int | None = None,
        rtt_ms: float | None = None,
    ) -> ProbeResult:
        return ProbeResult(
            host, 0, "icmp", state, time.monotonic() - started, diagnostic, ttl, rtt_ms
        )

    if cancelled():
        return result("cancelled")
    system = platform.system()
    timeout = timeout_ms / 1000
    if system == "Windows":
        command = ["ping", "-4", "-n", "1", "-w", str(timeout_ms), host]
    elif system == "Darwin":
        command = ["ping", "-n", "-c", "1", "-W", str(timeout_ms), host]
    else:
        command = ["ping", "-4", "-n", "-c", "1", "-W", f"{timeout:g}", host]
    process = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if system == "Windows" else 0,
            env=None if system == 'Windows' else {**os.environ, 'LC_ALL': 'C'},
        )
        deadline = started + timeout + 1
        while True:
            if cancelled():
                return result("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result("timeout")
            try:
                stdout, stderr = process.communicate(
                    timeout=min(_WAIT_SLICE, remaining)
                )
                break
            except subprocess.TimeoutExpired:
                continue
        encoding = "oem" if system == "Windows" and os.name == "nt" else "utf-8"
        output = stdout.decode(encoding, errors="replace")
        # TTL должен принадлежать строке ответа целевого адреса, не заголовку
        # ping и не сообщению шлюза о недоступности (Windows иногда даёт rc=0).
        for line in output.splitlines():
            ttl = re.search(r"\bttl[=:]\s*(\d{1,3})", line, re.IGNORECASE)
            source = re.search(r"(?<![\d.])" + re.escape(host) + r"(?![\d.])", line)
            named_host = re.search(r'[a-zA-Z]', host) is not None
            if process.returncode == 0 and ttl and (source or named_host):
                delay = re.search(
                    r"(?:time|время)\s*[=<]\s*(\d+(?:[.,]\d+)?)\s*(?:ms|мс)",
                    line, re.IGNORECASE,
                )
                return result(
                    "open", ttl=int(ttl.group(1)),
                    rtt_ms=float(delay[1].replace(",", ".")) if delay else None,
                )
        no_reply_codes = (0, 2) if system == "Darwin" else (0, 1)
        local_failure = re.search(
            r"general failure|transmit failed|permission denied|operation not permitted|"
            r"общий сбой|сбой передачи|ошибка передачи|отказано в доступе",
            output,
            re.IGNORECASE,
        )
        if process.returncode not in no_reply_codes or stderr or local_failure:
            diagnostic = (stderr or stdout).decode(encoding, errors="replace").strip()
            return result("error", f"ping: {diagnostic or process.returncode}")
        return result("no_reply")
    except OSError as exc:
        return result("error", f"Не удалось выполнить ping: {exc}")
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate()
