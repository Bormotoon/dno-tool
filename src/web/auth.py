"""Аутентификация для веб-сервера (HTTP Basic + token + session_id).

Многопользовательский режим: аутентификация идёт по списку
``config.app.web.users`` (list[WebUser]). Каждый пользователь видит только
свои задания (owner = username). Вторым источником может быть каталог LDAP
(``config.app.web.ldap``, ``src.web.ldap_auth``): логин локального пользователя
в каталог не отправляется, владелец заданий для LDAP — логин в нижнем регистре.
Без локальных пользователей и без LDAP сервер не запускается (см. WebServer.start).

Token в query/cookie остаётся base64(user:pass) для обратной совместимости
с localStorage. Session_id — отдельный идентификатор, выдаётся сервером
через POST /api/login и хранится клиентом в localStorage('dno-tool_session').
"""

import hmac
import base64
import ipaddress
import secrets
import time
import threading
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status

from src.config import config
from src.logger import logger

_BEARER_LOCK = threading.Lock()
# token -> (owner, session_id, expires_at)
_BEARER_TOKENS: dict[str, tuple[str, str | None, float]] = {}
_BEARER_TTL_SEC = 24 * 3600
_MAX_BEARER_TOKENS = 2048


def issue_session_token(owner: str, session_id: str | None = None, ttl_sec: float = _BEARER_TTL_SEC) -> str:
    """Выпустить криптостойкий непрозрачный токен сессии (без пароля внутри)."""
    now = time.monotonic()
    token = "st_" + secrets.token_urlsafe(32)
    with _BEARER_LOCK:
        if len(_BEARER_TOKENS) >= _MAX_BEARER_TOKENS:
            expired = [k for k, v in _BEARER_TOKENS.items() if v[2] <= now]
            for k in expired:
                _BEARER_TOKENS.pop(k, None)
            if len(_BEARER_TOKENS) >= _MAX_BEARER_TOKENS:
                oldest = min(_BEARER_TOKENS, key=lambda k: _BEARER_TOKENS[k][2])
                _BEARER_TOKENS.pop(oldest, None)
        _BEARER_TOKENS[token] = (owner, session_id, now + ttl_sec)
    return token


def resolve_session_token(token: str | None) -> tuple[str, str | None] | None:
    """Проверить непрозрачный токен `st_...` и вернуть (owner, session_id) или None."""
    if not token or not token.startswith("st_"):
        return None
    now = time.monotonic()
    with _BEARER_LOCK:
        entry = _BEARER_TOKENS.get(token)
        if not entry:
            return None
        owner, session_id, expires_at = entry
        if expires_at <= now:
            _BEARER_TOKENS.pop(token, None)
            return None
        return owner, session_id


def revoke_session_token(token: str | None) -> None:
    if token:
        with _BEARER_LOCK:
            _BEARER_TOKENS.pop(token, None)


@dataclass
class RequestUser:
    """Аутентифицированный пользователь + его сессия.

    owner — ключ изоляции заданий в JobManager: username.
    """

    username: str
    session_id: Optional[str] = None

    @property
    def owner(self) -> str:
        # Изоляция по username (многопользовательский режим).
        return self.username


def _constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _has_users_config() -> bool:
    """True если в конфиге задан непустой список users."""
    users = getattr(config.app.web, "users", None) or []
    return any(u.username for u in users)


def _ldap_config():
    """Настройка LDAP, если вход через каталог включён."""
    cfg = getattr(config.app.web, "ldap", None)
    return cfg if cfg is not None and cfg.enabled else None


def has_web_accounts() -> bool:
    """Есть ли кому войти: локальные пользователи или каталог LDAP."""
    return _has_users_config() or _ldap_config() is not None


def _local_user(username: str):
    """Локальная учётная запись с таким логином или None."""
    users = getattr(config.app.web, "users", None) or []
    for u in users:
        if u.username and _constant_eq(u.username, username):
            return u
    return None


def _find_user_in_list(username: str, password: str) -> bool:
    """Проверить credentials по списку users."""
    user = _local_user(username)
    return user is not None and _constant_eq(user.password or "", password)


_UNDECIDED = object()


def _authenticate_fast(username: str, password: str):
    """Решение без обращения к каталогу: логин, None или ``_UNDECIDED``."""
    if not username:
        return None
    if _local_user(username) is not None:
        # Локальная запись перекрывает каталог: её логин в LDAP не отправляется.
        return username if _find_user_in_list(username, password) else None
    cfg = _ldap_config()
    if cfg is None:
        return None
    from .ldap_auth import MISS, get_ldap_verifier

    decided = get_ldap_verifier().cached(cfg, username, password)
    return _UNDECIDED if decided is MISS else decided


def authenticate(username: str, password: str) -> Optional[str]:
    """Логин владельца заданий или None. Может ждать ответа каталога LDAP."""
    decided = _authenticate_fast(username, password)
    if decided is not _UNDECIDED:
        return decided
    cfg = _ldap_config()
    if cfg is None:
        return None
    from .ldap_auth import get_ldap_verifier

    return get_ldap_verifier().verify(cfg, username, password)


async def authenticate_async(username: str, password: str) -> Optional[str]:
    """То же для цикла событий: запрос к каталогу уходит в пул потоков."""
    decided = _authenticate_fast(username, password)
    if decided is not _UNDECIDED:
        return decided
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(authenticate, username, password)


def _verify(user: str, pwd: str) -> bool:
    """Проверка учётных данных: локальные пользователи, затем LDAP."""
    return authenticate(user, pwd) is not None


def credential_fingerprint(username: str, password: str) -> str:
    """Отпечаток проверенного входа для cookie файлов интерфейса.

    Локальный вход перестаёт действовать при смене пароля или удалении
    пользователя, вход через каталог — при выключении или смене настройки LDAP.
    """
    if _local_user(username) is not None:
        return local_fingerprint(username, password)
    cfg = _ldap_config()
    if cfg is None:
        return local_fingerprint(username, password)
    from .ldap_auth import get_ldap_verifier

    return "ldap:" + get_ldap_verifier().signature(cfg)


def local_fingerprint(username: str, password: str) -> str:
    import hashlib

    return hashlib.sha256(f"{username}\0{password}".encode()).hexdigest()


def fingerprint_valid(fingerprint: str) -> bool:
    """Действует ли ещё вход, по которому выдана cookie."""
    if fingerprint.startswith("ldap:"):
        cfg = _ldap_config()
        if cfg is None:
            return False
        from .ldap_auth import get_ldap_verifier

        return hmac.compare_digest(fingerprint[5:], get_ldap_verifier().signature(cfg))
    users = getattr(config.app.web, "users", None) or []
    return any(
        hmac.compare_digest(fingerprint, local_fingerprint(u.username, u.password or ""))
        for u in users
    )


def _basic_credentials(request) -> Optional[tuple[str, str]]:
    """Логин и пароль из заголовка Authorization: Basic."""
    auth = request.headers.get("authorization")
    if not auth or not auth.lower().startswith("basic "):
        return None
    return _decode_token(auth.split(" ", 1)[1])


def _decode_token(token: str) -> Optional[tuple[str, str]]:
    """Декодировать base64(user:pass) token. None если невалиден."""
    try:
        decoded = base64.b64decode(token).decode("utf-8")
        username, _, password = decoded.partition(":")
        if username:
            return username, password
    except Exception:
        pass
    return None


def _extract_session(request: Request) -> Optional[str]:
    """Извлечь session_id из заголовка X-Session-Id или query ?session=."""
    sid = request.headers.get("x-session-id")
    if sid:
        return sid
    sid = request.query_params.get("session")
    if sid:
        return sid
    return None


INSECURE_TRANSPORT_MESSAGE = (
    "Терминал и файлы работают только по HTTPS. Включите HTTPS в настройках "
    "веб-сервера или откройте интерфейс на самом компьютере"
)


def is_secure_transport(scheme: str, client_host: str | None) -> bool:
    """Разрешено ли передавать шелл и файлы по этому соединению.

    HTTPS самого сервера и proxy с ``X-Forwarded-Proto`` на loopback видны как
    схема; внешний ``public_url`` означает, что TLS завершает reverse proxy.
    """
    if scheme in ("https", "wss"):
        return True
    if client_host:
        try:
            if ipaddress.ip_address(client_host).is_loopback:
                return True
        except ValueError:
            if client_host == "localhost":
                return True
    return bool(getattr(config.app.web, "public_url", ""))


async def require_secure_auth(request: Request) -> RequestUser:
    """Авторизация для операций, которым нужен защищённый транспорт."""
    user = await require_auth(request)
    client = request.client.host if request.client else None
    if not is_secure_transport(request.url.scheme, client):
        raise HTTPException(status.HTTP_403_FORBIDDEN, INSECURE_TRANSPORT_MESSAGE)
    return user


async def require_auth(request: Request) -> RequestUser:
    """Зависимость FastAPI: требовать HTTP Basic аутентификацию + session_id.

    Возвращает RequestUser с owner для изоляции заданий.
    """
    creds = _basic_credentials(request)
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется аутентификация",
            headers={"WWW-Authenticate": "Basic"},
        )

    owner = await authenticate_async(*creds)
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учётные данные",
            headers={"WWW-Authenticate": "Basic"},
        )

    return RequestUser(username=owner, session_id=_extract_session(request))


async def require_auth_or_token(request: Request) -> RequestUser:
    """Зависимость для эндпоинтов, доступных через Basic-заголовок ИЛИ ?token=.

    Браузер не отправляет Basic-заголовок для <img>/<video> запросов, поэтому
    для эндпоинтов, отдающих изображения (иконки, миниатюры), принимаем также
    токен из query-параметра ``token`` (base64(user:pass)).

    Важно: при 401 НЕ отсылается заголовок WWW-Authenticate, иначе браузер
    покажет нативный диалог логина для <img>, что ломает UX.
    """
    _no_auth_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Требуется аутентификация",
        # Намеренно БЕЗ WWW-Authenticate — чтобы браузер не показывал native prompt для <img>
    )

    # Basic-заголовок, затем token в query (для <img>), затем cookie.
    token = request.query_params.get("token")
    cookie_token = request.cookies.get("dno-tool_token")
    for t in (token, cookie_token):
        resolved = resolve_session_token(t)
        if resolved:
            owner, sid = resolved
            return RequestUser(username=owner, session_id=_extract_session(request) or sid)

    for creds in (
        _basic_credentials(request),
        _decode_token(token) if token else None,
        _decode_token(cookie_token) if cookie_token else None,
    ):
        if creds is None:
            continue
        owner = await authenticate_async(*creds)
        if owner is not None:
            return RequestUser(username=owner, session_id=_extract_session(request))

    raise _no_auth_exc
