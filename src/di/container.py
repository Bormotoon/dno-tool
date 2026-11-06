"""
Dependency Injection Container - Контейнер зависимостей приложения.

Предоставляет централизованное управление сервисами и компонентами
с поддержкой ленивой инициализации и внедрения зависимостей.
"""

from typing import Dict, Any, Optional, Callable, Type, TypeVar, get_type_hints
import inspect
import threading
from src.logger import logger

T = TypeVar('T')


class DIContainer:
    """
    Контейнер зависимостей для управления сервисами приложения.
    
    Особенности:
    - Ленивая инициализация сервисов
    - Автоматическое разрешение зависимостей
    - Поддержка синглтонов
    - Регистрация через фабрику или класс
    
    Пример использования:
        # Регистрация
        container = DIContainer()
        container.register(IDeviceService, DeviceService)
        container.register_instance(EventBus, event_bus_instance)
        
        # Получение
        device_service = container.resolve(IDeviceService)
    """

    def __init__(self):
        self._services: Dict[type, Any] = {}
        self._factories: Dict[type, Callable[[], Any]] = {}
        self._instances: Dict[type, Any] = {}
        self._lock = threading.RLock()
        self._logger = logger
        
        self._logger.debug("DIContainer: Initialized")

    def register(
        self,
        interface: type,
        implementation: Optional[type] = None,
        factory: Optional[Callable[[], Any]] = None,
        singleton: bool = True
    ) -> None:
        """
        Зарегистрировать сервис в контейнере.

        Можно указать либо ``implementation`` (класс, экземпляр которого
        будет создан через авто-разрешение зависимостей), либо ``factory``
        (фабричную функцию, возвращающую экземпляр).

        Args:
            interface: Интерфейс или базовый класс, по которому будет производиться resolve.
            implementation: Класс-реализация (опционально, если указана factory).
            factory: Фабричная функция для создания экземпляра (опционально).
            singleton: Если True, создаётся единственный экземпляр (кэшируется).

        Raises:
            ValueError: Если не указан ни ``implementation``, ни ``factory``.
        """
        if implementation is None and factory is None:
            raise ValueError("Must provide either implementation or factory")
        
        if factory is None:
            impl = implementation
            def create_instance():
                return self._auto_resolve(impl)
            factory = create_instance
        
        with self._lock:
            self._factories[interface] = factory
            self._services[interface] = {
                'singleton': singleton,
                'factory': factory
            }
        
        self._logger.debug(f"DIContainer: Registered {interface.__name__}")

    def register_instance(
        self,
        interface: type,
        instance: Any,
        singleton: bool = True
    ) -> None:
        with self._lock:
            if singleton:
                self._instances[interface] = instance
            self._services[interface] = {
                'singleton': singleton,
                'instance': instance
            }
        
        self._logger.debug(f"DIContainer: Registered instance {interface.__name__}")

    def resolve(self, interface: type) -> Any:
        with self._lock:
            if interface in self._instances:
                return self._instances[interface]
            
            if interface not in self._services:
                raise KeyError(f"Service {interface.__name__} not registered")
            
            service_info = self._services[interface]
            
            if 'instance' in service_info:
                return service_info['instance']
            
            factory = service_info['factory']
            instance = factory()
            
            if service_info['singleton']:
                self._instances[interface] = instance
        
        self._logger.debug(f"DIContainer: Resolved {interface.__name__}")
        return instance

    def resolve_optional(self, interface: type, default: Any = None) -> Any:
        """
        Получить экземпляр сервиса или значение по умолчанию.
        
        Args:
            interface: Интерфейс или базовый класс
            default: Значение по умолчанию
        
        Returns:
            Экземпляр сервиса или default
        """
        try:
            return self.resolve(interface)
        except KeyError:
            return default

    def _auto_resolve(self, implementation: type) -> Any:
        """
        Автоматическое разрешение зависимостей через инспекцию конструктора.
        
        Inspects the implementation's __init__ for type-hinted parameters
        and resolves them from the container.
        """
        try:
            localns = {}
            for iface in self._services:
                localns[iface.__name__] = iface
            hints = get_type_hints(implementation.__init__, localns=localns)
        except Exception:
            hints = {}
        
        hints.pop('return', None)
        
        sig = inspect.signature(implementation.__init__)
        kwargs = {}
        
        for param_name, param in sig.parameters.items():
            if param_name in ('self', 'args', 'kwargs'):
                continue
            
            type_hint = hints.get(param_name)
            if type_hint is None:
                continue
            
            unwrapped = self._unwrap_optional(type_hint)
            if unwrapped and self.is_registered(unwrapped):
                try:
                    kwargs[param_name] = self.resolve(unwrapped)
                except KeyError:
                    pass
            elif param.default is inspect.Parameter.empty:
                logger.error(f"DIContainer: Cannot resolve required dependency '{param_name}:{type_hint}' for {implementation.__name__}")
                continue
        
        return implementation(**kwargs)

    @staticmethod
    def _unwrap_optional(type_hint: Any) -> Optional[type]:
        """Извлечь тип из Optional[Type] (Union[Type, None])."""
        import typing
        origin = getattr(type_hint, '__origin__', None)
        
        # Python 3.10+: X | None
        if origin is type(int | None):
            args = type_hint.__args__
            for arg in args:
                if arg is not type(None):
                    return arg
        
        # typing.Optional[X] or Union[X, None]
        if origin is typing.Union:
            args = type_hint.__args__
            for arg in args:
                if arg is not type(None):
                    return arg
        
        # Plain type
        if isinstance(type_hint, type):
            return type_hint
        
        return None

    def is_registered(self, interface: type) -> bool:
        """
        Проверить, зарегистрирован ли сервис.
        
        Args:
            interface: Интерфейс или базовый класс
        
        Returns:
            True если зарегистрирован
        """
        return interface in self._services

    def unregister(self, interface: type) -> bool:
        """
        Отменить регистрацию сервиса.
        
        Args:
            interface: Интерфейс или базовый класс
        
        Returns:
            True если сервис был удален
        """
        if interface not in self._services:
            return False
        
        del self._services[interface]
        self._instances.pop(interface, None)
        self._factories.pop(interface, None)
        
        self._logger.debug(f"DIContainer: Unregistered {interface.__name__}")
        return True

    def clear(self) -> None:
        """Очистить все регистрации."""
        self._services.clear()
        self._instances.clear()
        self._factories.clear()
        self._logger.info("DIContainer: Cleared all registrations")

    def get_stats(self) -> Dict[str, int]:
        """
        Получить статистику контейнера.
        
        Returns:
            Словарь со статистикой
        """
        return {
            'registered_services': len(self._services),
            'cached_instances': len(self._instances),
            'factories': len(self._factories)
        }


# Глобальный контейнер приложения
_app_container: Optional[DIContainer] = None


def get_container() -> DIContainer:
    """
    Получить глобальный контейнер приложения.
    
    Returns:
        Экземпляр DIContainer
    """
    global _app_container
    if _app_container is None:
        _app_container = DIContainer()
    return _app_container


def reset_container() -> None:
    """Сбросить глобальный контейнер."""
    global _app_container
    if _app_container is not None:
        _app_container.clear()
    _app_container = None
