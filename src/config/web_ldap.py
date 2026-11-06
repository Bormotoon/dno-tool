"""Настройки входа в веб-интерфейс через каталог LDAP (Active Directory, OpenLDAP).

Модуль без Qt и без сетевого кода: модель, проверка и разбор адресов серверов.
Проверку учётных данных выполняет ``src.web.ldap_auth``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from typing import Any
from urllib.parse import urlsplit

try:
    from src.logger import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

LDAP_BIND_MODES = ("direct", "search")
USERNAME_PLACEHOLDER = "{username}"
# Диапазоны числовых полей: (минимум, максимум).
LDAP_RANGES = {
    "timeout_sec": (1, 60),
    # Без кэша каждый запрос API (статусы, миниатюры) шёл бы в каталог.
    "cache_ttl_sec": (10, 3600),
}
_STRING_FIELDS = (
    "servers",
    "ca_file",
    "bind_mode",
    "user_template",
    "bind_dn",
    "bind_password",
    "base_dn",
    "user_filter",
    "required_group",
)
_BOOL_FIELDS = ("enabled", "start_tls", "verify_cert", "nested_groups")


@dataclass
class WebLdapConfig:
    """Вход пользователей веб-интерфейса по учётной записи каталога.

    Локальные пользователи веб-сервера проверяются первыми: логин, совпавший
    с локальным, в каталог не отправляется.
    """

    enabled: bool = False
    # Один или несколько адресов ldap:// или ldaps:// через пробел; опрашиваются по порядку.
    servers: str = ""
    start_tls: bool = False  # StartTLS для ldap://; у ldaps:// TLS и так есть
    verify_cert: bool = True  # Проверять сертификат и имя сервера
    ca_file: str = ""  # PEM с корневым сертификатом; пусто — системное хранилище
    # direct — привязка по шаблону логина; search — поиск DN служебной записью.
    bind_mode: str = "direct"
    # Шаблон для direct: "{username}@example.org", "EXAMPLE\\{username}" или DN.
    user_template: str = ""
    bind_dn: str = ""  # Служебная запись для search; пусто — анонимный поиск
    bind_password: str = field(default="", repr=False)  # В памяти открыт, на диске зашифрован
    base_dn: str = ""  # Где искать пользователя (search и проверка группы)
    user_filter: str = "(sAMAccountName={username})"
    required_group: str = ""  # DN группы; пусто — вход любому пользователю каталога
    nested_groups: bool = False  # Вложенные группы Active Directory
    timeout_sec: int = 5
    cache_ttl_sec: int = 300


@dataclass(frozen=True)
class LdapServerAddress:
    """Разобранный адрес сервера каталога."""

    host: str
    port: int
    use_ssl: bool

    @property
    def label(self) -> str:
        scheme = "ldaps" if self.use_ssl else "ldap"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme}://{host}:{self.port}"


def parse_ldap_servers(text: str) -> list[LdapServerAddress]:
    """Адреса через пробел, запятую или точку с запятой; ошибка — ValueError."""
    result: list[LdapServerAddress] = []
    for item in re.split(r"[\s,;]+", text.strip()):
        if not item:
            continue
        if "://" not in item:
            item = "ldap://" + item
        try:
            parts = urlsplit(item)
            port = parts.port
        except ValueError as error:
            raise ValueError(f"Некорректный адрес сервера LDAP: {item}") from error
        scheme = parts.scheme.lower()
        if scheme not in ("ldap", "ldaps") or not parts.hostname:
            raise ValueError(
                f"Адрес сервера LDAP должен начинаться с ldap:// или ldaps://: {item}"
            )
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            raise ValueError(f"В адресе сервера LDAP допустимы только имя и порт: {item}")
        use_ssl = scheme == "ldaps"
        result.append(
            LdapServerAddress(parts.hostname, port or (636 if use_ssl else 389), use_ssl)
        )
    return result


def _check_filter(value: str, title: str) -> None:
    if not (value.startswith("(") and value.endswith(")")):
        raise ValueError(f"{title} должен быть в скобках, например (uid={USERNAME_PLACEHOLDER})")
    if value.count("(") != value.count(")"):
        raise ValueError(f"{title}: не совпадает число скобок")


def validate_ldap_config(cfg: WebLdapConfig) -> None:
    """Проверить связность полей включённой настройки; ошибка — ValueError с причиной."""
    if not cfg.enabled:
        return
    if not parse_ldap_servers(cfg.servers):
        raise ValueError("Укажите адрес сервера LDAP")
    if cfg.ca_file and not os.path.isfile(cfg.ca_file):
        raise ValueError(f"Файл корневого сертификата не найден: {cfg.ca_file}")
    if cfg.bind_mode == "direct":
        if USERNAME_PLACEHOLDER not in cfg.user_template:
            raise ValueError(
                f"Шаблон учётной записи должен содержать {USERNAME_PLACEHOLDER}"
            )
    else:
        if not cfg.base_dn:
            raise ValueError("Для поиска пользователя укажите базовый DN")
        if cfg.bind_dn and not cfg.bind_password:
            raise ValueError("Укажите пароль служебной учётной записи")
    needs_search = cfg.bind_mode == "search" or (
        cfg.required_group and "=" not in cfg.user_template
    )
    if needs_search:
        if USERNAME_PLACEHOLDER not in cfg.user_filter:
            raise ValueError(f"Фильтр пользователя должен содержать {USERNAME_PLACEHOLDER}")
        _check_filter(cfg.user_filter, "Фильтр пользователя")
        if not cfg.base_dn:
            raise ValueError("Для проверки группы укажите базовый DN")
    if cfg.required_group and "=" not in cfg.required_group:
        raise ValueError("Группа указывается полным DN, например cn=dno,ou=groups,dc=example,dc=org")


def normalize_ldap_config(data: Any, *, strict: bool = True) -> WebLdapConfig:
    """Собрать настройку из словаря.

    ``strict`` — для сохранения из окна: любая ошибка прерывает. При чтении файла
    испорченное поле заменяется значением по умолчанию с записью в журнал,
    а несогласованная включённая настройка выключается: вход только по локальным
    пользователям лучше, чем вход с неверными правилами.
    """
    defaults = WebLdapConfig()
    result = WebLdapConfig()
    if data is None:
        return result
    if not isinstance(data, dict):
        if strict:
            raise ValueError("Настройки LDAP должны быть объектом")
        logger.warning("Настройки LDAP: ожидался объект, используются значения по умолчанию")
        return result

    def invalid(message: str) -> None:
        if strict:
            raise ValueError(message)
        logger.warning("Настройки LDAP: %s", message)

    unknown = set(data) - {f.name for f in fields(WebLdapConfig)}
    if unknown:
        invalid("Неизвестные поля: " + ", ".join(sorted(map(str, unknown))))
    for key in _BOOL_FIELDS:
        value = data.get(key, getattr(defaults, key))
        if type(value) is not bool:
            invalid(f"Поле {key} должно быть логическим")
        else:
            setattr(result, key, value)
    for key in _STRING_FIELDS:
        value = data.get(key, getattr(defaults, key))
        if not isinstance(value, str):
            invalid(f"Поле {key} должно быть строкой")
        else:
            setattr(result, key, value if key == "bind_password" else value.strip())
    if result.bind_mode not in LDAP_BIND_MODES:
        invalid("Поле bind_mode: допустимы " + ", ".join(LDAP_BIND_MODES))
        result.bind_mode = defaults.bind_mode
    for key, (low, high) in LDAP_RANGES.items():
        value = data.get(key, getattr(defaults, key))
        if type(value) is not int or not low <= value <= high:
            invalid(f"Поле {key}: требуется целое число {low}..{high}")
        else:
            setattr(result, key, value)
    try:
        parse_ldap_servers(result.servers)
        validate_ldap_config(result)
    except ValueError as error:
        if strict:
            raise
        if result.enabled:
            logger.warning("Настройки LDAP: %s; вход через LDAP выключен", error)
            result.enabled = False
    return result
