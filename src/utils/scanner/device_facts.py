"""Общие признаки устройств и короткие сетевые запросы без зависимостей от Qt."""

from __future__ import annotations

import ipaddress
import platform
import re
import socket
import threading
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

import psutil

from src.utils.network.topology import decode_output, run_process
from src.utils.scanner.device_classifier import (
    classify_ap_by_ports_and_banner,
    classify_device_detailed,
)
from src.utils.scanner.scanner_transport import tcp_probe
from src.utils.scanner.scanner_types import ProbeResult
from src.utils.scanner.scanner_udp import nbstat_probe, snmp_probe

CLASSIFICATION_PORTS = (
    631, 9100, 5060, 22, 80, 135, 554, 445, 139, 2049,
    5000, 5001, 443, 11100, 5900, 53, 23,
    4352, 8060, 8123, 1400, 102, 502, 4370, 3493, 548,
)


def normalize_mac(value: str) -> str | None:
    """Принять только ненулевой индивидуальный MAC-адрес."""
    value = value.replace("-", ":").upper()
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", value):
        return None
    octets = bytes.fromhex(value.replace(":", ""))
    return value if any(octets) and not octets[0] & 1 else None


def get_mac_address(
    ip_address: str, timeout: float = 3.0,
    stop_event: threading.Event | None = None,
) -> str | None:
    """MAC своего интерфейса или точного соседа, без подстановки MAC шлюза."""
    address = ipaddress.IPv4Address(ip_address)
    ip_address = str(address)
    event = stop_event or threading.Event()
    deadline = time.monotonic() + timeout
    if event.is_set() or timeout <= 0 or address.is_loopback:
        return None
    local, mac = _local_mac_address(ip_address)
    if event.is_set() or time.monotonic() >= deadline:
        return None
    if local:
        return mac
    windows = platform.system().lower() == "windows"
    if not windows:
        try:
            for line in Path("/proc/net/arp").read_text().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip_address and int(parts[2], 16) & 2:
                    mac = normalize_mac(parts[3])
                    if mac:
                        return mac
        except (OSError, ValueError):
            pass
    commands = (
        [["arp", "-a", ip_address]] if windows
        else [["ip", "neigh", "show", ip_address], ["arp", "-n", ip_address]]
    )
    for command in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or event.is_set():
            break
        result = run_process(command, remaining, event)
        if result.reason != "completed" or result.returncode != 0:
            continue
        for line in decode_output(result.output).splitlines():
            parts = line.split()
            if not parts or not (
                parts[0] == ip_address or f"({ip_address})" in parts
            ) or re.search(r"\b(INCOMPLETE|FAILED)\b", line):
                continue
            match = re.search(r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b", line)
            if match:
                mac = normalize_mac(match[0])
                if mac:
                    return mac
    return None


def _local_mac_address(ip_address: str) -> tuple[bool, str | None]:
    """Найти MAC только у интерфейса с точным IPv4, включая вторичные адреса.

    Первый элемент отличает локальный туннель без Ethernet-адреса от удалённого
    хоста: для такого интерфейса не нужно выполнять ARP-запросы.
    """
    local = False
    link_families = {psutil.AF_LINK}
    for name in ("AF_LINK", "AF_PACKET"):
        family = getattr(socket, name, None)
        if family is not None:
            link_families.add(family)
    try:
        for addresses in psutil.net_if_addrs().values():
            if not any(
                item.family == socket.AF_INET and item.address == ip_address
                for item in addresses
            ):
                continue
            local = True
            for item in addresses:
                if item.family in link_families:
                    mac = normalize_mac(item.address)
                    if mac:
                        return True, mac
    except (OSError, psutil.Error):
        # Сетевой стек может измениться во время чтения списка интерфейсов.
        pass
    return local, None


def _read_banner(
    ip_address: str, port: int, timeout: float, request: bytes = b"",
    stop_event: threading.Event | None = None,
) -> str | None:
    """Прочитать ограниченный ответ с общим deadline и проверками отмены."""
    event = stop_event or threading.Event()
    deadline = time.monotonic() + timeout
    if event.is_set():
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(max(.001, timeout))
            sock.connect((ip_address, port))
            if request and not event.is_set():
                sock.settimeout(max(.001, deadline - time.monotonic()))
                sock.sendall(request)
            response = b""
            while len(response) < 2048 and not event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(min(.1, remaining))
                try:
                    chunk = sock.recv(min(1024, 2048 - len(response)))
                except TimeoutError:
                    continue
                if not chunk:
                    break
                response += chunk
                if (request and b"\r\n\r\n" in response) or (not request and b"\n" in response):
                    break
            return response.decode("utf-8", errors="replace").strip() or None
    except OSError:
        return None


def fetch_ssh_banner(
    ip_address: str, port: int = 22, timeout: float = 1.5,
    stop_event: threading.Event | None = None,
) -> str | None:
    """Считать приветствие SSH без аутентификации."""
    return _read_banner(ip_address, port, timeout, stop_event=stop_event)


def fetch_http_server_header(
    ip_address: str, port: int = 80, timeout: float = 1.5,
    stop_event: threading.Event | None = None,
) -> str | None:
    """Считать Server из HEAD-ответа без перехода по редиректам."""
    request = (
        f"HEAD / HTTP/1.0\r\nHost: {ip_address}\r\n"
        "User-Agent: dno-tool-scanner\r\n\r\n"
    ).encode("ascii")
    response = _read_banner(ip_address, port, timeout, request, stop_event)
    if response and re.match(r"^HTTP/1\.[01] \d{3}(?:\s|$)", response):
        headers = response.split("\r\n\r\n", 1)[0]
        match = re.search(r"^Server:[ \t]*([^\r\n]+)", headers, re.MULTILINE | re.IGNORECASE)
        if match:
            return match[1].strip()
    return None


def fetch_equipment_banners(
    ip_address: str, open_ports: Collection[int], timeout: float = .4,
    stop_event: threading.Event | None = None,
) -> tuple[tuple[int, str], ...]:
    """Считать приветствие PJLink; команды и пароли проектору не отправляются."""
    if 4352 not in open_ports or timeout <= 0:
        return ()
    banner = _read_banner(ip_address, 4352, timeout, stop_event=stop_event)
    if banner and re.fullmatch(r"PJLINK (?:0|1 [0-9a-fA-F]{8})", banner):
        return ((4352, banner),)
    return ()


@dataclass(frozen=True)
class DeviceFacts:
    """Наблюдаемые признаки; роль узла в L3-графе здесь не задается."""

    hostname: str = ""
    mac_address: str = ""
    ttl: int | None = None
    probes: tuple[ProbeResult, ...] = ()
    netbios_name: str | None = None
    netbios_suffix: int | None = None
    ssh_banner: str | None = None
    http_server: str | None = None
    snmp_description: str | None = None
    service_banners: tuple[tuple[int, str], ...] = ()

    @property
    def open_ports(self) -> tuple[int, ...]:
        return tuple(sorted({p.port for p in self.probes if p.protocol == "tcp" and p.state == "open"}))

    def classify(self) -> tuple[str, str]:
        """Классифицировать по общей модели сканера, не превращая timeout в closed."""
        states = {
            p.port: p.state == "open" for p in self.probes
            if p.protocol == "tcp" and p.state in ("open", "closed")
        }
        ap_candidate = (
            states.get(22) and states.get(80) and states.get(443)
        ) or (
            states.get(22) and states.get(443) is False
            and states.get(11100) is False
        )
        if ap_candidate:
            ap_type = classify_ap_by_ports_and_banner(
                states, self.ssh_banner, None, self.http_server
            )
            if ap_type:
                return ap_type, "порт-паттерн AP + баннер"
        return classify_device_detailed(
            self.open_ports, self.hostname, self.mac_address,
            ttl=self.ttl, netbios_name=self.netbios_name,
            netbios_suffix=self.netbios_suffix, ssh_banner=self.ssh_banner,
            http_server=self.http_server, snmp_description=self.snmp_description,
            service_banners=dict(self.service_banners),
        )


def collect_device_facts(
    ip: str, initial_probes: tuple[ProbeResult, ...] = (),
    stop_event: threading.Event | None = None,
) -> DeviceFacts:
    """Уточнить признаки найденного узла в конечном бюджете, без DNS и повторного ping."""
    event = stop_event or threading.Event()
    deadline = time.monotonic() + 6
    probes = list(initial_probes)
    known = {p.port for p in probes if p.protocol == "tcp"}
    for port in CLASSIFICATION_PORTS:
        if event.is_set() or time.monotonic() >= deadline:
            break
        if port not in known:
            probes.append(tcp_probe(ip, port, min(.15, deadline - time.monotonic())))
    opened = {p.port for p in probes if p.protocol == "tcp" and p.state == "open"}

    def budget(maximum: float) -> float:
        return max(.001, min(maximum, deadline - time.monotonic()))

    def active() -> bool:
        return not event.is_set() and time.monotonic() < deadline

    mac = get_mac_address(ip, budget(.5), event) if active() else ""
    netbios_name, netbios_suffix = None, None
    if active():
        probe, value = nbstat_probe(ip, timeout=budget(.3), cancelled=event.is_set)
        probes.append(probe)
        netbios_name, netbios_suffix = value or (None, None)
    snmp_description = None
    if active():
        probe, snmp_description = snmp_probe(ip, timeout=budget(.3), cancelled=event.is_set)
        probes.append(probe)
    ssh = fetch_ssh_banner(ip, timeout=budget(.4), stop_event=event) if 22 in opened and active() else None
    http = fetch_http_server_header(ip, timeout=budget(.4), stop_event=event) if 80 in opened and active() else None
    services = fetch_equipment_banners(ip, opened, budget(.4), event) if active() else ()
    ttl = next((p.ttl for p in probes if p.protocol == "icmp" and p.state == "reply" and p.ttl is not None), None)
    return DeviceFacts(
        hostname=netbios_name or "", mac_address=mac or "", ttl=ttl,
        probes=tuple(probes), netbios_name=netbios_name, netbios_suffix=netbios_suffix,
        ssh_banner=ssh, http_server=http, snmp_description=snmp_description,
        service_banners=services,
    )
