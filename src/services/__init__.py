"""
Service Layer - Сервисный слой для бизнес-логики.

Предоставляет централизованный доступ к данным через сервисы,
абстрагируя UI компоненты от прямого доступа к DataStore.
"""

from .base import BaseService
from .media import MediaService
from src.services.hosts.device_service import DeviceService
from src.services.hosts.kde_service import KdeService
from src.services.commands.command_service import CommandService
from src.services.configuration.config_service import ConfigService
from src.services.configuration.credential_service import CredentialService
from src.services.remote.veyon_service import VeyonService
from src.services.thumbnails.thumbnail_service import ThumbnailService
from src.services.hosts.host_control_service import HostControlService
from src.services.hosts.device_info import DeviceInfoService
from src.services.remote.krfb_service import KRFBService
from src.services.remote.camera_service import CameraService
from src.services.remote.node_service import NodeService
from src.services.hosts.host_info_service import (
    HostInfoService,
    get_host_info_service,
    reset_host_info_service,
)
from .init import initialize_services

__all__ = [
    "BaseService",
    "MediaService",
    "DeviceService",
    "KdeService",
    "CommandService",
    "ConfigService",
    "CredentialService",
    "VeyonService",
    "ThumbnailService",
    "HostControlService",
    "DeviceInfoService",
    "KRFBService",
    "CameraService",
    "NodeService",
    "HostInfoService",
    "get_host_info_service",
    "reset_host_info_service",
    "initialize_services",
]
