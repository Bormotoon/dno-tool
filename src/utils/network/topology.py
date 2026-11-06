"""Измерения L3, ограниченное выполнение процессов и планирование адресов."""

from __future__ import annotations

import ipaddress
import platform
import re
import subprocess
import tempfile
import threading
import time
from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import uuid4

from src.utils.network.net_utils import source_ip_for_target

IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HOP_RE = re.compile(r"^\s*(\d+)\s+")


@dataclass
class TraceResult:
    """Целостная попытка; последовательность TTL никогда не сливается с другой."""

    destination: str
    source: str | None
    method: str
    started_at: float
    hops: list[dict] = field(default_factory=list)
    reached: bool = False
    reason: str = "max_hops"
    measurement_id: str = field(default_factory=lambda: uuid4().hex)
    attempts: tuple[TraceResult, ...] = ()
    provenance: str = "personal"
    diagnostic: str = ""
    run_id: str = ""

    def __iter__(self) -> Iterator[dict]:
        return iter(self.hops)

    def __len__(self) -> int:
        return len(self.hops)

    def __getitem__(self, index: int | slice) -> dict | list[dict]:
        return self.hops[index]


@dataclass(frozen=True)
class ProcessResult:
    """Вывод и причина остановки дочернего процесса после его завершения."""

    output: bytes
    returncode: int | None
    reason: str


def run_process(
    command: list[str],
    timeout: float,
    stop_event: threading.Event | None = None,
    cut_silent: bool = False,
) -> ProcessResult:
    """Проверять deadline без блокирующего чтения pipe; всегда дожидаться child."""
    deadline = time.monotonic() + timeout
    event = stop_event or threading.Event()
    if event.is_set():
        return ProcessResult(b"", None, "cancelled")
    info = None
    if platform.system().lower() == "windows":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = subprocess.SW_HIDE
    with (
        tempfile.TemporaryDirectory(prefix="dno-trace-") as directory,
        open(directory + "/output", "w+b") as output,
    ):
        try:
            proc = subprocess.Popen(
                command, stdout=output, stderr=subprocess.STDOUT, startupinfo=info
            )
        except FileNotFoundError:
            return ProcessResult(b"", None, "missing_utility")
        except OSError as exc:
            return ProcessResult(str(exc).encode(), None, "launch_error")
        reason = "completed"
        try:
            while proc.poll() is None:
                if event.is_set():
                    reason = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                # Файл ограничен: ошибочная утилита не может бесконечно писать вывод.
                if output.tell() > 1024 * 1024:
                    reason = "output_limit"
                    break
                if cut_silent:
                    with open(directory + "/output", "rb") as snapshot:
                        text = decode_output(snapshot.read(1024 * 1024))
                        hops = parse_hops(text[: text.rfind("\n") + 1])
                    if len(hops) >= 4 and all(not h["ip"] for h in hops[-4:]):
                        reason = "fast_cutoff"
                        break
                event.wait(0.02)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        if reason == "completed" and output.tell() > 1024 * 1024:
            reason = "output_limit"
        output.seek(0)
        return ProcessResult(output.read(1024 * 1024), proc.returncode, reason)


def decode_output(raw: bytes) -> str:
    """Численные ответы ASCII; имена декодируются с учетом платформы."""
    return raw.decode(
        "cp866" if platform.system().lower() == "windows" else "utf-8", errors="replace"
    )


def parse_hops(text: str) -> list[dict]:
    """Сохранить повторения IP, молчащие TTL и терминальные ICMP-признаки."""
    hops = []
    for line in text.splitlines():
        match = HOP_RE.match(line)
        if not match:
            continue
        ttl = int(match[1])
        if not 1 <= ttl <= 255:
            continue
        previous = hops[-1]["hop"] if hops else 0
        for missing in range(previous + 1, ttl):
            hops.append({"hop": missing, "ip": "", "hostname": "", "terminal": ""})
        addresses = []
        for value in IP_RE.findall(line):
            try:
                addresses.append(str(ipaddress.IPv4Address(value)))
            except ValueError:
                continue
        terminal = re.search(r"!(?:H|N|X|P|S|F|V|C|\d+)", line)
        hops.append(
            {
                "hop": ttl,
                "ip": addresses[0] if addresses else "",
                "hostname": "",
                "terminal": terminal[0] if terminal else "",
            }
        )
    return hops


def measure_trace(
    ip: str,
    max_hops: int,
    timeout_ms: int,
    stop_event: threading.Event | None = None,
    *,
    exact: bool = True,
    timeout: float | None = None,
) -> TraceResult:
    """Системная численная трасса, без DNS и скрытого объединения попыток."""
    windows = platform.system().lower() == "windows"
    method = "windows-icmp" if windows else "linux-udp"
    result = TraceResult(
        ip, source_ip_for_target(ip, fallback=False), method, time.time()
    )
    if windows:
        command = ["tracert", "-d", "-h", str(max_hops), "-w", str(timeout_ms), ip]
    else:
        command = [
            "traceroute",
            "-n",
            "-m",
            str(max_hops),
            "-w",
            str(timeout_ms / 1000),
            "-q",
            "1",
            ip,
        ]
    budget = (
        timeout
        if timeout is not None
        else min(300, max_hops * timeout_ms / 1000 * (3 if windows else 1) + 5)
    )
    process = run_process(command, budget, stop_event, cut_silent=not exact)
    result.hops = parse_hops(decode_output(process.output))
    result.reached = any(h["ip"] == ip and not h["terminal"] for h in result.hops)
    if process.reason != "completed":
        result.reason = process.reason
    elif any(h["terminal"] for h in result.hops):
        result.reason = "icmp_error"
    elif result.reached:
        result.reason = "reached"
    elif process.returncode:
        result.reason = "utility_error"
    elif not result.hops:
        result.reason = "parse_error"
    elif result.hops[-1]["hop"] >= max_hops:
        result.reason = "max_hops"
    else:
        result.reason = "parse_error"
    if result.reason in ("utility_error", "parse_error", "launch_error"):
        result.diagnostic = decode_output(process.output)[:500]
    return result


def usable_count(network: ipaddress.IPv4Network) -> int:
    """Количество usable-адресов без перечисления сети."""
    return (
        network.num_addresses if network.prefixlen >= 31 else network.num_addresses - 2
    )


class AddressIntervals:
    """Компактное множество IPv4: соседние проверенные адреса хранятся интервалом."""

    def __init__(self) -> None:
        self.ranges: list[tuple[int, int]] = []
        self._count = 0

    def __contains__(self, ip: str) -> bool:
        number = int(ipaddress.IPv4Address(ip))
        index = bisect_right(self.ranges, (number, 0xFFFFFFFF)) - 1
        return index >= 0 and number <= self.ranges[index][1]

    def __len__(self) -> int:
        return self._count

    def add(self, ip: str) -> None:
        number = int(ipaddress.IPv4Address(ip))
        self.add_range(number, number)

    def add_range(self, low: int, high: int) -> None:
        """Добавить диапазон и объединить пересечения без перечисления адресов."""
        first = bisect_left(self.ranges, (low, -1))
        if first and self.ranges[first - 1][1] + 1 >= low:
            first -= 1
        last = first
        while last < len(self.ranges) and self.ranges[last][0] <= high + 1:
            old_low, old_high = self.ranges[last]
            low, high = min(low, old_low), max(high, old_high)
            self._count -= old_high - old_low + 1
            last += 1
        self.ranges[first:last] = [(low, high)]
        self._count += high - low + 1

    def uncovered(self, low: int, high: int) -> Iterator[tuple[int, int]]:
        """Части диапазона, которые еще не запланированы."""
        index = max(0, bisect_left(self.ranges, (low, -1)) - 1)
        for start, end in self.ranges[index:]:
            if start > high:
                break
            if end < low:
                continue
            if low < start:
                yield low, start - 1
            low = max(low, end + 1)
            if low > high:
                return
        if low <= high:
            yield low, high


@dataclass(frozen=True)
class AddressBatch:
    """Повторно итерируемый план: адреса создаются только при выдаче в очередь."""

    ranges: tuple[tuple[str, int, int], ...]

    def __len__(self) -> int:
        return sum(high - low + 1 for _, low, high in self.ranges)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        for network, low, high in self.ranges:
            for number in range(low, high + 1):
                yield network, str(ipaddress.IPv4Address(number))


class AddressPlan:
    """Полный охват CIDR без лимита хостов и без повторных проб на пересечениях."""

    def __init__(self) -> None:
        self.planned = 0
        self.checked = 0
        self.available = 0
        self._scheduled = AddressIntervals()
        self._groups: set[ipaddress.IPv4Network] = set()

    def add(self, networks: list[ipaddress.IPv4Network]) -> AddressBatch:
        """Спланировать все usable-адреса; наиболее узкая группа получает приоритет."""
        segments: list[tuple[str, int, int]] = []
        fresh = AddressIntervals()
        self._groups.update(networks)
        for net in networks:
            low = int(net.network_address) + (net.prefixlen < 31)
            high = int(net.broadcast_address) - (net.prefixlen < 31)
            for start, end in self._scheduled.uncovered(low, high):
                fresh.add_range(start, end)
            self._scheduled.add_range(low, high)
        assigned = AddressIntervals()
        for net in sorted(
            self._groups, key=lambda n: (-n.prefixlen, int(n.network_address))
        ):
            for start, end in fresh.ranges:
                low = max(start, int(net.network_address))
                high = min(end, int(net.broadcast_address))
                if low > high:
                    continue
                segments.extend(
                    (str(net), first, last)
                    for first, last in assigned.uncovered(low, high)
                )
                assigned.add_range(low, high)
        self.available = self.planned = len(self._scheduled)
        return AddressBatch(tuple(segments))
