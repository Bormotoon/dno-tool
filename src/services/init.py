"""
Service Initialization - Инициализация сервисов приложения.

Регистрирует все сервисы в DI контейнере с правильными зависимостями.
DI контейнер автоматически разрешает зависимости через type hints.
"""

from src.di import get_container, DIContainer
from src.services import (
    DeviceService,
    CommandService,
    ConfigService,
    CredentialService,
    VeyonService,
    HostControlService,
    KRFBService,
    CameraService,
    NodeService,
    DeviceInfoService,
    KdeService,
)
from src.services.hosts.host_info_service import HostInfoService, get_host_info_service
from src.services.thumbnails.thumbnail_service import ThumbnailService
from src.services.media import MediaService
from src.architecture import EventBus, DialogManager, WorkerBridge, ViewState
from src.ui.tray import TrayController
from src.data import DataStore, datastore
from src.logger import logger


def initialize_services(container: DIContainer = None) -> DIContainer:
    """
    Инициализировать и зарегистрировать все сервисы.

    Args:
        container: DI контейнер (по умолчанию используется глобальный)

    Returns:
        Настроенный DI контейнер
    """
    if container is None:
        container = get_container()

    logger.info("Initializing services...")

    # Блокировка копии после очистки ноды проверяется перед каждым SSH-подключением.
    from src.utils.security.ban import install_ssh_guard, is_banned

    install_ssh_guard()
    is_banned()

    # Создаём архитектурные компоненты с правильными зависимостями
    event_bus = EventBus()
    dialog_manager = DialogManager(event_bus=event_bus)
    worker_bridge = WorkerBridge(event_bus=event_bus)
    view_state = ViewState()

    # Регистрируем архитектурные компоненты как singleton-экземпляры
    container.register_instance(EventBus, event_bus)
    container.register_instance(DialogManager, dialog_manager)
    container.register_instance(WorkerBridge, worker_bridge)
    container.register_instance(ViewState, view_state)

    # Регистрируем DataStore как singleton и привязываем EventBus
    datastore.set_event_bus(event_bus)
    container.register_instance(DataStore, datastore)

    # Регистрируем сервисы — DI автоматически внедряет EventBus через type hints
    container.register(DeviceService, DeviceService, singleton=True)
    container.register(CommandService, CommandService, singleton=True)
    container.register(ConfigService, ConfigService, singleton=True)
    container.register(CredentialService, CredentialService, singleton=True)
    container.register(VeyonService, VeyonService, singleton=True)
    container.register(ThumbnailService, ThumbnailService, singleton=True)
    container.register(MediaService, MediaService, singleton=True)
    container.register(HostControlService, HostControlService, singleton=True)
    container.register(KRFBService, KRFBService, singleton=True)
    container.register(CameraService, CameraService, singleton=True)
    container.register(NodeService, NodeService, singleton=True)
    container.register(DeviceInfoService, DeviceInfoService, singleton=True)
    container.register(KdeService, KdeService, singleton=True)

    # HostInfoService — QObject-синглтон через фабрику (без __new__: PySide6
    # на Linux падает, если QObject-наследник переопределяет __new__).
    container.register_instance(HostInfoService, get_host_info_service())

    # TrayController требует EventBus; экземпляр создаётся позже при первом
    # resolve, когда QApplication уже существует (QSystemTrayIcon требует QApplication).
    container.register(TrayController, TrayController, singleton=True)

    logger.info("Services initialized successfully")

    return container
