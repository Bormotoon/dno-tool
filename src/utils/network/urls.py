"""Проверка внешнего адреса веб-сервера без импорта приложения и Qt."""

from typing import Any
from urllib.parse import urlsplit


def normalize_public_web_url(value: str) -> str:
    """Пустой URL или HTTPS origin/префикс без секретов в URL."""
    if not isinstance(value, str):
        raise ValueError("Внешний URL должен быть строкой")
    value = value.strip().rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Укажите HTTPS URL без учётных данных, query и fragment")
    parsed.port
    return value


def web_base_url(web: Any, host: str | None = None) -> str:
    """Прямой адрес встроенного сервера с учётом включённого HTTPS."""
    target = host if host is not None else (getattr(web, "host", "") or "0.0.0.0")
    if target in ("0.0.0.0", "", "::"):
        target = "localhost"
    if ":" in target and not target.startswith("["):
        target = f"[{target}]"
    scheme = "http" if getattr(web, "tls_mode", "off") == "off" else "https"
    return f"{scheme}://{target}:{getattr(web, 'port', 0) or 8765}"
