"""
Veyon RFB Bridge — клиентская реализация Veyon-аутентификации для установки
стандартного RFB-соединения с удалённым хостом.

Veyon использует стандартный VNC-сервер (x11vnc на Linux, UltraVNC на Windows)
внутри, но оборачивает его в прокси с proprietary security type 40 и RSA
key-file аутентификацией. Этот модуль реализует Veyon-side handshake, после
которого соединение становится обычным RFB 3.8 и может проксироваться
напрямую в WebSocket для noVNC.

Протокол (Veyon main branch):

  1. RFB 003.008 version exchange (12 bytes each way)
  2. Security types: server sends [0x01, 0x28] (count=1, type=40=Veyon)
     Client replies 0x28.
  3. VariantArrayMessage framing: 4-byte BE uint32 length + QDataStream(Qt_5_5)
     payload of QVariant values.
  4. Server sends auth-type list: [Int count, Int authType...]
     KeyFile = 3.
  5. Client sends [Int chosenAuthType, QString username].
  6. Server sends empty ACK (4 bytes = 0x00000000).
  7. Server sends challenge: [QByteArray 128 random bytes].
  8. Client signs challenge with RSA-4096 PKCS1v15-SHA512, sends
     [QString authKeyName, QByteArray signature].
  9. Server sends SecurityResult: 4 bytes BE uint32 (0 = OK).
 10. Access control (server-side, silent): client sends ClientInit (1 byte),
     then waits for ServerInit (may take seconds while dialog runs).
 11. If ServerInit width/height = 0 — access denied. Otherwise: raw RFB.

QDataStream Qt_5_5 QVariant serialization:
  Each QVariant = [uint32 BE typeId][uint8 isNull][type-specific body]
  - Int (typeId=2): 4 bytes BE qint32
  - QString (typeId=10): uint32 BE byteLength + UTF-16BE chars (0xFFFFFFFF = null)
  - QByteArray (typeId=12): uint32 BE byteLength + raw bytes (0xFFFFFFFF = null)
"""

import socket
import struct
import getpass
import time
import threading
from typing import Optional, Tuple

from src.logger import logger


# QMetaType type IDs used in Veyon QVariant serialization
_QTYPE_INT = 2
_QTYPE_STRING = 10
_QTYPE_BYTEARRAY = 12

# Veyon constants
RFB_SEC_TYPE_VEYON = 40
RFB_VNC_AUTH_OK = 0
AUTH_TYPE_KEYFILE = 3
CHALLENGE_SIZE = 128
MAX_SIGNATURE_SIZE = 8192
MAX_MESSAGE_SIZE = 32 * 1024 * 1024

# RFB client→server message types allowed by Veyon proxy
RFB_MSG_SET_PIXEL_FORMAT = 0
RFB_MSG_SET_ENCODINGS = 2
RFB_MSG_FRAMEBUFFER_UPDATE_REQUEST = 3
RFB_MSG_KEY_EVENT = 4
RFB_MSG_POINTER_EVENT = 5
RFB_MSG_XVP = 250

# Message sizes (fixed) for filtering
_RFB_FIXED_SIZES = {
    RFB_MSG_SET_PIXEL_FORMAT: 4,        # 1 type + 3 padding + 16 format
    RFB_MSG_FRAMEBUFFER_UPDATE_REQUEST: 10,
    RFB_MSG_KEY_EVENT: 8,
    RFB_MSG_POINTER_EVENT: 6,
    RFB_MSG_XVP: 6,
}


class VeyonRFBError(Exception):
    """Base exception for Veyon RFB bridge errors."""


class VeyonAuthError(VeyonRFBError):
    """Authentication failed."""


class VeyonAccessControlError(VeyonRFBError):
    """Access control denied by remote user or rules."""


class _QDataStreamReader:
    """Minimal QDataStream(Qt_5_5) QVariant reader for Veyon handshake."""

    def __init__(self, buf: bytes):
        self._buf = buf
        self._pos = 0

    def _read_u32(self) -> int:
        v = struct.unpack_from(">I", self._buf, self._pos)[0]
        self._pos += 4
        return v

    def _read_u8(self) -> int:
        v = self._buf[self._pos]
        self._pos += 1
        return v

    def read_variant(self):
        """Read one QVariant. Returns int, str, bytes, or None."""
        type_id = self._read_u32()
        is_null = self._read_u8()
        if is_null:
            return None

        if type_id == _QTYPE_INT:
            v = struct.unpack_from(">i", self._buf, self._pos)[0]
            self._pos += 4
            return v

        if type_id == _QTYPE_STRING:
            n = self._read_u32()
            if n == 0xFFFFFFFF:
                return ""
            raw = self._buf[self._pos:self._pos + n]
            self._pos += n
            return raw.decode("utf-16-be")

        if type_id == _QTYPE_BYTEARRAY:
            n = self._read_u32()
            if n == 0xFFFFFFFF:
                return b""
            raw = bytes(self._buf[self._pos:self._pos + n])
            self._pos += n
            return raw

        raise VeyonRFBError(f"unsupported QVariant typeId {type_id} at pos {self._pos}")


def _write_int(v: int) -> bytes:
    return struct.pack(">IBi", _QTYPE_INT, 0, v)


def _write_qstring(s: str) -> bytes:
    enc = s.encode("utf-16-be")
    return struct.pack(">IBI", _QTYPE_STRING, 0, len(enc)) + enc


def _write_qbytearray(b: bytes) -> bytes:
    return struct.pack(">IBI", _QTYPE_BYTEARRAY, 0, len(b)) + b


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes from socket, raising on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise VeyonRFBError(f"connection closed: expected {n} bytes, got {len(buf)}")
        buf.extend(chunk)
    return bytes(buf)


def _recv_variant_array_message(sock: socket.socket) -> bytes:
    """Read a VariantArrayMessage: 4-byte BE length + payload. Returns payload."""
    size_bytes = _recv_exactly(sock, 4)
    size = struct.unpack(">I", size_bytes)[0]
    if size > MAX_MESSAGE_SIZE:
        raise VeyonRFBError(f"message too large: {size} bytes")
    return _recv_exactly(sock, size)


def _send_variant_array_message(sock: socket.socket, payload: bytes) -> None:
    """Send a VariantArrayMessage: 4-byte BE length + payload."""
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def _sign_challenge(private_key, challenge: bytes) -> bytes:
    """Sign challenge with RSA PKCS#1 v1.5 + SHA-512.

    QCA's EMSA3_SHA512 == RSASSA-PKCS1-v1_5 with SHA-512.
    """
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes
    return private_key.sign(challenge, padding.PKCS1v15(), hashes.SHA512())


def _load_private_key(key_data: str):
    """Load RSA private key from PEM string."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    return load_pem_private_key(key_data.encode("utf-8"), password=None)


class VeyonRFBHandshake:
    """
    Performs the Veyon authentication handshake over a TCP socket and
    returns a socket that is in standard RFB running state after success.

    Usage:
        hs = VeyonRFBHandshake(host="10.0.0.5", key_data=pem_string,
                               key_name="teacher", username="admin")
        sock = hs.connect()  # sock is now in RFB running state
        # ... read/write RFB messages on sock ...
        sock.close()
    """

    def __init__(
        self,
        host: str,
        port: int = 11100,
        key_data: str = "",
        key_name: str = "teacher",
        username: str = "",
        connect_timeout: float = 10.0,
        access_control_timeout: float = 60.0,
    ):
        self.host = host
        self.port = port
        self.key_data = key_data
        self.key_name = key_name
        self.username = username or getpass.getuser()
        self.connect_timeout = connect_timeout
        self.access_control_timeout = access_control_timeout
        self._sock: Optional[socket.socket] = None
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()

    def connect(self) -> Tuple[socket.socket, bytes]:
        """Open TCP connection and perform full Veyon handshake.

        Returns:
            (socket, server_init_bytes) — socket is in RFB running state,
            server_init_bytes is the raw rfbServerInit message (24 + nameLen
            bytes) that should be sent to the RFB client (browser) first.

        Raises:
            VeyonAuthError: authentication failed.
            VeyonAccessControlError: access control denied.
            VeyonRFBError: protocol or connection error.
        """
        if self._cancelled.is_set():
            raise VeyonRFBError("Подключение отменено")
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            )
            # Отмена могла прийти, пока connect ещё не передал сокет владельцу.
            if self._cancelled.is_set():
                self._close_quietly()
                raise VeyonRFBError("Подключение отменено")
            self._sock.settimeout(self.connect_timeout)
        except Exception as e:
            raise VeyonRFBError(f"cannot connect to {self.host}:{self.port}: {e}")

        try:
            self._do_rfb_version_exchange()
            self._do_security_type_negotiation()
            self._do_auth_type_negotiation()
            challenge = self._receive_challenge()
            signature = self._sign_challenge(challenge)
            self._send_signature(signature)
            self._check_security_result()
            server_init = self._wait_for_server_init()

            logger.info(
                f"VeyonRFB: handshake completed for {self.host} "
                f"(desktop: {server_init[2]!r}, {server_init[0]}x{server_init[1]})"
            )

            self._sock.settimeout(None)
            return self._sock, server_init[3]
        except (VeyonAuthError, VeyonAccessControlError, VeyonRFBError):
            self._close_quietly()
            raise
        except Exception as e:
            self._close_quietly()
            raise VeyonRFBError(f"handshake failed for {self.host}: {e}")

    def _do_rfb_version_exchange(self) -> None:
        """Step 1: RFB 003.008 version exchange."""
        server_version = _recv_exactly(self._sock, 12)
        if not server_version.startswith(b"RFB "):
            raise VeyonRFBError(f"invalid RFB version: {server_version!r}")
        self._sock.sendall(b"RFB 003.008\n")

    def _do_security_type_negotiation(self) -> None:
        """Step 2: Security type negotiation — server offers [count=1, type=40]."""
        count = _recv_exactly(self._sock, 1)[0]
        types = _recv_exactly(self._sock, count)
        if RFB_SEC_TYPE_VEYON not in types:
            raise VeyonRFBError(
                f"Veyon security type {RFB_SEC_TYPE_VEYON} not offered: {list(types)}"
            )
        self._sock.sendall(bytes([RFB_SEC_TYPE_VEYON]))

    def _do_auth_type_negotiation(self) -> None:
        """Step 3-5: Receive auth-type list, send chosen + username, receive ACK."""
        payload = _recv_variant_array_message(self._sock)
        reader = _QDataStreamReader(payload)
        n_types = reader.read_variant()
        if not isinstance(n_types, int) or n_types < 1:
            raise VeyonRFBError(f"invalid auth type count: {n_types}")
        auth_types = []
        for _ in range(n_types):
            t = reader.read_variant()
            auth_types.append(t)
        if AUTH_TYPE_KEYFILE not in auth_types:
            raise VeyonRFBError(
                f"KeyFile auth (type {AUTH_TYPE_KEYFILE}) not offered: {auth_types}"
            )

        reply = _write_int(AUTH_TYPE_KEYFILE) + _write_qstring(self.username)
        _send_variant_array_message(self._sock, reply)

        ack_payload = _recv_variant_array_message(self._sock)
        if len(ack_payload) != 0:
            logger.debug(f"VeyonRFB: non-empty ACK received ({len(ack_payload)} bytes)")

    def _receive_challenge(self) -> bytes:
        """Step 7: Receive 128-byte challenge from server."""
        payload = _recv_variant_array_message(self._sock)
        reader = _QDataStreamReader(payload)
        challenge = reader.read_variant()
        if not isinstance(challenge, (bytes, bytearray)):
            raise VeyonRFBError("challenge is not a QByteArray")
        if len(challenge) != CHALLENGE_SIZE:
            raise VeyonRFBError(
                f"challenge size mismatch: expected {CHALLENGE_SIZE}, got {len(challenge)}"
            )
        return bytes(challenge)

    def _sign_challenge(self, challenge: bytes) -> bytes:
        """Sign challenge with RSA-4096 PKCS1v15-SHA512."""
        try:
            private_key = _load_private_key(self.key_data)
        except Exception as e:
            raise VeyonRFBError(f"cannot load private key: {e}")
        try:
            signature = _sign_challenge(private_key, challenge)
        except Exception as e:
            raise VeyonRFBError(f"cannot sign challenge: {e}")
        if len(signature) > MAX_SIGNATURE_SIZE:
            raise VeyonRFBError(f"signature too large: {len(signature)} bytes")
        return signature

    def _send_signature(self, signature: bytes) -> None:
        """Step 8: Send authKeyName + signature to server."""
        payload = _write_qstring(self.key_name) + _write_qbytearray(signature)
        _send_variant_array_message(self._sock, payload)

    def _check_security_result(self) -> None:
        """Step 9: Receive SecurityResult (4 bytes BE = 0 = OK)."""
        result_bytes = _recv_exactly(self._sock, 4)
        result = struct.unpack(">I", result_bytes)[0]
        if result != RFB_VNC_AUTH_OK:
            raise VeyonAuthError(f"authentication failed: result code {result}")

    def _wait_for_server_init(self) -> Tuple[int, int, str, bytes]:
        """Step 10-11: Send ClientInit, wait for ServerInit (access control delay).

        Returns:
            (width, height, desktop_name, raw_server_init_bytes)
            raw_server_init_bytes = 24-byte header + name bytes, ready to
            forward to an RFB client (browser) verbatim.

        Raises:
            VeyonAccessControlError: if ServerInit is empty (denied).
        """
        self._sock.sendall(b"\x01")  # ClientInit: shared=1

        self._sock.settimeout(self.access_control_timeout)
        try:
            header = _recv_exactly(self._sock, 24)
        except socket.timeout:
            raise VeyonAccessControlError(
                f"access control timed out after {self.access_control_timeout}s"
            )

        width, height = struct.unpack_from(">HH", header, 0)
        name_len = struct.unpack_from(">I", header, 20)[0]
        name_bytes = _recv_exactly(self._sock, name_len) if name_len > 0 else b""
        desktop_name = name_bytes.decode("latin-1", errors="replace")

        if width == 0 and height == 0:
            try:
                details = _recv_variant_array_message(self._sock)
                reader = _QDataStreamReader(details)
                detail_msg = reader.read_variant()
                reason = detail_msg if isinstance(detail_msg, str) else "access denied"
            except Exception:
                reason = "access denied"
            raise VeyonAccessControlError(reason)

        raw_server_init = header + name_bytes
        return width, height, desktop_name, raw_server_init

    def _close_quietly(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


def filter_rfb_client_message(msg_type: int, allow_extended: bool = False) -> bool:
    """Check if an RFB client→server message type is allowed by Veyon proxy.

    Veyon only accepts: SetPixelFormat(0), SetEncodings(2),
    FramebufferUpdateRequest(3), KeyEvent(4), PointerEvent(5), Xvp(250).
    All other message types (ClientCutText, FileTransfer, etc.) would cause
    the Veyon proxy to close the connection.

    Args:
        msg_type: RFB message type byte.
        allow_extended: если True — пропускает все типы (для прямого VNC-режима,
            где нет ограничений Veyon-прокси; позволяет clipboard, file transfer,
            DesktopSize и другие расширенные сообщения).
    """
    if allow_extended:
        return True
    return msg_type in (
        RFB_MSG_SET_PIXEL_FORMAT,
        RFB_MSG_SET_ENCODINGS,
        RFB_MSG_FRAMEBUFFER_UPDATE_REQUEST,
        RFB_MSG_KEY_EVENT,
        RFB_MSG_POINTER_EVENT,
        RFB_MSG_XVP,
    )
