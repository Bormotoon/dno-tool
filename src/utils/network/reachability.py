"""Массовые TCP-проверки доступности в одном потоке: неблокирующий connect и selector.

Ожидание ответа не занимает потоков. Настройка потоков сети ограничивает число
одновременных соединений так же, как раньше ограничивала потоки: сверх лимита
проверки ждут в очереди, она видна в мониторе пулов.

Очереди групп обслуживаются по кругу: фоновый опрос сотен недоступных хостов не
задерживает статусы для нод, веб-проверки и камеры. Лимит при этом общий.

Колбэки Future выполняются в потоке пробера: внутри допустимы только короткие
действия, например отправка сигнала. Медленный колбэк задержит все проверки.
"""

from __future__ import annotations

import errno
import heapq
import itertools
import os
import selectors
import socket
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, cast

from src.logger import logger
from src.utils.network.dns_resolver import (
    Address,
    DnsBusyError,
    DnsResolver,
    numeric_endpoint,
)
from src.utils.system.pool_registry import register_pool, unregister_pool

# Ожидание DNS не входит в таймаут соединения, но не должно быть бесконечным.
DNS_TIMEOUT_SEC = 10.0
_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, 10035}
_LABEL = "Проверки доступности (TCP, один поток)"


def _default_max_inflight() -> int:
    """Лимит равен настройке потоков сети; применяется при запуске, как у общих пулов.

    Сверх лимита проверки ждут в очереди. На POSIX учитывается лимит дескрипторов.
    """
    from src.config import config

    limit = max(1, config.app.network.thread_count)
    if os.name != "posix":
        return limit
    import resource

    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY:
        return limit
    return max(1, min(limit, soft // 4))


@dataclass(eq=False)
class _Probe:
    host: str
    port: int
    timeout: float
    future: Future[bool]
    metadata: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    deadline: float = 0.0
    sock: socket.socket | None = None
    group: str = ""
    finished: bool = False


class ReachabilityProber:
    """TCP-проверки в одном потоке; результат совпадает с connect_ex(...) == 0."""

    accepts_metadata = True

    def __init__(self, max_inflight: int | None = None) -> None:
        self._max_inflight = max(
            1, _default_max_inflight() if max_inflight is None else max_inflight
        )
        # Очередь вмещает все проверки: иначе имя получило бы ложное «недоступен».
        self._resolver = DnsResolver(
            "reachability-dns",
            workers=min(4, self._max_inflight),
            queue_size=self._max_inflight,
        )
        self._lock = threading.Lock()
        self._queues: OrderedDict[str, deque[_Probe]] = OrderedDict()
        self._resolved: deque[tuple[_Probe, list[Address] | None]] = deque()
        self._active: dict[int, _Probe] = {}
        self._deadlines: list[tuple[float, int, _Probe]] = []
        self._sequence = itertools.count()
        self._stats = {"submitted": 0, "completed": 0, "failed": 0, "cancelled": 0}
        self._closed = False
        self._thread: threading.Thread | None = None
        self._selector: selectors.BaseSelector | None = None
        self._wake_reader: socket.socket | None = None
        self._wake_writer: socket.socket | None = None

    @property
    def label(self) -> str:
        return _LABEL

    def probe(
        self,
        host: str,
        port: int,
        timeout: float,
        *,
        metadata: dict[str, Any] | None = None,
        group: str | None = None,
    ) -> Future[bool]:
        """Проверить порт; любая ошибка даёт False, исключения не возникают.

        group — очередь, которая делит соединения с другими по кругу; по умолчанию
        тип задачи из metadata.
        """
        # Пакет src.workers тянет Qt: модулю утилит он нужен только при вызове.
        from src.workers.task_metadata import task_metadata

        future: Future[bool] = Future()
        info = task_metadata(metadata)
        try:
            probe = _Probe(
                host,
                int(port),
                float(timeout),
                future,
                info,
                group=group or str(info.get("type") or ""),
            )
        except (TypeError, ValueError):
            future.set_result(False)
            return future
        with self._lock:
            if self._closed:
                future.set_result(False)
                return future
            self._stats["submitted"] += 1
            self._queues.setdefault(probe.group, deque()).append(probe)
            self._ensure_thread_locked()
        self._wake()
        return future

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None:
            return
        self._selector = selectors.DefaultSelector()
        self._wake_reader, self._wake_writer = socket.socketpair()
        self._wake_reader.setblocking(False)
        self._wake_writer.setblocking(False)
        self._selector.register(self._wake_reader, selectors.EVENT_READ)
        self._thread = threading.Thread(
            target=self._run, name="dno-reachability", daemon=True
        )
        self._thread.start()

    def _wake(self) -> None:
        writer = self._wake_writer
        if writer is None:
            return
        try:
            writer.send(b"\0")
        except OSError:
            pass  # Буфер полон, значит поток и так будет разбужен.

    def close(self, wait: bool = True) -> None:
        """Остановить поток; незавершённые проверки получают False."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        self._wake()
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _run(self) -> None:
        try:
            while not self._closed:
                self._start_queued()
                self._connect_resolved()
                self._wait_for_events()
                self._expire()
        except Exception:  # noqa: BLE001 - проверки не должны зависнуть без ответа
            logger.exception("ReachabilityProber: сбой цикла проверок")
            with self._lock:
                self._closed = True
        finally:
            self._drain()

    def _next_queued_locked(self) -> _Probe:
        """Взять проверку из следующей по кругу группы."""
        group, queue = next(iter(self._queues.items()))
        probe = queue.popleft()
        if queue:
            self._queues.move_to_end(group)
        else:
            del self._queues[group]
        return probe

    def _start_queued(self) -> None:
        while True:
            with self._lock:
                if len(self._active) >= self._max_inflight or not self._queues:
                    return
                probe = self._next_queued_locked()
                if not probe.future.set_running_or_notify_cancel():
                    self._stats["cancelled"] += 1
                    continue
                self._active[id(probe)] = probe
            probe.started_at = time.time()
            self._begin(probe)

    def _set_deadline(self, probe: _Probe, seconds: float) -> None:
        """Назначить срок; запись с прежним сроком _expire пропустит."""
        probe.deadline = time.monotonic() + seconds
        heapq.heappush(self._deadlines, (probe.deadline, next(self._sequence), probe))

    def _begin(self, probe: _Probe) -> None:
        try:
            addresses = numeric_endpoint(probe.host, probe.port)
        except (OSError, ValueError):
            self._finish(probe, False)
            return
        if addresses is not None:
            self._connect(probe, addresses)
            return
        # Таймаут относится только к соединению, как у connect_ex в tcp_ping.
        # Иначе медленный DNS показывал бы работающий хост недоступным.
        self._set_deadline(probe, DNS_TIMEOUT_SEC)

        def resolved(found: list[Address] | None, _error: OSError | None) -> None:
            with self._lock:
                self._resolved.append((probe, found))
            self._wake()

        try:
            self._resolver.resolve_async(
                probe.host, probe.port, probe.deadline, resolved
            )
        except DnsBusyError:
            self._finish(probe, False)

    def _thread_selector(self) -> selectors.BaseSelector:
        """Селектор создаётся до запуска потока, а снимается только в его _drain."""
        if self._selector is None:
            raise RuntimeError("Селектор пробера уже закрыт")
        return self._selector

    def _connect_resolved(self) -> None:
        while True:
            with self._lock:
                if not self._resolved:
                    return
                probe, addresses = self._resolved.popleft()
            if not probe.finished:
                self._connect(probe, addresses or [])

    def _connect(self, probe: _Probe, addresses: list[Address]) -> None:
        # Первый адрес с предпочтением IPv4; полный перебор адресов не выполняется.
        usable = [
            item for item in addresses if item[0] in (socket.AF_INET, socket.AF_INET6)
        ]
        usable.sort(key=lambda item: item[0] != socket.AF_INET)
        if not usable:
            self._finish(probe, False)
            return
        family, kind, protocol, _name, endpoint = usable[0]
        self._set_deadline(probe, probe.timeout)
        sock = None
        try:
            sock = socket.socket(family, kind, protocol)
            sock.setblocking(False)
            code = sock.connect_ex(endpoint)
        except OSError:
            if sock is not None:
                sock.close()
            self._finish(probe, False)
            return
        probe.sock = sock
        if code == 0:
            self._finish(probe, True)
        elif code in _IN_PROGRESS:
            try:
                self._thread_selector().register(sock, selectors.EVENT_WRITE, probe)
            except (OSError, ValueError):
                # Сбой одного сокета не должен останавливать все проверки.
                self._finish(probe, False)
        else:
            self._finish(probe, False)

    def _wait_for_events(self) -> None:
        now = time.monotonic()
        timeout = 1.0
        while self._deadlines and self._deadlines[0][2].finished:
            heapq.heappop(self._deadlines)
        if self._deadlines:
            timeout = min(timeout, max(0.0, self._deadlines[0][0] - now))
        with self._lock:
            # Ждать незачем, если есть свободные места и задачи в очереди.
            if self._resolved or (
                self._queues and len(self._active) < self._max_inflight
            ):
                timeout = 0.0
        for key, _events in self._thread_selector().select(timeout):
            # Зарегистрированы только сокеты: пробуждающий и сокеты проверок.
            sock = cast(socket.socket, key.fileobj)
            if sock is self._wake_reader:
                try:
                    while sock.recv(4096):
                        pass
                except OSError:
                    pass
                continue
            probe = key.data
            try:
                ok = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0
            except OSError:
                ok = False
            self._finish(probe, ok)

    def _expire(self) -> None:
        now = time.monotonic()
        while self._deadlines and self._deadlines[0][0] <= now:
            deadline, _order, probe = heapq.heappop(self._deadlines)
            if not probe.finished and deadline == probe.deadline:
                self._finish(probe, False)

    def _finish(self, probe: _Probe, ok: bool) -> None:
        if probe.finished:
            return
        probe.finished = True
        sock, probe.sock = probe.sock, None
        if sock is not None:
            try:
                self._thread_selector().unregister(sock)
            except (KeyError, ValueError):
                pass
            sock.close()
        with self._lock:
            self._active.pop(id(probe), None)
            self._stats["completed"] += 1
        probe.future.set_result(ok)

    def _drain(self) -> None:
        """Завершить всё незавершённое и освободить сокеты."""
        with self._lock:
            queued = [probe for queue in self._queues.values() for probe in queue]
            self._queues.clear()
            active = list(self._active.values())
        for probe in active:
            self._finish(probe, False)
        for probe in queued:
            if probe.future.set_running_or_notify_cancel():
                probe.future.set_result(False)
        selector, self._selector = self._selector, None
        if selector is not None:
            selector.close()
        for sock in (self._wake_reader, self._wake_writer):
            if sock is not None:
                sock.close()
        self._wake_reader = self._wake_writer = None

    def get_snapshot(self) -> dict[str, Any]:
        """Снимок в формате пулов для монитора."""
        now = time.time()
        with self._lock:
            active = list(self._active.values())
            queued = [probe for queue in self._queues.values() for probe in queue]
            stats = dict(self._stats)

        def row(probe: _Probe, state: str) -> dict[str, Any]:
            since = (
                probe.started_at if probe.started_at is not None else probe.created_at
            )
            return {
                "task_id": str(id(probe)),
                "fn": "probe",
                "metadata": dict(probe.metadata),
                "state": state,
                "duration": max(0.0, now - since),
            }

        tasks = [row(probe, "running") for probe in active]
        tasks += [row(probe, "pending") for probe in queued]
        tasks.sort(key=lambda task: (task["state"] != "running", -task["duration"]))
        return {
            "stats": {
                "label": _LABEL,
                "max_workers": self._max_inflight,
                "limit_unit": "соединений",
                "active": len(active),
                "pending": len(queued),
                "total_tasks": stats["submitted"],
                **stats,
            },
            "tasks": tasks,
        }


_factory_lock = threading.Lock()
_prober: ReachabilityProber | None = None


def get_reachability_prober() -> ReachabilityProber:
    """Вернуть общий пробер процесса; создаётся при первом обращении."""
    global _prober
    with _factory_lock:
        if _prober is None:
            _prober = ReachabilityProber()
            register_pool(_prober)
        return _prober


def shutdown_reachability_prober() -> None:
    """Закрыть пробер при выходе; новые проверки получают False, поток не создаётся."""
    global _prober
    with _factory_lock:
        if _prober is None:
            _prober = ReachabilityProber()
        prober = _prober
    prober.close()
    unregister_pool(prober)
