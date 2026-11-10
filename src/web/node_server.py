"""
NodeServer — отдельный HTTPS-сервер федерации нод (связка инстансов).

Слушает на ``config.app.nodes.listen_port`` (default 8801) и принимает
запросы от операторских инстансов (других нод dno-tool). Доступ
односторонний: кто знает ключ доступа этой ноды, тот и подключается
(challenge-response, ответ — HMAC секрета, см. src/utils/security/node_crypto.py).
Список подключающихся ноде не нужен. Владелец служебного мастер-ключа
подключается подписью Ed25519 (src/utils/security/node_master.py), если в
сборке есть открытая часть ключа; без неё подпись не принимается. Последующие
запросы авторизуются сессионным токеном (``Authorization: Node <token>``).
TLS 1.2+ шифрует канал, клиент до HTTP-запроса сверяет полный Ed25519-ключ
сертификата с NodeRecord. HTTP и редиректы запрещены.

Реализуемые роли ноды:
- ``hello/challenge/verify`` — рукопожатие.
- ``GET /api/node/hosts`` — дерево хостов ноды-владельца (как /api/hosts).
- ``GET /api/node/status/{iid}`` — статус одного хоста (tcp-ping по ssh-порту).
- ``GET /api/node/thumbnail/{iid}`` — миниатюра через Veyon WebAPI.
- ``POST /api/node/execute`` — запуск команд на хостах (WebCommandExecutor),
  polling результатов через ``GET /api/node/execute/{job_id}``.
- ``POST /api/node/hosts`` — CRUD устройств (add/update/delete).
- ``POST /api/node/veyon`` — Veyon/host-power фичи (lock/unlock/power…),
  маршрутизация по VeyonService/HostControlService через FEATURE_MAP.
- ``GET /api/node/host_info/{iid}?fields=`` — live-данные для тултипа (HostInfoService
  ноды-владельца: SSH-опрос выполняется на её стороне; ``fields`` — csv живых
  ключей оператора, без параметра — настройки тултипа ноды).
- ``GET /api/node/vnc/{iid}`` — URL веб-вьювера VNC ноды (редирект браузера
  оператора на ноду-владельца; оператор не проксирует кадры).

Запускается в daemon-потоке (шаблон src/web/server.py). Не требует
configured web-users — auth только по ключам нод.
"""

import asyncio
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

from src.logger import logger
from src.config import config
from src.workers.application_pools import PoolBusyError, PoolLease, get_application_pool
from src.workers.tracked_pool import run_in_tracked_executor
from src.web.jobs import JobManager
from src.web.node_api_models import (
    NodeBodyLimitMiddleware, NodeChallengePayload, NodeExecutePayload,
    NodeFeaturePayload, NodeHostsPayload, NodeInputPayload, NodeReleasePayload,
    NodeStatusBatchPayload, NodeVerifyPayload, validate_node_payload,
)

if TYPE_CHECKING:
    from src.services.thumbnails.thumbnail_service import ThumbnailService


# ---------------------------------------------------------------------------
# Сессии и challenges (in-memory, потокобезопасно)
# ---------------------------------------------------------------------------

_CHALLENGE_TTL_SEC = 60
_TOKEN_TTL_SEC = 12 * 3600
# Меньше таймаута чтения клиента (8 с): иначе при длинной очереди проверок клиент
# не получил бы ничего, и статусы всей пачки не обновлялись бы никогда.
NODE_STATUS_BATCH_DEADLINE_SEC = 6.0


# Неавторизованный клиент может запрашивать challenge: число ожидающих
# ограничено, иначе память сервера растёт без предела.
_MAX_PENDING_CHALLENGES = 1024


class _Session:
    __slots__ = ("token", "owner", "kind", "client_name", "expires")

    def __init__(self, token: str, owner: str, kind: str, client_name: str = "") -> None:
        self.token = token
        # owner — "<kind>-<client_id>": владелец заданий, миниатюр и ссылок VNC.
        self.owner = owner
        self.kind = kind  # "key" (ключ доступа) или "master"
        self.client_name = client_name
        self.touch()

    def touch(self) -> None:
        self.expires = time.time() + _TOKEN_TTL_SEC

    @property
    def expired(self) -> bool:
        return time.time() > self.expires


class _SessionRegistry:
    """Реестр сессий и challenges. Одна сессия на владельца."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._challenges: Dict[str, float] = {}  # challenge_hex -> срок
        self._sessions: Dict[str, _Session] = {}  # token -> Session
        self._sessions_by_owner: Dict[str, str] = {}  # owner -> token

    # ---------- challenges ----------
    def create_challenge(self) -> Optional[str]:
        """Новый одноразовый challenge; None, если ожидающих слишком много."""
        challenge = secrets.token_hex(32)
        with self._lock:
            self._gc_challenges()
            if len(self._challenges) >= _MAX_PENDING_CHALLENGES:
                return None
            self._challenges[challenge] = time.time() + _CHALLENGE_TTL_SEC
        return challenge

    def take_challenge(self, challenge: str) -> bool:
        """Взять challenge, если он существует и не истёк (одноразовый)."""
        with self._lock:
            expires = self._challenges.pop(challenge, None)
        return expires is not None and time.time() <= expires

    def _gc_challenges(self) -> None:
        now = time.time()
        expired = [c for c, expires in self._challenges.items() if expires < now]
        for c in expired:
            self._challenges.pop(c, None)

    # ---------- sessions ----------
    def create_session(self, owner: str, kind: str, client_name: str = "") -> str:
        # Одна активная сессия на владельца: старую затираем.
        with self._lock:
            old = self._sessions_by_owner.get(owner)
            if old is not None:
                self._sessions.pop(old, None)
            token = secrets.token_urlsafe(32)
            self._sessions[token] = _Session(token, owner, kind, client_name)
            self._sessions_by_owner[owner] = token
        return token

    def get_session(self, token: str) -> Optional[_Session]:
        with self._lock:
            s = self._sessions.get(token)
        if s is None or s.expired:
            return None
        s.touch()
        return s

    def drop_session(self, token: str) -> None:
        with self._lock:
            s = self._sessions.pop(token, None)
            if s is not None:
                self._sessions_by_owner.pop(s.owner, None)

    def owners(self, kind: Optional[str] = None) -> list:
        """Владельцы активных сессий (для адресного отзыва)."""
        with self._lock:
            return [
                s.owner for s in self._sessions.values()
                if kind is None or s.kind == kind
            ]

    def clear(self, kind: Optional[str] = None) -> list:
        """Отозвать сессии (все или одного вида) и незавершённые рукопожатия.

        Возвращает владельцев отозванных сессий.
        """
        removed = []
        with self._lock:
            for token, session in list(self._sessions.items()):
                if kind is None or session.kind == kind:
                    self._sessions.pop(token, None)
                    self._sessions_by_owner.pop(session.owner, None)
                    removed.append(session.owner)
            self._challenges.clear()
        return removed


_registry = _SessionRegistry()


# ---------------------------------------------------------------------------
# NodeServer — обёртка uvicorn (шаблон src/web/server.py)
# ---------------------------------------------------------------------------


class NodeServer:
    """Встроенный сервер федерации нод."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[object] = None  # uvicorn.Server
        self._lock = threading.RLock()
        self._started = False
        self._tls_context = None
        # Причина последней неудачи запуска; текст показывается пользователю.
        self._last_error = ""

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def is_running(self) -> bool:
        # Без блокировки: запуск удерживает её, пока сервер поднимается.
        thread = self._thread
        return self._started and thread is not None and thread.is_alive()

    def start(self, host: str = None, port: int = None) -> bool:
        """Запустить NodeServer в фоновом потоке."""
        with self._lock:
            if self.is_running:
                self._last_error = "Сервер нод уже запущен."
                logger.warning("NodeServer: уже запущен")
                return False
            if self._thread is not None and self._thread.is_alive():
                self._last_error = (
                    "Предыдущий сервер нод ещё завершается, повторите через несколько секунд."
                )
                logger.warning("NodeServer: previous server thread is still stopping")
                return False
            self._last_error = ""

            import uvicorn

            app = create_node_app()

            bind_host = host or "0.0.0.0"
            bind_port = int(port or config.app.nodes.listen_port or 8801)

            cfg = uvicorn.Config(
                app,
                host=bind_host,
                port=bind_port,
                log_level="warning",
                access_log=False,
                workers=1,
                timeout_graceful_shutdown=3,
                # Клиент держит соединения между опросами: иначе сервер закрывал бы
                # их раньше следующего цикла статусов.
                timeout_keep_alive=75,
                # log_config=None: встроенный dictConfig ломается в PyInstaller
                # (dotted-path форматтеры uvicorn). У dno-tool собственный логгер.
                log_config=None,
            )
            from src.utils.security.node_transport import load_node_tls

            from src.di import get_container
            from src.services import NodeService

            node_service = get_container().resolve_optional(NodeService)
            if node_service is None:
                self._last_error = "Служба нод недоступна."
                logger.error("NodeServer: NodeService недоступен")
                return False
            try:
                self._tls_context = load_node_tls(cfg, node_service.ensure_private_key())
                # Для новых TLS handshake выбирается актуальный контекст.
                # Уже использованный SSLContext не перенастраивается.
                def select_identity(connection, server_name, initial_context):
                    connection.context = self._tls_context

                cfg.ssl.sni_callback = select_identity
            except Exception as error:
                self._last_error = f"Ошибка загрузки TLS: {type(error).__name__}"
                logger.error(f"NodeServer: ошибка загрузки TLS: {type(error).__name__}")
                return False
            from src.web.uvicorn_server import (
                create_uvicorn_server,
                startup_failure_message,
                uvicorn_log_relay,
                wait_for_startup,
            )

            # Клиент держит соединение между запросами и на закрытие TLS не отвечает.
            self._server = create_uvicorn_server(cfg)

            def _run():
                try:
                    self._server.run()
                except Exception as e:
                    logger.error(f"NodeServer: ошибка в потоке сервера: {e}")
                finally:
                    # Без блокировки: её держит start(), пока ждёт итога запуска,
                    # и упавший поток не смог бы завершиться.
                    self._started = False

            self._thread = threading.Thread(
                target=_run, name="dno-tool-node", daemon=True
            )
            relay = uvicorn_log_relay()
            relay.forget(self._thread.name)
            self._thread.start()
            self._started = True

            display_host = (
                "localhost" if bind_host in ("0.0.0.0", "", "::") else bind_host
            )
            outcome = wait_for_startup(self._server, self._thread)
            if outcome == "started":
                logger.info(
                    f"NodeServer: запущен на https://{display_host}:{bind_port}/"
                )
                return True

            self._started = False
            # Не успевший запуститься сервер не должен занять порт позже.
            self._server.should_exit = True
            self._last_error = startup_failure_message(
                bind_port, outcome, relay.take(self._thread.name)
            )
            logger.error(f"NodeServer: {self._last_error}")
            return False

    def stop(self) -> bool:
        """Остановить NodeServer."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                # Даже если сервер не запущен — пул мог быть создан в
                # create_node_app(). Гасим его, иначе ThreadPoolExecutor с
                # running future блокирует выход процесса (_python_exit join).
                try:
                    from src.web.node_server import shutdown_node_pools

                    shutdown_node_pools()
                except Exception as e:
                    logger.debug(f"NodeServer: ошибка shutdown пулов: {e}")
                return False
            srv = self._server
            thread = self._thread
            if srv is not None:
                try:
                    srv.should_exit = True
                except Exception as e:
                    logger.warning(f"NodeServer: ошибка сигнала остановки: {e}")
            self._started = False

        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("NodeServer: поток не завершился за 5 секунд")

        # Shutdown пула миниатюр ноды (зеркало WebServer.stop /
        # shutdown_web_pools) + закрытие persistent WebAPI-соединений
        # (зеркало app.py shutdown-хука).
        try:
            from src.web.node_server import shutdown_node_pools

            shutdown_node_pools()
        except Exception as e:
            logger.warning(f"NodeServer: ошибка shutdown пулов: {e}")
        stopped = thread is None or not thread.is_alive()
        if stopped:
            logger.info("NodeServer: остановлен")
        return stopped

    def restart(self, host: str = None, port: int = None) -> bool:
        self.stop()
        return self.start(host=host, port=port)

    def has_pending_workers(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()


_node_server: Optional[NodeServer] = None
_node_server_lock = threading.Lock()

# Node-owned leases use process-wide role budgets. Stopping this server only
# cancels its own queued operations, never Qt/web captures or commands.
_node_thumb_pool: PoolLease | None = None
_node_command_pool: PoolLease | None = None
_node_job_manager: JobManager | None = None
_node_thumbnail_consumers: dict[str, "ThumbnailService"] = {}
_node_thumbnail_epochs: dict[tuple[str, str], int] = {}
_node_thumbnail_ownership: dict[tuple[str, str], tuple[object, int]] = {}
_node_thumbnail_lock = threading.RLock()


def shutdown_node_pools() -> None:
    """Shutdown пула миниатюр ноды (вызывается из NodeServer.stop)."""
    global _node_thumb_pool, _node_command_pool
    if _node_job_manager is not None:
        _node_job_manager.shutdown()
    if _node_command_pool is not None:
        _node_command_pool.shutdown(wait=False, cancel_futures=True)
    _node_command_pool = None
    with _node_thumbnail_lock:
        pool = _node_thumb_pool
        _node_thumb_pool = None
        consumers = list(_node_thumbnail_consumers.items())
        _node_thumbnail_consumers.clear()
        _node_thumbnail_epochs.clear()
        _node_thumbnail_ownership.clear()
    for consumer, service in consumers:
        service.release_consumer(consumer)
    if pool is None:
        return
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception as e:
        logger.debug(f"NodeAPI: ошибка shutdown пула миниатюр: {e}")


def get_node_server() -> NodeServer:
    """Глобальный синглтон NodeServer."""
    global _node_server
    if _node_server is None:
        with _node_server_lock:
            if _node_server is None:
                _node_server = NodeServer()
    return _node_server


# ---------------------------------------------------------------------------
# FastAPI-приложение ноды
# ---------------------------------------------------------------------------


def create_node_app() -> Any:
    """Создать FastAPI-приложение федерации нод.

    Сервисы резолвятся лениво из DI-контейнера внутри endpoint'ов —
    приложение можно создать до вызова initialize_services.
    """
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse, Response
    from src.services.thumbnails.types import ThumbnailResult, ThumbnailStatus
    from src.web.app import (
        _default_thumbnail_dimensions,
        _thumbnail_dimensions,
        _thumbnail_headers,
    )

    app = FastAPI(
        title="DNO Tool Node API",
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(NodeBodyLimitMiddleware)

    @app.exception_handler(PoolBusyError)
    async def _pool_busy(_request: Request, error: PoolBusyError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(error)},
            headers={"Retry-After": "1"},
        )

    # Постоянный пул миниатюр (модуль-глобал). Пересоздаётся на каждый
    # create_node_app (рестарт NodeServer) — старый гасится shutdown'ом.
    global _node_thumb_pool, _node_command_pool, _node_job_manager
    shutdown_node_pools()
    _node_thumb_pool = get_application_pool("thumbnails").lease(source="nodes")
    _node_command_pool = get_application_pool("commands").lease(source="nodes")
    _node_job_manager = JobManager(
        _node_command_pool, ttl_minutes=config.app.web.job_ttl_minutes
    )

    @app.on_event("startup")
    async def _startup_quiet_proactor():
        from anyio.to_thread import current_default_thread_limiter

        current_default_thread_limiter().total_tokens = max(
            1, min(8, config.app.network.thread_count)
        )
        # Windows Proactor: гасим шум ConnectionResetError при закрытии
        # транспорта (известный дефект CPython gh-88964 при обрыве RST).
        from src.web.proactor_quiet import install_proactor_quiet_handler

        install_proactor_quiet_handler(asyncio.get_running_loop())
        if _node_job_manager is not None:
            _node_job_manager.set_loop(asyncio.get_running_loop())
            _node_job_manager.start_gc()

    @app.on_event("shutdown")
    async def _shutdown_thumbnails():
        shutdown_node_pools()

    # --- зависимости/утилиты ------------------------------------------------

    def _get_node_service():
        try:
            from src.di import get_container
            from src.services import NodeService, initialize_services

            container = get_container()
            svc = container.resolve_optional(NodeService)
            if svc is None:
                # DI не был инициализирован (например, в тестах) — инициализируем
                initialize_services(container)
                svc = container.resolve(NodeService)
            return svc
        except Exception as e:
            logger.error(f"NodeAPI: NodeService недоступен: {e}")
            return None

    def _get_device_service():
        try:
            from src.di import get_container
            from src.services import DeviceService

            return get_container().resolve(DeviceService)
        except Exception as e:
            logger.error(f"NodeAPI: DeviceService недоступен: {e}")
            return None

    def _get_veyon_service():
        try:
            from src.di import get_container
            from src.services import VeyonService

            return get_container().resolve(VeyonService)
        except Exception:
            return None

    def _get_thumbnail_service():
        try:
            from src.di import get_container
            from src.services.thumbnails.thumbnail_service import ThumbnailService

            return get_container().resolve(ThumbnailService)
        except Exception:
            return None

    def _get_host_control():
        try:
            from src.di import get_container
            from src.services import HostControlService

            return get_container().resolve(HostControlService)
        except Exception:
            return None

    def _get_krfb_service():
        try:
            from src.di import get_container
            from src.services import KRFBService

            return get_container().resolve(KRFBService)
        except Exception:
            return None

    async def _require_node(request: Request):
        """Зависимость: session-token ноды в заголовке Authorization."""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Node "):
            raise HTTPException(401, "Требуется заголовок Authorization: Node <token>")
        token = auth[5:].strip()
        session = _registry.get_session(token)
        if session is None:
            raise HTTPException(401, "Сессия истекла или недействительна")
        return session

    async def _require_master_node(
        request: Request,
        session: _Session = Depends(_require_node),
    ) -> _Session:
        """Разрешить выполнение служебных команд только сессиям, подписанным мастер-ключом."""
        if getattr(session, "kind", None) != "master":
            raise HTTPException(
                status_code=403,
                detail="Эта операция доступна только для сессии со служебным мастер-ключом",
            )
        return session

    def _require_not_banned() -> None:
        """Зависимость: на этой копии не запрещено выполнение команд."""
        from src.utils.security.ban import BAN_MESSAGE, is_banned

        if is_banned():
            raise HTTPException(403, BAN_MESSAGE)

    # --- handshake ----------------------------------------------------------

    @app.get("/api/node/hello")
    async def hello():
        """Открытый endpoint: публичный ключ, имя, версия, fingerprint."""
        svc = _get_node_service()
        if svc is None:
            raise HTTPException(503, "NodeService не инициализирован")
        pub = svc.get_public_key_str()
        fp = svc.get_fingerprint()
        try:
            from src import __version__
        except Exception:
            __version__ = ""
        # Имя ноды — hostname машины (операторы видят его в дереве).
        import socket

        from src.utils.security.node_master import master_key_enabled

        name = socket.gethostname()
        thumbnails = _get_thumbnail_service()
        backends = thumbnails.supported_backends if thumbnails else []
        return {
            "pubkey": pub,
            "fingerprint": fp,
            "name": name,
            "version": __version__,
            # Capability session (D3): поддержка session-режима нодой.
            # Клиент проверяет ДО отправки; без capability - честный отказ.
            "capabilities": {
                "session": True,
                "desktop_session": True,
                "desktop_session_steps": True,
                "background_commands": True,
                "transport": "https-ed25519-v1",
                # Односторонний доступ по ключу ноды и служебный мастер-ключ.
                "auth": "access-key-v1",
                "master_key": master_key_enabled(),
                "ban": True,
                "hosts_etag": True,
                "vnc_view": "ui-mode",
                "thumbnail_backends": True,
                "thumbnails_release": True,
                "killswitch": True,
                "status_batch": True,
            },
            "thumbnail_backends": backends,
        }

    @app.post("/api/node/challenge")
    async def challenge(payload: Dict[str, Any]):
        """Выдать одноразовый challenge для рукопожатия."""
        validate_node_payload(NodeChallengePayload, payload or {})
        ch = _registry.create_challenge()
        if ch is None:
            raise HTTPException(503, "Слишком много незавершённых подключений")
        return {"challenge": ch, "expires": _CHALLENGE_TTL_SEC}

    @app.post("/api/node/verify")
    async def verify(payload: Dict[str, Any]):
        """Проверить ответ на challenge и открыть сессию.

        ``proof`` — HMAC секрета ключа доступа, ``master_signature`` —
        подпись служебным мастер-ключом; ровно одно из двух.
        """
        payload = validate_node_payload(NodeVerifyPayload, payload)
        challenge_hex = payload["challenge"]
        client_id = payload["client_id"]
        client_name = payload.get("client_name", "")
        proof = payload.get("proof")
        master_signature = payload.get("master_signature")
        if (proof is None) == (master_signature is None):
            raise HTTPException(400, "Требуется ровно одно из proof, master_signature")
        svc = _get_node_service()
        if svc is None:
            raise HTTPException(503, "NodeService не инициализирован")
        if not _registry.take_challenge(challenge_hex):
            raise HTTPException(403, "Challenge недействителен или истёк")
        challenge = bytes.fromhex(challenge_hex)
        server_pubkey = svc.get_public_key_str()
        if proof is not None:
            from src.utils.security.node_crypto import verify_access_proof

            if not verify_access_proof(
                svc.get_access_secret(), server_pubkey, challenge, proof
            ):
                raise HTTPException(403, "Ключ доступа не подходит")
            kind = "key"
        else:
            from src.utils.security.node_master import verify_master_challenge

            if not verify_master_challenge(server_pubkey, challenge, master_signature):
                raise HTTPException(403, "Ключ доступа не подходит")
            kind = "master"
        token = _registry.create_session(f"{kind}-{client_id}", kind, client_name)
        if client_name:
            logger.info(f"NodeServer: подключение ноды «{client_name}»")
        return {"token": token, "ttl": _TOKEN_TTL_SEC}

    # --- хосты --------------------------------------------------------------

    @app.get("/api/node/hosts")
    async def node_hosts(request: Request, session: _Session = Depends(_require_node)):
        """Дерево хостов ноды-владельца (формат get_devices_raw, нормализованный)."""
        svc = _get_device_service()
        if svc is None:
            raise HTTPException(503, "DeviceService не инициализирован")
        raw = svc.get_devices_raw()
        # Поле-набор для нод-пиров: шире веб-поля (порт/MAC/связка/sudo/локаль),
        # без login/password — учётные данные остаются на ноде-владельце.
        from src.web.app import _strip_host_for_node

        def _strip_leaf(n):
            if isinstance(n, dict):
                return _strip_host_for_node(n)
            return n

        def _norm(n):
            if _is_host_leaf_node(n):
                return _strip_host_for_node(n)
            if isinstance(n, dict):
                out = {}
                for key, value in n.items():
                    if isinstance(value, list):
                        out[key] = [_norm(c) for c in value]
                    elif _is_host_leaf_node(value):
                        out[key] = [_strip_host_for_node(value)]
                    else:
                        out[key] = []
                return out
            return n

        def _is_host_leaf_node(x):
            return isinstance(x, dict) and "host" in x and "name" in x

        import hashlib
        import json

        tree = [_norm(item) for item in (raw or [])]
        etag = '"' + hashlib.sha256(json.dumps(tree, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest() + '"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(tree, headers={"ETag": etag})

    @app.get("/api/node/status/{iid}")
    async def node_status(iid: str, session: _Session = Depends(_require_node)):
        """Статус одного хоста: {"online": bool}.

        Одиночный tcp_ping ssh-порта (1с) — вызывается per-host таймером
        оператора (NodeStatusTimer) с каденсом локального пинга
        (network.ping_interval, первый опрос через 0–3с, джиттер ±10%).
        Для новых клиентов есть /api/node/status/batch: они объединяют запросы
        таймеров в коротком окне, поэтому статусы по-прежнему появляются
        небольшими группами, а не одной синхронной волной.
        """
        svc = _get_device_service()
        if svc is None:
            raise HTTPException(503, "DeviceService не инициализирован")
        dev = svc.get_device_by_iid(iid)
        if dev is None:
            raise HTTPException(404, "Устройство не найдено")

        from src.utils.network.reachability import get_reachability_prober

        ssh_port = getattr(dev, "port", None) or config.app.ssh.port or 22
        host = getattr(dev, "host", "") or ""
        if not host:
            return {"online": False}
        online = await asyncio.wrap_future(
            get_reachability_prober().probe(
                host,
                ssh_port,
                1.0,
                metadata={"host": host, "type": "node_owner_status", "source": "nodes"},
            )
        )
        return {"online": bool(online)}

    @app.post("/api/node/status/batch")
    async def node_status_batch(
        payload: dict[str, Any],
        session: _Session = Depends(_require_node),  # noqa: B008 - зависимость FastAPI
    ):
        """Статусы нескольких хостов в пределах лимита проверок, без потока на пробу.

        {"statuses": {iid: bool}, "unknown": [iid]}. unknown — нет такого хоста;
        клиент трактует это как ошибку API, а не offline. Хост, не проверенный к сроку,
        в ответ не попадает: клиент оставит последний статус и спросит снова.
        """
        from src.utils.network.reachability import get_reachability_prober

        payload = validate_node_payload(NodeStatusBatchPayload, payload)
        svc = _get_device_service()
        if svc is None:
            raise HTTPException(503, "DeviceService не инициализирован")
        prober = get_reachability_prober()
        statuses: dict[str, bool] = {}
        unknown: list[str] = []
        probes: dict[str, Any] = {}
        for iid in dict.fromkeys(payload["iids"]):
            dev = svc.get_device_by_iid(iid)
            if dev is None:
                unknown.append(iid)
                continue
            host = getattr(dev, "host", "") or ""
            if not host:
                statuses[iid] = False
                continue
            probes[iid] = asyncio.wrap_future(
                prober.probe(
                    host,
                    getattr(dev, "port", None) or config.app.ssh.port or 22,
                    1.0,
                    metadata={"host": host, "type": "node_owner_status", "source": "nodes"},
                )
            )
        if probes:
            _, late = await asyncio.wait(
                probes.values(), timeout=NODE_STATUS_BATCH_DEADLINE_SEC
            )
            for future in late:
                future.cancel()
            statuses.update(
                {
                    iid: bool(future.result())
                    for iid, future in probes.items()
                    if future not in late
                }
            )
        return {"statuses": statuses, "unknown": unknown}

    @app.get("/api/node/thumbnail/{iid}")
    async def node_thumbnail(
        iid: str,
        request: Request,
        width: str = "",
        height: str = "",
        session: _Session = Depends(_require_node),
    ):
        """Capture on the owning node, with consumer-scoped ownership."""
        # Omitted query sizes use this owner instance's clamped defaults.
        default_width, default_height = _default_thumbnail_dimensions()
        if "width" not in request.query_params:
            width = str(default_width)
        if "height" not in request.query_params:
            height = str(default_height)
        request_width, request_height = _thumbnail_dimensions(width, height, query=True)
        svc = _get_device_service()
        if svc is None:
            raise HTTPException(503, "DeviceService не инициализирован")
        dev = svc.get_device_by_iid(iid)
        if dev is None:
            raise HTTPException(404, "Устройство не найдено")
        thumbnails = _get_thumbnail_service()
        if thumbnails is None:
            raise HTTPException(503, "Сервис миниатюр недоступен")

        from src.workers.tracked_pool import run_in_tracked_executor

        pool = _node_thumb_pool
        if pool is None:
            raise HTTPException(503, "Пул миниатюр недоступен")
        consumer = "node:" + session.owner
        owner_key = (consumer, iid)
        with _node_thumbnail_lock:
            epoch = _node_thumbnail_epochs.setdefault(owner_key, 0)
            owner_token = (pool, epoch)
            _node_thumbnail_ownership[owner_key] = owner_token

        def _current():
            return (
                pool is _node_thumb_pool
                and _node_thumbnail_epochs.get(owner_key) == epoch
            )

        def _capture():
            from src.utils.network.net_utils import tcp_ping

            backend = thumbnails.effective_backend(dev)
            with _node_thumbnail_lock:
                if not _current():
                    return ThumbnailResult(
                        backend=backend, status=ThumbnailStatus.CANCELLED
                    )
                _node_thumbnail_consumers[consumer] = thumbnails
                thumbnails.subscribe(dev, consumer)
            try:
                port = getattr(dev, "port", None) or config.app.ssh.port or 22
                online = tcp_ping(dev.host, port, 1.0)
                with _node_thumbnail_lock:
                    if not _current():
                        return ThumbnailResult(
                            backend=backend, status=ThumbnailStatus.CANCELLED
                        )
                return thumbnails.get_thumbnail(
                    dev, request_width, request_height, online=online, consumer=consumer
                )
            finally:
                # A running request may finish after shutdown or restart.
                with _node_thumbnail_lock:
                    active = _node_thumbnail_ownership.get(owner_key)
                    if not _current() and (active is None or active == owner_token):
                        thumbnails.release(iid, consumer=consumer)

        try:
            result = await run_in_tracked_executor(
                pool,
                _capture,
                metadata={
                    "host": dev.host,
                    "type": "node_thumbnail",
                    "backend": thumbnails.effective_backend(dev),
                    "device": dev.name or dev.host,
                },
            )
        except Exception as e:
            logger.debug(
                f"NodeAPI: thumbnail infrastructure error iid={iid}: {type(e).__name__}"
            )
            raise HTTPException(503, "Ошибка подсистемы миниатюр") from None
        with _node_thumbnail_lock:
            if not _current():
                result = ThumbnailResult(
                    backend=result.backend, status=ThumbnailStatus.CANCELLED
                )
        headers = _thumbnail_headers(result)
        if not result.data or result.status != ThumbnailStatus.OK:
            failed = result.status in (
                ThumbnailStatus.UNAVAILABLE,
                ThumbnailStatus.AUTH_FAILED,
                ThumbnailStatus.TIMEOUT,
                ThumbnailStatus.PROTOCOL_ERROR,
                ThumbnailStatus.UNSUPPORTED,
            )
            return Response(status_code=503 if failed else 204, headers=headers)
        return Response(
            content=result.data,
            media_type=result.mime,
            headers=headers,
        )

    # --- команды ------------------------------------------------------------

    @app.post("/api/node/execute")
    async def node_execute(
        payload: Dict[str, Any],
        session: _Session = Depends(_require_node),
        _allowed: None = Depends(_require_not_banned),  # noqa: B008 - зависимость FastAPI
    ):
        """Запустить команды на хостах ноды-владельца.

        Тело: {"commands": [{"type": "ssh|sftp|local", "text": "..."}],
               "device_iids": [...], "timeout": int, "params": {},
               "session": true (опционально, только если True)}
        Ответ: {"job_id": "..."}.

        Результаты — polling через GET /api/node/execute/{job_id}.
        """
        svc = _get_device_service()
        if svc is None:
            raise HTTPException(503, "DeviceService не инициализирован")

        payload = validate_node_payload(NodeExecutePayload, payload)
        commands_data = (payload or {}).get("commands") or []
        device_iids = (payload or {}).get("device_iids") or []
        timeout = int((payload or {}).get("timeout") or config.app.ssh.command_timeout)
        params = (payload or {}).get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise HTTPException(400, "Параметры команды должны быть словарем")
        use_session = bool((payload or {}).get("session", False))
        desktop_session = (payload or {}).get("desktop_session", False)

        if not commands_data or not device_iids:
            raise HTTPException(400, "Требуются commands и device_iids")

        devices = []
        for iid in device_iids:
            dev = svc.get_device_by_iid(iid)
            if dev is not None:
                devices.append(dev)
        if not devices:
            raise HTTPException(404, "Ни одно устройство не найдено")

        from src.domain.models.command import Command

        commands = [Command(c) for c in commands_data if isinstance(c, dict)]

        # JobManager веб-сервера — переиспользуем (он потокобезопасный).
        from src.web.app import _run_parallel
        from src.web.executor import WebCommandExecutor

        _job_manager, _command_pool = _node_job_manager, _node_command_pool
        if _job_manager is None or _command_pool is None:
            raise HTTPException(503, "Пул заданий веб-сервера недоступен")

        executor = WebCommandExecutor()
        job = _job_manager.create_job(
            owner=f"node:{session.owner}",
            task_iid="",
            device_iids=[d.iid for d in devices],
            params=params,
            timeout=timeout,
            executor=executor,
        )
        # Запускаем параллельное выполнение в фоне (тот же механизм,
        # что и для веб-заданий).
        _job_manager.submit_run(
            job,
            _run_parallel,
            job,
            devices,
            commands,
            timeout,
            params,
            _job_manager,
            use_session,
            desktop_session,
        )
        return {"job_id": job.id}

    @app.get("/api/node/execute/{job_id}")
    async def node_execute_events(
        job_id: str,
        last_seq: int = 0,
        compact: bool = False,
        session: _Session = Depends(_require_node),
    ):
        """Получить события задания с seq > last_seq (polling).

        compact=1 отправляет новый клиент; старый получает прежний полный ответ.
        """
        _job_manager = _node_job_manager

        if _job_manager is None:
            raise HTTPException(503, "Пул заданий недоступен")
        job = _job_manager.get(job_id)
        if job is None or job.owner != f"node:{session.owner}":
            raise HTTPException(404, "Задание не найдено")
        body = await asyncio.to_thread(
            _job_manager.encode_events, job_id, last_seq, compact=compact
        )
        if body is None:
            raise HTTPException(404, "Задание не найдено")
        return Response(content=body, media_type="application/json")

    @app.post("/api/node/execute/{job_id}/abort")
    async def node_execute_abort(
        job_id: str, session: _Session = Depends(_require_node)
    ):
        _job_manager = _node_job_manager

        if _job_manager is None:
            raise HTTPException(503, "Пул заданий недоступен")
        job = _job_manager.get(job_id)
        if job is None or job.owner != f"node:{session.owner}":
            raise HTTPException(404, "Задание не найдено")
        ok = _job_manager.abort(job_id)
        if not ok:
            raise HTTPException(404, "Задание не найдено")
        return {"aborted": True}

    @app.post("/api/node/execute/{job_id}/input")
    async def node_execute_input(
        job_id: str,
        payload: Dict[str, Any],
        session: _Session = Depends(_require_node),
        _allowed: None = Depends(_require_not_banned),  # noqa: B008 - зависимость FastAPI
    ):
        """Передать ввод в PTY активной SSH-сессии задания."""
        _job_manager = _node_job_manager

        if _job_manager is None:
            raise HTTPException(503, "Пул заданий недоступен")
        job = _job_manager.get(job_id)
        if (
            job is None
            or job.owner != f"node:{session.owner}"
            or job.executor is None
        ):
            raise HTTPException(404, "Задание не найдено")
        payload = validate_node_payload(NodeInputPayload, payload)
        device_iid = (payload or {}).get("device_iid", "")
        text = (payload or {}).get("text", "")
        # WebCommandExecutor не хранит по-устроечных сессий — интерактивный
        # ввод на ноде доступен только для однохостовых заданий.
        if not device_iid and job.device_iids:
            device_iid = job.device_iids[0]
        try:
            # WebCommandExecutor не поддерживает send_input напрямую —
            # используем SSHWorker-очередь если он её предоставит (fallback no-op).
            fn = getattr(job.executor, "send_input", None)
            if fn is None:
                return {"ok": False, "error": "Ввод не поддерживается"}
            fn(device_iid, text)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # --- CRUD хостов --------------------------------------------------------

    @app.post("/api/node/hosts")
    async def node_hosts_crud(
        payload: Dict[str, Any],
        session: _Session = Depends(_require_node),
    ):
        """CRUD устройств на ноде-владельце.

        Тело: {"op": "add"|"update"|"delete", ...}
          add:    {op, device: {name, host, port?, credential_bundle_iid?, ...}, folder?}
          update: {op, iid, device: {...поля для обновления...}}
          delete: {op, iid}
        Ответ: {"ok": bool, "message": str, "device"?: {...}}.
        """
        svc = _get_device_service()
        if svc is None:
            raise HTTPException(503, "DeviceService не инициализирован")

        payload = validate_node_payload(NodeHostsPayload, payload)
        op = (payload or {}).get("op", "")
        if op in ("add", "update"):
            device_data = (payload or {}).get("device")
            if not isinstance(device_data, dict):
                raise HTTPException(400, "Ожидается объект device")
            from src.config import validate_thumbnail_backend

            if "thumbnail_backend" in device_data:
                try:
                    validate_thumbnail_backend(
                        device_data["thumbnail_backend"], allow_none=True
                    )
                except ValueError as e:
                    raise HTTPException(400, str(e)) from None
        if op == "add":
            device_data = dict((payload or {}).get("device") or {})
            folder = (payload or {}).get("folder")
            ok, msg, dev = svc.add_device(device_data, folder=folder, persist=True)
            out = {"ok": ok, "message": msg}
            if ok and dev is not None:
                from src.web.app import _strip_host_for_node

                out["device"] = _strip_host_for_node(dev.to_dict())
            return out
        if op == "update":
            iid = (payload or {}).get("iid", "")
            device_data = dict((payload or {}).get("device") or {})
            ok, msg, dev = svc.update_device_by_iid(iid, device_data)
            out = {"ok": ok, "message": msg}
            if ok and dev is not None:
                from src.web.app import _strip_host_for_node

                out["device"] = _strip_host_for_node(dev.to_dict())
            return out
        if op == "delete":
            iid = (payload or {}).get("iid", "")
            ok, msg = svc.delete_device_by_iid(iid, persist=True)
            return {"ok": ok, "message": msg}
        raise HTTPException(400, f"Неизвестная операция: {op}")

    # --- Kill-switch ---------------------------------------------------------

    @app.post("/api/node/killswitch")
    @app.post("/api/node/wipe")
    async def node_killswitch(
        payload: Dict[str, Any],
        session: _Session = Depends(_require_master_node),
    ):
        """Необратимо стереть данные этой ноды и завершить процесс.

        Тело: {"confirm": "WIPE"}. Доступно только мастер-сессии ноды. До очистки
        копия программы блокируется (отметка переживает очистку, см.
        src/utils/security/ban.py).
        """
        if (payload or {}).get("confirm") != "WIPE" or len(payload) != 1:
            raise HTTPException(400, 'Требуется {"confirm": "WIPE"}')
        from src.utils.system.killswitch import schedule_wipe_and_exit

        logger.warning("NodeServer: получена команда kill-switch")
        started = schedule_wipe_and_exit()
        from src.utils.security.ban import is_banned

        return {"ok": True, "started": started, "banned": is_banned()}

    @app.post("/api/node/block")
    async def node_block(session: _Session = Depends(_require_master_node)):
        """Заблокировать эту копию: запрет команд и подключения к нодам.

        Данные не стираются. Доступно только мастер-сессии ноды. Снять блокировку
        может только сессия, открытая служебным мастер-ключом (``/unblock``).
        """
        from src.utils.security.ban import ban_instance, is_banned

        logger.warning("NodeServer: получена команда блокировки")
        ban_instance()
        return {"ok": True, "banned": is_banned()}

    @app.post("/api/node/unblock")
    async def node_unblock(session: _Session = Depends(_require_master_node)):
        """Снять блокировку этой копии. Только для сессии мастер-ключа."""
        from src.utils.security.ban import is_banned, remove_ban

        ok, message = remove_ban()
        logger.warning(f"NodeServer: снятие блокировки: {message}")
        return {"ok": ok, "message": message, "banned": is_banned()}

    # --- Veyon / host-power фичи ---------------------------------------------

    @app.post("/api/node/veyon")
    async def node_veyon_feature(
        payload: Dict[str, Any],
        session: _Session = Depends(_require_node),
        _allowed: None = Depends(_require_not_banned),  # noqa: B008 - зависимость FastAPI
    ):
        """Выполнить Veyon/host-power/krfb фичу на хосте ноды-владельца.

        Тело: {"iid": "<device iid>", "feature": "<token>",
               "args": {...}  # опционально: mac, username, password, url, text, applications}
        Токены — из VeyonService.FEATURE_MAP + HostControlService.FEATURE_MAP
        + KRFBService.FEATURE_MAP (krfb_restart/krfb_password).
        """
        svc = _get_device_service()
        if svc is None:
            raise HTTPException(503, "DeviceService не инициализирован")
        payload = validate_node_payload(NodeFeaturePayload, payload)
        iid = (payload or {}).get("iid", "")
        feature = (payload or {}).get("feature", "")
        args = (payload or {}).get("args") or {}
        dev = svc.get_device_by_iid(iid)
        if dev is None:
            raise HTTPException(404, "Устройство не найдено")
        host = dev.host

        vs = _get_veyon_service()
        hc = _get_host_control()
        ks = _get_krfb_service()

        # Host-control (SSH/power/WoL) фичи — не требуют Veyon.
        # krfb-фичи — через KRFBService (SSH).
        hc_map = getattr(hc, "FEATURE_MAP", {}) if hc else {}
        vs_map = getattr(vs, "FEATURE_MAP", {}) if vs else {}
        ks_map = getattr(ks, "FEATURE_MAP", {}) if ks else {}
        method_name, _needs_args = None, None
        target = None
        if feature in hc_map:
            target = hc
            method_name = hc_map[feature][0]
        elif feature in ks_map:
            if ks is None:
                return {"ok": False, "error": "KRFBService недоступен"}
            target = ks
            method_name = ks_map[feature][0]
        elif feature in vs_map:
            if vs is None:
                return {"ok": False, "error": "VeyonService недоступен"}
            target = vs
            method_name = vs_map[feature][0]
        else:
            return {"ok": False, "error": f"Неизвестная фича: {feature}"}
        if target is None or getattr(target, method_name, None) is None:
            return {"ok": False, "error": f"Метод {method_name} не реализован"}

        method = getattr(target, method_name)

        def _run_feature():
            try:
                # Сравнение по ТОКЕНАМ фичей (FEATURE_MAP keys), не по именам
                # методов: token "website" → метод open_website(host, url),
                # "message" → text_message(host, text), "login" → login_user(
                # host, user, pass). Старые имена методов оставлены как
                # fallback для совместимости со старыми клиентами.
                if feature == "power_on":
                    return method(args.get("mac") or dev.mac_address or "")
                if feature == "krfb_password":
                    return method(host, args.get("password", ""))
                if feature in ("login_ssh",):
                    return method(
                        host,
                        args.get("username", ""),
                        args.get("password", ""),
                    )
                if feature in ("login", "login_user"):
                    return method(
                        host,
                        args.get("username", ""),
                        args.get("password", ""),
                    )
                if feature in ("website", "open_website"):
                    return method(host, args.get("url", ""))
                if feature in ("start_app",):
                    return method(host, args.get("applications", []))
                if feature in ("message", "send_message"):
                    return method(host, args.get("text", ""))
                return method(host)
            except Exception as e:
                logger.debug(f"NodeAPI: feature {feature} on {host}: {e}")
                return False

        # Фичи синхронные и долгие (veyon-cli subprocess до 45с, SSH до 30с):
        # выполняем в пуле потоков — event loop остаётся живым (polling
        # статусов/миниатюр других нод не замирает), несколько фичей
        # обрабатываются параллельно.
        from src.services.operation_errors import run_capturing_errors

        # Ошибка читается в потоке операции: поле сервиса к этому моменту
        # могла перезаписать параллельная операция с другим хостом.
        outcome = await run_in_tracked_executor(
            _node_command_pool,
            lambda: run_capturing_errors(target, _run_feature),
            metadata={"host": host, "type": "node_feature", "feature": feature},
        )
        return {"ok": bool(outcome), "error": outcome.error}

    @app.post("/api/node/thumbnails/release")
    @app.post("/api/node/veyon/release")
    async def node_veyon_release(
        payload: Dict[str, Any],
        session: _Session = Depends(_require_node),
    ):
        """Release only the caller's subscriptions, including deleted devices."""
        thumbnails = _get_thumbnail_service()
        if thumbnails is None:
            raise HTTPException(503, "Сервис миниатюр недоступен")
        payload = validate_node_payload(NodeReleasePayload, payload)
        iids = (payload or {}).get("iids") or []
        if not isinstance(iids, list):
            raise HTTPException(400, "Ожидается {iids: [...]}")
        iids = list(dict.fromkeys(iid for iid in iids if isinstance(iid, str)))
        consumer = "node:" + session.owner

        def _release():
            with _node_thumbnail_lock:
                for iid in iids:
                    key = (consumer, iid)
                    if key in _node_thumbnail_epochs:
                        _node_thumbnail_epochs[key] += 1
                    _node_thumbnail_ownership.pop(key, None)
                    thumbnails.release(iid, consumer=consumer)
            return len(iids)

        released = _release()
        return {"ok": True, "released": released}

    # --- live-тултип (host_info) ---------------------------------------------

    @app.get("/api/node/host_info/{iid}")
    async def node_host_info(
        iid: str,
        fields: str = "",
        session: _Session = Depends(_require_node),
    ):
        """Live-данные хоста для тултипа.

        На стороне ноды-владельца выполняется SSH-опрос (HostInfoService);
        оператору возвращаются уже разобранные поля (без host).

        Query-параметр ``fields`` (csv живых ключей, например
        ``user,uptime,memory``) — выбор полей ОПЕРАТОРА: без него ответ
        фильтруется настройками тултипа самой ноды (совместимость со
        старыми клиентами). Неизвестные/статические ключи отбрасываются.

        Реализация — общий коллектор src/web/host_info_api.py
        (tcp_ping-гейт ssh-порта, окно ожидания первого сбора до 4с,
        выборка включённых полей) — тот же контракт, что и у веб-роута
        /api/host_info/{iid}. Клиентский read-таймаут 8с покрывает окно.
        """
        svc = _get_device_service()
        if svc is None:
            raise HTTPException(503, "DeviceService не инициализирован")
        dev = svc.get_device_by_iid(iid)
        if dev is None:
            raise HTTPException(404, "Устройство не найдено")

        field_list = [f for f in (fields or "").split(",") if f.strip()]

        from src.web.host_info_api import collect_host_info_fields_async

        return await collect_host_info_fields_async(dev, 4.0, field_list, source="nodes")

    # --- VNC (редирект на веб-вьювер ноды-владельца) -------------------------

    @app.get("/api/node/vnc/{iid}")
    async def node_vnc_url(
        iid: str,
        request: Request,
        session: _Session = Depends(_require_node),
        view: int = 0,
    ):
        """URL веб-вьювера VNC ноды-владельца для хоста.

        Оператор открывает этот URL в браузере/виджете — нода сама рендерит
        VNC-сеанс. Оператор не проксирует кадры (избегаем нагрузки и утечек).
        Веб-сервер ноды-владельца должен быть запущен (config.app.web.enabled
        + пользователи веб-доступа).

        Аутентификация вьювера: capability-токен (?vt=, src.web.vnc_access),
        подписанный нодой для конкретного iid — web-креды ноды браузеру
        оператора не передаются. view=1 задаёт режим просмотра интерфейса,
        view=0 — режим управления. Это не разделение полномочий: доверенный
        пир может запросить управление, серверной ACL только для чтения нет.
        """
        from urllib.parse import quote

        svc = _get_device_service()
        if svc is None:
            raise HTTPException(503, "DeviceService не инициализирован")
        dev = svc.get_device_by_iid(iid)
        if dev is None:
            raise HTTPException(404, "Устройство не найдено")
        if not config.app.web.enabled:
            return {
                "ok": False,
                "error": "Веб-сервер ноды отключён (Настройки → Веб-доступ)",
            }
        from src.web.auth import has_web_accounts

        if not has_web_accounts():
            return {
                "ok": False,
                "error": "На ноде не настроены пользователи веб-доступа",
            }
        from src.web.server import get_web_server

        if not get_web_server().is_running:
            return {"ok": False, "error": "Веб-сервер ноды не запущен"}
        # Capability-токен для конкретного iid (TTL 15 мин).
        from src.utils.network.urls import web_base_url
        from src.web.vnc_access import create_vnc_access, normalize_public_web_url

        vt = create_vnc_access(iid, owner=session.owner)
        # URL.hostname корректно разбирает DNS, IPv4 и литералы IPv6.
        node_host = request.url.hostname or ""
        if not node_host:
            node_host = request.client.host if request.client else ""
        if ":" in node_host:
            node_host = f"[{node_host}]"
        try:
            base = normalize_public_web_url(config.app.web.public_url)
        except ValueError:
            return {"ok": False, "error": "Некорректный внешний HTTPS URL веб-сервера"}
        base = base or web_base_url(config.app.web, node_host)
        name = dev.name or dev.host
        return {
            "ok": True,
            "url": (
                f"{base}/vnc.html?iid={quote(iid, safe='')}"
                f"&vt={quote(vt, safe='')}&view={1 if view else 0}"
                f"&name={quote(name, safe='')}"
            ),
        }

    return app
