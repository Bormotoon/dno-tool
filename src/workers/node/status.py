"""Индивидуальные таймеры статуса для хостов нод и менеджер таймеров.

NodeStatusTimer — зеркало HostPingTimer (src/workers/network/host_timer.py),
но «пинг» выполняется не локальным tcp_ping, а HTTP-запросом к ноде-владельцу
(``GET /api/node/status/{iid}`` — одиночный tcp_ping на стороне ноды).
Поведение идентично локальному пингу: интервал network.ping_interval (30с),
первый опрос через random 0–3000 мс после старта, далее джиттер ±10% после
каждого результата, _pending-dedup (не более одной задачи на хост).

NodeStatusManager — синглтон через фабрику get_node_status_manager()
(без __new__ — PySide6 segfault'ает на __new__-singleton QObject; см. AGENTS.md).
Владеет таймерами, эмитит единый сигнал status_updated(DeviceModel, bool).

NodeStatusBatcher объединяет запросы таймеров одной ноды в коротком окне. Таймеры
остаются индивидуальными: статусы появляются небольшими группами, а не одной
синхронной волной. Старая нода без capability status_batch опрашивается как раньше.

Пул — get_ping_worker_pool() (общий с локальными пингами): HTTP-запрос
короткий (timeout 3+5с), не должен конкурировать с захватами миниатюр.
"""

from concurrent.futures import Future
from dataclasses import dataclass, field
from PySide6.QtCore import QObject, QTimer, Signal
from typing import Any, Dict, Optional
import random
import time
import threading

from src.config import config
from src.domain.models.device import DeviceModel
from src.domain.device_identity import DeviceKey, device_identity, device_key
from src.logger import logger
from src.workers.gui_relay import post_to_gui


# Окно объединения запросов и лимит пакета, совпадает с лимитом маршрута ноды.
BATCH_WINDOW_MS = 2000
BATCH_MAX_IIDS = 512


@dataclass
class _NodeBatch:
    """Накопленные запросы одной ноды; не больше одного запроса к ней за раз."""

    node_service: Any
    pending: dict[str, list[Future[bool | None]]] = field(default_factory=dict)
    scheduled: bool = False
    in_flight: bool = False
    flushed_at: float = 0.0


class NodeStatusBatcher(QObject):
    """Объединить запросы статусов одной ноды. Вызывается только из GUI-потока."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._nodes: dict[str, _NodeBatch] = {}

    def request(self, node_service, node_iid: str, iid: str) -> Future[bool | None]:
        """Запросить статус; результат: True, False или None при ошибке API."""
        future: Future[bool | None] = Future()
        batch = self._nodes.get(node_iid)
        if batch is None:
            batch = self._nodes[node_iid] = _NodeBatch(node_service)
        batch.node_service = node_service
        batch.pending.setdefault(iid, []).append(future)
        self._schedule(node_iid, batch, BATCH_WINDOW_MS)
        return future

    def _schedule(self, node_iid: str, batch: _NodeBatch, delay: int) -> None:
        if batch.scheduled or batch.in_flight:
            return
        batch.scheduled = True
        QTimer.singleShot(delay, self, lambda: self._flush(node_iid))

    def _flush(self, node_iid: str) -> None:
        batch = self._nodes.get(node_iid)
        if batch is None:
            return
        batch.scheduled = False
        batch.flushed_at = time.monotonic()
        node_service = batch.node_service
        if not batch.pending:
            return
        iids = list(batch.pending)[:BATCH_MAX_IIDS]
        taken = {iid: batch.pending.pop(iid) for iid in iids}
        batch.in_flight = True

        def fetch() -> dict[str, bool | None]:
            client = node_service.get_client(node_iid)
            if client is None:
                # Нода недоступна → offline (паритет с недостижимым хостом).
                return dict.fromkeys(iids, False)
            return client.get_status_batch(iids)

        batcher_ref = self
        batch_finished = self._on_batch_finished

        def deliver(results: dict[str, bool | None]) -> None:
            # Выполняется в потоке пула: завершить Future и вернуть очередь в GUI.
            for iid, futures in taken.items():
                for waiter in futures:
                    waiter.set_result(results.get(iid))
            post_to_gui(batcher_ref, batch_finished, node_iid)

        from src.workers.network import get_ping_worker_pool

        try:
            get_ping_worker_pool().submit(
                fetch,
                on_complete=lambda _task, results: deliver(results),
                on_error=lambda _task, _error: deliver({}),
                metadata={
                    "type": "node_status",
                    "node_iid": node_iid,
                    "count": len(iids),
                },
            )
        except RuntimeError as error:
            logger.debug(f"NodeStatusBatcher: submit failed: {error}")
            deliver({})

    def _on_batch_finished(self, node_iid: str) -> None:
        batch = self._nodes.get(node_iid)
        if batch is None:
            return
        batch.in_flight = False
        if batch.pending:
            # Накопилось, пока шёл запрос. Окно считается от начала прошлого запроса:
            # иначе быстрые ответы превращали бы пакеты в запрос на каждый хост.
            elapsed_ms = (time.monotonic() - batch.flushed_at) * 1000
            self._schedule(node_iid, batch, max(0, int(BATCH_WINDOW_MS - elapsed_ms)))

    def discard_node(self, node_iid: str) -> None:
        """Забыть очередь ноды; ожидающие получают None, как при ошибке API."""
        batch = self._nodes.pop(node_iid, None)
        if batch is None:
            return
        for futures in batch.pending.values():
            for waiter in futures:
                waiter.set_result(None)


class NodeStatusTimer(QObject):
    """Per-host таймер запроса статуса хоста у ноды (зеркало HostPingTimer)."""

    MAX_CONCURRENT_PER_HOST = 1

    def __init__(
        self,
        device: DeviceModel,
        node_service,
        node_iid: str,
        on_result,
        parent: Optional[QObject] = None,
        batcher: NodeStatusBatcher | None = None,
    ):
        super().__init__(parent)
        self._device = device
        self._host = device.host
        self._batcher = batcher
        self._model_signature = self._signature(device)
        self._generation = 0
        self._svc = node_service
        self._node_iid = node_iid
        self._on_result = on_result

        # Интервал/таймаут/первая задержка — как у локального HostPingTimer
        # (network.ping_interval = 30с; первый запрос через 0–3с).
        self._interval = config.app.network.ping_interval * 1000
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._execute_status)
        self._timer.setInterval(self._interval)

        self._is_running = False
        self._pending = False
        self._lock = threading.Lock()

        logger.debug(
            f"NodeStatusTimer: Created for {self._host} (node={node_iid}, "
            f"interval={self._interval}ms)"
        )

    @property
    def host(self) -> str:
        return self._host

    @property
    def device(self) -> DeviceModel:
        return self._device

    @property
    def is_running(self) -> bool:
        return self._is_running

    def update_device(self, device: DeviceModel) -> None:
        """Обновить ссылку на актуальную DeviceModel (защита от рассинхрона
        при пересоздании элементов дерева — как HostPingTimerManager)."""
        signature = self._signature(device)
        if signature != self._model_signature:
            self._generation += 1
            self._model_signature = signature
        self._device = device
        self._host = device.host

    @staticmethod
    def _signature(device):
        return device_identity(device), device.host, device.port

    def reset_timer(self) -> None:
        if self._is_running:
            was_active = self._timer.isActive()
            self._timer.stop()
            if was_active:
                self._timer.start()

    def _get_jittered_interval(self) -> int:
        jitter = int(self._interval * 0.1)
        return self._interval + random.randint(-jitter, jitter)

    def _reschedule_with_jitter(self) -> None:
        self._timer.stop()
        self._timer.setInterval(self._get_jittered_interval())
        self._timer.start()

    def start(self, immediate: bool = True) -> None:
        if self._is_running:
            return
        self._is_running = True
        self._timer.start()
        if immediate:
            # Размазывание первого опроса — как у локального пинга
            # (HostPingTimer.start: random 0..3000 мс).
            initial_delay = random.randint(0, 3000)
            QTimer.singleShot(initial_delay, self._execute_status)

    def stop(self) -> None:
        self._is_running = False
        self._generation += 1
        self._timer.stop()

    def _execute_status(self) -> None:
        if not self._is_running:
            return

        with self._lock:
            if self._pending:
                logger.debug(
                    f"NodeStatusTimer: Previous status still pending for {self._host}, skipping"
                )
                return
            self._pending = True

        if (
            self._batcher is not None
            and self._svc is not None
            and self._svc.supports_status_batch(self._node_iid)
        ):
            self._request_batched(self._batcher)
            return

        try:
            from src.workers.network import get_ping_worker_pool

            pool = get_ping_worker_pool()
        except Exception as e:
            logger.error(f"NodeStatusTimer: Cannot get ping pool for {self._host}: {e}")
            with self._lock:
                self._pending = False
            return

        node_iid = self._node_iid
        iid = self._device.iid
        svc = self._svc
        timer_ref = self
        handle_result = self._handle_status_result
        generation = self._generation

        def status_fn():
            # Клиент достаём на каждый вызов: клиент может быть пересоздан
            # (_nodes_reload_sections), таймер живёт дольше.
            try:
                current = svc
                if current is None:
                    from src.di import get_container
                    from src.services import NodeService

                    current = get_container().resolve(NodeService)
                client = current.get_client(node_iid) if current else None
            except Exception:
                client = None
            if client is None:
                return (
                    False  # нода недоступна → offline (паритет с недостижимым хостом)
                )
            try:
                return client.get_status(iid)
            except Exception:
                return None

        def on_complete(task_id, result):
            post_to_gui(timer_ref, handle_result, generation, result)

        def on_error(task_id, error):
            post_to_gui(timer_ref, handle_result, generation, None)

        try:
            pool.submit(
                status_fn,
                task_id=f"node_status_{node_iid}_{iid}_{int(time.time() * 1000)}",
                on_complete=on_complete,
                on_error=on_error,
                metadata={
                    "host": self._host,
                    "type": "node_status",
                    "node_iid": self._node_iid,
                    "device": self._device.name or self._host,
                },
            )
        except Exception as e:
            logger.debug(f"NodeStatusTimer: submit failed for {self._host}: {e}")
            with self._lock:
                self._pending = False

    def _request_batched(self, batcher: NodeStatusBatcher) -> None:
        """Запрос через общий пакет ноды; итог обрабатывается как одиночный."""
        timer_ref = self
        handle_result = self._handle_status_result
        generation = self._generation

        def on_done(future: Future[bool | None]) -> None:
            post_to_gui(timer_ref, handle_result, generation, future.result())

        batcher.request(self._svc, self._node_iid, self._device.iid).add_done_callback(
            on_done
        )

    def _handle_status_result(self, generation: int, is_online: bool | None) -> None:
        with self._lock:
            self._pending = False

        if not self._is_running:
            return
        if generation != self._generation or is_online is None:
            self._reschedule_with_jitter()
            return

        self._device.set_online(is_online)

        try:
            self._on_result(self._device, is_online)
        except Exception:
            pass

        logger.debug(
            f"NodeStatusTimer: {self._host} -> {'ONLINE' if is_online else 'OFFLINE'}"
        )

        self._reschedule_with_jitter()


class NodeStatusManager(QObject):
    """Глобальный менеджер таймеров статуса хостов нод (singleton через фабрику).

    Зеркало HostPingTimerManager: ref counting (start_ping/stop_ping),
    сигнал status_updated(DeviceModel, bool) для подписчиков UI.
    Дополнительно sync_node(node_iid, {iid: device}) — дифф-синк после
    массовых операций («отметить все», пересборка секции poll-циклом):
    нужные хосты получают ref=1 (таймер запускается, если не работал),
    лишние останавливаются.
    """

    status_updated = Signal(DeviceModel, bool)

    _initialized: bool = False

    def __init__(self, parent: Optional[QObject] = None):
        if NodeStatusManager._initialized:
            return
        NodeStatusManager._initialized = True
        super().__init__(parent)
        self._timers: Dict[DeviceKey, NodeStatusTimer] = {}
        self._ref_counts: Dict[DeviceKey, int] = {}
        self._batcher = NodeStatusBatcher(self)
        logger.debug("NodeStatusManager: Created (singleton)")

    def start_ping(self, device: DeviceModel) -> None:
        """Подписаться на статусы устройства (ref counting)."""
        iid = device_identity(device)
        niid = getattr(device, "node_iid", None)
        if not niid or niid == "local":  # LOCAL_NODE_IID — lazy-строка
            return
        if iid in self._timers:
            self._ref_counts[iid] += 1
            logger.debug(
                f"NodeStatusManager: start_ping reuse for {iid} (refs={self._ref_counts[iid]})"
            )
            timer = self._timers[iid]
            timer.update_device(device)
            if not timer.is_running:
                timer.start(immediate=True)
            return
        timer = NodeStatusTimer(
            device=device,
            node_service=self._resolve_node_service(),
            node_iid=niid,
            on_result=self._on_timer_result,
            parent=self,
            batcher=self._batcher,
        )
        self._timers[iid] = timer
        self._ref_counts[iid] = 1
        timer.start(immediate=True)
        logger.debug(f"NodeStatusManager: start_ping created for {iid}")

    def stop_ping(self, device_iid: str, node_iid: str) -> None:
        device_iid = device_key(device_iid, node_iid)
        if device_iid not in self._ref_counts:
            return
        self._ref_counts[device_iid] -= 1
        if self._ref_counts[device_iid] <= 0:
            del self._ref_counts[device_iid]
            t = self._timers.pop(device_iid, None)
            if t is not None:
                t.stop()
                t.deleteLater()
                logger.debug(
                    f"NodeStatusManager: stop_ping removed timer for {device_iid} (no more refs)"
                )
        else:
            logger.debug(
                f"NodeStatusManager: stop_ping released ref for {device_iid} (refs={self._ref_counts[device_iid]})"
            )

    def sync_node(self, node_iid: str, devices: Dict[str, DeviceModel]) -> None:
        """Дифф-синк таймеров ноды: devices — актуальный набор отмеченных хостов.

        - Для каждого iid из devices: если таймера нет — создать/запустить;
          если есть — обновить DeviceModel и нормализовать ref в 1.
        - Таймеры, не входящие в devices (снята галочка, скрыта нода,
          пересборка секции) — остановить и удалить.
        Вызывается из GUI-потока (MainWindow._sync_node_checked_iids).
        """
        desired = {device_key(iid, node_iid): dev for iid, dev in (devices or {}).items()}
        for iid, dev in desired.items():
            timer = self._timers.get(iid)
            if timer is not None:
                timer.update_device(dev)
                self._ref_counts[iid] = 1
                if not timer.is_running:
                    timer.start(immediate=True)
            else:
                t = NodeStatusTimer(
                    device=dev,
                    node_service=self._resolve_node_service(),
                    node_iid=node_iid,
                    on_result=self._on_timer_result,
                    parent=self,
                    batcher=self._batcher,
                )
                self._timers[iid] = t
                self._ref_counts[iid] = 1
                t.start(immediate=True)
        for iid in list(self._timers.keys()):
            if self._timers[iid]._node_iid == node_iid and iid not in desired:
                t = self._timers.pop(iid)
                t.stop()
                t.deleteLater()
                self._ref_counts.pop(iid, None)

    def stop_all(self) -> None:
        for t in self._timers.values():
            t.stop()
            t.deleteLater()
        self._timers.clear()
        self._ref_counts.clear()
        for node_iid in list(self._batcher._nodes):
            self._batcher.discard_node(node_iid)
        logger.debug("NodeStatusManager: Stopped all timers")

    def stop_node(self, node_iid: str) -> None:
        """Остановить таймеры всех хостов одной ноды (скрытие/остановка ноды)."""
        doomed = [
            iid
            for iid, t in self._timers.items()
            if getattr(t, "_node_iid", None) == node_iid
        ]
        for iid in doomed:
            t = self._timers.pop(iid)
            t.stop()
            t.deleteLater()
            self._ref_counts.pop(iid, None)
        self._batcher.discard_node(node_iid)
        if doomed:
            logger.debug(
                f"NodeStatusManager: stopped {len(doomed)} timer(s) for node {node_iid}"
            )

    @staticmethod
    def _resolve_node_service():
        try:
            from src.di import get_container
            from src.services import NodeService

            return get_container().resolve_optional(NodeService)
        except Exception:
            return None

    def _on_timer_result(self, device: DeviceModel, is_online: bool) -> None:
        self.status_updated.emit(device, is_online)

    @property
    def active_count(self) -> int:
        return len(self._timers)


_node_status_manager: Optional[NodeStatusManager] = None


def get_node_status_manager() -> NodeStatusManager:
    """Глобальный singleton NodeStatusManager (factory; без __new__)."""
    global _node_status_manager
    if _node_status_manager is None:
        _node_status_manager = NodeStatusManager()
    return _node_status_manager


def reset_node_status_manager() -> None:
    """Сброс singleton (тест-хук)."""
    global _node_status_manager
    if _node_status_manager is not None:
        _node_status_manager.stop_all()
    _node_status_manager = None
    NodeStatusManager._initialized = False
