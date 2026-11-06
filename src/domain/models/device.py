"""Модель устройства для представления сетевого узла."""

import uuid
from typing import Any

from ...config import (
    ICONS,
    Config,
    normalize_thumbnail_backend,
    validate_thumbnail_backend,
)


class DeviceModel(object):
    def __init__(self, data: dict | None = None) -> None:
        # iid: приоритет — из data (сохраняет связь с DataStore), иначе генерируем новый
        self._iid = (data.get("iid") if data else None) or uuid.uuid4().hex
        self.name = data.get("name", "") if data else ""
        self.host = data.get("host", "") if data else ""
        self.port = data.get("port", None) if data else None
        self._icon_offline = (
            data.get("icon", ICONS["offline"]) if data else ICONS["offline"]
        )
        self._icon_online = (
            data.get("icon", ICONS["online"]) if data else ICONS["online"]
        )
        # iid связки учётных данных (CredentialBundle.iid). None = наследовать
        # глобальную default_bundle. Приоритет в get_credentials:
        # device.credential_bundle_iid → credentials.default_bundle_iid → "root".
        # Учётные данные (логин и пароль, в т.ч. для sudo) — ТОЛЬКО из связки.
        self.credential_bundle_iid = (
            data.get("credential_bundle_iid", None) if data else None
        )
        self.mac_address = data.get("mac_address", None) if data else None
        # VNC-порт per-host override (x11vnc и krfb); None = глобальный
        # vnc_port.
        self.vnc_port = data.get("vnc_port", None) if data else None
        self.thumbnail_backend: str | None = normalize_thumbnail_backend(
            data.get("thumbnail_backend") if data else None, allow_none=True
        )
        # Флаг sudo: None = наследовать SSHConfig.use_sudo, True/False = per-device override.
        # Когда включено и SSH-логин не root, SSHWorker подаёт пароль sudo через PTY-expect
        # для команд, содержащих `sudo`. См. src/workers/command/sudo_utils.py
        self.use_sudo = data.get("use_sudo", None) if data else None
        # Локаль: None = наследовать SSHConfig.locale, строка (в т.ч. пустая) = per-device
        # override. Пустая строка = явно отключить экспорт локали для этого хоста.
        # См. src/workers/command/locale_utils.py
        self.locale = data.get("locale", None) if data else None

        # node_iid: iid ноды-владельца (см. систему нод / федерацию).
        # None = локальный хост. НЕ сериализуется (заполняется в памяти
        # при построении секции ноды в дереве).
        self.node_iid = getattr(data, "node_iid", None) if data else None

        # Инициализируем как offline по умолчанию, но со стандартной иконкой
        self._is_online = False
        self._online_revision = 0
        self.icon = ICONS["default"]

    @property
    def is_online(self) -> bool:
        """Проверка статуса онлайн"""
        return self._is_online

    def set_online(self, online: bool = False) -> None:
        """Установить статус онлайн/офлайн и обновить иконку."""
        if self._is_online != online:
            self._online_revision += 1
        self._is_online = online
        if online:
            self.icon = self._icon_online
        else:
            self.icon = self._icon_offline

    @property
    def online_revision(self) -> int:
        """Identify reachability transitions, including an offline/online round trip."""
        return self._online_revision

    def update(self, data: Any = None) -> None:
        """Обновить поля модели из словаря или другого объекта."""
        if data is None:
            return

        # Если объект предоставляет to_dict(), предпочитаем его
        if hasattr(data, "to_dict") and not isinstance(data, dict):
            try:
                data = data.to_dict()
            except Exception:
                pass

        # Если получили словарь, используем .get как раньше
        if isinstance(data, dict):
            thumbnail_backend = validate_thumbnail_backend(
                data.get("thumbnail_backend", self.thumbnail_backend), allow_none=True
            )
            # iid обновляем только если передан — иначе сохраняем существующий
            if data.get("iid"):
                self._iid = data.get("iid")
            self.name = data.get("name", self.name)
            self.host = data.get("host", self.host)
            self.port = data.get("port", self.port)
            self._icon_offline = data.get("icon_offline", self._icon_offline)
            self._icon_online = data.get("icon_online", self._icon_online)
            self.credential_bundle_iid = data.get(
                "credential_bundle_iid", self.credential_bundle_iid
            )
            self.mac_address = data.get("mac_address", self.mac_address)
            self.vnc_port = data.get("vnc_port", self.vnc_port)
            self.thumbnail_backend = thumbnail_backend
            self.use_sudo = data.get("use_sudo", self.use_sudo)
            self.locale = data.get("locale", self.locale)
            return

        # Резервный вариант: копируем атрибуты из переданного объекта
        thumbnail_backend = validate_thumbnail_backend(
            getattr(data, "thumbnail_backend", self.thumbnail_backend), allow_none=True
        )
        if hasattr(data, "iid") and data.iid:
            self._iid = data.iid
        self.name = getattr(data, "name", self.name)
        self.host = getattr(data, "host", self.host)
        self.port = getattr(data, "port", self.port)
        self._icon_offline = getattr(
            data, "_icon_offline", getattr(data, "icon_offline", self._icon_offline)
        )
        self._icon_online = getattr(
            data, "_icon_online", getattr(data, "icon_online", self._icon_online)
        )
        self.credential_bundle_iid = getattr(
            data, "credential_bundle_iid", self.credential_bundle_iid
        )
        self.mac_address = getattr(data, "mac_address", self.mac_address)
        self.vnc_port = getattr(data, "vnc_port", self.vnc_port)
        self.thumbnail_backend = thumbnail_backend
        self.use_sudo = getattr(data, "use_sudo", self.use_sudo)
        self.locale = getattr(data, "locale", self.locale)

    @property
    def iid(self) -> str:
        """Уникальный идентификатор экземпляра (генерируется автоматически)."""
        return self._iid

    def __str__(self) -> str:
        """Строковое представление устройства."""
        return f"{self.name} ({self.host}:{self.port})"

    def __repr__(self) -> str:
        """Представление для отладки."""
        return f"{self.name} ({self.host}:{self.port})"

    def to_dict(self) -> dict:
        """Сериализовать устройство в словарь со всеми полями."""
        return {
            "iid": self.iid,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "icon": self.icon,
            "credential_bundle_iid": self.credential_bundle_iid,
            "mac_address": self.mac_address,
            "vnc_port": self.vnc_port,
            "thumbnail_backend": self.thumbnail_backend,
            "use_sudo": self.use_sudo,
            "locale": self.locale,
        }

    def export(self) -> dict:
        """Экспортировать устройство в словарь для сохранения, опуская поля со значениями по умолчанию.

        iid включается и сохраняется в JSON — это персистентный идентификатор
        для связи между виджетами (дерево↔список↔таблица). Старые файлы без iid
        мигрируются автоматически при первом сохранении.
        """
        device = {
            "iid": self.iid,
            "name": self.name,
            "host": self.host,
        }
        if self.port and self.port != Config().app.ssh.port:
            device["port"] = self.port
        if self.credential_bundle_iid:
            device["credential_bundle_iid"] = self.credential_bundle_iid
        if self.mac_address:
            device["mac_address"] = self.mac_address
        if self.vnc_port:
            device["vnc_port"] = self.vnc_port
        if self.thumbnail_backend is not None:
            device["thumbnail_backend"] = self.thumbnail_backend
        if self.use_sudo is not None:
            device["use_sudo"] = self.use_sudo
        if self.locale is not None:
            device["locale"] = self.locale

        return device
