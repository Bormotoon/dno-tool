"""Ограниченный координатор обнаружения хостов и измерения L3-маршрутов."""

from __future__ import annotations

import ipaddress
import math
import platform
import queue
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, wait
from dataclasses import replace
from collections.abc import Callable, Iterable, Iterator
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from src.logger import logger
from src.utils.network.net_utils import source_ip_for_target
from src.utils.network.topology import (
    AddressPlan,
    AddressIntervals,
    TraceResult,
    decode_output,
    measure_trace,
    parse_hops,
    run_process,
)
from src.utils.scanner.scanner_transport import tcp_probe
from src.utils.scanner.scanner_types import ProbeResult
from src.utils.scanner.device_facts import collect_device_facts
from src.workers.tracked_pool import TrackedThreadPoolExecutor

MAX_SILENT_HOPS = 4
MAX_FOUND_DEPTH = 2
MAX_FOUND_SUBNETS = 16
FOUND_PREFIXLEN = 24
MAX_ROUTER_TRACES = 64


def ping_host(
    ip: str, timeout_ms: int = 700, stop_event: threading.Event | None = None
) -> bool:
    """ICMP с отменой, deadline и обязательным освобождением процесса."""
    return ping_probe(ip, timeout_ms, stop_event).state == "reply"


def ping_probe(
    ip: str, timeout_ms: int = 700, stop_event: threading.Event | None = None,
) -> ProbeResult:
    """Сохранить TTL из той же ICMP-пробы, которой обнаружен хост."""
    started = time.monotonic()
    if platform.system().lower() == "windows":
        command = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        command = [
            "ping",
            "-c",
            "1",
            "-W",
            str(max(1, math.ceil(timeout_ms / 1000))),
            ip,
        ]
    result = run_process(command, timeout_ms / 1000 + 1, stop_event)
    match = re.search(r"\bttl\s*[=\s]\s*(\d+)", decode_output(result.output), re.IGNORECASE)
    alive = result.reason == "completed" and result.returncode == 0
    # Windows может вернуть код 0 и при ICMP-ошибке от шлюза.
    if platform.system().lower() == "windows":
        alive = alive and match is not None
    ttl = int(match[1]) if alive and match and 1 <= int(match[1]) <= 255 else None
    state = "reply" if alive else "cancelled" if result.reason == "cancelled" else "no_reply"
    return ProbeResult(ip, 0, "icmp", state, time.monotonic() - started, ttl=ttl)


def resolve_hostname(ip: str, stop_event: threading.Event | None = None) -> str:
    """PTR в ограниченном процессе; зависший resolver не держит поток."""
    result = run_process(["nslookup", ip], 1.5, stop_event)
    if result.reason != "completed":
        return ""
    match = re.search(
        r"(?:Name|Имя)\s*:\s*(\S+)|name\s*=\s*(\S+)",
        decode_output(result.output),
        re.IGNORECASE,
    )
    return next((v.rstrip(".") for v in match.groups() if v), "") if match else ""


def traceroute_host(
    ip: str,
    max_hops: int = 20,
    timeout_ms: int = 5000,
    stop_event: threading.Event | None = None,
    *,
    exact: bool = True,
    timeout: float | None = None,
) -> TraceResult:
    """Вернуть измерение, включая частичные и ошибочные результаты."""
    return measure_trace(
        ip, max_hops, timeout_ms, stop_event, exact=exact, timeout=timeout
    )


def _parse_traceroute_output(text: str) -> list:
    """Совместимый численный парсер с сохранением всех TTL."""
    return [(h["hop"], h["ip"]) for h in parse_hops(text)]


def _parse_hops(text: str, ip: str) -> list:
    """Разобрать хопы без DNS."""
    return parse_hops(text)


def choose_representative(ips: list, network: str, tried: list) -> str | None:
    """До трех кандидатов, конечные адреса прежде вероятных шлюзов."""
    net = ipaddress.ip_network(network, strict=False)
    gateways = (
        {str(net.network_address + 1), str(net.broadcast_address - 1)}
        if net.prefixlen < 31
        else set()
    )
    ordered = sorted(
        set(ips), key=lambda ip: (ip in gateways, int(ipaddress.IPv4Address(ip)))
    )
    return next((ip for ip in ordered[:3] if ip not in tried), None)


def _merge_routes(first: list, second: list) -> list:
    """Выбрать целую информативную попытку без склейки TTL."""
    return max((first, second), key=lambda r: sum(bool(h.get("ip")) for h in r))


def _trace_with_retries(
    ip: str,
    max_hops: int,
    traceroute_timeout: int,
    trace_retries: int,
    stop_event: threading.Event | None = None,
    first: TraceResult | None = None,
    exact: bool = True,
) -> TraceResult:
    """Повторы занимают ту же ячейку пула; отдельные попытки сохраняются."""
    attempts = [first] if first is not None else []
    for _ in range(trace_retries + (0 if first is not None else 1)):
        if stop_event is not None and stop_event.is_set():
            break
        if attempts and (
            attempts[-1].reached
            or attempts[-1].reason
            in ("missing_utility", "launch_error", "utility_error", "icmp_error")
        ):
            break
        attempts.append(
            traceroute_host(ip, max_hops, traceroute_timeout, stop_event, exact=exact)
        )
    if not attempts:
        return TraceResult(ip, None, "unknown", time.time(), reason="cancelled")
    best = max(
        attempts, key=lambda r: (r.reached, sum(bool(h["ip"]) for h in r), len(r))
    )
    reason = (
        "cancelled" if stop_event is not None and stop_event.is_set() else best.reason
    )
    return replace(best, attempts=tuple(attempts), reason=reason)


def parse_networks(text: str) -> list:
    """Уникальные IPv4 CIDR из списка, без перечисления адресов."""
    result = []
    for line in text.splitlines():
        try:
            net = ipaddress.ip_network(line.strip(), strict=False)
            if net.version == 4 and net not in result:
                result.append(net)
        except ValueError:
            pass
    return result


def expand_hosts(network: ipaddress.IPv4Network) -> Iterator[str]:
    """Все usable-адреса без материализации; hosts() учитывает /31 и /32."""
    return (str(addr) for addr in network.hosts())


def found_subnet_for_hop(ip: str, scanned: list) -> str | None:
    """Предполагаемый /24 поиска только в RFC1918, без утверждения о маске."""
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return None
    private = any(
        addr in ipaddress.ip_network(n)
        for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    )
    if not private or any(addr in net for net in scanned):
        return None
    return str(ipaddress.ip_network(f"{ip}/24", strict=False))


def common_prefix(results: list[TraceResult], network: str = "") -> list[dict]:
    """Гипотеза из нескольких достигнутых целей с одним источником/методом."""
    if network:
        net = ipaddress.ip_network(network, strict=False)
        gateways = (
            {str(net.network_address + 1), str(net.broadcast_address - 1)}
            if net.prefixlen < 31
            else set()
        )
        results = [r for r in results if r.destination not in gateways]
    if len(results) < 2 or any(not r.reached or not r.source for r in results):
        return []
    if len({(r.source, r.method) for r in results}) != 1:
        return []
    prefix = []
    for hops in zip(*(r.hops[:-1] for r in results)):
        if len({(h["hop"], h["ip"]) for h in hops}) != 1 or not hops[0]["ip"]:
            break
        prefix.append(dict(hops[0]))
    return prefix


class TopologyWorker(QObject):
    """Координатор в фоне; ограниченная очередь доставляется GUI пакетами."""

    status = Signal(str)
    progress = Signal(object, object)
    phase_changed = Signal(str)
    host_found = Signal(str, str, str)
    route_found = Signal(str, str, object, bool)
    name_found = Signal(str, str)
    probe_found = Signal(str, object)
    device_found = Signal(str, object)
    finished = Signal()
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._complete = False
        self.generation = 0
        self.run_id = ""
        self.found_hosts: set[str] = set()
        self.checked_hosts = AddressIntervals()
        self._seen_hop_ips: set[str] = set()
        self.plan = AddressPlan()
        self._events: queue.Queue = queue.Queue(512)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._drain)

    def _post(self, signal: str, *args: Any) -> None:
        if signal == "route_found":
            args[2].run_id = self.run_id
            for attempt in args[2].attempts:
                attempt.run_id = self.run_id
        while True:
            try:
                self._events.put((signal, args), timeout=0.05)
                return
            except queue.Full:
                if self._stop_event.is_set():
                    return

    def _drain(self) -> None:
        deadline = time.monotonic() + 0.008
        for _ in range(64):
            if time.monotonic() >= deadline:
                break
            try:
                signal, args = self._events.get_nowait()
            except queue.Empty:
                break
            getattr(self, signal).emit(*args)
        if (
            self._complete
            and self._events.empty()
            and self._thread is not None
            and not self._thread.is_alive()
        ):
            self._complete = False
            self._running = False
            self._timer.stop()
            self.phase_changed.emit("idle")
            self.finished.emit()

    def is_running(self) -> bool:
        return self._running

    def start(
        self,
        networks: list,
        ping_timeout: int = 700,
        max_hops: int = 20,
        traceroute_timeout: int = 5000,
        ping_workers: int = 32,
        trace_workers: int = 8,
        trace_retries: int = 1,
        scan_found_subnets: bool = False,
        trace_exact_routes: bool = False,
        known_networks: list | None = None,
        discovery_mode: str = "icmp",
        tcp_ports: tuple[int, ...] = (22, 80, 443),
    ) -> bool:
        """Проверить параметры и начать скан всех адресов указанных диапазонов."""
        if self._running or (self._thread and self._thread.is_alive()):
            return False
        for value, low, high in (
            (ping_workers, 1, 128),
            (trace_workers, 1, 64),
            (max_hops, 1, 40),
            (traceroute_timeout, 200, 10000),
            (trace_retries, 0, 3),
            (ping_timeout, 100, 10000),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Параметр карты вне допустимого диапазона")
        if (
            discovery_mode not in ("icmp", "icmp_tcp")
            or not 1 <= len(tcp_ports) <= 8
            or any(type(p) is not int or not 1 <= p <= 65535 for p in tcp_ports)
        ):
            raise ValueError("Недопустимый режим обнаружения или порты TCP")
        self._stop_event.clear()
        self._events = queue.Queue(512)
        self._running = True
        self._complete = False
        self.run_id = uuid4().hex
        self._timer.start()
        self.found_hosts = set()
        self.checked_hosts = AddressIntervals()
        self._seen_hop_ips = set()
        self.plan = AddressPlan()
        self._thread = threading.Thread(
            target=self._run,
            args=(
                networks,
                ping_timeout,
                max_hops,
                traceroute_timeout,
                ping_workers,
                trace_workers,
                trace_retries,
                scan_found_subnets,
                trace_exact_routes,
                known_networks or [],
                discovery_mode,
                tcp_ports,
            ),
            name="topology-scan",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """Запрос отмены доходит до всех дочерних процессов."""
        self._stop_event.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _window(self, jobs: Iterable[tuple[Any, Callable, tuple, dict]], workers: int, label: str) -> Iterator[tuple[Any, Any]]:
        """Не более N Futures, включая повторы внутри выполняемой операции."""
        iterator = iter(jobs)
        with TrackedThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="topology",
            label=label,
            ephemeral=True,
        ) as pool:
            pending = {}
            while not self._stop_event.is_set():
                while len(pending) < workers and not self._stop_event.is_set():
                    try:
                        key, function, args, kwargs = next(iterator)
                    except StopIteration:
                        break
                    pending[pool.submit(function, *args, metadata={"source": "application", "host": str(key)}, **kwargs)] = key
                if not pending:
                    return
                done, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in done:
                    key = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception:
                        self._stop_event.set()
                        raise
                    yield key, result
            for future in pending:
                future.cancel()

    def _probe(
        self, ip: str, timeout: int, mode: str, ports: tuple
    ) -> tuple[bool, list]:
        icmp = ping_probe(ip, timeout, self._stop_event)
        alive = icmp.state == "reply"
        probes = [icmp]
        if not alive and mode == "icmp_tcp":
            deadline = time.monotonic() + 0.6
            for port in ports:
                if self._stop_event.is_set() or time.monotonic() >= deadline:
                    break
                probe = tcp_probe(ip, port, min(0.2, deadline - time.monotonic()))
                probes.append(probe)
                if probe.state in ("open", "closed"):
                    alive = True
                    break
        return alive, probes

    def _identify_devices(
        self, initial: dict[str, tuple[ProbeResult, ...]], workers: int,
    ) -> None:
        """Уточнить оборудование найденных узлов, сохраняя численную карту доступной."""
        if not initial or self._stop_event.is_set():
            return
        self._post("phase_changed", "identify")
        self._post("status", f"Определение типов устройств: {len(initial)}")
        jobs = (
            (ip, collect_device_facts, (ip, probes, self._stop_event), {})
            for ip, probes in initial.items()
        )
        self._post("progress", 0, len(initial))
        for done, (ip, facts) in enumerate(self._window(
            jobs, workers, "Карта сети (тип оборудования)"
        ), 1):
            if not self._stop_event.is_set():
                self._post("device_found", ip, facts)
                if facts.hostname:
                    self._post("name_found", ip, facts.hostname)
                self._post("progress", done, len(initial))

    def _discover_subnets(self, scanned: list, found_total: int) -> list:
        if found_total >= MAX_FOUND_SUBNETS:
            return []
        found = []
        for ip in sorted(self._seen_hop_ips, key=ipaddress.IPv4Address):
            subnet = found_subnet_for_hop(ip, scanned + found)
            if subnet:
                found.append(ipaddress.ip_network(subnet))
            if found_total + len(found) >= MAX_FOUND_SUBNETS:
                break
        return found

    def _run(
        self,
        networks: list[ipaddress.IPv4Network],
        ping_timeout: int,
        max_hops: int,
        trace_timeout: int,
        ping_workers: int,
        trace_workers: int,
        retries: int,
        discover: bool,
        exact: bool,
        known: list[ipaddress.IPv4Network],
        mode: str,
        ports: tuple[int, ...],
    ) -> None:
        try:
            pending, scanned, depth, added = list(networks), list(known), 0, 0
            router_done: set[tuple] = set()
            dns_seen: set[str] = set()
            identified: set[str] = set()
            while pending and not self._stop_event.is_set():
                targets = self.plan.add(pending)
                scanned.extend(pending)
                pending = []
                self._post("phase_changed", "ping")
                self._post(
                    "status",
                    f"Поиск {mode}: весь диапазон, {self.plan.planned} адресов",
                )
                jobs = (
                    ((net, ip), self._probe, (ip, ping_timeout, mode, ports), {})
                    for net, ip in targets
                )
                alive_by_net: dict[str, list] = {}
                initial_probes: dict[str, tuple[ProbeResult, ...]] = {}
                for (net, ip), (alive, probes) in self._window(
                    jobs, ping_workers, "Карта сети (обнаружение)"
                ):
                    self.checked_hosts.add(ip)
                    self.plan.checked += 1
                    self._post("progress", self.plan.checked, self.plan.planned)
                    self._post("probe_found", ip, probes)
                    if alive:
                        self.found_hosts.add(ip)
                        initial_probes[ip] = tuple(probes)
                        alive_by_net.setdefault(net, []).append(ip)
                        self._post("host_found", net, ip, "")
                self._identify_devices(initial_probes, ping_workers)
                identified.update(initial_probes)
                selected = []
                for net, ips in alive_by_net.items():
                    candidates = sorted(ips, key=ipaddress.IPv4Address) if exact else []
                    if not exact:
                        while (
                            rep := choose_representative(ips, net, candidates)
                        ) is not None:
                            candidates.append(rep)
                    selected.extend((net, ip) for ip in candidates)
                self._post("phase_changed", "trace")
                self._post(
                    "status",
                    f"Трассировка: {'Windows ICMP' if platform.system().lower() == 'windows' else 'Linux UDP'}; назначений {len(selected)}",
                )
                self._post("progress", 0, len(selected))
                measurements: dict[str, list] = {}
                traced = 0
                jobs = (
                    (
                        (net, ip),
                        _trace_with_retries,
                        (ip, max_hops, trace_timeout, retries),
                        {"stop_event": self._stop_event, "exact": exact},
                    )
                    for net, ip in selected
                )
                for (net, ip), result in self._window(
                    jobs, trace_workers, "Карта сети (трассы)"
                ):
                    traced += 1
                    self._post("progress", traced, len(selected))
                    measurements.setdefault(net, []).append(result)
                    self._post("route_found", net, ip, result, False)
                    self._seen_hop_ips.update(h["ip"] for h in result if h["ip"])
                if not exact:
                    for net, results in measurements.items():
                        results.sort(key=lambda r: ipaddress.IPv4Address(r.destination))
                        prefix = common_prefix(results, net)
                        self._post(
                            "status",
                            f"{net}: {len(results)} представителей; "
                            + (
                                "согласованный префикс — гипотеза"
                                if prefix
                                else "недостаточная выборка или неоднородные пути"
                            ),
                        )
                        hypothesis = TraceResult(
                            results[0].destination,
                            results[0].source,
                            results[0].method,
                            time.time(),
                            prefix,
                            reason="hypothesis"
                            if prefix
                            else "insufficient_or_divergent_sample",
                            provenance="shared",
                            attempts=tuple(results),
                        )
                        self._post(
                            "route_found", net, results[0].destination, hypothesis, True
                        )
                routers = []
                for ip in sorted(
                    self._seen_hop_ips - self.found_hosts, key=ipaddress.IPv4Address
                ):
                    key = (ip, source_ip_for_target(ip), platform.system())
                    if key not in router_done and len(router_done) < MAX_ROUTER_TRACES:
                        router_done.add(key)
                        routers.append(ip)
                jobs = (
                    (
                        ip,
                        traceroute_host,
                        (ip, max_hops, trace_timeout, self._stop_event),
                        {"exact": exact},
                    )
                    for ip in routers
                )
                for ip, result in self._window(
                    jobs, trace_workers, "Карта сети (достройка)"
                ):
                    result.provenance = "router"
                    self._post("route_found", "", ip, result, False)
                    self._seen_hop_ips.update(h["ip"] for h in result if h["ip"])
                    traced += 1
                    self._post("progress", traced, len(selected) + len(routers))
                names = sorted(
                    (self.found_hosts | self._seen_hop_ips) - dns_seen,
                    key=ipaddress.IPv4Address,
                )
                self._identify_devices(
                    {ip: () for ip in self._seen_hop_ips - identified}, ping_workers
                )
                identified.update(self._seen_hop_ips)
                dns_seen.update(names)
                self._post("phase_changed", "dns")
                self._post("status", "Уточнение имен обнаруженных адресов…")
                dns_deadline = time.monotonic() + 5
                jobs = (
                    (ip, resolve_hostname, (ip, self._stop_event), {})
                    for ip in names[:512]
                    if time.monotonic() < dns_deadline
                )
                for ip, name in self._window(
                    jobs, min(4, ping_workers), "Карта сети (имена)"
                ):
                    if name:
                        self._post("name_found", ip, name)
                if discover and depth < MAX_FOUND_DEPTH:
                    pending = self._discover_subnets(scanned, added)
                    added += len(pending)
                    depth += 1
                    if pending:
                        self._post(
                            "status",
                            "Предполагаемые диапазоны поиска: "
                            + ", ".join(map(str, pending)),
                        )
        except Exception as exc:
            logger.exception("Ошибка карты сети")
            self._post("error", f"{type(exc).__name__}: {exc}")
        finally:
            self._complete = True


class SingleTraceWorker(QObject):
    """Отменяемое персональное измерение; QObject удерживается до finished."""

    finished = Signal(str, object)

    def __init__(
        self,
        host: str,
        max_hops: int = 20,
        timeout_ms: int = 5000,
        retries: int = 1,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._host, self._max_hops, self._timeout_ms, self._retries = (
            host,
            max_hops,
            timeout_ms,
            retries,
        )
        self._stop_event = threading.Event()
        self.generation = 0
        self.run_id = uuid4().hex
        self._result = None
        self._thread = None
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._deliver)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="topology-single", daemon=True
        )
        self._thread.start()
        self._timer.start()

    def _deliver(self) -> None:
        if (
            self._thread is not None
            and not self._thread.is_alive()
            and self._result is not None
        ):
            self._timer.stop()
            self._result.run_id = self.run_id
            for attempt in self._result.attempts:
                attempt.run_id = self.run_id
            self.finished.emit(self._host, self._result)

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        try:
            self._result = _trace_with_retries(
                self._host,
                self._max_hops,
                self._timeout_ms,
                self._retries,
                self._stop_event,
                exact=True,
            )
        except Exception as exc:
            self._result = TraceResult(
                self._host,
                None,
                "unknown",
                time.time(),
                reason="launch_error",
                diagnostic=str(exc),
            )
