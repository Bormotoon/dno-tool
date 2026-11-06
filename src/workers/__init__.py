"""Пакет фоновых исполнителей (workers)."""

from .base import BaseWorker
from .tracked_pool import TrackedThreadPoolExecutor

# Импорт исполнителей команд
from .command.orchestrator import CommandWorker
from .command.executor_base import BaseCommandExecutor
from .command.ssh import SSHWorker
from .command.sftp import SFTPWorker
from .command.local import LocalWorker

# Импорт сетевых исполнителей
from .network.host_timer import (
    HostPingTimerManager,
    get_host_ping_timer_manager,
    reset_host_ping_timer_manager,
)
from .network.worker_pool import (
    WorkerPoolManager,
    get_ping_worker_pool,
    get_command_worker_pool,
    get_veyon_worker_pool,
    get_thumbnail_worker_pool,
)

# Импорт Veyon исполнителей
from .veyon import (
    ThumbnailTimerManager,
    get_thumbnail_timer_manager,
    reset_thumbnail_timer_manager,
    VeyonThumbnailTimerManager,
    get_veyon_thumbnail_timer_manager,
    reset_veyon_thumbnail_timer_manager,
)

# Импорт установщиков сервисов
from .setup.worker import RemoteServiceInstallerWorker

# Импорт воркеров камер ЕЦХД
from .camera_workers import (
    CameraPingWorker,
    CameraRecordWorker,
    launch_player,
    launch_system_player,
    resolve_player_executable,
    build_player_cmd,
    stop_worker_blocking,
    PING_INTERVAL_MS,
    TIMER_INTERVAL_MS,
    DEFAULT_PING_TIMEOUT,
    DEFAULT_PING_INTERVAL,
)

# Импорт моделей Command из домена
from src.domain.models import Command, CommandType

__all__ = [
    # Base
    "BaseWorker",
    "TrackedThreadPoolExecutor",
    # Command - Models
    "Command",
    "CommandType",
    # Command - Workers
    "CommandWorker",
    "BaseCommandExecutor",
    "SSHWorker",
    "SFTPWorker",
    "LocalWorker",
    # Per-host Timer
    "HostPingTimerManager",
    "get_host_ping_timer_manager",
    "reset_host_ping_timer_manager",
    # Pool Manager
    "WorkerPoolManager",
    "get_ping_worker_pool",
    "get_command_worker_pool",
    "get_veyon_worker_pool",
    "get_thumbnail_worker_pool",
    # Veyon Thumbnail Timer
    "VeyonThumbnailTimerManager",
    "ThumbnailTimerManager",
    "get_thumbnail_timer_manager",
    "reset_thumbnail_timer_manager",
    "get_veyon_thumbnail_timer_manager",
    "reset_veyon_thumbnail_timer_manager",
    # Service installers
    "RemoteServiceInstallerWorker",
    # Camera workers
    "CameraPingWorker",
    "CameraRecordWorker",
    "launch_player",
    "launch_system_player",
    "resolve_player_executable",
    "build_player_cmd",
    "stop_worker_blocking",
    "PING_INTERVAL_MS",
    "TIMER_INTERVAL_MS",
    "DEFAULT_PING_TIMEOUT",
    "DEFAULT_PING_INTERVAL",
]
