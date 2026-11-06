"""Lazy IPv4 scanner input; validation never enumerates the network."""

import ipaddress
from collections.abc import Sequence

MAX_SCAN_ADDRESSES = 65536


class ScannerAddressRange(Sequence):
    def __init__(self, text: str):
        text = text.strip()
        if ":" in text:
            raise ValueError("Сканер поддерживает только IPv4.")
        if "/" in text:
            network = ipaddress.IPv4Network(text, strict=False)
            self.start = int(network.network_address)
            self.count = network.num_addresses
            if network.prefixlen < 31:
                self.start += 1
                self.count -= 2
        elif "-" in text:
            left, right = map(str.strip, text.split("-", 1))
            start = ipaddress.IPv4Address(left)
            if "." not in right:
                right = left.rsplit(".", 1)[0] + "." + right
            self.start = int(start)
            self.count = int(ipaddress.IPv4Address(right)) - self.start + 1
        else:
            self.start = int(ipaddress.IPv4Address(text))
            self.count = 1
        if self.count <= 0:
            raise ValueError("Начальный IP не может быть больше конечного.")
        if self.count > MAX_SCAN_ADDRESSES:
            raise ValueError(f"Диапазон содержит {self.count} адресов. Максимум — {MAX_SCAN_ADDRESSES}.")

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self.count))]
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        return str(ipaddress.IPv4Address(self.start + index))
