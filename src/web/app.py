"""
FastAPI-приложение веб-сервера управления DNO Tool.

REST API:
  GET  /api/hosts              — дерево хостов (с группами), каждое устройство имеет iid
  GET  /api/commands           — иерархия задач (commands.json), каждая задача имеет iid
  GET  /api/version            — версия приложения
  POST /api/login              — аутентификация, выдаёт session_id
  GET  /api/session/hosts      — выбранные устройства сессии (массив iid, восстановление после refresh)
  POST /api/session/hosts      — сохранить выбранные устройства (массив iid)
  GET  /api/jobs/active        — активные задания текущего пользователя
  GET  /api/jobs/recent        — завершённые задания текущего пользователя (replay после refresh)
  GET  /api/jobs/{id}/events   — события задания с seq > ?since= (replay)
  POST /api/jobs/{id}/abort    — прервать задание

WebSocket:
  /ws/execute — асинхронное выполнение команды с потоковым выводом.
  Параметры query: token (base64(user:pass)), session (session_id),
  job_id (опционально — для переподключения к существующему заданию),
  last_seq (опционально — с какого seq дослать пропущенные события).
  Сообщение запуска: {action: 'execute', task_iid: '<iid>', hosts: ['<iid>', ...]}.
  События используют device_iid вместо host для идентификации устройства.

Статические файлы фронтенда раздаются из src/web/static.

Идентификация: устройства и задачи идентифицируются по iid (uuid4, hex).
iid — персистентный идентификатор, сохраняется в JSON-файлах (hosts.json,
commands.json). После рестарта сервера iid стабильны — выбор клиента валиден.
Старые файлы без iid мигрируются автоматически: iid генерируется при загрузке
и записывается при следующем сохранении.
"""

from __future__ import annotations

import os
import json
import base64
import asyncio
import random
import socket
import threading
from concurrent.futures import Future
from src.workers.tracked_pool import run_in_tracked_executor
from src.workers.application_pools import PoolBusyError, PoolLease, get_application_pool
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from src.config import config
from src.domain.models.device import DeviceModel
from src.domain.models.command import Command
from src.logger import logger
from src.utils.network.reachability import get_reachability_prober
from src.services.thumbnails.thumbnail_service import (
    ThumbnailService,
    clamp_thumbnail_dimensions,
    configured_max_thumbnail_dimension,
    validate_thumbnail_dimensions,
)
from src.services.thumbnails.types import ThumbnailResult, ThumbnailStatus
from src import __version__

from .asset_access import (
    SESSION_TTL_SEC as ASSET_SESSION_TTL_SEC,
    AssetSessions,
    GatedStaticFiles,
    install_asset_routes,
    require_asset_session,
    set_asset_cookie,
)
from .proactor_quiet import install_proactor_quiet_handler
from .duplex import run_duplex
from .auth import (
    require_auth,
    require_auth_or_token,
    RequestUser,
    _basic_credentials,
    _decode_token,
    _extract_session,
    authenticate,
    authenticate_async,
)
from .executor import WebCommandExecutor
from .jobs import Job, JobEvent, JobManager, SessionStore
from .vnc_access import verify_vnc_access
from .task_uploads import TaskUploadStore, MAX_UPLOAD_BYTES


# Глобальные сервисы устанавливаются при создании приложения
_device_service = None
_command_service = None
_config_service = None
_veyon_service = None
_thumbnail_service: ThumbnailService | None = None
_host_control_service = None
_camera_service = None
_desktop_scanner_shutdown = None
_desktop_cameras_shutdown = None
_desktop_host_tool_shutdowns: list = []

# Реестр заданий и хранилище сессий — создаются в create_app() (нужен event loop
# uvicorn для fan-out событий подписчикам и GC-таски).
_job_manager: Optional[JobManager] = None
_session_store: Optional[SessionStore] = None
# Глобальный пул потоков команд — единый лимит command_thread_count на все
# задания всех пользователей. Создаётся в create_app().
_command_pool: PoolLease | None = None
# Пулы для миниатюр и batch-пингов статусов. Глобальные, чтобы их можно
# было shutdown'ить при остановке веб-сервера (иначе ThreadPoolExecutor с
# running future блокирует выход процесса через _python_exit join).
_thumb_pool: PoolLease | None = None
_rfb_pool: PoolLease | None = None
_service_pool: PoolLease | None = None


def shutdown_web_pools() -> None:
    """Stop web-owned work without closing the shared GUI/node executors."""
    global _command_pool, _thumb_pool, _rfb_pool, _service_pool
    if _desktop_scanner_shutdown is not None:
        _desktop_scanner_shutdown()
    if _desktop_cameras_shutdown is not None:
        _desktop_cameras_shutdown()
    # Закрытие прерывает SSH-каналы терминалов, передачи файлов и сбор сведений.
    for stop_tool in _desktop_host_tool_shutdowns:
        stop_tool()
    _desktop_host_tool_shutdowns.clear()
    if _job_manager is not None:
        _job_manager.shutdown()
    for pool in (_command_pool, _thumb_pool, _rfb_pool, _service_pool):
        if pool is None:
            continue
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            logger.warning(f"WebAPI: ошибка shutdown пула: {e}")
    _command_pool = None
    _thumb_pool = None
    _rfb_pool = None
    _service_pool = None


def configure_services(
    device_service,
    command_service,
    config_service,
    veyon_service=None,
    host_control_service=None,
    thumbnail_service=None,
    camera_service=None,
) -> None:
    """Внедрить сервисы из DI-контейнера приложения."""
    global \
        _device_service, \
        _command_service, \
        _config_service, \
        _veyon_service, \
        _thumbnail_service, \
        _host_control_service, \
        _camera_service
    _device_service = device_service
    _command_service = command_service
    _config_service = config_service
    _veyon_service = veyon_service
    _thumbnail_service = thumbnail_service
    _host_control_service = host_control_service
    _camera_service = camera_service


def _services_ready() -> bool:
    return all([_device_service, _command_service, _config_service])


def _resolve_device(iid: str):
    """Резолв iid -> DeviceModel для endpoint'ов, идентифицирующих устройства.

    Возвращает (device, error_response|None). При ошибке device=None и
    error_response — HTTPException (400/404/503). На стороне endpoint'а:
        dev, err = _resolve_device(iid)
        if err is not None: raise err
    Реальный сетевой адрес (dev.host) используется только для исходящих
    TCP-подключений внутри web-сервера и не возвращается клиенту.
    """
    if not iid:
        return None, HTTPException(400, "Требуется iid")
    if _device_service is None:
        return None, HTTPException(503, "DeviceService не инициализирован")
    dev = _device_service.get_device_by_iid(iid)
    if dev is None:
        return None, HTTPException(404, f"Устройство {iid} не найдено")
    return dev, None


def _vnc_viewer_auth(request: "Request", iid: str = "") -> RequestUser:
    """Аутентификация запросов браузерного VNC-вьювера.

    Принимает (в порядке приоритета):
      1. HTTP Basic — локальный веб-UI (app.js открывает vnc.html с token).
      2. ?vt= — capability-токен (src.web.vnc_access), выданный нодой-владельцем
         для этого iid (федерация нод: оператор аутентифицирован нодой по ключу,
         веб-креды ноды браузеру не передаются).
      3. ?token= / cookie (require_auth_or_token) — обратная совместимость.

    Идентификация устройства — по iid; owner для изоляции заданий при vt —
    системный (вьювер выполняет только power/veyon-фичи, заданий не создаёт).

    iid опционален (default ""): эндпоинты без привязки к устройству
    (/api/vnc/password) не должны падать с 422 при отсутствии ?iid=.
    vt без iid отклонится сам (токен привязан к конкретному iid).
    """
    creds = _basic_credentials(request)
    owner = authenticate(*creds) if creds else None
    if owner is not None:
        return RequestUser(username=owner, session_id=_extract_session(request))

    vt = request.query_params.get("vt")
    if vt and verify_vnc_access(vt, iid):
        return RequestUser(username="vnc-viewer", session_id=None)

    return require_auth_or_token_sync(request)


def require_auth_or_token_sync(request: "Request") -> RequestUser:
    """Синхронная обёртка require_auth_or_token (vnc-viewer path).

    require_auth_or_token — async-функция (Depends), но из обычной функции её
    не вызвать; здесь дублирована синхронная логика (Basic уже проверен выше,
    остаются token/cookie).
    """
    for value in (
        request.query_params.get("token"),
        request.cookies.get("dno-tool_token"),
    ):
        creds = _decode_token(value) if value else None
        owner = authenticate(*creds) if creds else None
        if owner is not None:
            return RequestUser(username=owner, session_id=_extract_session(request))
    raise HTTPException(
        status_code=401,
        detail="Требуется аутентификация",
        # Без WWW-Authenticate — браузер не должен показывать native prompt.
    )


def _serve_asset_svg(filename: str, cache: bool = True):
    """Отдать SVG-файл из assets/ как Response (для иконок устройств)."""
    data = _read_asset_bytes(filename)
    headers = {"Content-Type": "image/svg+xml"}
    if not cache:
        headers["Cache-Control"] = "no-store"
    return Response(content=data, media_type="image/svg+xml", headers=headers)


def _read_asset_bytes(filename: str) -> bytes:
    """Прочитать SVG-файл из assets/ как bytes (для JSON-комплектов иконок).

    Простые имена (без слэшей) резолвятся в текущей теме иконок
    (assets/icons/<theme>/<name>) с fallback на дефолтную тему - после
    переезда SVG в папки тем (0.9.49) в корне assets/icons/ их нет.
    """
    from src.config import get_asset_path, get_icon_theme, themed_icon_path

    if "/" not in filename and "\\" not in filename:
        try:
            from src.config import get_themed_icon_bytes

            data = get_themed_icon_bytes(filename, get_icon_theme())
            if data:
                return data
        except Exception:  # noqa: BLE001 - откат на файлы
            pass
        path = themed_icon_path(filename, get_icon_theme())
    else:
        path = get_asset_path(filename)
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        logger.error(f"WebAPI: asset {filename} не найден: {e}")
        raise HTTPException(404, f"Иконка {filename} недоступна")


def _thumbnail_theme() -> dict[str, Any]:
    """Оформление миниатюр из той же темы и геометрии, что использует Qt."""
    import math
    import mimetypes
    from xml.etree import ElementTree

    from src.config import get_thumbnail_layout

    layout = get_thumbnail_layout()
    out: dict[str, Any] = {"style": config.app.thumbnails.style}
    for key in ("viewBox", "hole"):
        values = layout[key]
        if not all(math.isfinite(value) for value in values):
            return {"style": "plain"}
        out[key] = values
    for key in ("underlay", "overlay"):
        filename = layout.get(key)
        out[key] = None
        if not filename:
            continue
        try:
            data = _read_asset_bytes(filename)
            mime = mimetypes.guess_type(filename)[0] or ""
            if not mime.startswith("image/"):
                continue
            if key == "overlay" and mime == "image/svg+xml":
                # В Qt рамка растягивается без полей, независимо от пропорций viewBox.
                root = ElementTree.fromstring(data)
                root.set("preserveAspectRatio", "none")
                data = ElementTree.tostring(root, encoding="utf-8")
            out[key] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        except (HTTPException, ElementTree.ParseError):
            continue
    return out


async def _probe_tcp(
    host: str, port: int, timeout: float, metadata: dict[str, Any]
) -> bool:
    """Проверить TCP-порт, не занимая поток на весь таймаут.

    При отмене запроса снимается только ещё не начатая проверка.
    """
    return await asyncio.wrap_future(
        get_reachability_prober().probe(
            host, port, timeout, metadata={**metadata, "source": "web"}
        )
    )


def _ssh_port() -> int:
    """Порт проверки доступности: как у десктоп-пинга (host_timer.py)."""
    return config.app.ssh.port or 22


def _thumbnail_dimensions(
    width: object, height: object, *, query: bool = False
) -> tuple[int, int]:
    """Return HTTP 400, rather than coercing or allocating unbounded images."""
    try:
        if query:
            if not isinstance(width, str) or not isinstance(height, str):
                raise ValueError("Expected integer query parameters")
            width, height = int(width), int(height)
        if not isinstance(width, int) or not isinstance(height, int):
            raise ValueError("Expected integer image dimensions")
        return validate_thumbnail_dimensions(width, height)
    except (TypeError, ValueError):
        # Report the serving instance's own configured maximum, not a constant.
        raise HTTPException(
            400,
            "Размер миниатюры должен быть от 1 до "
            f"{configured_max_thumbnail_dimension()} пикселей",
        ) from None


def _default_thumbnail_dimensions() -> tuple[int, int]:
    """Derive web capture sizes from the canonical card width and scale."""
    thumbnails = config.app.thumbnails
    width, _ = clamp_thumbnail_dimensions(thumbnails.width * thumbnails.scale, 1)
    return width, max(1, width * 9 // 16)


def _thumbnail_headers(
    result: ThumbnailResult, online: bool | None = None
) -> dict[str, str]:
    headers = {
        "Cache-Control": "no-store",
        "X-Thumbnail-Backend": result.backend,
        "X-Thumbnail-Status": str(result.status),
    }
    if online is not None:
        headers["X-Host-Online"] = str(bool(online)).lower()
    return headers


def _thumbnail_event(
    iid: str, online: bool, result: ThumbnailResult | None = None
) -> dict[str, Any]:
    out = {
        "iid": iid,
        "online": bool(online),
        "thumbnail": base64.b64encode(result.data).decode("ascii")
        if result and result.data
        else None,
        "mime": result.mime if result and result.data else None,
        "backend": result.backend if result else None,
        "backend_ok": result.backend_ok if result else None,
        "thumbnail_status": str(result.status) if result else None,
    }
    # Preserve the old port-down meaning only for an equivalent Veyon failure.
    if (
        online
        and result
        and result.backend == "veyon"
        and result.status == ThumbnailStatus.UNAVAILABLE
        and result.error_code == "veyon_server_port_closed"
    ):
        out["veyon"] = False
    return out


def _recv_sock(sock: "socket.socket", n: int) -> Optional[bytes]:
    """Прочитать до n байт из blocking socket. None = EOF/ошибка."""
    try:
        data = sock.recv(n)
        if not data:
            return None
        return data
    except Exception:
        return None


def _normalize_pool_config_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept legacy web aliases without overriding explicit shared limits."""
    result = dict(payload)
    web = result.get("web")
    if not isinstance(web, dict):
        return result
    web = dict(web)
    result["web"] = web
    aliases = (
        ("thread_count", ("network", "thumbnails")),
        ("command_thread_count", ("execution",)),
    )
    for alias, sections in aliases:
        if alias not in web:
            continue
        value = web.pop(alias)
        for section in sections:
            current = result.get(section, {})
            if isinstance(current, dict):
                result[section] = {"thread_count": value, **current}
    return result


def create_app() -> FastAPI:
    app = FastAPI(
        title="DNO Tool Web API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    uploads = TaskUploadStore()
    upload_gc = None
    asset_sessions = AssetSessions()
    install_asset_routes(app, asset_sessions)

    @app.post("/api/task-uploads")
    async def upload_task_file(request: Request, task_iid: str, key: str,
                               filename: str, user: RequestUser = Depends(require_auth)):
        """Принять поток файла в tmp, не раскрывая серверный путь браузеру."""
        task = _command_service.get_command_by_iid(task_iid) if _command_service else None
        labels = task.get("params", []) if task else []
        allowed = {f"param{i + 1}" for i, label in enumerate(labels) if str(label).startswith("file:")}
        if key not in allowed:
            raise HTTPException(400, "Файловый параметр задачи не найден")
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Максимальный размер файла — 256 МиБ")
        token = None
        try:
            token = await asyncio.to_thread(uploads.begin, user.owner, task_iid, key, filename)
            async for chunk in request.stream():
                if chunk:
                    await asyncio.to_thread(uploads.write, token, chunk)
            uploads.complete(token)
            return {"upload_id": token}
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except OSError as error:
            raise HTTPException(507, "Не удалось записать файл во временный каталог сервера") from error
        finally:
            # Неполный поток не должен оставаться доступным для запуска.
            if token is not None and not uploads.is_ready(token):
                await asyncio.to_thread(uploads.discard, token)

    @app.delete("/api/task-uploads/{token}")
    async def delete_task_file(token: str, user: RequestUser = Depends(require_auth)):
        """Удалить неиспользованную загрузку текущего пользователя."""
        await asyncio.to_thread(uploads.discard, token, user.owner)
        return {"success": True}

    @app.exception_handler(PoolBusyError)
    async def _pool_busy(_request: Request, error: PoolBusyError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(error)},
            headers={"Retry-After": "1"},
        )

    global \
        _command_pool, \
        _job_manager, \
        _session_store, \
        _thumb_pool, \
        _rfb_pool, \
        _service_pool
    shutdown_web_pools()
    _thumb_pool = get_application_pool("thumbnails").lease(source="web")
    _command_pool = get_application_pool("commands").lease(source="web")
    _rfb_pool = get_application_pool("rfb").lease(source="web")
    _service_pool = get_application_pool("services").lease(source="web")
    _job_manager = JobManager(
        command_pool=_command_pool,
        ttl_minutes=config.app.web.job_ttl_minutes,
    )
    _session_store = SessionStore(ttl_seconds=24 * 3600)

    @app.on_event("startup")
    async def _on_startup():
        nonlocal upload_gc
        async def collect_uploads():
            while True:
                await asyncio.sleep(60)
                await asyncio.to_thread(uploads.sweep)
        upload_gc = asyncio.create_task(collect_uploads())
        from anyio.to_thread import current_default_thread_limiter

        # Sync FastAPI dependencies use AnyIO rather than application workers.
        current_default_thread_limiter().total_tokens = max(
            1, min(8, config.app.network.thread_count)
        )
        # Windows Proactor: гасим шум ConnectionResetError при закрытии
        # транспорта (известный дефект CPython gh-88964 при обрыве RST).
        install_proactor_quiet_handler(asyncio.get_running_loop())
        # Привязать event loop uvicorn к JobManager (для fan-out и GC-таски).
        loop = asyncio.get_event_loop()
        if _job_manager is not None:
            _job_manager.set_loop(loop)
            _job_manager.start_gc()

    @app.on_event("shutdown")
    async def _on_shutdown():
        if upload_gc is not None:
            upload_gc.cancel()
            await asyncio.gather(upload_gc, return_exceptions=True)
        # Останавливаем пулы потоков при shutdown uvicorn. Без этого
        # ThreadPoolExecutor с running future блокирует выход процесса
        # (concurrent.futures._python_exit ждёт join всех не-daemon потоков).
        shutdown_web_pools()
        await asyncio.to_thread(uploads.close)
        # HTTP captures have finite facade leases. Never close shared Qt sessions.

    from .desktop_api import install_desktop_routes

    install_desktop_routes(
        app,
        lambda: _service_pool,
        lambda: _camera_service,
        lambda: _thumbnail_service,
        lambda: _veyon_service,
    )
    from .scanner_api import install_scanner_routes

    global _desktop_scanner_shutdown
    _desktop_scanner_shutdown = install_scanner_routes(app, lambda: _device_service)
    from .camera_api import install_camera_routes

    global _desktop_cameras_shutdown
    _desktop_cameras_shutdown = install_camera_routes(app, lambda: _camera_service)

    def device_by_iid(iid: str) -> Any:
        return _device_service.get_device_by_iid(iid) if _device_service else None

    from .device_info_api import install_device_info_routes
    from .files_api import install_files_routes
    from .terminal_api import install_terminal_routes

    _desktop_host_tool_shutdowns.extend(
        (
            install_device_info_routes(app, lambda: _service_pool, device_by_iid),
            install_terminal_routes(app, device_by_iid),
            install_files_routes(app, device_by_iid),
        )
    )

    @app.get('/api/tools/icons', dependencies=[Depends(require_asset_session(asset_sessions))])
    async def desktop_icons():
        from src.utils.scanner.device_classifier import TYPE_ICON_KEYS

        names = ['menu_hosts', 'menu_scanner', 'menu_ping', 'camera',
                 'menu_monitoring', 'menu_info', 'menu_terminal', 'menu_sftp', 'folder', 'menu_import', 'menu_export', 'menu_delete',
                 'menu_camera_view', 'menu_camera_record', 'menu_camera_stop', 'menu_abort',
                 'command_ssh', 'command_local', 'command_sftp', 'command_background', 'command_desktop_session',
                 'menu_refresh', 'menu_up', 'menu_add_folder', 'menu_upload', 'menu_download',
                 'menu_rename', 'menu_save', 'camera_view_list', 'camera_view_1', 'camera_view_4',
                 'camera_view_6', 'camera_view_6_focus', 'camera_view_9', 'camera_view_16']
        names.extend(sorted(set(TYPE_ICON_KEYS.values()) | {'scan_ping', 'scan_analyze', 'online', 'offline'}))
        icons = {}
        for name in names:
            try:
                icons[name] = _read_asset_bytes(name + '.svg').decode('utf-8')
            except HTTPException:
                continue
        return icons

    # ===== REST API =====

    @app.get("/api/version")
    async def version(user: RequestUser = Depends(require_auth)):
        return {
            "version": __version__,
            "thread_count": max(1, config.app.network.thread_count),
            "command_thread_count": max(1, config.app.execution.thread_count),
            "job_ttl_minutes": config.app.web.job_ttl_minutes,
            "multi_user": True,
        }

    @app.post("/api/login")
    async def login(payload: Dict[str, Any], request: Request, response: Response):
        """Аутентифицировать пользователя и создать сессию.

        Тело: {"username": "...", "password": "..."}
        Возвращает: {"username": "...", "session_id": "...", "token": "base64(user:pass)"}
        Cookie открывает браузеру файлы интерфейса (см. asset_access).
        """
        username = (payload.get("username") or "").strip()
        password = payload.get("password") or ""
        owner = await authenticate_async(username, password) if username else None
        if owner is None:
            raise HTTPException(401, "Неверные учётные данные")

        set_asset_cookie(
            response,
            request,
            asset_sessions.issue_app(username, password),
            ASSET_SESSION_TTL_SEC,
        )
        # Создаём сессию (привязка выбранных хостов к вкладке браузера).
        st = _session_store.create(owner=owner) if _session_store else None
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode(
            "ascii"
        )
        return {
            "username": owner,
            "session_id": st.id if st else None,
            "token": token,
            "multi_user": True,
        }

    @app.get("/api/session/hosts")
    async def get_session_hosts(user: RequestUser = Depends(require_auth)):
        """Выбранные устройства сессии (по iid, восстановление после refresh)."""
        if _session_store is None or user.session_id is None:
            return {"hosts": []}
        st = _session_store.get(user.session_id)
        return {"hosts": list(st.selected_hosts) if st else []}

    @app.post("/api/session/hosts")
    async def set_session_hosts(
        payload: Dict[str, Any], user: RequestUser = Depends(require_auth)
    ):
        """Сохранить выбранные устройства сессии (массив iid)."""
        if _session_store is None or user.session_id is None:
            return {"success": False, "message": "Сессия не активна"}
        hosts = payload.get("hosts", [])
        if not isinstance(hosts, list):
            raise HTTPException(400, "Ожидается {hosts: [...]}")
        # hosts — массив iid устройств (строки)
        hosts = [h for h in hosts if isinstance(h, str)]
        ok = _session_store.set_hosts(user.session_id, hosts)
        return {"success": ok}

    @app.get("/api/jobs/active")
    async def jobs_active(user: RequestUser = Depends(require_auth)):
        """Активные (pending/running) задания текущего пользователя.

        Поле progress: {done, total} — число хостов с известным результатом
        (device_result) / общее число хостов. Вычисляется по событиям в
        буфере Job. done может быть < total пока задание выполняется.
        """
        if _job_manager is None:
            return {"jobs": []}
        jobs = _job_manager.list_active(owner=user.owner)
        out = []
        for j in jobs:
            done = 0
            task_name = ""
            for e in j.events:
                if e.type == "device_result":
                    done += 1
                if e.type == "start" and "task_name" in e.payload:
                    task_name = e.payload["task_name"]
            total = len(j.device_iids)
            out.append(
                {
                    "id": j.id,
                    "task_iid": j.task_iid,
                    "task_name": task_name,
                    "device_iids": list(j.device_iids),
                    "status": j.status,
                    "created_at": j.created_at,
                    "latest_seq": j.event_seq,
                    "truncated": j.truncated,
                    "progress": {"done": done, "total": total},
                }
            )
        return {"jobs": out}

    @app.get("/api/jobs/recent")
    async def jobs_recent(user: RequestUser = Depends(require_auth)):
        """Завершённые (finished/aborted/error) задания текущего пользователя.

        Глубина определяется серверным TTL (job_ttl_minutes): GC вычищает
        задания старше TTL. Используется фронтом для replay результатов
        после refresh страницы — пока задание живо в реестре, клиент может
        открыть viewer-WS и получить полный replay событий.
        """
        if _job_manager is None:
            return {"jobs": []}
        jobs = _job_manager.list_finished(owner=user.owner)
        out = []
        for j in jobs:
            done = 0
            task_name = ""
            for e in j.events:
                if e.type == "device_result":
                    done += 1
                if e.type == "start" and "task_name" in e.payload:
                    task_name = e.payload["task_name"]
            total = len(j.device_iids)
            out.append(
                {
                    "id": j.id,
                    "task_iid": j.task_iid,
                    "task_name": task_name,
                    "device_iids": list(j.device_iids),
                    "status": j.status,
                    "created_at": j.created_at,
                    "finished_at": j.finished_at,
                    "summary": j.summary,
                    "latest_seq": j.event_seq,
                    "truncated": j.truncated,
                    "progress": {"done": done, "total": total},
                }
            )
        return {"jobs": out}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(
        job_id: str, since: int = 0, user: RequestUser = Depends(require_auth)
    ):
        """События задания с seq > since (для replay при reconnect).

        Доступ только к своим заданиям (по owner). 403 если чужое.
        """
        if _job_manager is None:
            raise HTTPException(503, "JobManager не инициализирован")
        job = _job_manager.get(job_id)
        if job is None:
            raise HTTPException(404, "Задание не найдено")
        if job.owner != user.owner:
            raise HTTPException(403, "Доступ запрещён")
        body = await asyncio.to_thread(
            _job_manager.encode_events, job_id, int(since)
        )
        if body is None:
            raise HTTPException(404, "Задание не найдено")
        return Response(content=body, media_type="application/json")

    @app.post("/api/jobs/{job_id}/abort")
    async def job_abort(job_id: str, user: RequestUser = Depends(require_auth)):
        """Прервать задание."""
        if _job_manager is None:
            raise HTTPException(503, "JobManager не инициализирован")
        job = _job_manager.get(job_id)
        if job is None:
            raise HTTPException(404, "Задание не найдено")
        if job.owner != user.owner:
            raise HTTPException(403, "Доступ запрещён")
        ok = _job_manager.abort(job_id)
        return {"success": ok}

    @app.get("/api/veyon/status")
    async def veyon_status(user: RequestUser = Depends(require_auth)):
        """Статус Veyon/VNC и интервал обновления миниатюр.

        Возвращает гранулярные поля доступности:
        - `available` — Veyon установлен и сервис активен (master-флаг).
        - `thumbnails_available` — живые миниатюры через Veyon WebAPI.
        - `features_available` — Veyon-фичи (lock/unlock/powerdown/...).
        - `rfb_bridge_available` — браузерный RFB-мост (Veyon + config flag).

        Метрики процесса veyon-service (psutil, кэш 5с):
        - `service_pid`, `service_rss_mb`, `service_commit_mb` — память сервиса
          (диагностика upstream-утечки WebAPI-соединений Veyon).
        - `webapi_established` / `webapi_close_wait` — TCP-соединения к WebAPI.
        """
        interval = config.app.thumbnails.interval_sec

        def read_veyon_status():
            if _veyon_service is None:
                return False, False, {}
            installed = _veyon_service.is_installed()
            return (
                installed,
                installed and _veyon_service.is_features_available(),
                _veyon_service.get_webapi_server_stats(),
            )

        installed, features, service_stats = await run_in_tracked_executor(
            _service_pool, read_veyon_status, metadata={"type": "veyon_status"}
        )
        devices = (
            [DeviceModel(data) for data in _device_service.get_all_devices()]
            if _device_service is not None
            else []
        )
        thumbnails = _thumbnail_service is not None and _thumbnail_service.is_available(
            devices
        )
        rfb_bridge = installed and bool(
            getattr(config.app.veyon, "rfb_bridge_enabled", True)
        )
        server_port = (
            getattr(config.app.veyon, "server_port", 11100) if installed else 0
        )
        vnc_port = getattr(config.app.veyon, "vnc_port", 5900)

        # Метрики процесса veyon-service (память — диагностика upstream-утечки
        # WebAPI-соединений; psutil-вызов кэширован на 5 сек внутри сервиса).
        return {
            "available": installed,
            "thumbnails_available": thumbnails,
            "features_available": features,
            "power_features_available": True,
            "thumbnail_interval_sec": interval,
            "thumbnail_width": config.app.thumbnails.width,
            "thumbnail_scale": config.app.thumbnails.scale,
            "thumbnail_max_dimension": configured_max_thumbnail_dimension(),
            "thumbnail_backend": config.app.thumbnails.backend,
            "thumbnail_backends": _thumbnail_service.supported_backends
            if _thumbnail_service
            else [],
            "rfb_bridge_available": rfb_bridge,
            "server_port": server_port,
            "vnc_port": vnc_port,
            "vnc_enabled": True,
            "service_pid": service_stats.get("pid"),
            "service_rss_mb": service_stats.get("rss_mb"),
            "service_commit_mb": service_stats.get("commit_mb"),
            "webapi_established": service_stats.get("webapi_established"),
            "webapi_close_wait": service_stats.get("webapi_close_wait"),
        }

    @app.post("/api/veyon/feature")
    async def veyon_feature(
        payload: Dict[str, Any],
        request: Request,
        user: RequestUser = Depends(_vnc_viewer_auth),
    ):
        """Управление функциями на удалённом хосте.

        Body:
            {"iid": "<device-iid>", "feature": "<token>", "arguments": {...}}
            features без аргументов: lock, unlock, powerdown, reboot, logoff,
                lock_input, unlock_input, logoff_webapi,
                ssh_lock, ssh_unlock, ssh_lock_input, ssh_unlock_input
                (powerdown/reboot/logoff и ssh_* реализованы через SSH
                в HostControlService, не через Veyon WebAPI; при отказе
                поле error содержит причину)
            features с аргументами:
                login      → arguments: {username, password}  (Veyon WebAPI)
                login_ssh  → arguments: {username, password}  (DBus org.mos.auth;
                             требует SDDM greeter активен + mos-auth-config enable)
                website    → arguments: {url}
                start_app  → arguments: {cmd}
                message    → arguments: {text}
            power_on (WoL) → MAC берётся из DataStore по iid; если отсутствует —
                arguments: {mac}

        Идентификация устройства — по iid (персистентный uuid). Реальный
        сетевой адрес резолвится на стороне web-сервера через
        _device_service.get_device_by_iid и используется только для исходящих
        соединений к хосту; клиенту не возвращается.
        """
        if _veyon_service is None and _host_control_service is None:
            raise HTTPException(503, "Сервисы недоступны")

        iid = payload.get("iid")
        feature = payload.get("feature")
        if not iid or not feature:
            raise HTTPException(400, "Требуются поля 'iid' и 'feature'")

        arguments = payload.get("arguments") or {}

        # Объединённая карта токенов → (метод, нужны_аргументы).
        # Veyon-зависимые фичи (WebAPI/RFB) — в VeyonService.FEATURE_MAP.
        # SSH/systemd/UDP/DBus-фичи — в HostControlService.FEATURE_MAP.
        target_service = None
        entry = None
        if _veyon_service is not None:
            entry = _veyon_service.FEATURE_MAP.get(feature)
            if entry:
                target_service = _veyon_service
        if entry is None and _host_control_service is not None:
            entry = _host_control_service.FEATURE_MAP.get(feature)
            if entry:
                target_service = _host_control_service
        if entry is None or target_service is None:
            available = {}
            if _veyon_service is not None:
                available.update(_veyon_service.FEATURE_MAP)
            if _host_control_service is not None:
                available.update(_host_control_service.FEATURE_MAP)
            raise HTTPException(
                400, f"Неизвестная функция: {feature}. Доступные: {list(available)}"
            )
        method_name, has_args = entry

        # Резолв iid -> DeviceModel; host — внутренний сетевой адрес.
        dev, err = _resolve_device(iid)
        if err is not None:
            raise err
        host = dev.host

        # power_on (WoL) — нужен MAC, не Veyon-соединение
        if feature == "power_on":
            mac = arguments.get("mac") or dev.mac_address
            if not mac:
                raise HTTPException(
                    400,
                    "MAC-адрес не задан для устройства. Укажите mac_address в arguments.",
                )
            loop = asyncio.get_event_loop()
            try:
                result = await run_in_tracked_executor(
                    _command_pool,
                    target_service.power_on,
                    mac,
                    metadata={
                        "host": host,
                        "type": "web_veyon_feature",
                        "feature": "power_on",
                        "device": dev.name or host,
                    },
                )
            except Exception as e:
                logger.error(f"WebAPI: power_on error: {e}")
                raise HTTPException(500, f"Ошибка выполнения: {e}")
            return {"success": bool(result), "feature": feature, "iid": iid}

        loop = asyncio.get_event_loop()
        try:
            if has_args:
                if feature in ("login", "login_ssh"):
                    args = (
                        arguments.get("username", ""),
                        arguments.get("password", ""),
                    )
                elif feature == "website":
                    args = (arguments.get("url", ""),)
                elif feature == "start_app":
                    args = (arguments.get("cmd", ""),)
                elif feature == "message":
                    args = (arguments.get("text", ""),)
                else:
                    args = ()
                result = await run_in_tracked_executor(
                    _command_pool,
                    lambda: getattr(target_service, method_name)(host, *args),
                    metadata={
                        "host": host,
                        "type": "web_veyon_feature",
                        "feature": feature,
                        "device": dev.name or host,
                    },
                )
            else:
                from src.services.operation_errors import run_capturing_errors

                # Причина отказа берётся в том же потоке пула, что и вызов.
                outcome = await run_in_tracked_executor(
                    _command_pool,
                    lambda: run_capturing_errors(
                        target_service,
                        lambda: getattr(target_service, method_name)(host),
                    ),
                    metadata={
                        "host": host,
                        "type": "web_veyon_feature",
                        "feature": feature,
                        "device": dev.name or host,
                    },
                )
                return {
                    "success": bool(outcome.value),
                    "error": "" if outcome.value else outcome.error,
                    "feature": feature,
                    "iid": iid,
                }
        except Exception as e:
            logger.error(f"WebAPI: feature error: {e}")
            raise HTTPException(500, f"Ошибка выполнения: {e}")

        return {"success": bool(result), "feature": feature, "iid": iid}

    # ===== Прямое управление питанием хоста через SSH (без Veyon WebAPI) =====

    @app.post("/api/host/power")
    async def host_power(
        payload: Dict[str, Any],
        request: Request,
        user: RequestUser = Depends(_vnc_viewer_auth),
    ):
        """Перезагрузка/выключение/завершение сеанса хоста через SSH.

        Body: {"iid": "<device-iid>", "action": "reboot"|"powerdown"|"logoff"}
        Выполняется через WebCommandExecutor в _command_pool.
        Не требует Veyon — работает на любом Linux-хосте с SSH-доступом.
        logoff: общий скрипт HostControlService завершает пользовательские
        X11/Wayland-сеансы (KDE через DBus, остальные через loginctl),
        не перезапуская display-manager. Таймаут выхода 45 сек.
        reboot/powerdown: ``HostControlService.send_power_command`` — успех
        значит «команда принята», обрыв SSH после отправки не ошибка.

        Идентификация устройства — по iid. Реальный сетевой адрес резолвится
        на стороне web-сервера через _resolve_device(iid).
        """
        if not _services_ready():
            raise HTTPException(503, "Сервисы не инициализированы")

        iid = payload.get("iid")
        action = payload.get("action")
        if not iid or action not in ("reboot", "powerdown", "logoff"):
            raise HTTPException(
                400, "Требуются поля 'iid' и 'action' (reboot|powerdown|logoff)"
            )

        dev, err = _resolve_device(iid)
        if err is not None or dev is None:
            raise err or HTTPException(404, "Устройство не найдено")

        metadata = {
            "host": dev.host,
            "type": "web_power",
            "action": action,
            "device": dev.name or dev.host,
        }
        if action != "logoff":
            # Хост гасит SSH сам: успех означает, что команда принята,
            # обрыв связи после отправки ошибкой не считается.
            if _host_control_service is None:
                raise HTTPException(503, "Сервисы недоступны")
            try:
                sent = await run_in_tracked_executor(
                    _command_pool,
                    _host_control_service.send_power_command,
                    dev.host,
                    action,
                    metadata=metadata,
                )
            except Exception as e:
                logger.error(f"WebAPI: host_power {action} iid={iid}: {e}")
                raise HTTPException(500, f"Ошибка выполнения: {e}")
            if not sent:
                raise HTTPException(500, "Команда не отправлена")
            return {"success": True, "action": action, "iid": iid, "output": ""}

        from src.services.hosts.host_control_service import HostControlService

        cmd = Command(
            {"type": "ssh", "text": HostControlService.build_logoff_command()}
        )

        executor = WebCommandExecutor()

        def _run():
            try:
                return executor.execute(dev, [cmd], timeout=45)
            finally:
                if _host_control_service is not None:
                    _host_control_service._close_veyon_connections(dev.host)

        try:
            output, ok = await run_in_tracked_executor(
                _command_pool, _run, metadata=metadata
            )
        except Exception as e:
            logger.error(f"WebAPI: host_power {action} iid={iid}: {e}")
            raise HTTPException(500, f"Ошибка выполнения: {e}")

        if not ok:
            raise HTTPException(500, f"Команда не выполнена: {output or 'нет вывода'}")
        return {"success": True, "action": action, "iid": iid, "output": output}

    # ===== Live-данные хоста для тултипа (общий коллектор с нод-сервером) =====

    @app.get("/api/host_info/{iid}")
    async def web_host_info(
        iid: str, user: RequestUser = Depends(require_auth_or_token)
    ):
        """Live-данные хоста для подсказки при наведении (веб-интерфейс).

        Формат: {"ok": True, "fields": {ключ: значение}, "rows": [[подпись,
        значение], ...]} — rows для отображения, fields для программной
        обработки. Офлайн/нет данных: {"ok": False, "reason": "offline"|
        "no_data"} (HTTP 200 — единый контракт с /api/node/host_info).
        Реализация — src/web/host_info_api.collect_host_info_fields
        (tcp_ping-гейт, HostInfoService-кэш TTL 60с, окно ожидания первого
        сбора до 4с); выполняется в anyio-пуле (блокирующий I/O).
        """
        dev, err = _resolve_device(iid)
        if err is not None:
            raise err
        from .host_info_api import collect_host_info_fields_async

        return await collect_host_info_fields_async(dev, source="web")

    # ===== Проверка доступности VNC / Veyon на хосте (TCP-пинг) =====

    @app.get("/api/vnc/available")
    async def vnc_available(
        iid: str, request: Request, user: RequestUser = Depends(_vnc_viewer_auth)
    ):
        """Проверить доступность VNC-сервера (x11vnc) на хосте.

        TCP-пинг к host:vnc_port, где host резолвится из iid через
        _device_service.get_device_by_iid. vnc_port — per-host override из
        DeviceModel.vnc_port, иначе config.app.veyon.vnc_port (default 5900).
        Таймаут 2 сек. Идентификация устройства — по iid.
        """
        dev, err = _resolve_device(iid)
        if err is not None:
            raise err
        port = 5900
        if getattr(dev, "vnc_port", None):
            port = int(dev.vnc_port)
        else:
            port = int(getattr(config.app.veyon, "vnc_port", 5900) or 5900)
        host = dev.host
        loop = asyncio.get_event_loop()
        ok = await _probe_tcp(
            host,
            port,
            2.0,
            {
                "host": host,
                "type": "web_status",
                "check": "vnc_available",
                "device": dev.name or host,
            },
        )
        return {"available": ok, "port": port}

    @app.get("/api/vnc/password")
    async def vnc_password(
        iid: str = "",
        user: RequestUser = Depends(_vnc_viewer_auth),
    ):
        """Вернуть VNC-пароль из конфига для передачи в noVNC.

        noVNC выполняет VNC Authentication (security type 2) на клиентской
        стороне — ему нужен пароль для ответа на challenge от x11vnc.

        iid опционален (default ""): vt-токен проверяется по нему, когда
        передан (федерация нод); Basic/token работают и без iid.
        """
        pwd = getattr(config.app.veyon, "vnc_password", "") or ""
        return {"password": pwd}

    @app.get("/api/krfb/password")
    async def krfb_password(
        iid: str = "",
        user: RequestUser = Depends(_vnc_viewer_auth),
    ):
        """Вернуть KRFB-пароль из конфига для передачи в noVNC.

        krfb в режиме noVNC использует тот же VNC Authentication
        (security type 2) на клиентской стороне. Один общий VNC-пароль
        на оба бекенда (ключ veyon.vnc_password).
        """
        pwd = getattr(config.app.veyon, "vnc_password", "") or ""
        return {"password": pwd}

    @app.get("/api/krfb/available")
    async def krfb_available(
        iid: str, request: Request, user: RequestUser = Depends(_vnc_viewer_auth)
    ):
        """Проверить доступность krfb на хосте.

        TCP-пинг к host:vnc_port (per-host vnc_port приоритетнее
        глобального config.app.veyon.vnc_port; порт общий с x11vnc).
        """
        dev, err = _resolve_device(iid)
        if err is not None:
            raise err
        port = int(
            getattr(dev, "vnc_port", None)
            or getattr(config.app.veyon, "vnc_port", 5900)
            or 5900
        )
        host = dev.host
        loop = asyncio.get_event_loop()
        ok = await _probe_tcp(
            host,
            port,
            2.0,
            {
                "host": host,
                "type": "web_status",
                "check": "krfb_available",
                "device": dev.name or host,
            },
        )
        return {"available": ok, "port": port}

    @app.get("/api/veyon/available")
    async def veyon_available(
        iid: str, request: Request, user: RequestUser = Depends(_vnc_viewer_auth)
    ):
        """Проверить доступность Veyon Server на хосте.

        TCP-пинг к host:server_port (config.app.veyon.server_port, default
        11100), где host резолвится из iid. Таймаут 2 сек; при неудаче —
        одна повторная проба через секунду (холодный Veyon Server стартует
        позже SSH и может не ответить за первые 2с — без ретрая фронт
        падает в дефолтный vnc-режим к пустому порту 5900).
        """
        dev, err = _resolve_device(iid)
        if err is not None:
            raise err
        port = int(getattr(config.app.veyon, "server_port", 11100) or 11100)
        host = dev.host
        loop = asyncio.get_event_loop()

        async def _probe():
            return await _probe_tcp(
                host,
                port,
                2.0,
                {
                    "host": host,
                    "type": "web_status",
                    "check": "veyon_available",
                    "device": dev.name or host,
                },
            )

        ok = await _probe()
        if not ok:
            await asyncio.sleep(1.0)
            ok = await _probe()
        return {"available": ok, "port": port}

    @app.get("/api/veyon/icon-set")
    async def veyon_icon_set(user: RequestUser = Depends(require_auth_or_token)):
        """Отдать комплект SVG-иконок Veyon-фич одним запросом.

        Возвращает JSON {lock: "<svg>...", unlock: "...", ...} с сырыми SVG-строками.
        Cache-Control: public, max-age=3600.
        """
        features = (
            "lock",
            "unlock",
            "powerdown",
            "reboot",
            "logoff",
            "lock_input",
            "unlock_input",
            "login",
            "website",
            "start_app",
            "message",
            "power_on",
            "restart_service",
        )
        out: Dict[str, Any] = {}
        for f in features:
            try:
                data = _read_asset_bytes(f"veyon_{f}.svg")
                out[f] = data.decode("utf-8")
            except HTTPException:
                out[f] = None
        # Логотипы для кнопок-тогглеров панелей:
        # «Veyon» — menu_veyon.svg, «Управление» — menu_veyon_control.svg
        for key, asset in (
            ("veyon", "menu_veyon.svg"),
            ("control", "menu_veyon_control.svg"),
        ):
            try:
                out[key] = _read_asset_bytes(asset).decode("utf-8")
            except HTTPException:
                out[key] = None
        return JSONResponse(
            content=out,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # ===== Иконки онлайн/оффлайн (для режима «иконки») =====
    # Браузер не шлёт Basic-заголовок для <img>, поэтому auth через ?token=.

    @app.get("/api/icon/{iid}")
    async def device_icon(
        iid: str, s: str = None, user: RequestUser = Depends(require_auth_or_token)
    ):
        """Вернуть SVG-иконку устройства по статусу (online/offline).

        Если передан ?s=online|offline|default — отдаёт соответствующий SVG
        **без пинга и без резолва iid** (статус уже известен клиенту из batch
        SSE-зонда /api/hosts/probe).
        Без ?s= — резолвит iid -> host и проверяет через общий пробер.
        """
        if s == "online":
            return _serve_asset_svg("online.svg", cache=False)
        if s == "offline":
            return _serve_asset_svg("offline.svg", cache=False)
        if s == "default":
            return _serve_asset_svg("default.svg", cache=False)
        if s == "veyon_down":
            return _serve_asset_svg("veyon_down.svg", cache=False)
        dev, err = _resolve_device(iid)
        if err is not None:
            raise err
        host = dev.host
        online = await _probe_tcp(
            host,
            _ssh_port(),
            1.5,
            {
                "host": host,
                "type": "web_status",
                "check": "icon",
                "device": dev.name or host,
            },
        )
        icon_name = "online.svg" if online else "offline.svg"
        return _serve_asset_svg(icon_name, cache=False)

    @app.get("/api/icon-set")
    async def icon_set(user: RequestUser = Depends(require_auth_or_token)):
        """Отдать комплект SVG-иконок (online/offline/default) одним запросом.

        Возвращает JSON {online: "<svg>...", offline: "...", default: "..."} с
        сырыми SVG-строками. Фронт конвертирует их в data-URL и кэширует на
        сессию, чтобы не запрашивать одни и те же иконки 60+ раз для оффлайн-хостов.

        Cache-Control: public, max-age=3600 — иконки не меняются между сессиями.
        Auth через Basic ИЛИ ?token= (для использования на старте сессии).
        """
        statuses = ("online", "offline", "default", "veyon_down")
        out: Dict[str, Any] = {}
        for s in statuses:
            try:
                data = _read_asset_bytes(f"{s}.svg")
                out[s] = data.decode("utf-8")
            except HTTPException:
                out[s] = None
        out["thumbnail"] = _thumbnail_theme()
        return JSONResponse(
            content=out,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.post("/api/hosts/probe")
    async def hosts_probe(
        payload: Dict[str, Any], user: RequestUser = Depends(require_auth)
    ):
        """Унифицированный SSE-зонд хостов: статус и (опционально) миниатюры.

        Тело:
            {"iids": ["<device-iid>", ...], "thumbnail": true|false,
             "width": 320, "height": 180}
        Ответ: Server-Sent Events (text/event-stream), по одному событию на iid:
            data: {"iid": "...", "online": true, "thumbnail": null}
            data: {"iid": "...", "online": true, "thumbnail": "<base64>", "mime": "image/png"}
            data: {"iid": "...", "online": false, "thumbnail": null}
            ...
            event: done
            data: {}

        Идентификация устройств — по iid (персистентный uuid). Реальный host
        резолвится через _device_service.get_device_by_iid и используется только
        для исходящих TCP/Veyon-вызовов внутри web-сервера. Клиенту host не
        возвращается. iid уникален per-device — каждое устройство пингуется
        отдельно (без дедупликации по host).

        thumbnail=false — только TCP-пинг к ssh_port (быстро, ~1с) через
        общий пробер. Поле thumbnail в событии всегда null.
        thumbnail=true — TCP-пинг через общий пробер, затем (если онлайн и
        Veyon доступен) get_thumbnail через _thumb_pool (10-20с). Оффлайн-хосты
        отдаются быстро (thumbnail=null) — не ждут Veyon.

        Результаты отдаются по мере завершения каждого хоста (as_completed),
        без батчирования на стороне клиента — web.thread_count виден как
        скорость постепенного появления статусов/миниатюр.

        Cooperative cancellation: при отключении клиента (GeneratorExit) или
        отмене запроса выставляется threading.Event — рабочие потоки пулов
        проверяют его на старте и после дорогого вызова и сразу возвращают
        None, не выполняя Veyon-аутентификацию / TCP-пинг впустую. Уже бегущие
        блокирующие вызовы (C-уровень) прервать нельзя, но их результат
        отбрасывается, а поток освобождается по собственному таймауту
        (Veyon wait_for_framebuffer ≤10с, TCP connect ≤1с). Очередные задачи
        в пулах отменяются мгновенно — не удерживают потоки впустую.
        """
        iids = payload.get("iids", [])
        if not isinstance(iids, list):
            raise HTTPException(400, "Ожидается {iids: [...]}")
        iids = [i for i in iids if isinstance(i, str)]
        if not iids:
            return JSONResponse({})

        want_thumb = bool(payload.get("thumbnail", False))
        # Omitted sizes use this instance's own defaults, clamped to its
        # configured maximum; explicit sizes are validated strictly below.
        default_width, default_height = _default_thumbnail_dimensions()
        width, height = _thumbnail_dimensions(
            payload.get("width", default_width),
            payload.get("height", default_height),
        )

        cancelled = threading.Event()
        thumbnail_service = _thumbnail_service
        pool = _thumb_pool
        if pool is None:
            raise HTTPException(503, "Пулы веб-сервера недоступны")

        def _do_thumbnail(dev: DeviceModel, online: bool) -> ThumbnailResult:
            if thumbnail_service is None:
                return ThumbnailResult(status=ThumbnailStatus.BUSY)
            backend = thumbnail_service.effective_backend(dev)
            if cancelled.is_set():
                return ThumbnailResult(
                    backend=backend, status=ThumbnailStatus.CANCELLED
                )
            result = thumbnail_service.get_thumbnail(dev, width, height, online=online)
            if cancelled.is_set():
                return ThumbnailResult(
                    backend=backend, status=ThumbnailStatus.CANCELLED
                )
            return result

        async def _probe(dev: DeviceModel) -> dict[str, Any]:
            iid, host, dev_name = dev.iid, dev.host, dev.name
            # Per-iid random stagger: размазывает сабмишены во времени, чтобы
            # миниатюры появлялись постепенно (а не пачками по thread_count), а
            # пул стабильно работал под нагрузкой (видно в Мониторе пулов).
            # 0 = отключить. Имитирует GUI-паттерн (per-host QTimer + jitter).
            stagger = config.app.thumbnails.stagger_sec
            if stagger > 0:
                await asyncio.sleep(random.uniform(0, stagger))
            if cancelled.is_set():
                return _thumbnail_event(iid, False)
            # TCP-пинг — всегда (не занимает поток на весь таймаут).
            online = await _probe_tcp(
                host,
                _ssh_port(),
                1.0,
                {
                    "host": host,
                    "type": "web_status",
                    "check": "probe",
                    "device": dev_name or host,
                },
            )
            if not want_thumb or thumbnail_service is None:
                return _thumbnail_event(iid, online)
            result = await run_in_tracked_executor(
                pool,
                _do_thumbnail,
                dev,
                online,
                metadata={
                    "host": host,
                    "type": "web_thumbnail",
                    "device": dev_name or host,
                },
            )
            return _thumbnail_event(iid, online, result)

        # Резолв всех iid -> host upfront (быстро, без I/O). Нерезолвящиеся iid
        # отдают событие offline (как если бы хост не ответил на пинг).
        probe_tasks = []
        for iid in iids:
            dev, err = _resolve_device(iid)
            if err is not None:
                # Нерезолвленный iid — отдельная корутина, отдаёт offline сразу
                # без отправки в пул. Логируем для отладки.
                logger.debug(f"WebAPI: probe skip unresolved iid={iid}: {err.detail}")

                async def _skip(_iid=iid):
                    return _thumbnail_event(_iid, False)

                probe_tasks.append(asyncio.ensure_future(_skip()))
            else:
                probe_tasks.append(asyncio.ensure_future(_probe(dev)))

        async def event_gen():
            try:
                for coro in asyncio.as_completed(probe_tasks):
                    out = await coro
                    yield f"data: {json.dumps(out)}\n\n"
                yield "event: done\ndata: {}\n\n"
            except (asyncio.CancelledError, GeneratorExit):
                # Сигнализировать рабочим потокам: отмениться. Очередные задачи
                # в пулах проверят флаг на старте и вернут None без Veyon/пинга.
                cancelled.set()
                for t in probe_tasks:
                    if not t.done():
                        t.cancel()
                raise
            else:
                for t in probe_tasks:
                    if not t.done():
                        t.cancel()

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/thumbnail/{iid}")
    async def thumbnail(
        iid: str,
        request: Request,
        width: str = "",
        height: str = "",
        user: RequestUser = Depends(require_auth_or_token),
    ):
        """Получить миниатюру экрана устройства через Veyon WebAPI.

        Возвращает изображение:
        - PNG-миниатюра — если Veyon доступен и хост онлайн с framebuffer.
        - offline.svg (красный) — если хост офлайн.
        - default.svg (серый) — если хост онлайн, но миниатюры нет (Veyon недоступен
          или framebuffer пуст).

        Auth через Basic ИЛИ ?token= (для <img>). Идентификация устройства — по iid;
        реальный host резолвится через _resolve_device и используется только для
        исходящих TCP/Veyon-вызовов.
        """
        # Omitted query sizes fall back to this instance's clamped defaults.
        default_width, default_height = _default_thumbnail_dimensions()
        if "width" not in request.query_params:
            width = str(default_width)
        if "height" not in request.query_params:
            height = str(default_height)
        request_width, request_height = _thumbnail_dimensions(width, height, query=True)
        dev, err = _resolve_device(iid)
        if err is not None:
            raise err
        pool = _thumb_pool
        if pool is None:
            raise HTTPException(503, "Пулы веб-сервера недоступны")
        online = await _probe_tcp(
            dev.host,
            _ssh_port(),
            1.0,
            {
                "host": dev.host,
                "type": "web_status",
                "check": "thumbnail",
                "device": dev.name,
            },
        )
        thumbnail_service = _thumbnail_service
        if thumbnail_service is None:
            response = _serve_asset_svg(
                "online.svg" if online else "offline.svg", cache=False
            )
            response.headers["X-Host-Online"] = str(bool(online)).lower()
            return response
        result = await run_in_tracked_executor(
            pool,
            lambda: thumbnail_service.get_thumbnail(
                dev, request_width, request_height, online=online
            ),
            metadata={"host": dev.host, "type": "web_thumbnail", "device": dev.name},
        )
        if result.data and result.status == ThumbnailStatus.OK:
            return Response(
                content=result.data,
                media_type=result.mime,
                headers=_thumbnail_headers(result, online),
            )
        icon = (
            "offline.svg"
            if not online
            else "veyon_down.svg"
            if result.backend_ok is False
            else "online.svg"
        )
        response = _serve_asset_svg(icon, cache=False)
        response.headers.update(_thumbnail_headers(result, online))
        return response

    @app.get("/api/hosts")
    async def get_hosts(user: RequestUser = Depends(require_auth)):
        if not _services_ready():
            raise HTTPException(503, "Сервисы не инициализированы")
        raw = _device_service.get_devices_raw()
        return _normalize_hosts(raw)

    @app.get("/api/commands")
    async def get_commands(user: RequestUser = Depends(require_auth)):
        if not _services_ready():
            raise HTTPException(503, "Сервисы не инициализированы")
        data = _command_service.get_all_commands()
        return _normalize_commands(data)

    # ===== WebSocket выполнения команд =====

    @app.websocket("/ws/execute")
    async def ws_execute(ws: WebSocket):
        # Аутентификация через token (base64(user:pass)) в query — браузеры
        # не отправляют Basic-заголовок при WebSocket. Проверка — локальные
        # пользователи, затем LDAP (multi-user).
        token = ws.query_params.get("token")
        creds = _decode_token(token) if token else None
        username = await authenticate_async(*creds) if creds else None
        if username is None:
            await ws.close(code=4401)
            return

        # owner = username (многопользовательский режим).
        session_id = ws.query_params.get("session")
        owner = username

        # job_id + last_seq — переподключение к существующему заданию (refresh).
        reconnect_job_id = ws.query_params.get("job_id")
        try:
            last_seq = int(ws.query_params.get("last_seq", 0) or 0)
        except (TypeError, ValueError):
            last_seq = 0

        await ws.accept()

        # Очередь событий из JobManager → в WebSocket. Сюда fan-out'ятся
        # события от подписанного задания (как live, так и replay).
        out_queue: "asyncio.Queue[Optional[JobEvent]]" = asyncio.Queue()
        loop = asyncio.get_event_loop()

        # Текущее задание, к которому подключён этот WS.
        current_job: Optional[Job] = None
        current_sub_queue: Optional["asyncio.Queue[JobEvent]"] = None

        def send_event(evt: JobEvent) -> None:
            """Переслать событие JobEvent в WebSocket (через очередь)."""
            try:
                ws_dict = evt.to_dict()
                loop.call_soon_threadsafe(out_queue.put_nowait, _WSItem(ws_dict))
            except Exception:
                pass

        async def sender():
            """Пересылает сообщения из очереди в WebSocket."""
            while True:
                item = await out_queue.get()
                if item is None:
                    break
                try:
                    await ws.send_json(item.data)
                except Exception:
                    break

        sender_task = asyncio.create_task(sender())

        async def subscribe_to_job(job: Job, replay_since: int) -> None:
            """Подписаться на задания: дослать снимок состояний + события, затем live."""
            nonlocal current_sub_queue
            # Replay пропущенных событий (seq > replay_since).
            replay_data = await asyncio.to_thread(
                _job_manager.events_since, job.id, replay_since
            )
            if replay_data is None:
                return
            # Уведомить о truncation, если буфер переполнился.
            if replay_data["truncated"] and replay_since < (
                replay_data["events"][0]["seq"] if replay_data["events"] else 0
            ):
                await out_queue.put(
                    _WSItem(
                        {
                            "type": "truncated",
                            "job_id": job.id,
                            "message": "Часть истории вывода утеряна (буфер переполнен)",
                        }
                    )
                )
            # Снимок состояний хостов: для каждого хоста — последнее известное
            # device_start/device_result, даже если оно было до last_seq.
            # Без этого карточки хостов, завершившихся до disconnect, зависают
            # в «Восстановление». События с seq <= replay_since не досылаются —
            # но снимок содержит актуальный статус (без seq, клиент применит
            # его как «sync_state» и не учтёт для persistJobSeq).
            replayed_seqs = {e["seq"] for e in replay_data["events"]}
            for host, state in (replay_data.get("host_states") or {}).items():
                # Пропустить если это событие и так будет в replay (seq > last_seq).
                if state.get("seq") in replayed_seqs:
                    continue
                # Пометить как снимок — клиент применит статус к буферу,
                # но не обновит persistJobSeq (чтобы не «прыгнуть назад»).
                state = dict(state)
                state["snapshot"] = True
                await out_queue.put(_WSItem(state))
            for evt_dict in replay_data["events"]:
                await out_queue.put(_WSItem(evt_dict))
            # Если задание уже завершено — дослать finished (если ещё не в replay)
            # и не подписываться на live (нечего ждать).
            if job.status in ("finished", "aborted", "error"):
                return
            # Подписка на live-события.
            current_sub_queue = await _job_manager.subscribe(job.id)
            # Фоновая задача: переливает live-события из подписки в out_queue.
            asyncio.create_task(_relay_live(current_sub_queue, out_queue))

        async def _relay_live(
            sub_q: "asyncio.Queue[JobEvent]", out_q: "asyncio.Queue"
        ) -> None:
            """Переливать live-события из очереди подписки в очередь WS."""
            while True:
                evt = await sub_q.get()
                if evt is None:
                    break
                await out_q.put(_WSItem(evt.to_dict()))

        async def _stop_current_sub() -> None:
            """Отписаться от текущей подписки и остановить _relay_live."""
            nonlocal current_sub_queue, current_job
            if (
                current_sub_queue is not None
                and current_job is not None
                and _job_manager is not None
            ):
                _job_manager.unsubscribe(current_job.id, current_sub_queue)
                # Положить None, чтобы _relay_live завершилась.
                try:
                    current_sub_queue.put_nowait(None)
                except Exception:
                    pass
            current_sub_queue = None

        async def start_execution(msg: dict) -> None:
            """Создать Job и запустить выполнение."""
            nonlocal current_job, current_sub_queue
            try:
                await _start_execution_impl(msg)
            except Exception as e:
                # Любая непредвиденная ошибка запуска (несоответствие типов,
                # падение JobManager/пула, и пр.) — отправить клиенту осмысленное
                # событие error вместо молчаливого обрыва WebSocket. Без этого
                # исключение всплывает в ws_execute → WS закрывается, карточки
                # хостов зависают в «Ожидание», пользователь видит «Соединение
                # закрыто» без причины.
                logger.error(f"WebAPI: ошибка запуска задания: {e}", exc_info=True)
                try:
                    await out_queue.put(
                        _WSItem({"type": "error", "message": f"Ошибка запуска: {e}"})
                    )
                except Exception:
                    pass

        async def _start_execution_impl(msg: dict) -> None:
            nonlocal current_job, current_sub_queue
            if _job_manager is None:
                await out_queue.put(
                    _WSItem(
                        {"type": "error", "message": "JobManager не инициализирован"}
                    )
                )
                return
            task_iid = msg.get("task_iid")
            device_iids = msg.get("hosts", [])  # массив iid устройств
            params = msg.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                await out_queue.put(
                    _WSItem(
                        {
                            "type": "error",
                            "message": "Параметры команды должны быть словарем",
                        }
                    )
                )
                return
            timeout = msg.get("timeout", config.app.ssh.command_timeout)

            if not task_iid or not device_iids:
                await out_queue.put(
                    _WSItem(
                        {"type": "error", "message": "Не указана задача или устройства"}
                    )
                )
                return

            if not _services_ready():
                await out_queue.put(
                    _WSItem({"type": "error", "message": "Сервисы не инициализированы"})
                )
                return

            task = _command_service.get_command_by_iid(task_iid)
            if not task:
                await out_queue.put(
                    _WSItem(
                        {
                            "type": "error",
                            "message": f"Задача iid={task_iid} не найдена",
                        }
                    )
                )
                return

            commands_list = task.get("commands", [])
            try:
                commands = [Command(c) for c in commands_list]
            except Exception as e:
                await out_queue.put(
                    _WSItem({"type": "error", "message": f"Ошибка разбора команд: {e}"})
                )
                return

            missing = _missing_task_params(task, params, msg.get("uploads"))
            if missing:
                await out_queue.put(
                    _WSItem(
                        {
                            "type": "error",
                            "message": "Не заполнены параметры: " + ", ".join(missing),
                        }
                    )
                )
                return

            task_timeout = task.get("timeout", timeout)
            use_session = bool(task.get("session", False))
            desktop_session = task.get("desktop_session", False)

            # Плоский словарь устройств, индексированный iid (fallback на host).
            # DeviceService.get_all_devices() возвращает плоский список dict'ов через
            # DataStore.get_hosts_data() — единая реализация, без дублирования
            # логики обхода дерева с группами. Оборачиваем в DeviceModel: далее
            # весь конвейер (_run_single, WebCommandExecutor, SSHWorker) работает
            # с атрибутами (device.iid, device.host, ...) и ожидает
            # DeviceModel, а не сырой dict.
            devices = [DeviceModel(d) for d in _device_service.get_all_devices()]
            devices_map: Dict[str, DeviceModel] = {d.iid or d.host: d for d in devices}

            targets: List[DeviceModel] = []
            for diid in device_iids:
                dev = devices_map.get(diid)
                if dev is not None:
                    targets.append(dev)
                else:
                    await out_queue.put(
                        _WSItem(
                            {
                                "type": "error",
                                "device_iid": diid,
                                "message": f"Устройство iid={diid} не найдено",
                            }
                        )
                    )

            if not targets:
                await out_queue.put(
                    _WSItem(
                        {
                            "type": "finished",
                            "summary": {"success": 0, "error": 0, "total": 0},
                        }
                    )
                )
                return

            executor = WebCommandExecutor()
            job = _job_manager.create_job(
                owner=owner,
                task_iid=task_iid,
                device_iids=[d.iid for d in targets],
                params=params,
                timeout=task_timeout,
                executor=executor,
            )
            current_job = job

            # Подписаться на live-события ДО отправки start — чтобы подписчик
            # гарантированно получил start (и все последующие) через live-канал.
            # Replay не нужен (задание только что создано, буфер пуст).
            await subscribe_to_job(job, replay_since=job.event_seq)

            # Событие start — в буфер + fan-out (подписчик получит его live).
            start_evt = job.append_event(
                "start",
                task_iid=task_iid,
                task_name=task.get("name", ""),
                device_iids=[d.iid for d in targets],
                device_hosts=[d.host for d in targets],
                timeout=task_timeout,
                job_id=job.id,
            )
            _job_manager.dispatch_event(job, start_evt)

            # Запустить выполнение в глобальном пуле команд.
            def _run():
                tokens = msg.get("uploads", {})
                if not isinstance(tokens, dict):
                    raise ValueError("Некорректный список загруженных файлов")
                params.update(uploads.claim(owner, task_iid, task.get("params", []), tokens))
                job.params.update(params)

                def release_uploads(_future=None):
                    for token in tokens.values():
                        uploads.discard(token)

                try:
                    future = _run_parallel(
                        job,
                        targets,
                        commands,
                        task_timeout,
                        params,
                        _job_manager,
                        session=use_session,
                        desktop_session=desktop_session,
                    )
                except BaseException:
                    release_uploads()
                    raise
                future.add_done_callback(release_uploads)
                return future

            _job_manager.submit_run(job, _run)

        try:
            # Если переподключение к существующему заданию — подписаться сразу.
            if reconnect_job_id and _job_manager is not None:
                job = _job_manager.get(reconnect_job_id)
                if job is None:
                    await out_queue.put(
                        _WSItem({"type": "error", "message": "Задание не найдено"})
                    )
                elif job.owner != owner:
                    await out_queue.put(
                        _WSItem(
                            {
                                "type": "error",
                                "message": "Доступ к чужому заданию запрещён",
                            }
                        )
                    )
                else:
                    current_job = job
                    await subscribe_to_job(job, replay_since=last_seq)

            while True:
                msg = await ws.receive_json()
                action = msg.get("action")

                if action == "execute":
                    # Viewer-режим (подключение с job_id в query) — нельзя
                    # запускать новое задание, это WS только для просмотра.
                    if reconnect_job_id:
                        await out_queue.put(
                            _WSItem(
                                {
                                    "type": "error",
                                    "message": "Это соединение для просмотра задания. Откройте основную страницу для запуска.",
                                }
                            )
                        )
                        continue
                    # Создаём новое задание. Старую подписку (если была) отписываем.
                    await _stop_current_sub()
                    await start_execution(msg)

                elif action == "abort":
                    if current_job is not None and _job_manager is not None:
                        _job_manager.abort(current_job.id)
                    # Не шлём "aborted" вручную — JobManager.finalize положит событие.

                elif action == "ping":
                    await out_queue.put(_WSItem({"type": "pong"}))

        except WebSocketDisconnect:
            logger.debug("WebAPI: WebSocket отключён")
        except Exception as e:
            logger.error(f"WebAPI: ошибка WebSocket: {e}")
        finally:
            # ВАЖНО: НЕ вызываем executor.abort() при disconnect — задание
            # продолжает работать. Только отписываемся от live-событий.
            await _stop_current_sub()
            await out_queue.put(None)
            await sender_task

    # ===== WebSocket RFB-прокси для браузерного VNC-доступа =====

    @app.websocket("/ws/vnc/{iid}")
    async def ws_vnc(ws: WebSocket, iid: str):
        """WebSocket↔RFB binary proxy для noVNC.

        Режим определяется query-параметром ?mode=vnc|veyon (обязательный):
          - "veyon": WebSocket↔Veyon RFB proxy. Открывает TCP к Veyon Server
            (host:server_port, default 11100), выполняет Veyon handshake
            (security type 40 + RSA key-file auth), подделывает стандартный
            RFB 3.8 handshake для noVNC, фильтрует client→server сообщения.
          - "vnc": WebSocket↔прямой VNC (x11vnc). Открывает TCP к host:vnc_port
            (5900), передаёт байты напрямую — noVNC сам выполняет RFB handshake.
            Фильтрация сообщений отключена (clipboard, file transfer, DesktopSize
            работают полноценно).

        Аутентификация через ?token= (base64(user:pass)) в обоих режимах.
        Идентификация устройства — по iid (path-параметр). Реальный host
        Резолв через _resolve_device используется только для исходящих
        TCP/RFB-соединений внутри web-сервера; клиенту не возвращается.
        """
        logger.info(f"VNC WS endpoint called: iid={iid}")

        token = ws.query_params.get("token")
        creds = _decode_token(token) if token else None
        username = await authenticate_async(*creds) if creds else None
        if username is None:
            # Альтернатива: capability-токен ноды (?vt=) — федерация нод
            # (node_vnc_url строит vnc.html?...&vt=<capability>).
            vt = ws.query_params.get("vt")
            if not (vt and verify_vnc_access(vt, iid)):
                logger.warning(f"VNC WS: auth failed for iid={iid}")
                await ws.close(code=4401)
                return
            username = "vnc-viewer"
        logger.info(f"VNC WS: auth OK, username={username}")

        if _veyon_service is None:
            logger.warning("VNC WS: _veyon_service is None")
            await ws.close(code=4503, reason="Сервис удалённого доступа недоступен")
            return

        # Режим подключения: явный ?mode=vnc|krfb|veyon из query обязателен.
        # Фронтенд (vnc.js) определяет mode через /api/vnc|krfb|veyon/available
        # (автоопределение per-host) и передаёт его в WS URL.
        mode_param = ws.query_params.get("mode")
        is_vnc_mode = mode_param in ("vnc", "krfb")
        if mode_param not in ("vnc", "krfb", "veyon"):
            logger.warning(f"VNC WS: invalid mode={mode_param!r} for iid={iid}")
            await ws.close(
                code=4400, reason="Параметр mode обязателен (vnc|krfb|veyon)"
            )
            return
        logger.info(f"VNC WS: mode={mode_param}, is_vnc={is_vnc_mode}, iid={iid}")

        if not is_vnc_mode and not getattr(
            config.app.veyon, "rfb_bridge_enabled", True
        ):
            logger.warning("VNC WS: RFB bridge disabled, closing")
            await ws.close(code=4503, reason="RFB bridge отключён")
            return

        # Резолв iid -> DeviceModel. Для VNC-режима нужен DeviceModel для per-host
        # vnc_port override. Для Veyon-режима — только host (сетевой адрес).
        device, err = _resolve_device(iid)
        if err is not None:
            logger.warning(f"VNC WS: iid {iid} not resolved: {err.detail}")
            await ws.close(
                code=4404,
                reason=err.detail
                if isinstance(err.detail, str)
                else "Устройство не найдено",
            )
            return
        host = device.host
        logger.info(f"VNC WS: resolved iid={iid} -> host={host}")

        # noVNC открывает WS с subprotocol='binary'. Если сервер не
        # подтверждает subprotocol в accept, браузер рвёт соединение (code
        # 1006). Подтверждаем 'binary', если клиент его запросил.
        requested = ws.headers.get("sec-websocket-protocol", "")
        subprotocol = "binary" if "binary" in requested else None
        logger.info(
            f"VNC WS: accepting with subprotocol={subprotocol!r}, requested={requested!r}"
        )
        await ws.accept(subprotocol=subprotocol)
        logger.info(f"VNC WS: accepted, mode={mode_param}, iid={iid}, host={host}")

        # Открываем RFB-соединение в отдельном потоке.
        # В Veyon-режиме handshake может занять 10-60 сек (access-control dialog).
        # В VNC-режимe — обычный TCP connect (до 10с).
        loop = asyncio.get_running_loop()
        rfb_sock: Optional[socket.socket] = None
        try:
            logger.info(
                f"VNC WS: opening RFB connection, mode={mode_param}, iid={iid}, host={host}"
            )
            if is_vnc_mode:
                result = await run_in_tracked_executor(
                    _rfb_pool,
                    lambda: _veyon_service.open_rfb_connection(
                        host,
                        username=username,
                        device=device,
                        mode=mode_param,
                    ),
                    metadata={
                        "host": host,
                        "type": "web_rfb",
                        "mode": mode_param,
                        "device": device.name or host,
                    },
                )
            else:
                result = await run_in_tracked_executor(
                    _rfb_pool,
                    lambda: _veyon_service.open_rfb_connection(
                        host, username=username, mode="veyon"
                    ),
                    metadata={
                        "host": host,
                        "type": "web_rfb",
                        "mode": "veyon",
                        "device": device.name or host,
                    },
                )
            logger.info(
                f"VNC WS: open_rfb_connection returned: {result is None and 'None' or f'sock={result[0]}, init={len(result[1]) if result[1] else None}b'}"
            )
            if result is None:
                reason = (
                    "VNC connect failed"
                    if is_vnc_mode
                    else "Veyon auth/access control failed"
                )
                if mode_param == "krfb":
                    reason = "KRFB connect failed"
                logger.warning(
                    f"VNC WS: open_rfb_connection failed, closing ws: {reason}"
                )
                await ws.close(code=4503, reason=reason)
                return

            rfb_sock, server_init_bytes = result

            rfb_sock.setblocking(False)
            try:
                rfb_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass

            from src.services.remote.veyon_rfb_bridge import filter_rfb_client_message

            if is_vnc_mode:
                # === VNC-режим: прямая проксировка без fake handshake ===
                # server_init_bytes is None — VNC-сервер сам выполнит RFB
                # handshake с noVNC (ProtocolVersion, security, ServerInit).
                # Прокси только пересылает байты в обе стороны.
                closed = asyncio.Event()
                _vnc_recv_seq = 0
                _vnc_sent_seq = 0

                async def vnc_ws_to_server():
                    nonlocal _vnc_sent_seq
                    while not closed.is_set():
                        try:
                            data = await ws.receive_bytes()
                        except (WebSocketDisconnect, Exception) as e:
                            logger.info(
                                f"VNC ws→server: receive_bytes ended: {type(e).__name__}: {e}"
                            )
                            break
                        if not data:
                            logger.info("VNC ws→server: empty data, closing")
                            break
                        _vnc_sent_seq += 1
                        logger.info(
                            f"VNC ws→server #{_vnc_sent_seq}: {len(data)}b"
                        )
                        try:
                            await loop.sock_sendall(rfb_sock, data)
                        except Exception as e:
                            logger.error(f"VNC-IN: sendall failed: {e}")
                            break
                    logger.info("VNC ws→server: loop ended")
                    closed.set()

                async def vnc_server_to_ws():
                    nonlocal _vnc_recv_seq
                    while not closed.is_set():
                        try:
                            data = await loop.sock_recv(rfb_sock, 65536)
                        except Exception as e:
                            logger.info(
                                f"VNC server→ws: sock_recv ended: {type(e).__name__}: {e}"
                            )
                            break
                        if not data:
                            logger.info(
                                "VNC server→ws: EOF (empty recv), x11vnc closed connection"
                            )
                            break
                        _vnc_recv_seq += 1
                        logger.info(
                            f"VNC server→ws #{_vnc_recv_seq}: {len(data)}b"
                        )
                        try:
                            await ws.send_bytes(data)
                        except Exception as e:
                            logger.info(
                                f"VNC server→ws: send_bytes failed: {type(e).__name__}: {e}"
                            )
                            break
                    logger.info("VNC server→ws: loop ended")
                    closed.set()

                logger.info(
                    f"VNC proxy started: iid={iid}, host={host}, mode=vnc, sock={rfb_sock}"
                )
                await run_duplex(vnc_ws_to_server(), vnc_server_to_ws())
                logger.info(
                    f"VNC proxy ended: iid={iid}, host={host}, recv={_vnc_recv_seq}, sent={_vnc_sent_seq}"
                )

            else:
                # === Veyon-режим: fake RFB handshake + фильтрация ===
                # ДРЕНИРУЕМ сокет: Veyon может прислать дополнительные данные
                # (access control messages) после ServerInit.
                leftover = bytearray()
                while True:
                    try:
                        chunk = rfb_sock.recv(65536)
                        if chunk:
                            leftover.extend(chunk)
                        else:
                            break
                    except BlockingIOError:
                        break
                    except Exception:
                        break
                if leftover:
                    logger.debug(
                        f"VNC: discarded {len(leftover)}b leftover after ServerInit"
                    )

                # Прокси уже сделал Veyon handshake (security type 40 + RSA) на
                # стороне Python. Для браузерного RFB-клиента (noVNC) нужно
                # притвориться обычным VNC-сервером: провести стандартный RFB
                # 3.8 handshake с security type None(1), после чего отдать
                # ServerInit от Veyon. noVNC не знает Veyon security type 40 и
                # падает на "Invalid server version", если первым сообщением
                # пришёл не ProtocolVersion.
                #
                # Полная последовательность (server → client / client → server):
                #   S: ProtocolVersion  "RFB 003.008\n" (12 bytes)
                #   C: ProtocolVersion  (12 bytes) — отбрасываем
                #   S: Security types   [0x01, 0x01] (count=1, type=None=1)
                #   C: chosen type      (1 byte) — отбрасываем
                #   S: SecurityResult   [0x00,0x00,0x00,0x00] (4 bytes, OK)
                #   C: ClientInit       (1 byte, shared flag) — отбрасываем
                #   S: ServerInit       (server_init_bytes)
                client_handshake_buf = bytearray()

                async def _drain_client(n: int) -> bytes:
                    while len(client_handshake_buf) < n:
                        data = await ws.receive_bytes()
                        if not data:
                            raise WebSocketDisconnect()
                        client_handshake_buf.extend(data)
                    out = bytes(client_handshake_buf[:n])
                    del client_handshake_buf[:n]
                    return out

                try:
                    await ws.send_bytes(b"RFB 003.008\n")
                    await _drain_client(12)
                    await ws.send_bytes(bytes([0x01, 0x01]))
                    await _drain_client(1)
                    await ws.send_bytes(b"\x00\x00\x00\x00")
                    await _drain_client(1)
                    await ws.send_bytes(server_init_bytes)
                except WebSocketDisconnect:
                    logger.debug(
                        f"WebAPI: VNC WS disconnected during handshake for iid={iid} host={host}"
                    )
                    return
                except Exception as e:
                    logger.error(
                        f"WebAPI: VNC handshake with client failed for iid={iid} host={host}: {e}"
                    )
                    return

                # «Лишние» байты клиента — отправляем в Veyon как начало потока.
                if client_handshake_buf:
                    leftover_client = bytes(client_handshake_buf)
                    try:
                        await loop.sock_sendall(rfb_sock, leftover_client)
                        logger.debug(
                            f"VNC: forwarded {len(leftover_client)}b early client data to veyon"
                        )
                    except Exception as e:
                        logger.error(f"VNC: failed to forward early client data: {e}")

                closed = asyncio.Event()

                async def veyon_ws_to_server():
                    """Пересылает RFB-сообщения от браузера к Veyon с фильтрацией."""
                    while not closed.is_set():
                        try:
                            data = await ws.receive_bytes()
                        except (WebSocketDisconnect, Exception):
                            break
                        if not data:
                            break
                        if data and len(data) > 0:
                            msg_type = data[0]
                            if not filter_rfb_client_message(
                                msg_type, allow_extended=False
                            ):
                                logger.warning(
                                    f"VNC-IN: filtered out msg_type={msg_type} len={len(data)}"
                                )
                                continue
                        try:
                            await loop.sock_sendall(rfb_sock, data)
                        except Exception as e:
                            logger.error(f"VNC-IN: sendall to veyon failed: {e}")
                            break
                    closed.set()

                async def veyon_server_to_ws():
                    """Пересылает RFB-байты от Veyon к браузеру через async I/O."""
                    while not closed.is_set():
                        try:
                            data = await loop.sock_recv(rfb_sock, 65536)
                        except Exception:
                            break
                        if not data:
                            break
                        try:
                            await ws.send_bytes(data)
                        except Exception:
                            break
                    closed.set()

                await run_duplex(veyon_ws_to_server(), veyon_server_to_ws())

        except WebSocketDisconnect:
            logger.debug(f"WebAPI: VNC WS disconnected for iid={iid} host={host}")
        except Exception as e:
            logger.error(f"WebAPI: VNC WS error for iid={iid} host={host}: {e}")
        finally:
            if rfb_sock is not None:
                try:
                    rfb_sock.close()
                except Exception:
                    pass
            try:
                await ws.close()
            except Exception:
                pass

    # ===== Статические файлы фронтенда =====

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(static_dir):
        app.mount(
            "/",
            GatedStaticFiles(directory=static_dir, html=True, sessions=asset_sessions),
            name="static",
        )

    return app


class _WSItem:
    """Обёртка для очереди WS: None = стоп, _WSItem(dict) = сообщение."""

    __slots__ = ("data",)

    def __init__(self, data: dict):
        self.data = data


def _is_host_leaf(node: Any) -> bool:
    """Признак листа-устройства в дереве хостов."""
    return isinstance(node, dict) and "host" in node and "name" in node


def _is_task_leaf(node: Any) -> bool:
    """Признак листа-задачи в дереве команд."""
    return isinstance(node, dict) and "commands" in node and "name" in node


# Поля устройства, передаваемые веб-клиенту (только для отображения и управления).
# Служебные поля (login, password, mac_address, port, icon и пр.) не отправляются.
_HOST_PUBLIC_FIELDS = ("iid", "name", "host")

# Поля устройства для нод-пиров (федерация, аутентифицированный peer).
# Без login/password — учётные данные не покидают ноду-владельца.
_HOST_NODE_FIELDS = (
    "iid",
    "name",
    "host",
    "port",
    "mac_address",
    "credential_bundle_iid",
    "use_sudo",
    "locale",
    "vnc_port",
    "thumbnail_backend",
)


def _strip_host_sensitive(node: dict) -> dict:
    """Оставить в устройстве только публичные поля для веб-клиента."""
    return {k: v for k, v in node.items() if k in _HOST_PUBLIC_FIELDS}


def _strip_host_for_node(node: dict) -> dict:
    """Поля устройства для нод-пиров (аутентифицированная федерация).

    Шире, чем для веб-клиента: порт, MAC (WoL), связка, sudo, локаль —
    нужны для полноценного CRUD хостов на ноде. login/password не
    передаются (учётные данные остаются на ноде-владельце).
    """
    return {
        k: v
        for k, v in node.items()
        if k in _HOST_NODE_FIELDS and (v is not None or k == "thumbnail_backend")
    }


def _normalize_hosts_node(node: Any) -> Any:
    """Нормализовать узел дерева хостов.

    Группа с пустым значением ({}) или не-list значением приводится к [].
    Одиночное устройство-словарь вместо списка оборачивается в список.
    Служебные поля устройства (login, password, mac_address, port, icon) вырезаются.
    """
    if _is_host_leaf(node):
        return _strip_host_sensitive(node)
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for key, value in node.items():
            if isinstance(value, list):
                out[key] = [_normalize_hosts_node(c) for c in value]
            elif _is_host_leaf(value):
                out[key] = [value]
            else:
                out[key] = []
        return out
    return node


def _normalize_hosts(data: Any) -> List[Any]:
    """Нормализовать верхний уровень дерева хостов."""
    if not isinstance(data, list):
        return []
    return [_normalize_hosts_node(item) for item in data]


def _normalize_commands_node(node: Any) -> Any:
    """Нормализовать узел дерева команд (задач).

    Группа с пустым значением ({}) или не-list значением приводится к [].
    Одиночная задача-словарь вместо списка оборачивается в список.
    """
    if _is_task_leaf(node):
        return node
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for key, value in node.items():
            if isinstance(value, list):
                out[key] = [_normalize_commands_node(c) for c in value]
            elif _is_task_leaf(value):
                out[key] = [value]
            else:
                out[key] = []
        return out
    return node


def _normalize_commands(data: Any) -> List[Any]:
    """Нормализовать верхний уровень дерева команд."""
    if not isinstance(data, list):
        return []
    return [_normalize_commands_node(item) for item in data]


def _run_single(
    executor: WebCommandExecutor,
    device: DeviceModel,
    commands: List[Command],
    timeout: int,
    params: Dict,
    job: Job,
    job_manager: JobManager,
    session: bool = False,
    desktop_session: bool = False,
):
    """Выполнить команды на одном устройстве, события → job + fan-out."""
    evt = job.append_event("device_start", device_iid=device.iid)
    job_manager.dispatch_event(job, evt)

    def on_progress(message: str):
        e = job.append_event("progress", device_iid=device.iid, chunk=message)
        job_manager.dispatch_event(job, e)

    def on_command_start(cmd: Command, text: str):
        e = job.append_event(
            "command_start",
            device_iid=device.iid,
            command_type=str(cmd.commandType),
            command=text,
        )
        job_manager.dispatch_event(job, e)

    output, ok = executor.execute(
        device,
        commands,
        timeout,
        params,
        on_progress,
        on_command_start,
        session=session,
        desktop_session=desktop_session,
    )

    e = job.append_event(
        "device_result",
        device_iid=device.iid,
        success=ok,
        output=output,
    )
    job_manager.dispatch_event(job, e)
    return device.iid, ok, output



def _missing_task_params(task: Dict[str, Any], params: Dict[str, Any], uploads: Any) -> List[str]:
    """Подписи параметров задачи без значения; пустая строка и пробелы не считаются значением."""
    tokens = uploads if isinstance(uploads, dict) else {}
    missing = []
    for index, label in enumerate(task.get("params") or [], 1):
        key = f"param{index}"
        text = str(label)
        if text.startswith("file:"):
            if not tokens.get(key):
                missing.append(text[5:].strip() or "Файл")
        elif not str(params.get(key) or "").strip():
            missing.append(text)
    return missing

def _run_parallel(
    job: Job,
    devices: list[DeviceModel],
    commands: list[Command],
    timeout: int,
    params: dict[str, Any],
    job_manager: JobManager,
    session: bool = False,
    desktop_session: bool = False,
) -> Future[list[tuple[str, bool, str]]]:
    """Submit host operations and aggregate with callbacks, never worker waits."""
    executor = job.executor
    aggregate: Future[list[tuple[str, bool, str]]] = Future()
    targets = list(devices)
    results: list[tuple[str, bool, str] | None] = [None] * len(targets)
    remaining = len(targets)
    lock = threading.Lock()

    def finish() -> None:
        completed_results = [result for result in results if result is not None]
        success_count = sum(1 for _, ok, _ in completed_results if ok)
        summary = {
            "success": success_count,
            "error": len(results) - success_count,
            "total": len(results),
        }
        job_manager.finalize(job, completed_results, summary)
        if not aggregate.cancelled():
            aggregate.set_result(completed_results)

    def completed(
        future: Future[tuple[str, bool, str]], index: int, device: DeviceModel
    ) -> None:
        nonlocal remaining
        try:
            result = future.result()
        except Exception as error:
            output = "Отменено" if future.cancelled() else str(error)
            result = (device.iid, False, output)
            event = job.append_event(
                "device_result", device_iid=device.iid, success=False, output=output
            )
            job_manager.dispatch_event(job, event)
        with job._lock:
            job._device_futures.discard(future)
        with lock:
            results[index] = result
            remaining -= 1
            last = remaining == 0
        if last:
            finish()

    if not targets:
        finish()
    for index, dev in enumerate(targets):
        try:
            if job.status == "aborted":
                future = Future()
                future.cancel()
            else:
                future = job_manager._command_pool.submit(
                    _run_single,
                    executor,
                    dev,
                    commands,
                    timeout,
                    params,
                    job,
                    job_manager,
                    session,
                    desktop_session,
                    metadata={
                        "host": dev.host,
                        "type": "web_command",
                        "device": dev.name or dev.host,
                        "job_id": job.id,
                        "task_iid": job.task_iid,
                    },
                )
        except Exception as e:  # noqa: BLE001 - resolve rejected host submissions
            future = Future()
            future.set_exception(e)
        with job._lock:
            job._device_futures.add(future)
        future.add_done_callback(
            lambda done, i=index, device=dev: completed(done, i, device)
        )
        if job.status == "aborted":
            future.cancel()
    return aggregate
