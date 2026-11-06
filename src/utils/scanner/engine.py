"""Общий алгоритм Qt- и веб-сканера: обнаружение, имена, пробы и анализ."""

import platform
import queue
import re
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from src.config import PORTS, config
from src.logger import logger
from src.utils.scanner.device_classifier import (
    classify_ap_by_ports_and_banner,
    classify_device_detailed,
)
from src.utils.scanner.device_facts import (
    CLASSIFICATION_PORTS as _ICMP_PROBE_PORTS,
)
from src.utils.scanner.device_facts import (
    fetch_equipment_banners,
    get_mac_address,
)
from src.utils.scanner.device_facts import (
    fetch_http_server_header as _fetch_http_server_header,
)
from src.utils.scanner.device_facts import (
    fetch_ssh_banner as _fetch_ssh_banner,
)
from src.utils.scanner.scanner_icmp import check_icmplib, icmplib_probe, system_probe
from src.utils.scanner.scanner_transport import tcp_probe
from src.utils.scanner.scanner_types import HostScanResult, ProbeResult, ScanOutcome
from src.utils.scanner.scanner_udp import nbstat_probe, snmp_probe
from src.workers.tracked_pool import TrackedThreadPoolExecutor


class ScanCallback:
    """Синхронная доставка событий без привязки к Qt event loop."""

    def __init__(self) -> None:
        self._handlers: list[Callable[..., Any]] = []

    def connect(self, handler: Callable[..., Any]) -> None:
        self._handlers.append(handler)

    def emit(self, *args: Any) -> None:
        for handler in self._handlers:
            handler(*args)


def _netbios_name_query(ip_address: str, timeout: float = 0.6):
    _, value = nbstat_probe(ip_address, timeout=timeout)
    return value or (None, None)


def _resolve_hostname_direct(ip_address: str) -> Optional[str]:
    """Разрешить имя системным процессом с таймаутом внутри слота сканера.

    ping -a и getent учитывают hosts и локальные источники имён,
    не оставляя неотменяемый поток gethostbyaddr.
    """
    windows = platform.system() == "Windows"
    command = (
        ["ping", "-a", "-n", "1", "-w", "100", ip_address]
        if windows
        else ["getent", "hosts", ip_address]
    )
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="cp866" if windows else "utf-8",
            errors="replace",
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if windows else 0,
        )
        if windows:
            # Заголовок содержит имя и при блокировке ICMP; адрес в скобках
            # позволяет разобрать его независимо от языка системы.
            match = re.search(
                r"([^\s\[\]]+)\s+\[" + re.escape(ip_address) + r"\]", result.stdout
            )
            if match:
                return match.group(1).rstrip(".")
        elif result.returncode == 0:
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 2 and fields[0] == ip_address:
                    return fields[1].rstrip(".")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


# ---------------------------------------------------------------------------
# Долгоживущий пул сканера (переиспользование вместо эфемерных пулов).
#
# Раньше каждый скан создавал 2 пула по N тредов (скан + пробы), а в
# порт-режиме каждый scan_host — ещё и вложенный пул на 8 тредов:
# при N=128 это было ~1300 живых потоков → вылет программы.
# Теперь один персистентный пул ровно на N тредов (бюджет «Потоки»):
# создаётся лениво при первом скане, переживает сканы, пересоздаётся
# только при смене N и гасится при закрытии диалога. Вложенных пулов
# нет: задачи плоские (IP или пара (IP, порт)), конкурентных сокетов
# и процессов в любой момент ≤ N по построению.
# ---------------------------------------------------------------------------

_SCANNER_RUN_LOCK = threading.Lock()
_SCANNER_POOL_LOCK = threading.Lock()
_scan_pool: Optional[TrackedThreadPoolExecutor] = None
_scan_pool_size: int = 0


def get_scanner_pool(max_workers: int, *, _for_scan=False) -> TrackedThreadPoolExecutor:
    """Вернуть долгоживущий пул сканера на max_workers тредов.

    Пересоздаёт пул, если размер отличается (смена ползунка «Потоки»).
    Вызывать только когда активный скан завершён (гарантирует диалог).
    """
    global _scan_pool, _scan_pool_size
    want = max(1, int(max_workers))
    with _SCANNER_POOL_LOCK:
        if (
            _SCANNER_RUN_LOCK.locked()
            and not _for_scan
            and (_scan_pool is None or _scan_pool_size != want)
        ):
            raise RuntimeError("Сканер ещё освобождает задачи предыдущего запуска.")
        if _scan_pool is None or _scan_pool_size != want:
            old = _scan_pool
            _scan_pool = TrackedThreadPoolExecutor(
                max_workers=want,
                thread_name_prefix="dno-tool-scanner",
                label="Сканер хостов",
                ephemeral=False,
            )
            _scan_pool_size = want
            if old is not None:
                try:
                    old.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
        return _scan_pool


def shutdown_scanner_pools() -> None:
    """Погасить пул сканера (закрытие диалога). Сканы к этому моменту завершены."""
    global _scan_pool, _scan_pool_size
    if not _SCANNER_RUN_LOCK.acquire(blocking=False):
        return
    try:
        with _SCANNER_POOL_LOCK:
            pool, _scan_pool = _scan_pool, None
            _scan_pool_size = 0
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    finally:
        _SCANNER_RUN_LOCK.release()


class ScannerEngine:
    """Одинаковые фазы и сетевые доказательства для обоих интерфейсов."""

    result_ready: Any
    phase: Any

    def __init__(
        self,
        ips: Sequence[str],
        icmp: bool = True,
        resolve_mac: bool = False,
        max_workers: int = 32,
        classify_enabled: bool = True,
        use_port_signatures: bool = True,
        resolve_names: bool = True,
        combined: bool = False,
        udp_probes: bool = False,
        ping_backend: str | None = None,
    ) -> None:
        if not hasattr(self, "result_ready"):
            for name in (
                "finished",
                "result_ready",
                "phase_ready",
                "outcome_ready",
                "error",
                "scan_finished",
                "phase",
            ):
                setattr(self, name, ScanCallback())
        self.ips = ips
        self.scan_id = uuid4().hex
        self.resolve_names = resolve_names
        self.enabled_ports = tuple(
            sorted(p for p, enabled in PORTS.copy().items() if enabled)
        )
        self.outcome = "running"
        self.failure = ""
        self._futures = set()
        self.probes = {}
        self._probe_lock = threading.Lock()
        self._diagnostics = Counter()
        self._buffered_delivery = False
        self._results = queue.Queue(maxsize=512)
        self._abort_event = threading.Event()
        self.icmp = bool(icmp or combined)
        self.combined = combined
        self.udp_probes = udp_probes
        self._nb_names = {}
        self.resolve_mac = resolve_mac
        self.max_workers = max(1, int(max_workers))
        self.classify_enabled = bool(classify_enabled)
        self.use_port_signatures = bool(use_port_signatures)
        self._dns_cache: Dict[str, Optional[str]] = {}
        self._dns_lock = threading.Lock()
        self._port_timeout = float(config.app.network.port_scan_timeout)
        self._ping_timeout_ms = int(config.app.network.scanner_ping_timeout_ms)
        self._ping_backend = (
            config.app.network.scanner_ping_backend
            if ping_backend is None
            else ping_backend
        )
        self._icmp_privileged = True

    def enable_buffered_delivery(self):
        self._buffered_delivery = True

    def take_results(self, limit=64):
        results = []
        for _ in range(limit):
            try:
                results.append(self._results.get_nowait())
            except queue.Empty:
                break
        return results

    def _record_probe(self, result):
        with self._probe_lock:
            self.probes.setdefault(result.host, {})[
                (result.protocol, result.port)
            ] = result
            self._diagnostics[f"{result.protocol}:{result.state}"] += 1

    def _emit_result(self, *args):
        ip, hostname, online, ports, mac, kind, reason, state = args
        with self._probe_lock:
            probes = tuple(self.probes.get(ip, {}).values())
        result = HostScanResult(
            self.scan_id,
            ip,
            hostname,
            online,
            tuple(ports),
            mac,
            kind,
            reason,
            state,
            probes,
        )
        self.scan_finished.emit(*args)  # compatibility for existing observers
        if not self._buffered_delivery:
            self.result_ready.emit(result)
            return
        while not self.is_aborting:
            try:
                self._results.put(result, timeout=0.05)
                return
            except queue.Full:
                continue

    def _emit_outcome(self):
        self.outcome_ready.emit(
            ScanOutcome(
                self.scan_id,
                self.outcome,
                self.failure,
                tuple(sorted(self._diagnostics.items())),
            )
        )

    def _emit_phase(self, *args):
        self.phase.emit(*args)
        self.phase_ready.emit(self.scan_id, *args)

    def abort(self) -> None:
        self._abort_event.set()
        logger.info("ScannerRunnable: scan abort requested")

    @property
    def is_aborting(self) -> bool:
        return self._abort_event.is_set()

    def _probe_unit(self, unit: tuple) -> ProbeResult:
        ip, port = unit
        if self.is_aborting:
            return ProbeResult(ip, port, "tcp", "cancelled")
        return tcp_probe(ip, int(port), self._port_timeout)

    def ping_host(self, ip_address: str) -> tuple[bool, list[str], int | None, None]:
        """Проверить ICMP выбранным на весь запуск способом."""
        cancelled = self._abort_event.is_set
        if self._ping_backend == "icmplib":
            result = icmplib_probe(
                ip_address,
                self._ping_timeout_ms,
                cancelled,
                privileged=self._icmp_privileged,
            )
        else:
            result = system_probe(ip_address, self._ping_timeout_ms, cancelled)
        self._record_probe(result)
        if result.state == "error":
            raise RuntimeError(
                f"Ошибка ICMP-проверки {ip_address}: {result.diagnostic}"
            )
        return bool(result), ["icmp"] if result else [], result.ttl, None

    def _get_hostname(self, ip: str) -> str:
        """Обратный DNS с кэшем на сессию скана (синхронно, в задаче фазы «Имена»)."""
        with self._dns_lock:
            if ip in self._dns_cache:
                cached = self._dns_cache[ip]
                return cached if cached else ip
        name = _resolve_hostname_direct(ip)
        if not name and not self.is_aborting:
            probe, value = nbstat_probe(ip, cancelled=lambda: self.is_aborting)
            self._record_probe(probe)
            self._nb_names[ip] = value or (None, None)
            if value:
                name = value[0]
        if name:
            # PTR может содержать завершающую точку.
            name = name.strip().rstrip(".")
        with self._dns_lock:
            self._dns_cache[ip] = name
        return name if name else ip

    def _run_tasks(self, pool, items, fn, window, phase_name):
        """Координатор удерживает ограниченное окно задач до полного завершения."""
        source = iter(enumerate(items))
        pending = {}
        # Завершённые задачи приходят в очередь: опрос всего окна на каждую задачу
        # при большом окне занимал заметную долю GIL и мешал GUI.
        finished: queue.SimpleQueue[Any] = queue.SimpleQueue()
        exhausted = False
        try:
            while pending or not exhausted:
                while not exhausted and not self.is_aborting and len(pending) < window:
                    try:
                        index, item = next(source)
                    except StopIteration:
                        exhausted = True
                        break
                    host = item if isinstance(item, str) else item[0]
                    metadata = {
                        "type": phase_name,
                        "host": host,
                        "source": "application",
                    }
                    if (
                        isinstance(item, tuple)
                        and len(item) > 1
                        and isinstance(item[1], int)
                    ):
                        metadata["type"] = "port_probe"
                        metadata["phase"] = phase_name
                        metadata["port"] = item[1]
                    future = pool.submit(fn, item, metadata=metadata)
                    self._futures.add(future)
                    pending[future] = index
                    future.add_done_callback(finished.put)
                if self.is_aborting:
                    break
                if not pending:
                    continue
                try:
                    future = finished.get(timeout=0.05)
                except queue.Empty:
                    continue
                index = pending.pop(future)
                self._futures.discard(future)
                yield index, future.result()
        except Exception:
            # Ошибка пробы останавливает ожидание соседних ICMP-задач.
            self._abort_event.set()
            raise
        finally:
            # Ожидание идёт в координаторе, не в GUI. После ошибки остаёмся
            # владельцем сетевых задач до их полного завершения.
            for future in pending:
                future.cancel()
            while pending:
                try:
                    future = finished.get(timeout=0.05)
                except queue.Empty:
                    continue
                pending.pop(future, None)
                self._futures.discard(future)

    def _map_per_host(
        self, pool, items, fn, window, phase_name, total, get_active, on_done=None
    ):
        done = 0
        self._emit_phase(phase_name, 0, total, get_active())
        for index, result in self._run_tasks(
            pool, items, fn, max(1, window), phase_name
        ):
            if self.is_aborting:
                continue
            if on_done is not None:
                on_done(index, result)
            done += 1
            if done % max(1, total // 100) == 0 or done == total:
                self._emit_phase(phase_name, done, total, get_active())
        return []

    def _scan_ports_flat(
        self, pool, hosts_ports, phase_name, total_hosts, get_active, on_host=None
    ):
        # Одновременно живут не более N задач и их аккумуляторов.
        # Пустой список целей тоже завершается, не открывая сокет.
        result = {}
        remaining = {}
        open_ports = {}

        def units():
            for ip, ports in hosts_ports:
                ports = tuple(ports)
                remaining[ip] = max(1, len(ports))
                open_ports[ip] = []
                for port in ports or (None,):
                    yield ip, port

        def probe(unit):
            return (
                self._probe_unit(unit)
                if unit[1] is not None
                else (unit[0], None, False)
            )

        done = 0
        self._emit_phase(phase_name, 0, total_hosts, get_active())
        for _, response in self._run_tasks(
            pool, units(), probe, self.max_workers, phase_name
        ):
            if isinstance(response, ProbeResult):
                ip, port, is_open = response.host, response.port, bool(response)
                self._record_probe(response)
                if response.state == "error":
                    raise RuntimeError(
                        f"Локальная ошибка TCP-пробы {ip}:{port}: {response.diagnostic}"
                    )
            else:  # test/custom probe adapters
                ip, port, is_open = response
            if self.is_aborting:
                continue
            if is_open:
                open_ports[ip].append(port)
            remaining[ip] -= 1
            if remaining[ip] == 0:
                ports = sorted(open_ports.pop(ip))
                del remaining[ip]
                result[ip] = ports
                if on_host is not None:
                    on_host(ip, ports)
                done += 1
                if done % max(1, total_hosts // 100) == 0 or done == total_hosts:
                    self._emit_phase(phase_name, done, total_hosts, get_active())
        return result

    def _classify_ap(
        self,
        ip: str,
        open_set: set,
        ssh_banner: Optional[str],
        port_states: Optional[Dict[int, bool]] = None,
        http_server: Optional[str] = None,
    ) -> Optional[str]:
        """AP-детект по порт-паттерну + SSH-баннер (только порт-скан режим).

        Порты AP-сигнатур (22/80/443/11100/5900/53/23/161) к этому моменту
        уже пробиты: в порт-режиме — основным сканом, в ICMP-режиме —
        категорийными пробами стадии «Анализ». Открытые берутся из
        open_set, проверенные-закрытые — из port_states (результаты проб),
        и повторного tcp_ping больше не требуется (раньше до 8
        последовательных проб добавляли до ~8 сек на хост).
        """
        try:
            ap_ports: dict = {}
            for _ap_p in (22, 80, 443, 11100, 5900, 53, 23):
                if _ap_p in open_set:
                    ap_ports[_ap_p] = True
                elif port_states is not None and _ap_p in port_states:
                    ap_ports[_ap_p] = port_states[_ap_p]

            is_huawei_candidate = (
                ap_ports.get(22) and ap_ports.get(80) and ap_ports.get(443)
            )
            is_cisco_candidate = (
                ap_ports.get(22)
                and ap_ports.get(443) is False
                and ap_ports.get(11100) is False
            )
            if is_huawei_candidate or is_cisco_candidate:
                return classify_ap_by_ports_and_banner(
                    ap_ports, ssh_banner, None, http_server
                )
            return None
        except Exception as _ap_e:
            logger.debug(f"AP classify failed for {ip}: {_ap_e}")
            return None

    def execute(self) -> None:
        """Пинг/порты → имена → пробы → анализ, с межфазными барьерами.

        Один долгоживущий пул, не более max_workers Futures в полёте.
        Каждый запуск держит общую аренду до завершения всех своих задач,
        включая отмену и ошибки. Сетевые вызовы не создают вложенных пулов.
        """
        if not _SCANNER_RUN_LOCK.acquire(blocking=False):
            self.outcome = "error"
            self.failure = "Другой скан ещё выполняется."
            self.error.emit(self.failure)
            self._emit_outcome()
            self.finished.emit()
            return
        total = len(self.ips)
        try:
            pool = get_scanner_pool(self.max_workers, _for_scan=True)
            if not self.ips or self.is_aborting:
                return
            active = 0
            # Аккумулятор хоста: ports/ttl — фаза 1; hostname — фаза 2;
            # open_set/port_states — фаза 3. Фаза 4 только читает.
            hosts: Dict[str, Dict] = {}

            def _get_active() -> int:
                return active

            # ---- Фаза 1: пинг / порты ----
            if self.icmp:
                if self._ping_backend not in ("system", "icmplib"):
                    raise ValueError("Неизвестный способ ICMP-проверки")
                if self._ping_backend == "icmplib":
                    availability = check_icmplib()
                    if not availability.available:
                        raise RuntimeError(
                            f"Библиотека недоступна: {availability.reason}"
                        )
                    self._icmp_privileged = availability.privileged
                missing_ips = []

                def _on_ping(idx: int, res) -> None:
                    nonlocal active
                    ip = self.ips[idx]
                    ok, ports, ttl, _scanned = res if res else (False, [], None, None)
                    if ok:
                        active += 1
                        hosts[ip] = {
                            "ports": list(ports or []),
                            "ttl": ttl,
                            "hostname": ip,
                            "open_set": [],
                            "port_states": {},
                        }
                        self._emit_result(
                            ip, ip, True, list(ports or []), "", "", "", "scan"
                        )
                    elif self.combined:
                        missing_ips.append(ip)
                    else:
                        self._emit_result(ip, ip, False, [], "", "", "", "done")

                self._map_per_host(
                    pool,
                    self.ips,
                    self.ping_host,
                    max(1, self.max_workers),
                    "ping",
                    total,
                    _get_active,
                    _on_ping,
                )
                if self.combined and missing_ips and not self.is_aborting:
                    if not self.enabled_ports:
                        raise ValueError("Для ICMP + TCP выберите хотя бы один порт.")

                    def on_fallback(ip, ports):
                        nonlocal active
                        if ports:
                            active += 1
                            hosts[ip] = {
                                "ports": ports,
                                "ttl": None,
                                "hostname": ip,
                                "open_set": [],
                                "port_states": {},
                            }
                        self._emit_result(
                            ip,
                            ip,
                            bool(ports),
                            ports,
                            "",
                            "",
                            "",
                            "scan" if ports else "done",
                        )

                    self._scan_ports_flat(
                        pool,
                        ((ip, self.enabled_ports) for ip in missing_ips),
                        "tcp_discovery",
                        len(missing_ips),
                        _get_active,
                        on_fallback,
                    )
            else:
                enabled_ports = self.enabled_ports
                if not enabled_ports:
                    raise ValueError("Не выбраны порты для сканирования.")
                else:

                    def _on_found_port_host(ip: str, ok_ports: list) -> None:
                        nonlocal active
                        if ok_ports:
                            active += 1
                            hosts[ip] = {
                                "ports": list(ok_ports),
                                "ttl": None,
                                "hostname": ip,
                                "open_set": [],
                                "port_states": {},
                            }
                            self._emit_result(
                                ip, ip, True, list(ok_ports), "", "", "", "scan"
                            )
                        else:
                            self._emit_result(ip, ip, False, [], "", "", "", "done")

                    self._scan_ports_flat(
                        pool,
                        ((ip, enabled_ports) for ip in self.ips),
                        "ping",
                        total,
                        _get_active,
                        _on_found_port_host,
                    )
                    if self.is_aborting:
                        return
                    self._emit_phase("ping", total, total, active)

            if self.is_aborting:
                return
            online = list(hosts.keys())

            # ---- Фаза 2: имена (если включена) ----
            # Имена нужны импорту (name=hostname, host=IP) независимо от
            # галки «Определять тип устройства».
            if online and self.resolve_names:

                def _on_name(idx: int, res) -> None:
                    ip = online[idx]
                    if res and res != ip:
                        hosts[ip]["hostname"] = res
                        self._emit_result(
                            ip,
                            res,
                            True,
                            list(hosts[ip]["ports"]),
                            "",
                            "",
                            "",
                            "scan2",
                        )

                self._map_per_host(
                    pool,
                    online,
                    self._get_hostname,
                    max(1, self.max_workers),
                    "names",
                    len(online),
                    _get_active,
                    _on_name,
                )
                if self.is_aborting:
                    return

            if not self.classify_enabled and not self.resolve_mac:
                # Классификация выключена: финальный эмит с именами фазы 2.
                self._emit_phase("finish", 0, len(online), active)
                for ip in online:
                    self._emit_result(
                        ip,
                        hosts[ip].get("hostname") or ip,
                        True,
                        list(hosts[ip]["ports"]),
                        "",
                        "",
                        "",
                        "done",
                    )
                self._emit_phase("finish", len(online), len(online), active)
                return

            # ---- Фаза 3: категорийные пробы (только при use_port_signatures) ----
            # При выключенных портовых сигнатурах проб нет вообще: ни лишних
            # TCP-коннектов, ни эвента "ports" в статус-баре. Классификация
            # в фазе 4 идет только по hostname/MAC/TTL.
            if online and self.classify_enabled and self.use_port_signatures:
                targets = []
                for ip in online:
                    known = {
                        port
                        for (protocol, port) in self.probes.get(ip, {})
                        if protocol == "tcp"
                    }
                    # Адаптер может передать первичный результат без ProbeResult.
                    if not self.icmp:
                        known.update(self.enabled_ports)
                    targets.append(
                        (ip, [port for port in _ICMP_PROBE_PORTS if port not in known])
                    )

                def _on_probed(ip: str, open_ports: list) -> None:
                    primary_open = {p for p in hosts[ip]["ports"] if isinstance(p, int)}
                    hosts[ip]["open_set"] = sorted(primary_open | set(open_ports))
                    states = {}
                    for (protocol, port), result in self.probes.get(ip, {}).items():
                        if protocol == "tcp" and result.state in ("open", "closed"):
                            states[port] = result.state == "open"
                    hosts[ip]["port_states"] = states

                self._scan_ports_flat(
                    pool,
                    targets,
                    "ports",
                    len(online),
                    _get_active,
                    _on_probed,
                )
                if self.is_aborting:
                    return
                self._emit_phase("ports", len(online), len(online), active)

            if self.is_aborting:
                return

            # ---- Фаза 4: анализ ----
            if online:
                self._map_per_host(
                    pool,
                    [(ip, hosts[ip]) for ip in online],
                    self._finish_one,
                    max(1, self.max_workers),
                    "finish",
                    len(online),
                    _get_active,
                    None,
                )
        except Exception as exc:
            self.outcome = "error"
            self.failure = str(exc)
            self.error.emit(self.failure)
        finally:
            if self.outcome != "error":
                self.outcome = "cancelled" if self.is_aborting else "success"
            self._emit_outcome()
            _SCANNER_RUN_LOCK.release()
            try:
                self.finished.emit()
            except Exception:
                pass

    def _finish_one(self, item) -> Optional[str]:
        """Фаза 4 «Анализ» для одного хоста: последовательно MAC → факты → тип.

        Self-contained задача без саб-фьючей: порты уже пробиты в фазе 3
        и лежат в acc (open_set/port_states), DNS — в фазе 2. Здесь только
        короткие синхронные запросы (MAC, NetBIOS 0.6с, баннеры 1.5с) и
        CPU-классификация. При выключенных портовых сигнатурах
        (use_port_signatures=False) фаза 3 пропущена, open_set пуст,
        а NetBIOS/SSH/HTTP-опросы не выполняются. Финал — scan_finished(state="done").
        Возвращает ip (для runner'а).
        """
        ip, acc = item
        deadline = time.monotonic() + 6.0
        if self.is_aborting:
            return None
        hostname = acc.get("hostname") or ip
        ok_ports = list(acc.get("ports") or [])
        ttl = acc.get("ttl")

        mac_address = ""
        if self.resolve_mac and not self.is_aborting:
            mac_address = (
                get_mac_address(
                    ip, timeout=min(3.0, max(0.001, deadline - time.monotonic())),
                    stop_event=self._abort_event,
                )
                or ""
            )

        device_type = ""
        type_reason = ""
        if self.classify_enabled and not self.is_aborting:
            try:
                open_set: set = set()
                for _p in acc.get("open_set") or []:
                    try:
                        open_set.add(int(_p))
                    except Exception:
                        pass
                port_states: Dict[int, bool] = dict(acc.get("port_states") or {})
                ssh_banner = None
                http_server = None
                service_banners = ()

                # 1. NetBIOS Name Service (UDP 137): отвечает Windows
                #    и Samba. Дешёвый UDP-запрос, 0.6 сек таймаут.
                #    При выключенных сигнатурах не запрашивается вообще
                #    (вариант А: максимум скорости, тип только по hostname/MAC/TTL).
                netbios_name, netbios_suffix = self._nb_names.get(ip, (None, None))
                snmp_description = None
                if (
                    self.use_port_signatures
                    and not self.is_aborting
                    and time.monotonic() < deadline
                    and ip not in self._nb_names
                    and (self.udp_probes or 139 in open_set or 445 in open_set)
                ):
                    probe, value = nbstat_probe(
                        ip,
                        timeout=min(0.6, max(0.001, deadline - time.monotonic())),
                        cancelled=lambda: self.is_aborting,
                    )
                    self._record_probe(probe)
                    netbios_name, netbios_suffix = value or (None, None)
                    if netbios_name and hostname == ip and self.resolve_names:
                        hostname = netbios_name
                if (
                    self.udp_probes
                    and self.use_port_signatures
                    and not self.is_aborting
                    and time.monotonic() < deadline
                ):
                    probe, snmp_description = snmp_probe(
                        ip,
                        timeout=min(0.6, max(0.001, deadline - time.monotonic())),
                        cancelled=lambda: self.is_aborting,
                    )
                    self._record_probe(probe)

                # 2. SSH-баннер: Dropbear → embedded (роутер/AP),
                #    OpenSSH → Linux, Huawei/Cisco → AP.
                if (
                    self.use_port_signatures
                    and not self.is_aborting
                    and time.monotonic() < deadline
                    and 22 in open_set
                ):
                    ssh_banner = _fetch_ssh_banner(
                        ip,
                        22,
                        timeout=min(1.5, max(0.001, deadline - time.monotonic())),
                    )

                # 3. HTTP Server: embedded web → роутер/AP,
                #    вендоры принтеров/телефонов/камер/NAS, IIS → Windows.
                if (
                    self.use_port_signatures
                    and not self.is_aborting
                    and time.monotonic() < deadline
                    and 80 in open_set
                ):
                    http_server = _fetch_http_server_header(
                        ip,
                        80,
                        timeout=min(1.5, max(0.001, deadline - time.monotonic())),
                    )

                if (
                    self.use_port_signatures
                    and not self.is_aborting
                    and time.monotonic() < deadline
                ):
                    service_banners = fetch_equipment_banners(
                        ip,
                        open_set,
                        min(0.4, deadline - time.monotonic()),
                        self._abort_event,
                    )

                # 4. AP-детект (порт-паттерн + баннер) - приоритетный путь.
                # В ICMP-режиме порты AP-сигнатур пробиты фазой 3
                # (_ICMP_PROBE_PORTS), порт_states заполнен - логика та же.
                ap_type = None
                if self.use_port_signatures:
                    ap_type = self._classify_ap(
                        ip, open_set, ssh_banner, port_states, http_server
                    )

                if ap_type:
                    device_type = ap_type
                    type_reason = "порт-паттерн AP + баннер"
                else:
                    device_type, type_reason = classify_device_detailed(
                        open_set if self.use_port_signatures else [],
                        hostname or ip,
                        mac_address,
                        use_port_signatures=self.use_port_signatures,
                        ttl=ttl,
                        netbios_name=netbios_name,
                        netbios_suffix=netbios_suffix,
                        ssh_banner=ssh_banner,
                        http_server=http_server,
                        snmp_description=snmp_description,
                        service_banners=dict(service_banners),
                    )
            except Exception as _cls_e:
                logger.debug(f"Classify failed for {ip}: {_cls_e}")
                device_type = ""
                type_reason = ""

        if self.is_aborting:
            return None
        self._emit_result(
            ip,
            hostname or ip,
            True,
            ok_ports,
            mac_address,
            device_type,
            type_reason,
            "done",
        )
        return ip
