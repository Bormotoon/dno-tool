"""Credential Service — управление связками учётных данных (login/password).

Связки хранятся в settings.json (config.app.credentials.bundles). Пароли
шифруются при сохранении (Config.save) и расшифровываются при загрузке
(Config.load). Сервис работает с plaintext в памяти.

Приоритет разрешения учётных данных для хоста (resolve):
  1. device.credential_bundle_iid → связка по iid
  2. credentials.default_bundle_iid → глобальная связка по умолчанию
  3. (None, None) — caller fallback на 'root'
"""

import uuid
from typing import Optional, List, Dict, Any, Tuple

from src.config import config, CredentialBundle, CredentialsConfig
from src.services.base import BaseService
from src.architecture import EventType
from src.logger import logger

# Sentinel: пароль не менялся в UI (маска ********) — сохраняем старое значение.
PASSWORD_UNCHANGED = "********"


class CredentialService(BaseService):
    """Сервис для управления связками учётных данных."""

    def __init__(self, event_bus: Optional["EventBus"] = None) -> None:
        super().__init__(event_bus)
        self._config = config

    # ========== ЧТЕНИЕ ==========

    def get_bundles(self) -> List[Dict[str, Any]]:
        """Получить все связки (plaintext) как список dict для UI."""
        out = []
        for b in self._config.app.credentials.bundles or []:
            if isinstance(b, CredentialBundle):
                out.append(
                    {
                        "iid": b.iid,
                        "description": b.description,
                        "login": b.login,
                        "password": b.password,
                    }
                )
            elif isinstance(b, dict):
                # На случай если bundles хранятся как dict (до миграции)
                out.append(
                    {
                        "iid": b.get("iid", ""),
                        "description": b.get("description", ""),
                        "login": b.get("login", ""),
                        "password": b.get("password", ""),
                    }
                )
        return out

    def get_bundle_by_iid(self, iid: str) -> Optional[CredentialBundle]:
        """Найти связку по iid. Возвращает CredentialBundle или None."""
        if not iid:
            return None
        for b in self._config.app.credentials.bundles or []:
            if isinstance(b, CredentialBundle) and b.iid == iid:
                return b
        return None

    def get_default_bundle_iid(self) -> str:
        """iid связки по умолчанию (глобальная). Пусто = нет."""
        return self._config.app.credentials.default_bundle_iid or ""

    def get_default_bundle(self) -> Optional[CredentialBundle]:
        """Связка по умолчанию или None."""
        return self.get_bundle_by_iid(self.get_default_bundle_iid())

    # ========== РАЗРЕШЕНИЕ ==========

    def resolve(self, device: Any) -> Tuple[Optional[str], Optional[str]]:
        """Разрешить (login, password) для хоста по приоритету связок.

        Приоритет:
          1. device.credential_bundle_iid → связка
          2. credentials.default_bundle_iid → глобальная связка
          3. (None, None) — caller fallback на 'root'

        Args:
            device: DeviceModel или None (None = только глобальная связка).

        Returns:
            (login, password) — оба Optional[str]. (None, None) = нет связки.
        """
        # 1. Per-host bundle
        if device is not None:
            host_iid = getattr(device, "credential_bundle_iid", None)
            if host_iid:
                b = self.get_bundle_by_iid(host_iid)
                if b is not None:
                    return (b.login or None, b.password or None)

        # 2. Глобальная связка по умолчанию
        default_iid = self.get_default_bundle_iid()
        if default_iid:
            b = self.get_bundle_by_iid(default_iid)
            if b is not None:
                return (b.login or None, b.password or None)

        # 3. Нет связки
        return (None, None)

    # ========== ЗАПИСЬ ==========

    def set_default_bundle_iid(self, iid: str) -> None:
        """Установить iid связки по умолчанию (глобальная). Пусто = нет."""
        self._config.app.credentials.default_bundle_iid = iid or ""

    def update_bundles(self, bundles: List[Dict[str, Any]]) -> bool:
        """Обновить список связок из UI (plaintext passwords).

        Sentinel PASSWORD_UNCHANGED ('********') — сохранить старый пароль
        (когда UI показывает маску и пользователь не редактировал).

        Args:
            bundles: список dict с ключами iid, description, login, password.

        Returns:
            True если успешно.
        """
        try:
            existing = {
                b.iid: b
                for b in (self._config.app.credentials.bundles or [])
                if isinstance(b, CredentialBundle)
            }
            new_list: List[CredentialBundle] = []
            for entry in bundles:
                if not isinstance(entry, dict):
                    continue
                iid = (entry.get("iid") or "").strip()
                description = (entry.get("description") or "").strip()
                login = (entry.get("login") or "").strip()
                pw = entry.get("password", "")
                # Sentinel: пароль не менялся — берём из существующей связки
                if pw == PASSWORD_UNCHANGED:
                    old = existing.get(iid)
                    pw = old.password if old else ""
                # Новый iid если не задан
                if not iid:
                    iid = uuid.uuid4().hex
                new_list.append(
                    CredentialBundle(
                        iid=iid,
                        description=description,
                        login=login,
                        password=pw or "",
                    )
                )
            self._config.app.credentials.bundles = new_list
            self._logger.debug(f"CredentialService: updated {len(new_list)} bundles")
            return True
        except Exception as e:
            self._logger.error(f"CredentialService: Error updating bundles: {e}")
            return False

    # ========== СОХРАНЕНИЕ ==========

    def save(self) -> bool:
        """Сохранить конфиг (шифрует пароли связок) + опубликовать событие.

        Returns:
            True если успешно.
        """
        try:
            self._config.save()
            self._logger.info("CredentialService: credentials saved")
            if self._event_bus:
                self._event_bus.publish_typed(
                    EventType.DATA_SAVED,
                    source="CredentialService",
                    data={"type": "credentials"},
                )
            return True
        except Exception as e:
            self._logger.error(f"CredentialService: Error saving: {e}")
            return False
