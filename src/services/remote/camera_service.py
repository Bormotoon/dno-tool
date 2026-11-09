"""CameraService — сервис управления камерами видеонаблюдения ЕЦХД.

Загрузка/сохранение иерархии камер и глобальных настроек подключения/записи
через единый конфиг приложения (``settings.json`` → секция ``camera``).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from uuid import uuid4

from src.architecture import EventBus, EventType
from src.config import config
from src.config.settings import (
    THUMBNAIL_CONFIG_LOCK,
    CameraConfig,
    CredentialBundle,
    normalize_camera_config,
)
from src.domain.models import CameraModel
from src.domain.models.camera import (
    CAMERA_CREDENTIALS_REQUIRED,
    camera_credentials_notice,
)
from src.services.base import BaseService

if TYPE_CHECKING:
    from src.services.remote.camera_recording import CameraRecordingManager
    from src.services.remote.camera_web_bridge import CameraWebBridge


# Маска пароля в диалоге связок: пароль не менялся.
_PASSWORD_UNCHANGED = "********"


def resolve_camera_credentials(
    cfg: CameraConfig, camera_iid: str, bundle_iid: str | None = None
) -> Tuple[str, str, str]:
    """Вернуть (логин, пароль, ошибка) камеры.

    Порядок как у SSH: связка камеры → связка по умолчанию → без авторизации.
    Ссылка на отсутствующую связку не подменяется другой: возвращается
    требование заполнить данные для входа. ``bundle_iid`` задаёт связку
    камеры явно (несохранённый выбор в настройках).
    """
    bundles = {bundle.iid: bundle for bundle in cfg.bundles}
    own = cfg.camera_bundles.get(camera_iid, "") if bundle_iid is None else bundle_iid
    for iid in (own, cfg.default_bundle_iid):
        if not iid:
            continue
        bundle = bundles.get(iid)
        if bundle is None:
            return "", "", CAMERA_CREDENTIALS_REQUIRED
        return bundle.login, bundle.password, ""
    return "", "", ""


class CameraBundleStore:
    """Связки камер для общего диалога связок и BundleSelector."""

    title = "Связки учётных данных камер"
    hint = (
        "Связки учётных данных (логин/пароль) для RTSP-потоков камер.\n"
        "Дважды кликните по ячейке для редактирования. Пароль хранится "
        "в settings.json в зашифрованном виде."
    )
    saved_message = "Связки учётных данных камер сохранены."

    def __init__(self, service: "CameraService") -> None:
        self._service = service
        self._pending: list | None = None

    def get_bundles(self) -> List[Dict[str, Any]]:
        return self._service.get_bundles()

    def update_bundles(self, bundles: List[Dict[str, Any]]) -> bool:
        self._pending = list(bundles)
        return True

    def save(self) -> bool:
        pending, self._pending = self._pending, None
        return pending is None or self._service.update_bundles(pending)


class CameraService(BaseService):
    """Сервис камер ЕЦХД: чтение/запись настроек и иерархии камер."""

    def __init__(self, event_bus: Optional[EventBus] = None) -> None:
        super().__init__(event_bus)
        self._config = config
        self._recording_manager = None
        self._web_bridge: CameraWebBridge | None = None
        self._recording_shutdown = False

    # ---------- Чтение ----------

    @property
    def load_error(self) -> str | None:
        """Сообщение об ошибке загрузки, блокирующей сохранение настроек."""
        return getattr(self._config, "camera_load_error", None) or getattr(
            self._config, "_settings_load_error", None
        )

    def get_settings(self) -> CameraConfig:
        """Вернуть независимый глубокий снимок настроек."""
        with THUMBNAIL_CONFIG_LOCK:
            return deepcopy(self._config.app.camera)

    def recording_manager(self) -> CameraRecordingManager:
        """Вернуть менеджер записи, принадлежащий сервису, в GUI-потоке."""
        if self._recording_shutdown:
            raise RuntimeError("Приложение завершает работу")
        if self._recording_manager is None:
            from PySide6.QtCore import QCoreApplication, QThread

            from src.services.remote.camera_recording import CameraRecordingManager

            app = QCoreApplication.instance()
            if app is None or QThread.currentThread() != app.thread():
                raise RuntimeError("Менеджер записи создаётся только в GUI-потоке")
            self._recording_manager = CameraRecordingManager(self, app)
        return self._recording_manager

    def web_recording_bridge(self) -> CameraWebBridge:
        """Создать GUI-мост при старте веб-сервера; затем вернуть общий экземпляр."""
        if self._web_bridge is None:
            from PySide6.QtCore import QCoreApplication, QThread

            from src.services.remote.camera_web_bridge import CameraWebBridge

            app = QCoreApplication.instance()
            if app is None or QThread.currentThread() != app.thread():
                raise RuntimeError("Мост записи создаётся только в GUI-потоке")
            self._web_bridge = CameraWebBridge(self, app)
        return self._web_bridge

    def request_shutdown_recordings(self) -> None:
        """Остановить и веб-записи, даже если окно камер Qt не открывалось."""
        self._recording_shutdown = True
        if self._recording_manager is not None:
            self._recording_manager.request_shutdown_recordings()

    def has_pending_recordings(self) -> bool:
        """Удерживать владельца записи до настоящего завершения всех потоков."""
        return bool(
            self._recording_manager is not None
            and self._recording_manager.has_pending_workers()
        )

    @property
    def credentials_notice(self) -> str:
        """Напоминание заполнить данные для входа.

        Показывается всегда, пока у камер есть ссылки на связки, не
        прошедшие проверку (связка камеры или по умолчанию не найдена).
        """
        return camera_credentials_notice(
            sum(bool(camera.credential_error) for camera in self.load_cameras())
        )

    def get_bundles(self) -> List[Dict[str, Any]]:
        """Связки камер (пароли открыты) как список dict для UI."""
        return [asdict(bundle) for bundle in self.get_settings().bundles]

    def bundle_store(self) -> CameraBundleStore:
        """Хранилище связок для CredentialsDialog/BundleSelector."""
        return CameraBundleStore(self)

    def load_cameras(self) -> List[CameraModel]:
        """Собрать список CameraModel из иерархии в конфиге."""
        cfg = self.get_settings()
        cameras: List[CameraModel] = []
        for address, cabinets in (cfg.cameras or {}).items():
            for cabinet, cams in (cabinets or {}).items():
                for camera_name, ip in (cams or {}).items():
                    iid = cfg.camera_ids.get(f"{address}::{cabinet}::{camera_name}", "")
                    login, password, error = resolve_camera_credentials(cfg, iid)
                    cameras.append(
                        CameraModel(
                            address=address,
                            cabinet=cabinet,
                            camera_name=camera_name,
                            ip_address=ip,
                            login=login,
                            password=password,
                            player=cfg.player,
                            output_path=cfg.output,
                            path_mask=cfg.path_mask,
                            file_mask=cfg.file_mask,
                            iid=iid,
                            credential_error=error,
                        )
                    )
        return cameras

    # ---------- Запись ----------

    def update_settings(self, new_cfg: CameraConfig) -> bool:
        """Заменить секцию camera в конфиге и сохранить на диск.

        Публикует ``DATA_SAVED`` с ``type=camera_config``.
        """
        try:
            checked = normalize_camera_config(asdict(new_cfg))
            with THUMBNAIL_CONFIG_LOCK:
                old = self._config.app.camera
                self._config.app.camera = checked
                try:
                    self._config.save()
                except Exception:
                    self._config.app.camera = old
                    raise
            if self._recording_manager is not None:
                self._recording_manager.reload()
            self._logger.info("CameraService: camera config saved")
            if self._event_bus is not None:
                self._event_bus.publish_typed(
                    EventType.DATA_SAVED,
                    source="CameraService",
                    data={"type": "camera_config"},
                )
            return True
        except Exception as e:
            self._logger.error(f"CameraService: error saving camera config: {e}")
            return False

    def update_bundles(self, bundles: List[Dict[str, Any]]) -> bool:
        """Заменить связки камер и сохранить; маска пароля оставляет прежний."""
        cfg = self.get_settings()
        existing = {bundle.iid: bundle for bundle in cfg.bundles}
        result: List[CredentialBundle] = []
        for entry in bundles:
            if not isinstance(entry, dict):
                continue
            iid = str(entry.get("iid") or "").strip() or uuid4().hex
            password = entry.get("password", "")
            if password == _PASSWORD_UNCHANGED:
                old = existing.get(iid)
                password = old.password if old else ""
            result.append(
                CredentialBundle(
                    iid=iid,
                    description=str(entry.get("description") or "").strip(),
                    login=str(entry.get("login") or "").strip(),
                    password=password if isinstance(password, str) else "",
                )
            )
        cfg.bundles = result
        return self.update_settings(cfg)

    def update_cameras_hierarchy(self, cameras: dict) -> bool:
        """Заменить только иерархию камер (без настроек подключения)."""
        cfg = self.get_settings()
        cfg.cameras = cameras or {}
        return self.update_settings(cfg)
