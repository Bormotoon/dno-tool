"""NodeClientWorker — клиент федерации нод (QThread).

На каждую доверенную ноду (NodeRecord) создаётся один worker, который:
1. Выполняет handshake (challenge-response) и кэширует сессионный токен.
2. Периодически запрашивает дерево хостов (GET /api/node/hosts).
3. Предоставляет синхронные методы для one-shot операций (статусы,
   миниатюры, выполнение команд, CRUD, Veyon-фичи, host_info, VNC-url) —
   вызываются из пула потоков оператора. Статусы хостов — per-host таймеры
   (NodeStatusTimer, src/workers/node/status.py) дергают get_status(iid)
   с тем же каденсом, что локальный пинг (network.ping_interval).

Сигналы (cross-thread → GUI через QueuedConnection):
- hosts_updated(node_iid, tree: list)          — дерево хостов изменилось.
- node_online(node_iid, bool)                  — доступность ноды.

Используется MainWindow через NodeService: сервис хранит реестр клиентов
и маршрутизирует сигналы.
"""

import random
import hashlib
import json
from functools import wraps
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from PySide6.QtCore import QThread, Signal

from src.config import COMMAND_OUTPUT_HEAD_CHARS, COMMAND_OUTPUT_TAIL_CHARS, config
from src.utils.security.node_transport import NodeHTTPSession, node_base_url
from src.utils.text.output_stream import BoundedOutput
from src.workers.node.session_pool import NodeSessionPool
from src.logger import logger
from src.services.thumbnails.types import ThumbnailResult, ThumbnailStatus


def _node_operation(method):
    """Удержать HTTP-сессию на время операции и вернуть её в набор клиента."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        depth = getattr(self._tls, "depth", 0)
        self._tls.depth = depth + 1
        try:
            return method(self, *args, **kwargs)
        finally:
            self._tls.depth = depth
            if depth == 0:
                session = getattr(self._tls, "session", None)
                if session is not None:
                    del self._tls.session
                    broken = getattr(self._tls, "broken", False)
                    self._tls.broken = False
                    self._sessions.release(session, broken=broken)
    return wrapped


class NodeClientWorker(QThread):
    """Worker-клиент для одной ноды."""

    # Cross-thread сигналы в GUI
    hosts_updated = Signal(str, list)  # (node_iid, tree)
    node_online = Signal(str, bool)  # (node_iid, reachable)
    node_name_updated = Signal(str, str)  # (node_iid, name from hello)

    def __init__(
        self,
        node_iid: str,
        host: str,
        port: int,
        pubkey: str,
        node_service,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._node_iid = node_iid
        self._host = host
        self._port = int(port or 8801)
        self._pubkey = pubkey
        self._svc = node_service
        self._stop_flag = False
        self._token: Optional[str] = None
        self._base = node_base_url(self._host, self._port)
        # HTTP-слой без конвоя: requests.Session НЕ потокобезопасна, поэтому
        # одновременно сессией пользуется один поток:
        # 1) _poll_session — только QThread run()-цикл (poll hosts),
        #    доступ только из одного потока → lock не нужен.
        # 2) Набор _sessions для one-shot операций (миниатюры, фичи, execute,
        #    host_info, vnc_url, CRUD): поток берёт сессию на время операции.
        #    Общая сессия с блокировкой превращалась в конвой: пока veyon_feature
        #    держала lock до 60с, все миниатюры копились в очереди. Закрывать сессию
        #    после операции нельзя: каждая миниатюра платила бы за новое соединение.
        #    Авторизация через заголовок (не cookie), редиректы не используются,
        #    поэтому состояние сессии между операциями не переносится.
        self._poll_session = None
        self._sessions = NodeSessionPool(lambda: NodeHTTPSession(self._pubkey))
        self._hosts_etag = ""
        self._hosts_digest = ""
        self._tls = threading.local()
        self._last_hosts_at = 0.0
        # Handshake-токен защищён lock'ом: его запрашивают и run()-цикл,
        # и one-shot операции из pool-воркеров (get_status/get_thumbnail)
        # — иначе первые секунды после отметки хоста дают ложный
        # offline/пустой thumbnail, пока handshake не завершился.
        self._token_lock = threading.Lock()

    # ---------- жизненный цикл ----------

    def stop(self) -> None:
        self._stop_flag = True
        # Закрытие без сетевого ожидания: сокеты просто закрываются.
        self._sessions.close()

    def run(self) -> None:
        """Главный цикл: handshake → периодический опрос дерева хостов."""
        poll = max(2, int(getattr(config.app.nodes, "poll_interval_sec", 5) or 5))
        self._poll_session = NodeHTTPSession(self._pubkey)
        try:
            self._run_poll(poll)
        finally:
            self._poll_session.close()
            self._poll_session = None
        logger.debug(f"NodeClient[{self._node_iid}]: остановлен")

    def _run_poll(self, poll: int) -> None:
        while not self._stop_flag:
            try:
                if not self._ensure_token():
                    self._set_node_reachable(False)
                    self._sleep(poll * 3)  # backoff при недоступности
                    continue

                # Дерево хостов — не чаще раза в 5 poll-циклов или по запросу.
                now = time.time()
                if now - self._last_hosts_at >= poll * 5 or self._last_hosts_at == 0:
                    reachable = self._poll_hosts()
                    self._last_hosts_at = now if reachable else 0.0
                    self._set_node_reachable(reachable)
            except Exception as e:
                logger.debug(f"NodeClient[{self._node_iid}]: {e}")
                self._set_node_reachable(False)
            self._sleep(poll)


    # ---------- handshake ----------

    def _ensure_token(self) -> bool:
        """Убедиться, что есть валидный токен (handshake при необходимости).

        Потокобезопасно (run()-цикл + one-shot вызовы из pool-воркеров).
        """
        if self._stop_flag or self._svc.get_client(self._node_iid) is not self:
            return False
        if self._token:
            return True
        with self._token_lock:
            if self._stop_flag or self._svc.get_client(self._node_iid) is not self:
                return False
            if self._token:
                return True
            ok, msg, info = self._svc.start_handshake(self._node_iid, client=self)
            if not ok:
                logger.debug(f"NodeClient[{self._node_iid}] handshake: {msg}")
                return False
            if self._stop_flag:
                return False
            self._token = (info or {}).get("token")
            name = (info or {}).get("name") or ""
            if name:
                self.node_name_updated.emit(self._node_iid, name)
            return bool(self._token)

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Node {self._token}"} if self._token else {}

    @_node_operation
    def check_connection(self) -> tuple[bool, str]:
        """Проверить рабочую сессию, не аннулируя её новым handshake."""
        if not self._ensure_token():
            return False, "Не удалось подключиться к ноде"
        response = self._http_get("/api/node/hosts", headers=self._headers(), timeout=8)
        if response.status_code in (401, 403):
            self._token = None
            if self._ensure_token():
                response = self._http_get(
                    "/api/node/hosts", headers=self._headers(), timeout=8
                )
        ok = response.status_code == 200 and isinstance(response.json(), list)
        return ok, "Соединено" if ok else f"HTTP {response.status_code}"

    def _oneshot_session(self) -> requests.Session:
        """Сессия текущей операции: принадлежит потоку до её завершения."""
        sess = getattr(self._tls, "session", None)
        if sess is None:
            sess = self._sessions.acquire()
            self._tls.session = sess
        return sess

    def _replace_oneshot_session(self, session):
        """Заменить сломанную сессию текущей операции новой."""
        self._sessions.release(session, broken=True)
        fresh = self._sessions.acquire()
        self._tls.session = fresh
        return fresh

    def _http_get(self, path: str, oneshot: bool = True, **kwargs):
        """GET: one-shot — per-thread сессия; poll (oneshot=False) — poll-сессия
        (только из run()-потока)."""
        if self._stop_flag:
            raise requests.ConnectionError("Клиент ноды остановлен")
        kwargs.setdefault("allow_redirects", False)
        sess = self._oneshot_session() if oneshot else self._poll_session
        return self._request(sess, "get", path, **kwargs)

    def _http_post(self, path: str, oneshot: bool = True, **kwargs):
        """POST: one-shot — per-thread сессия; poll (oneshot=False) —
        poll-сессия (только из run()-потока)."""
        if self._stop_flag:
            raise requests.ConnectionError("Клиент ноды остановлен")
        kwargs.setdefault("allow_redirects", False)
        sess = self._oneshot_session() if oneshot else self._poll_session
        return self._request(sess, "post", path, **kwargs)

    def _request(self, session, method, path, *, retry_safe=None, **kwargs):
        if retry_safe is None:
            retry_safe = method == "get"
        url = f"{self._base}{path}"
        try:
            try:
                response = getattr(session, method)(url, **kwargs)
            except requests.Timeout:
                raise
            except requests.ConnectionError:
                # Сервер мог закрыть простаивающее соединение ровно в момент отправки.
                # Новая сессия не повторяется: мёртвая нода не должна отвечать вдвое дольше.
                reused = getattr(session, "reused", False)
                if not retry_safe or not reused or session is self._poll_session:
                    raise
                session = self._replace_oneshot_session(session)
                response = getattr(session, method)(url, **kwargs)
            session.reused = True
        except requests.RequestException as error:
            # Соединение могло остаться в неизвестном состоянии: сессию не переиспользуем.
            self._tls.broken = True
            self._svc.record_diagnostic(self._node_iid, stage="request", error=type(error).__name__)
            raise
        if 200 <= response.status_code < 300 or response.status_code == 304:
            self._svc.record_diagnostic(self._node_iid, stage="ready", error="")
        else:
            self._svc.record_diagnostic(self._node_iid, stage="request", error=f"HTTP {response.status_code}")
        return response

    # ---------- polling ----------

    def _set_node_reachable(self, reachable: bool) -> None:
        """Publish only actual network evidence, not a cached handshake token."""
        if not reachable:
            self._last_hosts_at = 0.0
        if not self._stop_flag and self._svc.set_reachable(
            self._node_iid, reachable, client=self
        ):
            self.node_online.emit(self._node_iid, reachable)

    def _poll_hosts(self) -> bool:
        try:
            r = self._http_get(
                "/api/node/hosts",
                headers={**self._headers(), **({"If-None-Match": self._hosts_etag} if self._hosts_etag else {})},
                timeout=8,
                oneshot=False,
            )
            if r.status_code in (401, 403):
                # Токен истёк — сбросим, следующий цикл сделает handshake.
                self._token = None
                return False
            if r.status_code == 304:
                return not self._stop_flag
            if r.status_code != 200:
                logger.debug(
                    f"NodeClient[{self._node_iid}] hosts: HTTP {r.status_code}"
                )
                return False
            tree = r.json()
            digest = hashlib.sha256(json.dumps(tree, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            self._hosts_etag = r.headers.get("ETag", "")
            if isinstance(tree, list) and digest == self._hosts_digest:
                return not self._stop_flag
            if not self._stop_flag and isinstance(tree, list) and self._svc.set_hosts(
                self._node_iid, tree, client=self
            ):
                self._hosts_digest = digest
                self.hosts_updated.emit(self._node_iid, tree)
                return True
        except Exception as e:
            logger.debug(f"NodeClient[{self._node_iid}] hosts: {e}")
        return False

    def refresh_now(self) -> None:
        """Запросить немедленное обновление дерева хостов (следующий цикл)."""
        self._last_hosts_at = 0.0

    # ---------- one-shot операции (синхронные, для пула) ----------

    @_node_operation
    def get_status(self, iid: str) -> bool | None:
        """Статус одного хоста ноды (вызывается из пула, NodeStatusTimer).

        На стороне ноды — одиночный tcp_ping ssh-порта (1с). Нода
        None означает ошибку API/перегрузку, а не offline самого хоста.
        """
        if not self._ensure_token():
            return None
        try:
            r = self._http_get(
                f"/api/node/status/{iid}",
                headers=self._headers(),
                # connect 3с — быстрый отказ при мёртвой ноде;
                # read 5с — tcp_ping на ноде занимает до ~1с + overhead.
                timeout=(3, 5),
            )
            if r.status_code == 401:
                self._token = None
                return None
            if r.status_code != 200:
                return None
            return bool(r.json().get("online", False))
        except Exception as e:
            logger.debug(f"NodeClient[{self._node_iid}] status {iid}: {e}")
            return None

    @_node_operation
    def get_status_batch(self, iids: list[str]) -> dict[str, bool | None]:
        """Статусы нескольких хостов; None — ошибка API, а не offline хоста."""
        results: dict[str, bool | None] = dict.fromkeys(iids)
        if not self._ensure_token():
            return results
        try:
            r = self._http_post(
                "/api/node/status/batch",
                json={"iids": iids},
                headers=self._headers(),
                retry_safe=True,
                # Лимит проверок на ноде растягивает ответ при большом пакете.
                timeout=(3, 8),
            )
            if r.status_code == 401:
                self._token = None
                return results
            if r.status_code != 200:
                return results
            statuses = r.json().get("statuses", {})
        except (OSError, ValueError, AttributeError) as e:
            # requests.RequestException наследует OSError; прочее — неверный ответ.
            logger.debug(f"NodeClient[{self._node_iid}] status batch: {type(e).__name__}")
            return results
        for iid in iids:
            if iid in statuses:
                results[iid] = bool(statuses[iid])
        return results

    @_node_operation
    def get_thumbnail(
        self, iid: str, width: int, height: int
    ) -> Tuple[Optional[bytes], bool]:
        """Compatibility tuple; unknown infrastructure errors are not backend-down."""
        result = self.get_thumbnail_result(iid, width, height)
        return result.data, result.backend_ok is not False

    @_node_operation
    def get_thumbnail_result(
        self, iid: str, width: int, height: int
    ) -> ThumbnailResult:
        """Read typed status and actual MIME, retaining old-node HTTP semantics."""
        unknown = ThumbnailResult(
            status=ThumbnailStatus.BUSY, error_code="node_infrastructure_error"
        )
        try:
            if not self._ensure_token():
                self._set_node_reachable(False)
                return ThumbnailResult(
                    status=ThumbnailStatus.UNAVAILABLE, error_code="node_unavailable"
                )
            r = self._http_get(
                f"/api/node/thumbnail/{iid}",
                params={"width": int(width), "height": int(height)},
                headers=self._headers(),
                timeout=(5, 45),
            )
            if r.status_code in (401, 403):
                self._token = None
                self._set_node_reachable(False)
                return ThumbnailResult(
                    status=ThumbnailStatus.UNAVAILABLE, error_code="node_auth_failed"
                )
            headers = {key.lower(): value for key, value in r.headers.items()}
            source = headers.get("x-thumbnail-backend", "veyon")
            status = headers.get("x-thumbnail-status")
            if status is not None:
                try:
                    parsed = ThumbnailStatus(status)
                except ValueError:
                    return unknown
                if source not in ("veyon", "vnc"):
                    return unknown
                data = (
                    r.content
                    if r.status_code == 200 and parsed == ThumbnailStatus.OK
                    else None
                )
                mime = headers.get("content-type", "").split(";", 1)[0]
                if data and mime not in ("image/png", "image/jpeg"):
                    return unknown
                return ThumbnailResult(
                    data=data,
                    mime=mime if data else None,
                    backend=source,
                    status=parsed,
                )
            if r.status_code == 200 and r.content:
                mime = headers.get("content-type", "image/png").split(";", 1)[0]
                if mime not in ("image/png", "image/jpeg"):
                    return unknown
                return ThumbnailResult(
                    data=r.content, mime=mime, status=ThumbnailStatus.OK
                )
            if r.status_code == 503 and not self._svc.supports_thumbnail_backends(
                self._node_iid
            ):
                return ThumbnailResult(
                    status=ThumbnailStatus.UNAVAILABLE, error_code="legacy_veyon_down"
                )
        except (requests.ConnectionError, requests.Timeout):
            self._set_node_reachable(False)
            return ThumbnailResult(
                status=ThumbnailStatus.UNAVAILABLE, error_code="node_unavailable"
            )
        except Exception as e:
            logger.debug(
                f"NodeClient[{self._node_iid}] thumbnail {iid}: {type(e).__name__}"
            )
        return unknown

    @_node_operation
    def execute_commands(
        self,
        commands: List[Dict[str, Any]],
        device_iids: List[str],
        timeout: int,
        params: Optional[Dict[str, Any]] = None,
        on_event=None,
        abort_check=None,
        session: bool = False,
        desktop_session: bool = False,
    ) -> Dict[str, Tuple[bool, str]]:
        """Выполнить команды на хостах ноды; polling результатов.

        on_event(evt: dict) — колбек для progress-событий (стриминг).
        abort_check() -> bool — вернуть True для прерывания.
        session — флаг session-задачи (D2): в payload только если True.

        Возвращает {device_iid: (success, output)}.
        """
        if type(desktop_session) is not bool:
            raise ValueError("desktop_session должен быть boolean")
        result: Dict[str, Tuple[bool, str]] = {
            iid: (False, "нет связи") for iid in device_iids
        }
        if abort_check is not None and abort_check():
            return {iid: (False, "Отменено до отправки задания") for iid in device_iids}
        from src.utils.security.ban import BAN_MESSAGE, is_banned

        if is_banned():
            return {iid: (False, BAN_MESSAGE) for iid in device_iids}
        if not self._ensure_token():
            return result
        from src.domain.task_options import background_enabled, desktop_session_for_step

        for command in commands:
            desktop_session_for_step(command, desktop_session)
        if (
            any("desktop_session" in command for command in commands)
            and not self._svc.supports_desktop_session_steps(self._node_iid)
        ):
            return {
                iid: (False, "Нода не поддерживает выбор окружения SSH-шага — обновите ноду")
                for iid in device_iids
            }
        has_background = any([background_enabled(command) for command in commands])
        if has_background and not self._svc.supports_background_commands(self._node_iid):
            return {
                iid: (False, "Нода не поддерживает фоновый запуск — обновите ноду")
                for iid in device_iids
            }
        payload: Dict[str, Any] = {
            "commands": commands,
            "device_iids": device_iids,
            "timeout": int(timeout),
            "params": params or {},
        }
        if session:
            payload["session"] = True
        if desktop_session:
            if not self._svc.supports_desktop_session(self._node_iid):
                return {
                    iid: (False, "Нода не поддерживает окружение рабочего стола — обновите ноду")
                    for iid in device_iids
                }
            payload["desktop_session"] = True
        try:
            if abort_check is not None and abort_check():
                return {iid: (False, "Отменено до отправки задания") for iid in device_iids}
            r = self._http_post(
                "/api/node/execute",
                json=payload,
                headers=self._headers(),
                timeout=10,
            )
            if r.status_code == 401:
                self._token = None
                return result
            if r.status_code != 200:
                logger.debug(
                    f"NodeClient[{self._node_iid}] execute: HTTP {r.status_code}"
                )
                body = r.json() if r.content else {}
                message = str(body.get("detail") or f"HTTP {r.status_code}")
                return {iid: (False, f"Нода отклонила запуск: {message}") for iid in device_iids}
            job_id = r.json().get("job_id")
            if not job_id:
                return result

            result = {
                iid: (False, f"{'Статус запуска неизвестен' if has_background else 'Результат неизвестен'} (задание {job_id})")
                for iid in device_iids
            }
            last_seq = 0
            outputs = {
                iid: BoundedOutput(COMMAND_OUTPUT_HEAD_CHARS, COMMAND_OUTPUT_TAIL_CHARS)
                for iid in device_iids
            }
            completed: set[str] = set()
            recovery_started: float | None = None
            failures = 0
            truncation_reported = False
            done = False
            while not done and not self._stop_flag:
                if abort_check is not None and abort_check():
                    # Прервать задание на ноде
                    try:
                        reply = self._http_post(
                            f"/api/node/execute/{job_id}/abort",
                            headers=self._headers(),
                            timeout=5,
                        )
                        if reply.status_code == 200 and reply.json().get("aborted") is True:
                            for iid in device_iids:
                                if iid not in completed:
                                    result[iid] = (
                                        False,
                                        outputs[iid].text()
                                        + f"\nОтмена запрошена (задание {job_id}); завершение удалённых команд не подтверждено",
                                    )
                    except Exception:
                        pass
                    break
                try:
                    if not self._ensure_token():
                        raise requests.ConnectionError("Нет авторизации ноды")
                    ev = self._http_get(
                        f"/api/node/execute/{job_id}",
                        # Старый сервер не знает compact и вернёт полный ответ.
                        params={"last_seq": last_seq, "compact": 1},
                        headers=self._headers(),
                        timeout=8,
                    )
                    if ev.status_code == 401:
                        self._token = None
                        raise requests.ConnectionError("Сессия ноды истекла")
                    if ev.status_code in (429, 502, 503, 504):
                        raise requests.ConnectionError("Нода временно недоступна")
                    if ev.status_code != 200:
                        break
                    data = ev.json()
                    status = data.get("status", "")
                    if data.get("truncated") and not truncation_reported:
                        truncation_reported = True
                        if on_event:
                            for iid in device_iids:
                                if iid not in completed:
                                    on_event({
                                        "type": "progress", "device_iid": iid,
                                        "chunk": "\n[Часть истории вывода утрачена]\n",
                                    })
                    # Форвардим новые события
                    events = [
                        event for event in data.get("events", [])
                        if event.get("seq", last_seq + 1) > last_seq
                    ]
                    for iid, state in data.get("host_states", {}).items():
                        if iid not in completed and state.get("type") == "device_result":
                            events.append(state)
                    for e in events:
                        diid = e.get("device_iid")
                        if diid not in result or diid in completed:
                            continue
                        if e.get("type") == "progress" and diid:
                            outputs[diid].append(e.get("chunk", ""))
                            if on_event:
                                on_event(e)
                        elif e.get("type") == "device_result" and diid:
                            result[diid] = (
                                bool(e.get("success")),
                                e.get("output") or outputs[diid].text(),
                            )
                            completed.add(diid)
                            if on_event:
                                on_event(e)
                        elif e.get("type") == "device_start" and on_event:
                            on_event(e)
                    last_seq = data.get("latest_seq", last_seq)
                    recovery_started = None
                    failures = 0
                    if status in ("finished", "aborted", "error"):
                        if status == "aborted":
                            for iid in device_iids:
                                if iid not in completed:
                                    result[iid] = (False, "Отменено")
                        done = True
                    else:
                        time.sleep(0.3 + random.uniform(0, 0.2))
                except Exception as e:
                    logger.debug(f"NodeClient[{self._node_iid}] poll exec: {type(e).__name__}")
                    now = time.monotonic()
                    if recovery_started is None:
                        recovery_started = now
                    if now - recovery_started >= 30:
                        break
                    failures += 1
                    self._sleep(min(5.0, 0.5 * 2 ** min(failures, 4)))
            return result
        except Exception as e:
            logger.debug(f"NodeClient[{self._node_iid}] execute: {type(e).__name__}")
            return {
                iid: (False, "Статус запуска неизвестен: ответ ноды не получен" if has_background else "Результат запуска неизвестен: ответ ноды не получен")
                for iid in device_iids
            }

    @_node_operation
    def host_crud(self, op: str, **kwargs) -> Tuple[bool, str, Optional[dict]]:
        """CRUD устройства на ноде: op = add | update | delete."""
        if not self._ensure_token():
            return False, "нет соединения с нодой", None
        device = kwargs.get("device") or {}
        if device.get(
            "thumbnail_backend"
        ) == "vnc" and not self._svc.supports_thumbnail_backends(self._node_iid):
            return False, "Для VNC-миниатюр обновите dno-tool на ноде", None
        payload = {"op": op}
        payload.update(kwargs)
        try:
            r = self._http_post(
                "/api/node/hosts",
                json=payload,
                headers=self._headers(),
                timeout=10,
            )
            if r.status_code == 401:
                self._token = None
                return False, "сессия истекла", None
            body = r.json() if r.content else {}
            ok = bool(body.get("ok"))
            msg = str(body.get("message") or body.get("detail") or "")
            dev = body.get("device")
            if ok:
                self.refresh_now()
            return ok, msg, dev
        except Exception as e:
            return False, self._friendly_error(e), None

    @_node_operation
    def veyon_feature(
        self, iid: str, feature: str, args: Optional[dict] = None
    ) -> Tuple[bool, str]:
        """Выполнить Veyon/host-power фичу на хосте ноды."""
        from src.utils.security.ban import BAN_MESSAGE, is_banned

        if is_banned():
            return False, BAN_MESSAGE
        if not self._ensure_token():
            return False, "нет соединения с нодой"
        try:
            r = self._http_post(
                "/api/node/veyon",
                json={"iid": iid, "feature": feature, "args": args or {}},
                headers=self._headers(),
                # Фича на ноде выполняется долго (veyon-cli subprocess до 45с,
                # SSH-фичи до 30с) — read-таймаут покрывает worst-case;
                # connect-таймаут 5с — быстрый отказ при мёртвой ноде.
                timeout=(5, 60),
            )
            if r.status_code == 401:
                self._token = None
                return False, "сессия истекла"
            body = r.json() if r.content else {}
            return bool(body.get("ok")), str(body.get("error", ""))
        except Exception as e:
            return False, self._friendly_error(e)

    @_node_operation
    def release_thumbnails(self, iids: List[str]) -> bool:
        """Запросить у ноды закрытие WebAPI-соединений для хостов.

        Вызывается при снятии подписки миниатюр (NodeThumbnailManager
        stop_thumbnail/stop_node): соединения хостов, запрошенных через
        /api/node/thumbnail, остаются на ноде открытыми (локального таймера
        у ноды нет) — release закрывает их немедленно, страховка — idle-свипер
        ноды (TTL ~90с). Fire-and-forget: ошибки глотаются, short-timeout
        (нода может быть уже мертва — запрос не должен ничего блокировать).
        """
        if not iids or not self._ensure_token():
            return False
        try:
            r = self._http_post(
                "/api/node/thumbnails/release",
                json={"iids": list(iids)},
                headers=self._headers(),
                # connect 3с — быстрый отказ при мёртвой ноде; read 8с —
                # нода делает DELETE + session.close per host (blocking I/O).
                timeout=(3, 8),
            )
            if r.status_code in (404, 405):
                r = self._http_post(
                    "/api/node/veyon/release",
                    json={"iids": list(iids)},
                    headers=self._headers(),
                    timeout=(3, 8),
                )
            if r.status_code == 401:
                self._token = None
                return False
            return r.status_code == 200
        except Exception as e:
            logger.debug(f"NodeClient[{self._node_iid}] release thumbnails: {e}")
            return False

    @_node_operation
    def host_info(self, iid: str, fields: Optional[List[str]] = None) -> Optional[dict]:
        """Live-данные для тултипа хоста ноды.

        fields — включённые живые поля оператора (None/пусто → нода
        фильтрует по своим настройкам тултипа; старые ноды параметр
        игнорируют — без регрессии).
        """
        if not self._ensure_token():
            return None
        try:
            params = {"fields": ",".join(fields)} if fields else None
            r = self._http_get(
                f"/api/node/host_info/{iid}",
                params=params,
                headers=self._headers(),
                timeout=8,
            )
            if r.status_code == 401:
                self._token = None
                return None
            if r.status_code != 200:
                # 404 = нода старой версии без host_info-роута (обновите
                # dno-tool на ноде) — иначе вечное «Данные запрашиваются…»
                # без диагностики. Не-200 логируем на INFO (не DEBUG),
                # причина должна быть видна в логе оператора.
                logger.info(
                    f"NodeClient[{self._node_iid}] host_info {iid}: "
                    f"нода ответила HTTP {r.status_code}"
                    + (
                        " — на ноде старая версия dno-tool (обновите её)"
                        if r.status_code == 404
                        else ""
                    )
                )
                return None
            body = r.json()
            if body.get("ok"):
                return body
            logger.debug(
                f"NodeClient[{self._node_iid}] host_info {iid}: "
                f"reason={body.get('reason', '?')}"
            )
            return None
        except Exception as e:
            logger.debug(f"NodeClient[{self._node_iid}] host_info {iid}: {e}")
            return None

    @_node_operation
    def vnc_url(self, iid: str, view: int = 0) -> Tuple[str, str]:
        """URL веб-вьювера VNC ноды-владельца для хоста.

        Args:
            iid: iid устройства на ноде-владельце.
            view: 1 — «Просмотр экрана» (viewOnly), 0 — «Управление».

        Returns:
            (url, error): url пуст при ошибке, error — человеко-читаемая
            причина (текст от ноды или сетевая ошибка). URL от ноды уже
            содержит единственный view= — дописывать его НЕ надо
            (URLSearchParams.get берёт первый view=).
        """
        if not self._ensure_token():
            return "", "нет соединения с нодой"
        try:
            r = self._http_get(
                f"/api/node/vnc/{iid}?view={1 if view else 0}",
                headers=self._headers(),
                timeout=8,
            )
            if r.status_code == 401:
                self._token = None
                return "", "сессия с нодой истекла"
            if r.status_code == 404:
                return "", "Устройство не найдено на ноде"
            if r.status_code != 200:
                return "", f"Ошибка ноды (HTTP {r.status_code})"
            body = r.json()
            if body.get("ok") and body.get("url"):
                return body["url"], ""
            return "", str(body.get("error") or "Нода не вернула URL")
        except Exception as e:
            logger.debug(f"NodeClient[{self._node_iid}] vnc_url {iid}: {e}")
            return "", self._friendly_error(e)

    # ---------- вспомогательные ----------

    @staticmethod
    def _friendly_error(e: Exception) -> str:
        """Человеко-читаемое сообщение сетевой ошибки (без traceback-текста
        requests вроде «HTTPConnectionPool(host=...): Read timed out»)."""
        try:
            import requests

            if isinstance(e, requests.exceptions.ConnectionError):
                return "Нода недоступна (нет соединения)"
            if isinstance(
                e,
                (requests.exceptions.Timeout, requests.exceptions.ReadTimeout),
            ):
                return "Нода не ответила вовремя (таймаут)"
        except Exception:
            pass
        text = str(e).strip()
        return text or type(e).__name__

    def _sleep(self, seconds: float) -> None:
        """Прерывистый сон (проверяет _stop_flag каждые 200 мс)."""
        end = time.time() + seconds
        while not self._stop_flag and time.time() < end:
            time.sleep(0.2)
