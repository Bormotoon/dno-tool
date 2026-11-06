"""Bounded, Qt-free NBSTAT codec (RFC 1002)."""

import struct


def nbstat_request(transaction_id: int) -> bytes:
    return (struct.pack("!6H", transaction_id, 0, 1, 0, 0, 0)
            + b"\x20CK" + b"A" * 30 + b"\0\0\x21\0\x01")


def _skip_name(data: bytes, offset: int) -> int:
    end = None
    seen = set()
    size = 0
    while True:
        if offset >= len(data) or offset in seen:
            raise ValueError("Invalid compressed name")
        seen.add(offset)
        length = data[offset]
        offset += 1
        if length & 0xC0 == 0xC0:
            if offset >= len(data):
                raise ValueError("Truncated pointer")
            end = end or offset + 1
            offset = ((length & 0x3F) << 8) | data[offset]
            continue
        if length & 0xC0 or offset + length > len(data):
            raise ValueError("Invalid label")
        size += length + 1
        if size > 255:
            raise ValueError("Name too long")
        offset += length
        if not length:
            return end or offset


def parse_nbstat(data: bytes, transaction_id: int) -> tuple[str | None, int | None]:
    try:
        if len(data) > 4096:
            return None, None
        tid, flags, questions, answers, _, _ = struct.unpack_from("!6H", data)
        if tid != transaction_id or not flags & 0x8000 or flags & 0x7A0F:
            return None, None
        offset = 12
        for _ in range(questions):
            offset = _skip_name(data, offset) + 4
        candidates = []
        for _ in range(answers):
            offset = _skip_name(data, offset)
            kind, cls, _, size = struct.unpack_from("!HHIH", data, offset)
            offset += 10
            end = offset + size
            if end > len(data):
                raise ValueError("Truncated record")
            if kind == 0x21 and cls == 1 and size:
                count = data[offset]
                statistics_size = size - (1 + count * 18)
                # Real JetDirect devices return 44 statistics bytes instead
                # of RFC 1002's 46. Keep the declared record fully bounded;
                # accepting this variant must not admit truncated datagrams.
                if statistics_size != 44 and statistics_size < 46:
                    raise ValueError("Truncated node status")
                for i in range(count):
                    pos = offset + 1 + i * 18
                    name = data[pos:pos + 15].decode("cp866").strip("\0 .")
                    suffix, name_flags = struct.unpack_from("!BH", data, pos + 15)
                    # Unique browser/domain names (e.g. WORKGROUP<1D>) do
                    # not identify a host. Use workstation/file-server names.
                    if name and suffix in (0x00, 0x20) and not name_flags & 0x8000:
                        candidates.append((suffix != 0, name, suffix))
            offset = end
        if candidates:
            _, name, suffix = min(candidates)
            return name, suffix
    except (ValueError, struct.error, IndexError):
        pass
    return None, None
