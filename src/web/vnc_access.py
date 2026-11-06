"""Capability-токен браузерного VNC-вьювера с HMAC и отзывом по владельцу.

Нода-владелец генерирует короткоживущий подписанный токен для конкретного
iid устройства (``create_vnc_access``) и подставляет его в URL веб-вьювера
(``/vnc.html?...&vt=...``). Вьювер передаёт токен в query (?vt=) всех своих
запросов; эндпоинты вьювера принимают его как альтернативу Basic-аутентификации
(``verify_vnc_access``).

Свойства:
- Секрет — случайный на процесс: токен невозможно подделать вне процесса
  ноды-владельца; web-креды ноды в URL не разглашаются.
- Токен привязан к iid (чужой iid с тем же токеном не пройдёт).
- Ссылки федерации также привязаны к сессии ноды и поколению отзыва.
- TTL по умолчанию 15 мин — достаточно открыть вьювер и переподключений.
"""

import hashlib
import hmac
import secrets
import threading
import time
from typing import Optional
from src.utils.network.urls import normalize_public_web_url as normalize_public_web_url

DEFAULT_TTL_SEC = 900

_secret: Optional[bytes] = None
_lock = threading.Lock()
_owner_epochs: dict[str, str] = {}


def _get_secret() -> bytes:
    """Секрет HMAC — случайный на процесс (ленивая инициализация)."""
    global _secret
    if _secret is None:
        with _lock:
            if _secret is None:
                _secret = secrets.token_bytes(32)
    return _secret


def create_vnc_access(
    iid: str, ttl_sec: int = DEFAULT_TTL_SEC, *, owner: str = ""
) -> str:
    """Подписанный capability-токен для iid.

    Без владельца сохраняется формат expiry.mac; для федерации используется
    expiry.owner.epoch.mac с проверкой актуального доверия и поколения.
    """
    expiry = int(time.time()) + max(60, int(ttl_sec))
    if owner:
        with _lock:
            epoch = _owner_epochs.setdefault(owner, secrets.token_hex(16))
        payload = f"{expiry}.{owner}.{epoch}"
        mac = hmac.new(
            _get_secret(), f"{iid}|{payload}".encode("utf-8"), hashlib.sha256
        ).hexdigest()[:32]
        return f"{payload}.{mac}"
    mac = hmac.new(
        _get_secret(), f"{iid}|{expiry}".encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    return f"{expiry}.{mac}"


def revoke_vnc_owner(owner: str) -> None:
    """Отозвать выданные ссылки одной сессии ноды."""
    with _lock:
        _owner_epochs.pop(owner, None)


def revoke_all_vnc_access() -> None:
    """Сбросить все ссылки (отзыв всех сессий нод)."""
    global _secret
    with _lock:
        _secret = secrets.token_bytes(32)
        _owner_epochs.clear()


def verify_vnc_access(token: str, iid: str) -> bool:
    """Проверить capability-токен для iid (expiry + HMAC). False при любой ошибке."""
    try:
        parts = (token or "").split(".")
        if len(parts) == 4:
            expiry_str, owner, epoch, mac = parts
            if int(expiry_str) < int(time.time()):
                return False
            # Отзыв сессии ноды (перегенерация ключа доступа) снимает поколение.
            with _lock:
                if not epoch or _owner_epochs.get(owner) != epoch:
                    return False
            payload = ".".join(parts[:3])
            expect = hmac.new(
                _get_secret(), f"{iid}|{payload}".encode("utf-8"), hashlib.sha256
            ).hexdigest()[:32]
            return hmac.compare_digest(mac, expect)
        expiry_str, mac = (token or "").split(".", 1)
        expiry = int(expiry_str)
        if expiry < int(time.time()):
            return False
        expect = hmac.new(
            _get_secret(), f"{iid}|{expiry}".encode("utf-8"), hashlib.sha256
        ).hexdigest()[:32]
        return hmac.compare_digest(mac, expect)
    except Exception:
        return False
