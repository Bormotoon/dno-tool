"""Bounded, read-only RFB 3.3/3.7/3.8 client with resumable update parsing.

Only Raw, standard Zlib and DesktopSize are advertised. Classic VNC DES
authentication uses the first eight *UTF-8 bytes*, not eight characters. This
is not an encrypted transport; use it only on a trusted/protected network.
"""

from __future__ import annotations

import logging
import socket
import struct
import time
import zlib
from collections.abc import Callable, Generator
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, modes

try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
except ImportError:
    # Keep existing installations with cryptography versions before 43 working.
    from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES

from src.utils.network.dns_resolver import (
    Address,
    DnsBusyError,
    DnsResolver,
    DnsTimeoutError,
)

logger = logging.getLogger(__name__)

MAX_PIXELS = 16_000_000
MAX_FRAME_BYTES = 64_000_000
MAX_TEXT_BYTES = 1_048_576
MAX_RECTANGLES = 16_384
PIXEL_FORMAT = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)


@dataclass(frozen=True)
class RfbLimits:
    """Immutable per-session decode policy derived from the allowed screen area.

    The framebuffer budget is exactly ``4 * max_screen_pixels`` bytes, and the
    decoded/wire update budgets follow the same documented policy: at the
    default 16,000,000 pixels every threshold reproduces the historical fixed
    64,000,000-byte guards. Overlapping rectangles can still exceed an update
    budget, so the cumulative checks are never replaced by a screen check.
    """

    max_screen_pixels: int = MAX_PIXELS

    @property
    def max_frame_bytes(self) -> int:
        return 4 * self.max_screen_pixels

    @property
    def max_update_bytes(self) -> int:
        """Cumulative decoded rectangle bytes allowed in one update."""
        return 4 * self.max_screen_pixels

    @property
    def max_wire_update_bytes(self) -> int:
        """Cumulative payload bytes allowed in one update, including Zlib."""
        return 4 * self.max_screen_pixels


class _Resolver(DnsResolver):
    """Сообщать об ошибках очереди DNS кодами RfbError."""

    def resolve(self, host: str, port: int, deadline: float) -> list[Address]:
        try:
            return super().resolve(host, port, deadline)
        except DnsBusyError as error:
            raise RfbError("dns_busy", "busy") from error
        except DnsTimeoutError as error:
            raise RfbError("dns_timeout", "timeout") from error


_RESOLVER = _Resolver("thumbnail-dns")


class RfbError(Exception):
    """Expose only a fixed, non-secret diagnostic code and result status."""

    def __init__(self, code: str, status: str = "protocol_error") -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def vnc_response(challenge: bytes, password: str) -> bytes:
    """Answer a classic VNC challenge using first-eight-byte UTF-8 semantics."""
    if len(challenge) != 16:
        raise RfbError("challenge_length")
    encoded = password.encode("utf-8")[:8].ljust(8, b"\x00")
    key = bytes(int(f"{value:08b}"[::-1], 2) for value in encoded)
    encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    return encryptor.update(challenge) + encryptor.finalize()


class RfbSession:
    """Keep one framebuffer and at most one outstanding update request.

    Calls must be serialized by the backend. A poll deadline retains its
    generator, partial field and outstanding request, including during a
    fragmented compressed rectangle. Handshake timeouts close the session.
    """

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        *,
        reserve: Callable[[int], None] | None = None,
        read_timeout: float = 5.0,
        limits: RfbLimits | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._password = password
        self._reserve = reserve
        self.read_timeout = read_timeout
        self.limits = limits if limits is not None else RfbLimits()
        self.sock: socket.socket | None = None
        self.width = 0
        self.height = 0
        self.framebuffer = bytearray()
        self._coverage = bytearray()
        self._covered = 0
        self.complete = False
        self.pending = False
        self.frame_version = 0
        self.frame_time: float | None = None
        self.bytes_received = 0
        self.bytes_sent = 0
        self.encoding_counts: dict[int, int] = {}
        self.protocol_version = 0
        self.security_type = 0
        self._allow_veyon_messages = False
        self._decompressor = zlib.decompressobj()
        self._buffer = bytearray()
        self._parser: Generator[int, bytes, None] | None = None
        self._needed = 0

    @property
    def memory_bytes(self) -> int:
        """Return persistent memory, including conservative zlib/parser buffers."""
        return len(self.framebuffer) + len(self._coverage) + len(self._buffer) + 262_144

    def _remaining(self, deadline: float, cap: float | None = None) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RfbError("deadline", "timeout")
        return min(remaining, cap or self.read_timeout)

    def _send(self, data: bytes, deadline: float) -> None:
        if self.sock is None:
            raise RfbError("connection_closed", "unavailable")
        self.sock.settimeout(self._remaining(deadline))
        self.sock.sendall(data)
        self.bytes_sent += len(data)

    def _read(self, size: int, deadline: float) -> bytes:
        while len(self._buffer) < size:
            self._receive(size - len(self._buffer), deadline)
        data = bytes(memoryview(self._buffer)[:size])
        del self._buffer[:size]
        return data

    def _receive(self, size: int, deadline: float) -> None:
        if self.sock is None:
            raise RfbError("connection_closed", "unavailable")
        self.sock.settimeout(self._remaining(deadline))
        try:
            data = self.sock.recv(min(size, 65_536))
        except TimeoutError as error:
            raise RfbError("read_timeout", "timeout") from error
        if not data:
            raise RfbError("connection_closed", "unavailable")
        self.bytes_received += len(data)
        self._buffer.extend(data)

    def _text(self, deadline: float) -> None:
        size = struct.unpack(">I", self._read(4, deadline))[0]
        if size > MAX_TEXT_BYTES:
            raise RfbError("text_length")
        self._discard(size, deadline)

    def _discard(self, size: int, deadline: float) -> None:
        """Consume ignored metadata in small chunks, never retaining its contents."""
        while size:
            chunk = min(size, 65_536)
            self._read(chunk, deadline)
            size -= chunk

    def _configure_keepalive(self) -> None:
        """Detect a silently lost network without requesting extra screen updates."""
        if self.sock is None:
            return
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            ioctl = getattr(socket, "SIO_KEEPALIVE_VALS", None)
            if ioctl is not None:
                self.sock.ioctl(ioctl, (1, 5000, 3000))
            for name, value in (
                ("TCP_KEEPIDLE", 5),
                ("TCP_KEEPINTVL", 3),
                ("TCP_KEEPCNT", 3),
            ):
                option = getattr(socket, name, None)
                if option is not None:
                    self.sock.setsockopt(socket.IPPROTO_TCP, option, value)
        except OSError:
            logger.debug("Transport keepalive tuning unsupported; OS defaults retained")

    def connect(self, deadline: float) -> None:
        """Authenticate, initialize, and select the fixed BGRX wire format."""
        try:
            connect_deadline = min(deadline, time.monotonic() + 3.0)
            addresses = _RESOLVER.resolve(self.host, self.port, connect_deadline)
            last_error: OSError | None = None
            for family, kind, protocol, _, endpoint in addresses[:4]:
                if family not in (socket.AF_INET, socket.AF_INET6):
                    continue
                self.sock = socket.socket(family, kind, protocol)
                try:
                    self.sock.settimeout(self._remaining(connect_deadline, 3.0))
                    self.sock.connect(endpoint)
                    break
                except OSError as error:
                    self.sock.close()
                    self.sock = None
                    last_error = error
            if self.sock is None:
                if time.monotonic() >= connect_deadline:
                    raise RfbError("connect_timeout", "timeout")
                if last_error is not None:
                    raise last_error
                raise RfbError("endpoint_address", "unavailable")
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._configure_keepalive()
            banner = self._read(12, deadline)
            if (
                banner[:8] != b"RFB 003."
                or banner[-1:] != b"\n"
                or not banner[8:11].isdigit()
            ):
                raise RfbError("protocol_version", "unsupported")
            minor = int(banner[8:11])
            if minor < 3:
                raise RfbError("protocol_version", "unsupported")
            self.protocol_version = 8 if minor >= 8 else 7 if minor >= 7 else 3
            self._send(f"RFB 003.00{self.protocol_version}\n".encode(), deadline)
            if self.protocol_version == 3:
                self.security_type = struct.unpack(">I", self._read(4, deadline))[0]
                if self.security_type == 0:
                    self._text(deadline)
                    raise RfbError("security_rejected", "unavailable")
            else:
                count = self._read(1, deadline)[0]
                if not count:
                    self._text(deadline)
                    raise RfbError("security_rejected", "unavailable")
                offered = self._read(count, deadline)
                self.security_type = (
                    2 if 2 in offered and self._password else 1 if 1 in offered else 2
                )
                if self.security_type not in offered:
                    raise RfbError("security_type", "unsupported")
                self._send(bytes([self.security_type]), deadline)
            if self.security_type not in (1, 2):
                raise RfbError("security_type", "unsupported")
            if self.security_type == 2:
                if (
                    len(self._password.encode("utf-8")) > 8
                    or not self._password.isascii()
                ):
                    logger.warning(
                        "Classic VNC authentication uses only the first eight UTF-8 "
                        "password bytes; non-ASCII interoperability is server-specific"
                    )
                challenge = self._read(16, deadline)
                self._send(vnc_response(challenge, self._password), deadline)
            if self.security_type == 2 or self.protocol_version == 8:
                result = struct.unpack(">I", self._read(4, deadline))[0]
                if result:
                    # The reason is untrusted and not necessary for diagnosis.
                    raise RfbError("authentication_rejected", "auth_failed")
            self._send(b"\x01", deadline)  # shared, never displace interactive clients
            init = self._read(24, deadline)
            width, height = struct.unpack(">HH", init[:4])
            name_size = struct.unpack(">I", init[20:])[0]
            if name_size > MAX_TEXT_BYTES:
                raise RfbError("name_length")
            self._resize(width, height)
            self._discard(name_size, deadline)
            self._select_pixel_format(deadline)
        except (RfbError, OSError):
            self.close()
            raise

    def adopt_authenticated_socket(
        self, sock: socket.socket, server_init: bytes, deadline: float
    ) -> None:
        """Принять владение сокетом после внешней аутентификации Veyon.

        ServerInit уже прочитан внешним handshake. Настройка формата и декодер
        сохраняют те же ограничения, что и обычное read-only подключение.
        При ошибке принятый сокет закрывается.
        """
        if self.sock is not None:
            raise RfbError("session_already_connected")
        self.sock = sock
        self._allow_veyon_messages = True
        try:
            if len(server_init) < 24:
                raise RfbError("server_init_length")
            name_size = struct.unpack(">I", server_init[20:24])[0]
            if name_size > MAX_TEXT_BYTES or len(server_init) != 24 + name_size:
                raise RfbError("name_length")
            self._resize(*struct.unpack(">HH", server_init[:4]))
            self._select_pixel_format(deadline)
        except (RfbError, OSError):
            self.close()
            raise

    def _select_pixel_format(self, deadline: float) -> None:
        """Запросить только поддерживаемые форматы без ввода и clipboard."""
        self._send(b"\x00\x00\x00\x00" + PIXEL_FORMAT, deadline)
        self._send(struct.pack(">BBHiii", 2, 0, 3, 6, 0, -223), deadline)

    def _resize(self, width: int, height: int) -> None:
        pixels = width * height
        if not width or not height or pixels > self.limits.max_screen_pixels:
            raise RfbError("framebuffer_size")
        if self._reserve is not None:
            # Frame, coverage, compressed/raw copies, receive and zlib workspace.
            self._reserve(max(13 * pixels + 262_144, self.memory_bytes))
        # Drop old allocations before allocating a resized framebuffer.
        self.framebuffer = bytearray()
        self._coverage = bytearray()
        self.width, self.height = width, height
        self.framebuffer = bytearray(4 * pixels)
        self._coverage = bytearray(pixels)
        self._covered = 0
        self.complete = False
        self.frame_time = None
        self.frame_version += 1

    def _write_rectangle(self, x: int, y: int, w: int, h: int, data: bytes) -> None:
        for row in range(h):
            offset = (y + row) * self.width + x
            self.framebuffer[offset * 4 : (offset + w) * 4] = data[
                row * w * 4 : (row + 1) * w * 4
            ]
            if self._coverage:
                self._covered += self._coverage[offset : offset + w].count(0)
                self._coverage[offset : offset + w] = b"\x01" * w
        self.frame_version += 1

    def _messages(self) -> Generator[int, bytes, None]:
        """Suspend only at field boundaries; never lose a partial TCP message."""
        while True:
            message_type = (yield 1)[0]
            if message_type == 41 and self._allow_veyon_messages:
                # FeatureMessage Veyon: длина VariantArrayMessage и тело.
                # Для проверки кадра содержимое не нужно; ответ не отправляется.
                length = struct.unpack(">I", (yield 4))[0]
                if length > MAX_TEXT_BYTES:
                    raise RfbError("veyon_feature_length")
                while length:
                    chunk = min(length, 65_536)
                    yield chunk
                    length -= chunk
                continue
            if message_type == 2:  # Bell has no payload.
                continue
            if message_type == 3:  # Ignore bounded ServerCutText, never reply.
                header = yield 7
                length = struct.unpack(">I", header[3:])[0]
                if length > MAX_TEXT_BYTES:
                    raise RfbError("clipboard_length")
                while length:
                    chunk = min(length, 65_536)
                    yield chunk
                    length -= chunk
                continue
            if message_type != 0:
                raise RfbError("unsupported_message", "unsupported")
            header = yield 3
            count = struct.unpack(">H", header[1:])[0]
            if count > MAX_RECTANGLES:
                raise RfbError("rectangle_count")
            decoded_bytes = 0
            wire_bytes = 0
            for index in range(count):
                rect = yield 12
                x, y, w, h, encoding = struct.unpack(">HHHHi", rect)
                if encoding == -223:
                    if x or y or index != count - 1:
                        raise RfbError("desktop_size")
                    self._resize(w, h)
                    continue
                if not w or not h or x + w > self.width or y + h > self.height:
                    raise RfbError("rectangle_bounds")
                expected = w * h * 4
                decoded_bytes += expected
                if decoded_bytes > self.limits.max_update_bytes:
                    raise RfbError("update_size")
                if encoding == 0:
                    wire_bytes += expected
                    if wire_bytes > self.limits.max_wire_update_bytes:
                        raise RfbError("update_size")
                    data = yield expected
                elif encoding == 6:
                    length = struct.unpack(">I", (yield 4))[0]
                    wire_bytes += length
                    if (
                        not length
                        or length
                        > min(self.limits.max_wire_update_bytes, expected + 65_536)
                        or wire_bytes > self.limits.max_wire_update_bytes
                    ):
                        raise RfbError("zlib_length")
                    compressed = yield length
                    try:
                        data = self._decompressor.decompress(compressed, expected + 1)
                    except zlib.error as error:
                        raise RfbError("zlib_stream") from error
                    if (
                        len(data) != expected
                        or self._decompressor.unconsumed_tail
                        or self._decompressor.unused_data
                        or self._decompressor.eof
                    ):
                        raise RfbError("zlib_bounds")
                    # Standard Zlib rectangles end at a Z_SYNC_FLUSH boundary.
                    # Exact pixel output alone does not prove a complete payload.
                    if not compressed.endswith(b"\x00\x00\xff\xff"):
                        raise RfbError("zlib_stream")
                    del compressed
                else:
                    raise RfbError("unsupported_encoding", "unsupported")
                self._write_rectangle(x, y, w, h, data)
                del data
                self.encoding_counts[encoding] = (
                    self.encoding_counts.get(encoding, 0) + 1
                )
            if self._coverage and self._covered == self.width * self.height:
                self._coverage = bytearray()
                self.complete = True
            if self.complete:
                self.frame_time = time.monotonic()
            self.pending = False
            return

    def poll(self, deadline: float) -> bool:
        """Process one update; a quiet/fragmented screen returns False, not EOF."""
        if not self.pending:
            self._send(
                struct.pack(
                    ">BBHHHH", 3, int(self.complete), 0, 0, self.width, self.height
                ),
                deadline,
            )
            self.pending = True
            self._parser = self._messages()
            self._needed = next(self._parser)
        if self._parser is None:
            raise RfbError("parser_state")
        try:
            while True:
                data = self._read(self._needed, deadline)
                try:
                    self._needed = self._parser.send(data)
                    del data
                except StopIteration:
                    self._parser = None
                    return True
        except RfbError as error:
            if error.status == "timeout":
                return False
            raise

    def interrupt(self) -> None:
        """Прервать блокирующую операцию из другого потока при выходе из программы.

        Нужны оба вызова: на Linux поток будит shutdown, на Windows только close.
        Ошибку получит владелец операции, он же освободит буферы.
        """
        sock = self.sock
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def close(self) -> None:
        """Release the socket and parser without emitting protocol input."""
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if self._parser is not None:
            self._parser.close()
            self._parser = None
        self._buffer.clear()
        self.framebuffer.clear()
        self._coverage.clear()
        self.complete = False
        self.pending = False
        self._allow_veyon_messages = False
