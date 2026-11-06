"""
Base Service - Базовый класс для всех сервисов.

Предоставляет общую инфраструктуру для сервисов:
- Интеграция с EventBus
- Централизованная обработка ошибок
- Логирование
"""

from abc import ABC
from typing import Optional, Callable, Any
from src.logger import logger
from src.architecture import EventBus, EventType


class BaseService(ABC):
    """
    Базовый класс для всех сервисов приложения.
    
    Предоставляет:
    - Доступ к EventBus для публикации событий
    - Метод для безопасного выполнения операций с обработкой ошибок
    - Логирование операций
    """

    def __init__(self, event_bus: Optional[EventBus] = None) -> None:
        """
        Инициализация сервиса.
        
        Args:
            event_bus: Экземпляр EventBus для публикации событий
        """
        self._event_bus = event_bus
        self._logger = logger

    def _execute_safely(
        self,
        operation: Callable[[], Any],
        context: str,
        on_success: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        publish_event: Optional[EventType] = None,
        event_data: Optional[dict] = None
    ) -> Any:
        """
        Безопасное выполнение операции с обработкой ошибок.
        
        Args:
            operation: Функция для выполнения
            context: Описание операции для логирования
            on_success: Callback при успехе
            on_error: Callback при ошибке
            publish_event: Тип события для публикации при успехе
            event_data: Данные события для публикации
        
        Returns:
            Результат выполнения операции
        
        Raises:
            Exception: Пробрасывает исключение после обработки
        """
        try:
            self._logger.debug(f"Service: Starting operation - {context}")
            result = operation()
            
            if on_success:
                on_success()
            
            if publish_event and self._event_bus:
                self._event_bus.publish_typed(
                    event_type=publish_event,
                    source=self.__class__.__name__,
                    data=event_data or {}
                )
            
            self._logger.debug(f"Service: Completed operation - {context}")
            return result
            
        except Exception as e:
            self._logger.error(f"Service: Error in operation '{context}': {e}")
            
            if on_error:
                on_error(e)
            
            # Публикуем событие об ошибке
            if self._event_bus:
                self._event_bus.publish_typed(
                    event_type=EventType.CUSTOM,
                    source=self.__class__.__name__,
                    data={'context': context, 'error': str(e)}
                )
            
            raise

    @property
    def event_bus(self) -> EventBus:
        """Получить EventBus сервиса."""
        return self._event_bus
