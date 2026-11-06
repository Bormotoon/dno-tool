"""CameraService — сервис управления камерами видеонаблюдения ЕЦХД.

Загрузка/сохранение иерархии камер и глобальных настроек подключения/записи
через единый конфиг приложения (``settings.json`` → секция ``camera``).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import TYPE_CHECKING, List, Optional

from src.architecture import EventBus, EventType
from src.config import config
from src.config.settings import (
    THUMBNAIL_CONFIG_LOCK,
    CameraConfig,
    normalize_camera_config,
)
from src.domain.models import CameraModel
from src.services.base import BaseService

if TYPE_CHECKING:
    from src.services.remote.camera_recording import CameraRecordingManager
    from src.services.remote.camera_web_bridge import CameraWebBridge


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

    def load_cameras(self) -> List[CameraModel]:
        """Собрать список CameraModel из иерархии в конфиге."""
        cfg = self.get_settings()
        cameras: List[CameraModel] = []
        for address, cabinets in (cfg.cameras or {}).items():
            for cabinet, cams in (cabinets or {}).items():
                for camera_name, ip in (cams or {}).items():
                    cameras.append(
                        CameraModel(
                            address=address,
                            cabinet=cabinet,
                            camera_name=camera_name,
                            ip_address=ip,
                            login=cfg.login,
                            password=cfg.password,
                            player=cfg.player,
                            output_path=cfg.output,
                            path_mask=cfg.path_mask,
                            file_mask=cfg.file_mask,
                            iid=cfg.camera_ids.get(
                                f"{address}::{cabinet}::{camera_name}", ""
                            ),
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

    def update_cameras_hierarchy(self, cameras: dict) -> bool:
        """Заменить только иерархию камер (без настроек подключения)."""
        cfg = self.get_settings()
        cfg.cameras = cameras or {}
        return self.update_settings(cfg)
