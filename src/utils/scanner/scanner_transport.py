"""TCP-пробы с ограничением времени и явной классификацией результата."""

import errno
import socket
import time

from src.utils.scanner.scanner_types import ProbeResult, ProbeState


def _connect_state(code: int | None) -> ProbeState:
    """Отделяет сетевые ответы connect от локальных ошибок сокета."""
    if code == 0:
        return "open"
    if code in (errno.ECONNREFUSED, errno.ECONNRESET, 10061, 10054):
        return "closed"
    if code in (errno.ETIMEDOUT, errno.EAGAIN, errno.EWOULDBLOCK, 10060, 10035):
        return "timeout"
    # Linux возвращает ENOPROTOOPT при ICMP Protocol Unreachable.
    if code in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENETDOWN,
                errno.ENOPROTOOPT, 10065, 10051, 10050, 10042):
        return "unreachable"
    return "error"


def tcp_probe(host: str, port: int, timeout: float) -> ProbeResult:
    started = time.monotonic()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            try:
                code = sock.connect_ex((host, port))
                diagnostic = f"connect_errno={code}" if code else ""
            except TimeoutError:
                raise
            except OSError as exc:
                code = getattr(exc, "winerror", None) or exc.errno
                diagnostic = f"socket_errno={code}"
            state = _connect_state(code)
    except TimeoutError:
        state, diagnostic = "timeout", "connect_timeout"
    except OSError as exc:
        state, diagnostic = "error", f"socket_errno={exc.errno}"
    return ProbeResult(host, port, "tcp", state, time.monotonic() - started, diagnostic)
