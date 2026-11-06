"""
Реестр заданий веб-сервера (JobManager) и хранилище сессий (SessionStore).

Задание (Job) живёт независимо от WebSocket-соединения: при отключении
клиента (refresh страницы) задание продолжает выполняться, прогресс
буферизуется в events. Новый клиент подписывается на job_id с указанием
последнего полученного seq — сервер досылает пропущенные события из
буфера, затем стримит живые.

SessionStore хранит выбранное множество хостов и привязку session_id →
username, чтобы восстановить состояние UI после refresh.

Потокобезопасность: JobManager и SessionStore используют RLock. asyncio-
очереди подписчиков создаются в event loop'е uvicorn — вызовы из рабочих
потоков идут через loop.call_soon_threadsafe.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import secrets
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from src.logger import logger


# Верхний лимит событий в буфере одного задания (deque maxlen). Защищает
# память при очень длинном выводе. При переполнении старые события
# затираются — клиент при reconnect с last_seq меньше первой записи в
# deque получит событие 'truncated'.
JOB_EVENT_BUFFER_MAX = 10000
# Ограничение журнала по числу записей не защищает от крупных фрагментов вывода.
JOB_PROGRESS_BUFFER_CHARS = 64 * 1024 * 1024
# Итоги всех хостов задания живут до конца TTL: сверх бюджета хранится только конец.
JOB_FINAL_OUTPUT_BUDGET_CHARS = 512 * 1024 * 1024
JOB_OVER_BUDGET_TAIL_CHARS = 64 * 1024


@dataclass
class JobEvent:
    """Одно событие выполнения задания (для replay + live stream)."""

    seq: int  # монотонный номер в рамках задания
    type: str  # start | device_start | command_start | progress | device_result | finished | aborted | error
    device_iid: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"seq": self.seq, "type": self.type}
        if self.device_iid is not None:
            out["device_iid"] = self.device_iid
        out.update(self.payload)
        return out


@dataclass
class Job:
    """Выполняемое/завершённое задание."""

    id: str
    owner: str  # username владельца задания
    task_iid: str  # iid задачи (персистентный, хранится в commands.json)
    device_iids: List[str]  # iid устройств
    params: Dict[str, Any]
    timeout: int
    created_at: float
    status: str = "pending"  # pending | running | finished | aborted | error
    finished_at: Optional[float] = None
    summary: Optional[Dict[str, Any]] = None
    events: deque = field(default_factory=lambda: deque(maxlen=JOB_EVENT_BUFFER_MAX))
    event_seq: int = 0
    # Живые подписчики: каждый — asyncio.Queue, в которую fan-out'ятся события.
    # Создаётся в event loop'е uvicorn (subscribe() — async).
    subscribers: Set["asyncio.Queue[JobEvent]"] = field(default_factory=set)
    # WebCommandExecutor, прерываемый через abort().
    executor: Any = None
    # Будущее результата _run_parallel — для ожидания завершения в GC.
    _run_future: Any = None
    _device_futures: set[Future[Any]] = field(default_factory=set, repr=False)
    _lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)
    truncated: bool = False  # буфер переполнился, часть истории утеряна
    final_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Индексы для ответа за O(хостов): журнал событий не сканируется.
    _first_start: dict[str, JobEvent] = field(default_factory=dict, repr=False)
    _result_seq: dict[str, int] = field(default_factory=dict, repr=False)
    _progress_chars: int = 0
    _final_chars: int = 0

    def append_event(
        self, evt_type: str, device_iid: Optional[str] = None, **payload
    ) -> JobEvent:
        """Добавить событие в буфер и fan-out подписчикам.

        Вызывается из рабочих потоков — fan-out через loop.call_soon_threadsafe
        выполняется вызывающей стороной (put_sync), здесь только буфер и
        синхронная часть. Подписчики получают событие через dispatch().
        """
        with self._lock:
            return self._append_event_locked(evt_type, device_iid, **payload)

    def _append_event_locked(
        self, evt_type: str, device_iid: Optional[str] = None, **payload
    ) -> JobEvent:
        self.event_seq += 1
        # job_id во ВСЕ события — клиенту нужно знать какому заданию принадлежит
        # событие при множественных viewer-WS (подхват нескольких заданий).
        payload.setdefault("job_id", self.id)
        if evt_type == "device_result" and device_iid is not None:
            self._apply_final_budget(device_iid, payload)
        evt = JobEvent(
            seq=self.event_seq, type=evt_type, device_iid=device_iid, payload=payload
        )
        # deque(maxlen) вытеснит первую запись молча: учесть её символы заранее.
        if len(self.events) == self.events.maxlen:
            self._progress_chars -= self._progress_size(self.events[0])
        self.events.append(evt)
        if evt_type == "progress":
            self._progress_chars += self._progress_size(evt)
            while self._progress_chars > JOB_PROGRESS_BUFFER_CHARS and len(self.events) > 1:
                self._progress_chars -= self._progress_size(self.events.popleft())
                self.truncated = True
        elif evt_type == "device_start" and device_iid is not None:
            self._first_start.setdefault(device_iid, evt)
        elif evt_type == "device_result" and device_iid is not None:
            # Итоги сохраняются отдельно от вытесняемого журнала прогресса.
            self.final_results[device_iid] = evt.to_dict()
            self._result_seq[device_iid] = evt.seq
        # Если deque переполнился — отмечаем (для уведомления клиента при reconnect).
        if (
            len(self.events) >= JOB_EVENT_BUFFER_MAX
            and self.event_seq > JOB_EVENT_BUFFER_MAX
        ):
            self.truncated = True
        return evt

    @staticmethod
    def _progress_size(evt: JobEvent) -> int:
        return len(evt.payload.get("chunk") or "") if evt.type == "progress" else 0

    def _apply_final_budget(self, device_iid: str, payload: dict[str, Any]) -> None:
        """Учесть итог хоста; при исчерпании бюджета сохранить только его конец."""
        previous = self.final_results.get(device_iid)
        if previous is not None:
            self._final_chars -= len(previous.get("output") or "")
        output = payload.get("output")
        if not isinstance(output, str):
            return
        if (
            self._final_chars + len(output) > JOB_FINAL_OUTPUT_BUDGET_CHARS
            and len(output) > JOB_OVER_BUDGET_TAIL_CHARS
        ):
            skipped = len(output) - JOB_OVER_BUDGET_TAIL_CHARS
            output = (
                f"[… пропущено {skipped} символов: исчерпан бюджет памяти задания …]\n"
                + output[-JOB_OVER_BUDGET_TAIL_CHARS:]
            )
            payload["output"] = output
        self._final_chars += len(output)

    def host_last_state(self, device_iid: str) -> Optional[Dict[str, Any]]:
        """Вернуть последнее событие для устройства (для снимка при reconnect).

        Используется при reconnect: если оригинальное событие было до disconnect
        (seq <= last_seq), клиент его не получит в replay — но статус устройства
        нужно знать (иначе карточка зависнет в «Восстановление»).
        Возвращает dict события или None.

        Логика:
          - Если есть device_result — вернуть его (устройство завершено, актуальный output).
          - Иначе вернуть device_start (статус running). Не progress/command_start:
            у них нет статуса, клиент не поймёт. Запись хранится отдельно от журнала,
            поэтому вытеснение не теряет состояние.
        """
        with self._lock:
            return self._host_last_state_locked(device_iid)

    def _host_last_state_locked(self, device_iid: str) -> dict[str, Any] | None:
        if device_iid in self.final_results:
            return dict(self.final_results[device_iid])
        start = self._first_start.get(device_iid)
        return start.to_dict() if start is not None else None


class JobManager:
    """Реестр заданий с TTL-очисткой завершённых.

    Потокобезопасный. Создаётся в create_app(), владеет глобальным пулом
    потоков команд (ThreadPoolExecutor) — все задания ставят свои задачи
    в этот пул, реальный лимит command_thread_count соблюдается при любой
    параллельности (в т.ч. между пользователями).
    """

    def __init__(
        self,
        command_pool: "Any",
        ttl_minutes: int = 30,
    ) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.RLock()
        self._command_pool = command_pool
        self._ttl_seconds = float(ttl_minutes * 60) if ttl_minutes > 0 else 0.0
        # event loop uvicorn — устанавливается извне (set_loop) для fan-out.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._gc_task: Optional[asyncio.Task] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Привязать event loop uvicorn (для создания очередей подписчиков)."""
        self._loop = loop

    def start_gc(self) -> None:
        """Запустить фоновую задачу очистки завершённых заданий по TTL."""
        if self._gc_task is not None:
            return
        if self._loop is None:
            logger.warning("JobManager: loop не установлен, GC не запущен")
            return
        self._gc_task = self._loop.create_task(self._gc_loop())

    async def _gc_loop(self) -> None:
        """Периодически удалять завершённые задания старше TTL."""
        while True:
            try:
                await asyncio.sleep(60)
                self.cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"JobManager: ошибка GC: {e}")

    def cleanup_expired(self) -> int:
        """Удалить завершённые задания старше TTL. Возвращает кол-во удалённых."""
        if self._ttl_seconds <= 0:
            return 0
        now = time.time()
        removed = 0
        with self._lock:
            to_remove = []
            for jid, job in self._jobs.items():
                if (
                    job.status in ("finished", "aborted", "error")
                    and job.finished_at is not None
                    and (job._run_future is None or job._run_future.done())
                ):
                    if (now - job.finished_at) > self._ttl_seconds:
                        to_remove.append(jid)
            for jid in to_remove:
                self._jobs.pop(jid, None)
                removed += 1
        if removed:
            logger.info(f"JobManager: GC удалил {removed} завершённых заданий")
        return removed

    def create_job(
        self,
        owner: str,
        task_iid: str,
        device_iids: List[str],
        params: Dict[str, Any],
        timeout: int,
        executor: Any,
    ) -> Job:
        """Создать новое задание (статус pending). Запуск выполняется отдельно."""
        jid = uuid.uuid4().hex
        job = Job(
            id=jid,
            owner=owner,
            task_iid=task_iid,
            device_iids=list(device_iids),
            params=dict(params),
            timeout=timeout,
            created_at=time.time(),
            executor=executor,
        )
        with self._lock:
            self._jobs[jid] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_active(self, owner: str) -> List[Job]:
        """Только активные (pending/running) задания владельца."""
        with self._lock:
            return [
                j
                for j in self._jobs.values()
                if j.owner == owner and j.status in ("pending", "running")
            ]

    def list_finished(self, owner: str) -> List[Job]:
        """Только завершённые (finished/aborted/error) задания владельца.

        Сортировка по убыванию finished_at (свежие первыми).
        Используется для replay результатов после refresh страницы,
        пока GC не вычистил задание по TTL.
        """
        with self._lock:
            out = [
                j
                for j in self._jobs.values()
                if j.owner == owner
                and j.status in ("finished", "aborted", "error")
                and j.finished_at is not None
            ]
        out.sort(key=lambda j: -j.finished_at)
        return out

    def submit_run(self, job: Job, run_fn: Callable, *args) -> "Any":
        """Schedule host operations without occupying a command worker.

        run_fn only submits work and returns its aggregate Future. It must not
        perform I/O or wait for host results in the calling event loop.
        """
        job.status = "running"
        try:
            future = run_fn(*args)
            if not isinstance(future, Future):
                raise TypeError("Job scheduler must return a Future")
        except Exception:
            job.status = "error"
            job.finished_at = time.time()
            raise
        job._run_future = future
        return future

    def abort(self, job_id: str) -> bool:
        """Прервать задание. Возвращает True если задание найдено.

        Немедленно кладёт событие ``aborted`` и fan-out'ит его подписчикам —
        клиент получает реакцию на нажатие «Отменить» без ожидания завершения
        уже запущенных SSH-команд (они прерываются через executor.abort(),
        но могут длиться ещё до таймаута).
        """
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.executor is not None:
            try:
                job.executor.abort()
            except Exception as e:
                logger.warning(f"JobManager: ошибка abort для {job_id}: {e}")
        with self._lock:
            already_finished = job.status in ("finished", "aborted", "error")
            if not already_finished:
                job.status = "aborted"
                job.finished_at = time.time()
                # Немедленно послать событие aborted подписчикам.
                evt = job.append_event("aborted")
            else:
                evt = None
        with job._lock:
            pending = tuple(job._device_futures)
        for future in pending:
            future.cancel()
        if evt is not None:
            self._dispatch(job, evt)
        return True

    def shutdown(self) -> None:
        """Cancel only this manager's jobs, leaving shared executors alive."""
        with self._lock:
            jobs = tuple(self._jobs.values())
        for job in jobs:
            if job.status in ("pending", "running"):
                self.abort(job.id)
        task = self._gc_task
        if task is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The owning server's loop has already stopped.

    def finalize(self, job: Job, results: List, summary: Dict[str, Any]) -> None:
        """Отметить задание завершённым и положить событие finished/aborted.

        Вызывается из run_fn после завершения _run_parallel.

        Если задание уже отменено (abort() вызван раньше) — событие aborted
        уже отправлено подписчикам в abort(); здесь только фиксируем итоги
        (без дублирования события). Иначе кладём finished.
        """
        with self._lock:
            if job.status == "aborted":
                # Уже отменено — событие aborted уже отправлено в abort().
                # Не дублируем. summary не сохраняем (задание прервано).
                return
            job.status = "finished"
            job.finished_at = time.time()
            job.summary = summary
            evt = job.append_event("finished", **{"summary": summary})
        self._dispatch(job, evt)

    def dispatch_event(self, job: Job, evt: JobEvent) -> None:
        """Fan-out события подписчикам (потокобезопасный).

        Вызывается из рабочих потоков через put_sync (call_soon_threadsafe),
        поэтому создание очереди/put_nowait происходит в event loop.
        """
        self._dispatch(job, evt)

    def _dispatch(self, job: Job, evt: JobEvent) -> None:
        """Синхронная часть fan-out. Подписчики — asyncio.Queue.

        Вызывается либо из event loop (subscribe/replay), либо из рабочих
        потоков через call_soon_threadsafe → put_nowait в очередь.
        """
        if not job.subscribers:
            return
        if self._loop is None:
            return

        # put_nowait потокобезопасен только в event loop'е; из рабочих
        # потоков вызываем через call_soon_threadsafe.
        def _put():
            for q in list(job.subscribers):
                try:
                    q.put_nowait(evt)
                except asyncio.QueueFull:
                    # Очередь подписчика переполнена — он отстаёт, дропаем.
                    logger.warning(
                        f"JobManager: очередь подписчика переполнена для {job.id}"
                    )

        try:
            self._loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass  # A server shutdown must not fail a finishing host operation.

    async def subscribe(
        self, job_id: str, maxsize: int = 1000
    ) -> Optional["asyncio.Queue[JobEvent]"]:
        """Подписаться на живые события задания.

        Возвращает asyncio.Queue, в которую будут приходить новые события.
        None если задание не найдено. Должна вызываться в event loop'е.
        """
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        q: "asyncio.Queue[JobEvent]" = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            job.subscribers.add(q)
        return q

    def unsubscribe(self, job_id: str, queue: "asyncio.Queue[JobEvent]") -> None:
        """Отписаться от событий. НЕ прерывает задание."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return
        with self._lock:
            job.subscribers.discard(queue)

    def events_since(
        self, job_id: str, last_seq: int, *, compact: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Получить события с seq > last_seq из буфера (для replay при reconnect).

        Возвращает {events: [...], truncated: bool, latest_seq: int,
                    status, summary, host_states} или None если задание не найдено.

        host_states: для каждого хоста — последнее известное состояние
        (device_start/device_result), даже если оно было до last_seq.
        Клиент применяет их чтобы карточки не зависали в «Восстановление».

        compact: для частого опроса. В host_states только итоги, вытесненные из
        журнала и ещё не полученные клиентом: иначе каждый ответ повторял бы
        вывод всех завершённых хостов.
        """
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        with job._lock:
            # seq идут подряд, поэтому хвост берётся с конца за O(пропущенных).
            missing = max(0, job.event_seq - last_seq)
            tail = list(itertools.islice(reversed(job.events), missing))
            tail.reverse()
            events = [e.to_dict() for e in tail]
            if compact:
                first_seq = job.events[0].seq if job.events else job.event_seq + 1
                host_states = {
                    diid: dict(job.final_results[diid])
                    for diid, seq in job._result_seq.items()
                    if last_seq < seq < first_seq
                }
            else:
                host_states = {}
                for diid in job.device_iids:
                    st = job._host_last_state_locked(diid)
                    if st is not None:
                        host_states[diid] = st
            return {
                "events": events,
                "truncated": job.truncated,
                "latest_seq": job.event_seq,
                "status": job.status,
                "summary": job.summary,
                "host_states": host_states,
            }

    def encode_events(
        self, job_id: str, last_seq: int, *, compact: bool = False
    ) -> Optional[bytes]:
        """Собрать ответ опроса в байтах; вызывается вне event loop.

        Сериализация большого ответа заняла бы цикл и остановила все соединения
        сервера, включая проксирование VNC. Кодируется по частям: C-кодировщик json
        держит GIL на весь вызов, и другой поток сам по себе цикл бы не освободил.
        """
        data = self.events_since(job_id, last_seq, compact=compact)
        if data is None:
            return None

        def encode(value: Any) -> bytes:
            return json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), default=str
            ).encode("utf-8")

        events = b",".join(encode(event) for event in data["events"])
        states = b",".join(
            encode(iid) + b":" + encode(state)
            for iid, state in data["host_states"].items()
        )
        rest = {
            key: value
            for key, value in data.items()
            if key not in ("events", "host_states")
        }
        return b"".join(
            (b'{"events":[', events, b'],"host_states":{', states, b"},", encode(rest)[1:])
        )


@dataclass
class SessionState:
    """Состояние сессии браузера (восстанавливается после refresh)."""

    id: str
    owner: str  # username владельца сессии
    selected_hosts: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


class SessionStore:
    """Хранилище сессий: session_id -> SessionState.

    TTL по last_seen (по умолчанию 24ч). Хранит выбранные хосты для
    восстановления после refresh.
    """

    def __init__(self, ttl_seconds: float = 24 * 3600) -> None:
        self._sessions: Dict[str, SessionState] = {}
        self._lock = threading.RLock()
        self._ttl_seconds = ttl_seconds

    def create(self, owner: str) -> SessionState:
        """Создать новую сессию и вернуть её."""
        sid = secrets.token_urlsafe(24)
        st = SessionState(id=sid, owner=owner)
        with self._lock:
            self._sessions[sid] = st
        return st

    def get(self, session_id: str) -> Optional[SessionState]:
        with self._lock:
            st = self._sessions.get(session_id)
            if st is not None:
                st.last_seen = time.time()
            return st

    def set_hosts(self, session_id: str, hosts: List[str]) -> bool:
        with self._lock:
            st = self._sessions.get(session_id)
            if st is None:
                return False
            st.selected_hosts = list(hosts)
            st.last_seen = time.time()
            return True
