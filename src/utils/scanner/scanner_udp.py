"""Read-only UDP service probes: NBSTAT and SNMPv1 sysDescr (public).

No reply means unknown/timeout, never a closed port. Only a validated reply
from the connected peer establishes service evidence. No SET operations.
"""

import secrets
import socket
import time

from src.utils.scanner.scanner_protocol import nbstat_request, parse_nbstat
from src.utils.scanner.scanner_types import ProbeResult

SYS_DESCR = bytes.fromhex("2b06010201010100")  # 1.3.6.1.2.1.1.1.0


def _tlv(tag: int, value: bytes) -> bytes:
    size = len(value)
    length = bytes([size]) if size < 128 else b"\x82" + size.to_bytes(2, "big")
    return bytes([tag]) + length + value


def snmp_request(request_id: int) -> bytes:
    binding = _tlv(0x30, _tlv(6, SYS_DESCR) + _tlv(5, b""))
    pdu = _tlv(0xA0, _tlv(2, request_id.to_bytes(4, "big"))
               + _tlv(2, b"\0") * 2 + _tlv(0x30, binding))
    return _tlv(0x30, _tlv(2, b"\0") + _tlv(4, b"public") + pdu)


def _read(data: bytes, offset: int, expected: int):
    if offset + 2 > len(data) or data[offset] != expected:
        raise ValueError("Invalid SNMP tag")
    size = data[offset + 1]
    offset += 2
    if size & 0x80:
        width = size & 0x7F
        if not 1 <= width <= 2 or offset + width > len(data):
            raise ValueError("Invalid BER length")
        size = int.from_bytes(data[offset:offset + width], "big")
        offset += width
    end = offset + size
    if end > len(data):
        raise ValueError("Truncated SNMP value")
    return data[offset:end], end


def parse_snmp(data: bytes, request_id: int) -> str | None:
    try:
        if len(data) > 4096:
            return None
        message, end = _read(data, 0, 0x30)
        if end != len(data):
            return None
        version, pos = _read(message, 0, 2)
        community, pos = _read(message, pos, 4)
        pdu, end = _read(message, pos, 0xA2)
        if version != b"\0" or community != b"public" or end != len(message):
            return None
        rid, pos = _read(pdu, 0, 2)
        error, pos = _read(pdu, pos, 2)
        error_index, pos = _read(pdu, pos, 2)
        bindings, end = _read(pdu, pos, 0x30)
        if (not 1 <= len(rid) <= 5 or int.from_bytes(rid, "big", signed=True) != request_id
                or error != b"\0" or error_index != b"\0" or end != len(pdu)):
            return None
        binding, end = _read(bindings, 0, 0x30)
        if end != len(bindings):
            return None
        oid, pos = _read(binding, 0, 6)
        value, end = _read(binding, pos, 4)
        if oid != SYS_DESCR or end != len(binding) or len(value) > 1024:
            return None
        return " ".join(value.decode("utf-8", errors="replace").split())
    except (ValueError, IndexError):
        return None


def udp_exchange(host, port, packet, parser, timeout=.6, cancelled=lambda: False):
    started = time.monotonic()
    deadline = started + timeout
    invalid = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))  # kernel filters source IP and port
            sock.send(packet)
            while time.monotonic() < deadline and not cancelled():
                sock.settimeout(min(.1, max(.001, deadline - time.monotonic())))
                try:
                    data = sock.recv(4097)
                except TimeoutError:
                    continue
                value = parser(data)
                if value is not None:
                    return ProbeResult(host, port, "udp", "open", time.monotonic() - started), value
                invalid = True
        state = "cancelled" if cancelled() else "invalid" if invalid else "timeout"
        diagnostic = "unvalidated_reply" if invalid else "no_reply"
    except ConnectionRefusedError:
        state, diagnostic = "closed", "icmp_port_unreachable"
    except TimeoutError:
        state, diagnostic = "timeout", "send_timeout"
    except OSError as exc:
        if exc.errno in (10054, 10061):
            state, diagnostic = "closed", "icmp_port_unreachable"
        else:
            state, diagnostic = "error", f"socket_errno={exc.errno}"
    return ProbeResult(host, port, "udp", state, time.monotonic() - started, diagnostic), None


def nbstat_probe(host, timeout=.6, cancelled=lambda: False):
    transaction_id = secrets.randbelow(65536)
    def parse(data):
        name, suffix = parse_nbstat(data, transaction_id)
        return (name, suffix) if name else None
    return udp_exchange(host, 137, nbstat_request(transaction_id), parse, timeout, cancelled)


def snmp_probe(host, timeout=.6, cancelled=lambda: False):
    request_id = secrets.randbelow(2**31)
    return udp_exchange(host, 161, snmp_request(request_id),
                        lambda data: parse_snmp(data, request_id), timeout, cancelled)
