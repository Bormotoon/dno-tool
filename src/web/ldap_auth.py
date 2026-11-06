"""Проверка учётных данных веб-интерфейса в каталоге LDAP.

Браузер отправляет логин и пароль с каждым запросом API (Basic или ``?token=``),
поэтому удачная проверка запоминается на ``cache_ttl_sec``: иначе статусы
и миниатюры шли бы в каталог десятками запросов в секунду. Отказ с тем же
паролем запоминается на ``REJECT_BACKOFF_SEC``: вкладка со старым паролем
не должна блокировать учётную запись в Active Directory частыми попытками.
Пароли в памяти не хранятся, только HMAC со случайным ключом процесса.

Модуль без Qt; блокирующие вызовы выполнять вне цикла событий.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import ssl
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable

from src.config.web_ldap import (
    USERNAME_PLACEHOLDER,
    LdapServerAddress,
    WebLdapConfig,
    parse_ldap_servers,
)
from src.logger import logger

# Отказ с теми же учётными данными повторно в каталог не отправляется.
REJECT_BACKOFF_SEC = 60.0
# После недоступности каталога новая попытка не раньше чем через столько секунд.
UNAVAILABLE_BACKOFF_SEC = 5.0
# Пока каталог недоступен, недавно подтверждённый пароль продолжает действовать.
OUTAGE_GRACE_SEC = 3600.0
MAX_CACHE_ENTRIES = 4096
MAX_USERNAME_LENGTH = 256
_UNAVAILABLE_LOG_INTERVAL_SEC = 60.0
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# Правило LDAP_MATCHING_RULE_IN_CHAIN: членство с учётом вложенных групп AD.
_AD_IN_CHAIN = "1.2.840.113556.1.4.1941"
# Коды результата LDAP.
_INVALID_CREDENTIALS = 49
_STRONGER_AUTH_REQUIRED = 8
_BUSY_CODES = (51, 52)
_NO_SUCH_OBJECT = 32
_SIZE_LIMIT_EXCEEDED = 4
# Уточнение причины отказа Active Directory: "data 52e" в тексте ответа.
_AD_REASONS = {
    "525": "пользователь не найден",
    "52e": "неверный логин или пароль",
    "530": "вход запрещён в это время",
    "531": "вход запрещён с этого компьютера",
    "532": "срок действия пароля истёк",
    "533": "учётная запись отключена",
    "701": "срок действия учётной записи истёк",
    "773": "требуется сменить пароль",
    "775": "учётная запись заблокирована",
}


class LdapError(Exception):
    """Ошибка настройки или ответа каталога; текст безопасен для показа."""


class LdapUnavailable(LdapError):
    """Ни один сервер каталога не ответил (сеть, TLS, таймаут)."""


@dataclass(frozen=True)
class LdapOutcome:
    """Итог проверки: канонический логин при успехе, причина отказа иначе."""

    username: str | None
    reason: str
    server: str = ""


def canonical_username(username: str) -> str:
    """Владелец заданий для пользователя каталога: каталоги не различают регистр."""
    return username.strip().lower()


def _valid_username(username: str) -> bool:
    return (
        0 < len(username) <= MAX_USERNAME_LENGTH
        and not _CONTROL_CHARS.search(username)
        and username == username.strip()
    )


def _result_reason(result: dict | None) -> str:
    """Причина из ответа сервера без технических подробностей."""
    result = result or {}
    message = str(result.get("message") or "")
    match = re.search(r"data ([0-9a-f]{3})", message, re.IGNORECASE)
    if match and match.group(1).lower() in _AD_REASONS:
        return _AD_REASONS[match.group(1).lower()]
    description = str(result.get("description") or "")
    return description or f"код {result.get('result')}"


def _tls_context(cfg: WebLdapConfig) -> ssl.SSLContext:
    """TLS 1.2+; при проверке — цепочка и имя сервера средствами ``ssl``."""
    context = ssl.create_default_context(cafile=cfg.ca_file or None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if not cfg.verify_cert:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _make_tls(cfg: WebLdapConfig):
    """Tls для ldap3 со стандартной проверкой имени.

    Собственная проверка ldap3 на Python 3.12+ не сверяет IP-адреса из
    сертификата и отключает ``check_hostname`` контекста, поэтому обёртка
    сокета заменена: имя сверяет сам ``ssl`` при рукопожатии.
    """
    import ldap3

    context = _tls_context(cfg)

    class _Tls(ldap3.Tls):
        def wrap_socket(self, connection, do_handshake=False):
            host = connection.server.host
            connection.socket = context.wrap_socket(
                connection.socket,
                server_side=False,
                do_handshake_on_connect=do_handshake,
                server_hostname=host,
            )

    return _Tls(validate=ssl.CERT_REQUIRED if cfg.verify_cert else ssl.CERT_NONE)


class _Directory:
    """Соединения с одним сервером каталога в рамках одной проверки."""

    def __init__(self, cfg: WebLdapConfig, address: LdapServerAddress) -> None:
        import ldap3

        self._cfg = cfg
        self.address = address
        tls = _make_tls(cfg)
        self._server = ldap3.Server(
            address.host,
            port=address.port,
            use_ssl=address.use_ssl,
            tls=tls,
            get_info=ldap3.NONE,
            connect_timeout=cfg.timeout_sec,
        )

    def connect(self, user: str | None, password: str | None):
        """Открыть соединение (с TLS) и выполнить bind; возвращает (соединение, успех)."""
        import ldap3
        from ldap3.core.exceptions import LDAPException

        conn = ldap3.Connection(
            self._server,
            user=user,
            password=password,
            authentication=ldap3.SIMPLE if user else ldap3.ANONYMOUS,
            auto_bind=ldap3.AUTO_BIND_NONE,
            receive_timeout=self._cfg.timeout_sec,
            read_only=True,
            raise_exceptions=False,
            auto_referrals=False,
        )
        try:
            conn.open(read_server_info=False)
            if self._cfg.start_tls and not self.address.use_ssl:
                conn.start_tls(read_server_info=False)
            ok = conn.bind(read_server_info=False)
        except LDAPException as error:
            self.close(conn)
            raise LdapUnavailable(
                f"{self.address.label}: {_exception_text(error)}"
            ) from error
        result = conn.result or {}
        if not ok and result.get("result") in _BUSY_CODES:
            self.close(conn)
            raise LdapUnavailable(f"{self.address.label}: {_result_reason(result)}")
        if not ok and result.get("result") == _STRONGER_AUTH_REQUIRED:
            self.close(conn)
            raise LdapError(
                "Сервер требует защищённое соединение: используйте ldaps:// или StartTLS"
            )
        return conn, ok

    def search(self, conn, base: str, scope: str, search_filter: str) -> tuple[list[str], int]:
        """DN найденных записей (не больше двух) и код результата."""
        from ldap3.core.exceptions import LDAPException

        try:
            conn.search(
                base,
                search_filter,
                search_scope=scope,
                attributes=["1.1"],
                size_limit=2,
                time_limit=self._cfg.timeout_sec,
            )
        except LDAPException as error:
            raise LdapUnavailable(
                f"{self.address.label}: {_exception_text(error)}"
            ) from error
        entries = [
            item["dn"]
            for item in (conn.response or [])
            if item.get("type") == "searchResEntry"
        ]
        return entries, int((conn.result or {}).get("result") or 0)

    @staticmethod
    def close(conn) -> None:
        try:
            conn.unbind()
        except Exception:
            pass


def _exception_text(error: Exception) -> str:
    text = str(error).strip() or type(error).__name__
    lowered = text.lower()
    if "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
        return "ошибка TLS: " + text
    if "timed out" in lowered or "timeout" in lowered:
        return "нет ответа (таймаут)"
    if "socket" in lowered or "connect" in lowered:
        return "нет соединения"
    return text


def authenticate_ldap(cfg: WebLdapConfig, username: str, password: str) -> LdapOutcome:
    """Проверить логин и пароль в каталоге без кэша.

    Отказ — ``LdapOutcome(None, причина)``. Недоступность всех серверов —
    ``LdapUnavailable``, ошибка настройки (служебная запись, базовый DN) — ``LdapError``.
    """
    if not cfg.enabled:
        return LdapOutcome(None, "вход через LDAP выключен")
    if not _valid_username(username):
        return LdapOutcome(None, "недопустимый логин")
    if not password:
        # Пустой пароль в LDAP — анонимный вход, он всегда «успешен».
        return LdapOutcome(None, "пустой пароль")
    addresses = parse_ldap_servers(cfg.servers)
    if not addresses:
        raise LdapError("Не указан сервер LDAP")
    errors: list[str] = []
    for address in addresses:
        try:
            directory = _Directory(cfg, address)
        except Exception as error:
            raise LdapError(f"Настройка TLS: {error}") from error
        try:
            return _authenticate_on(directory, cfg, username, password)
        except LdapUnavailable as error:
            errors.append(str(error))
    raise LdapUnavailable("Каталог недоступен: " + "; ".join(errors))


def _authenticate_on(
    directory: "_Directory", cfg: WebLdapConfig, username: str, password: str
) -> LdapOutcome:
    from ldap3 import SUBTREE
    from ldap3.utils.conv import escape_filter_chars
    from ldap3.utils.dn import escape_rdn

    server = directory.address.label
    user_filter = cfg.user_filter.replace(
        USERNAME_PLACEHOLDER, escape_filter_chars(username)
    )

    def find_user(conn) -> str | None:
        found, code = directory.search(conn, cfg.base_dn, SUBTREE, user_filter)
        if code == _NO_SUCH_OBJECT:
            raise LdapError(f"Базовый DN не найден: {cfg.base_dn}")
        if len(found) > 1 or code == _SIZE_LIMIT_EXCEEDED:
            logger.warning("LDAP: логин %r соответствует нескольким записям", username)
            return None
        return found[0] if found else None

    user_dn: str | None = None
    if cfg.bind_mode == "search":
        service, ok = directory.connect(cfg.bind_dn or None, cfg.bind_password or None)
        try:
            if not ok:
                raise LdapError(
                    "Служебная учётная запись не принята: " + _result_reason(service.result)
                )
            user_dn = find_user(service)
        finally:
            directory.close(service)
        if user_dn is None:
            return LdapOutcome(None, "пользователь не найден или не единственный", server)
        bind_name = user_dn
    else:
        is_dn = "=" in cfg.user_template
        value = escape_rdn(username) if is_dn else username
        bind_name = cfg.user_template.replace(USERNAME_PLACEHOLDER, value)
        if is_dn:
            user_dn = bind_name

    conn, ok = directory.connect(bind_name, password)
    try:
        if not ok:
            code = (conn.result or {}).get("result")
            reason = _result_reason(conn.result)
            if code == _INVALID_CREDENTIALS and reason.startswith("invalid"):
                reason = "неверный логин или пароль"
            return LdapOutcome(None, reason, server)
        if cfg.required_group:
            if user_dn is None:
                user_dn = find_user(conn)
                if user_dn is None:
                    return LdapOutcome(
                        None, "запись пользователя не найдена для проверки группы", server
                    )
            if not _is_member(directory, conn, cfg, user_dn, username):
                return LdapOutcome(None, "пользователь не входит в разрешённую группу", server)
    finally:
        directory.close(conn)
    return LdapOutcome(canonical_username(username), "вход разрешён", server)


def _is_member(directory: "_Directory", conn, cfg: WebLdapConfig, user_dn: str, username: str) -> bool:
    """Членство по memberOf пользователя, иначе по записи группы (OpenLDAP)."""
    from ldap3 import BASE
    from ldap3.utils.conv import escape_filter_chars

    group = escape_filter_chars(cfg.required_group)
    rule = f":{_AD_IN_CHAIN}:" if cfg.nested_groups else ""
    found, _code = directory.search(conn, user_dn, BASE, f"(memberOf{rule}={group})")
    if found:
        return True
    dn = escape_filter_chars(user_dn)
    login = escape_filter_chars(username)
    found, _code = directory.search(
        conn,
        cfg.required_group,
        BASE,
        f"(|(member={dn})(uniqueMember={dn})(memberUid={login}))",
    )
    return bool(found)


def _signature(cfg: WebLdapConfig) -> str:
    """Отпечаток настройки: смена любого поля сбрасывает кэш и cookie интерфейса."""
    return hashlib.sha256(repr(sorted(asdict(cfg).items())).encode()).hexdigest()


@dataclass
class _Accepted:
    digest: bytes
    username: str
    verified_at: float


_MISS = object()


class LdapVerifier:
    """Кэш проверок поверх ``authenticate_ldap`` с объединением одновременных запросов."""

    def __init__(
        self,
        backend: Callable[[WebLdapConfig, str, str], LdapOutcome] = authenticate_ldap,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._signature = ""
        self._accepted: dict[str, _Accepted] = {}
        self._rejected: dict[tuple[str, bytes], float] = {}
        self._inflight: dict[tuple[str, bytes], threading.Event] = {}
        self._unavailable_logged = -_UNAVAILABLE_LOG_INTERVAL_SEC

    def _digest(self, username: str, password: str) -> bytes:
        return hmac.new(
            self._key, f"{username}\0{password}".encode("utf-8"), hashlib.sha256
        ).digest()

    def _sync_signature(self, cfg: WebLdapConfig) -> None:
        """Вызывать под блокировкой."""
        signature = _signature(cfg)
        if signature != self._signature:
            self._signature = signature
            self._accepted.clear()
            self._rejected.clear()

    def cached(self, cfg: WebLdapConfig, username: str, password: str):
        """Решение из кэша: логин, ``None`` (отказ) или ``MISS`` — нужен каталог."""
        if not cfg.enabled:
            return None
        digest = self._digest(username, password)
        with self._lock:
            self._sync_signature(cfg)
            return self._lookup(cfg, username, digest)

    def _lookup(self, cfg: WebLdapConfig, username: str, digest: bytes):
        now = self._clock()
        item = self._accepted.get(username)
        if (
            item is not None
            and hmac.compare_digest(item.digest, digest)
            and now - item.verified_at < cfg.cache_ttl_sec
        ):
            return item.username
        until = self._rejected.get((username, digest))
        if until is not None:
            if until > now:
                return None
            self._rejected.pop((username, digest), None)
        return _MISS

    def verify(self, cfg: WebLdapConfig, username: str, password: str) -> str | None:
        """Канонический логин или ``None``; блокирует поток на время запроса к каталогу."""
        if not cfg.enabled:
            return None
        digest = self._digest(username, password)
        key = (username, digest)
        with self._lock:
            self._sync_signature(cfg)
            decided = self._lookup(cfg, username, digest)
            if decided is not _MISS:
                return decided
            event = self._inflight.get(key)
            leader = event is None
            if leader:
                event = threading.Event()
                self._inflight[key] = event
            stale = self._accepted.get(username)
        if not leader:
            # Ту же пару уже проверяет другой поток: ждём его результата.
            servers = max(1, len(parse_ldap_servers(cfg.servers)))
            event.wait(cfg.timeout_sec * (3 * servers + 2))
            with self._lock:
                decided = self._lookup(cfg, username, digest)
            return None if decided is _MISS else decided
        try:
            return self._verify_remote(cfg, username, password, digest, stale)
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            event.set()

    def _verify_remote(
        self,
        cfg: WebLdapConfig,
        username: str,
        password: str,
        digest: bytes,
        stale: _Accepted | None,
    ) -> str | None:
        key = (username, digest)
        try:
            outcome = self._backend(cfg, username, password)
        except LdapUnavailable as error:
            now = self._clock()
            if (
                stale is not None
                and hmac.compare_digest(stale.digest, digest)
                and now - stale.verified_at < cfg.cache_ttl_sec + OUTAGE_GRACE_SEC
            ):
                self._log_unavailable(f"{error}; действует прежняя проверка {username!r}")
                return stale.username
            self._log_unavailable(str(error))
            self._remember_reject(key, UNAVAILABLE_BACKOFF_SEC)
            return None
        except LdapError as error:
            # Ошибка настройки повторяется у всех пользователей: журнал не засоряется.
            self._log_unavailable(str(error))
            self._remember_reject(key, UNAVAILABLE_BACKOFF_SEC)
            return None
        except Exception as error:
            logger.exception("LDAP: непредвиденная ошибка проверки: %s", error)
            self._remember_reject(key, UNAVAILABLE_BACKOFF_SEC)
            return None
        if outcome.username is None:
            logger.info("LDAP: вход %r отклонён: %s", username, outcome.reason)
            self._remember_reject(key, REJECT_BACKOFF_SEC)
            return None
        with self._lock:
            if _signature(cfg) == self._signature:
                self._trim(self._accepted)
                self._accepted[username] = _Accepted(digest, outcome.username, self._clock())
        return outcome.username

    def _remember_reject(self, key: tuple[str, bytes], seconds: float) -> None:
        with self._lock:
            self._trim(self._rejected)
            self._rejected[key] = self._clock() + seconds

    @staticmethod
    def _trim(items: dict) -> None:
        """Вызывать под блокировкой: самая старая запись уступает место."""
        while len(items) >= MAX_CACHE_ENTRIES:
            items.pop(next(iter(items)))

    def _log_unavailable(self, message: str) -> None:
        now = self._clock()
        with self._lock:
            if now - self._unavailable_logged < _UNAVAILABLE_LOG_INTERVAL_SEC:
                return
            self._unavailable_logged = now
        logger.warning("LDAP: %s", message)

    def signature(self, cfg: WebLdapConfig) -> str:
        return _signature(cfg)

    def clear(self) -> None:
        with self._lock:
            self._accepted.clear()
            self._rejected.clear()


MISS = _MISS
_verifier = LdapVerifier()


def get_ldap_verifier() -> LdapVerifier:
    return _verifier


def check_ldap_login(cfg: WebLdapConfig, username: str, password: str) -> tuple[bool, str]:
    """Проверка из окна настроек: без кэша, с понятной причиной отказа."""
    probe = WebLdapConfig(**{**asdict(cfg), "enabled": True})
    try:
        outcome = authenticate_ldap(probe, username, password)
    except LdapError as error:
        return False, str(error)
    except Exception as error:  # ошибки ldap3 вне ожидаемых
        logger.exception("LDAP: проверка входа завершилась ошибкой")
        return False, f"Ошибка: {error}"
    if outcome.username is None:
        return False, f"Отказ ({outcome.server or 'каталог'}): {outcome.reason}"
    return True, f"Вход разрешён ({outcome.server}), пользователь: {outcome.username}"
