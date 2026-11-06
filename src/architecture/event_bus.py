"""
Event Bus - централизованная шина событий для коммуникации между компонентами.

Предоставляет механизм подписки/публикации событий с поддержкой:
- Типизированных событий
- Фильтрации событий
- Приоритетов обработчиков
- Асинхронной доставки
"""

import time
import uuid
from enum import Enum, auto
from typing import Dict, List, Callable, Any, Optional, Set, Union
from dataclasses import dataclass, field
from PySide6.QtCore import QObject, Signal, Slot, QThread, Qt
from collections import defaultdict
import weakref
from src.logger import logger

from .interfaces import EventFilter


class EventType(Enum):
    """Стандартные типы событий системы."""

    # Диалоги
    DIALOG_OPENED = auto()
    DIALOG_CLOSED = auto()
    DIALOG_RESULT = auto()

    # Данные
    DATA_CHANGED = auto()
    DATA_LOADED = auto()
    DATA_SAVED = auto()

    # Устройства
    DEVICE_SELECTED = auto()
    DEVICE_ADDED = auto()
    DEVICE_REMOVED = auto()
    DEVICE_UPDATED = auto()

    # Команды
    COMMAND_STARTED = auto()
    COMMAND_PROGRESS = auto()
    COMMAND_FINISHED = auto()
    COMMAND_ABORTED = auto()

    # Workers
    WORKER_STARTED = auto()
    WORKER_PROGRESS = auto()
    WORKER_FINISHED = auto()
    WORKER_ERROR = auto()

    # UI
    UI_STATE_CHANGED = auto()
    VIEW_CHANGED = auto()

    # Ноды (федерация инстансов)
    NODE_CONNECTED = auto()
    NODE_DISCONNECTED = auto()
    NODE_HOSTS_UPDATED = auto()
    NODE_CONFIG_CHANGED = auto()

    # Пользовательские
    CUSTOM = auto()


@dataclass
class Event:
    """Контейнер для данных события."""

    event_type: EventType
    source: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def get(self, key: str, default: Any = None) -> Any:
        """Получить значение из данных события."""
        return self.data.get(key, default)

    def has(self, key: str) -> bool:
        """Проверить наличие ключа в данных."""
        return key in self.data


class EventBus(QObject):
    """
    Централизованная шина событий.

    Реализует паттерн Publish-Subscribe для слабосвязанной коммуникации
    между компонентами приложения.
    """

    # Сигнал для внутренней маршрутизации событий в Qt
    _internal_event = Signal(Event)

    def __init__(self):
        super().__init__()

        # Хранилище подписчиков: {EventType: [(handler_id, handler, filter, priority)]}
        self._subscribers: Dict[EventType, List[tuple]] = defaultdict(list)
        self._global_subscribers: List[tuple] = []  # Подписчики на все события
        self._handler_ids: Set[str] = set()
        self._id_counter = 0

        # Статистика
        self._stats = {"published": 0, "delivered": 0, "dropped": 0}

        # Подключаем внутренний сигнал
        self._internal_event.connect(self._route_event, Qt.QueuedConnection)

    def _generate_id(self) -> str:
        """Генерация уникального ID для обработчика."""
        self._id_counter += 1
        return f"handler_{self._id_counter}_{uuid.uuid4().hex[:4]}"

    def subscribe(
        self,
        event_type: Union[EventType, List[EventType]],
        handler: Callable[[Event], None],
        event_filter: Optional[EventFilter] = None,
        priority: int = 0,
        weak: bool = False,
    ) -> str:
        """
        Подписать обработчик на одно или несколько событий.

        Args:
            event_type: Тип события или список типов для подписки.
            handler: Функция-обработчик, принимающая экземпляр Event.
            event_filter: Опциональный фильтр для отбора событий.
            priority: Приоритет обработчика (выше = вызывается раньше).
            weak: Если True, обработчик хранится как weakref.

        Returns:
            Идентификатор подписки для последующей отписки.
        """
        handler_id = self._generate_id()
        self._handler_ids.add(handler_id)

        actual_handler = handler
        if weak:
            ref = weakref.ref(handler)

            def weak_handler(event: Event):
                h = ref()
                if h is not None:
                    h(event)

            actual_handler = weak_handler

        types = [event_type] if isinstance(event_type, EventType) else event_type

        for et in types:
            self._subscribers[et].append(
                (handler_id, actual_handler, event_filter, priority)
            )
            self._subscribers[et].sort(key=lambda x: x[3], reverse=True)

        logger.debug(f"EventBus: Handler {handler_id} subscribed to {types}")
        return handler_id

    def subscribe_all(
        self,
        handler: Callable[[Event], None],
        event_filter: Optional[EventFilter] = None,
        priority: int = 0,
    ) -> str:
        """
        Подписаться на все события.

        Args:
            handler: Функция-обработчик события
            event_filter: Опциональный фильтр событий
            priority: Приоритет обработчика

        Returns:
            ID подписки для отписки
        """
        handler_id = self._generate_id()
        self._handler_ids.add(handler_id)
        self._global_subscribers.append((handler_id, handler, event_filter, priority))
        self._global_subscribers.sort(key=lambda x: x[3], reverse=True)

        logger.debug(f"EventBus: Handler {handler_id} subscribed to all events")
        return handler_id

    def unsubscribe(self, handler_id: str) -> bool:
        """
        Отписаться от событий.

        Args:
            handler_id: ID подписки

        Returns:
            True если отписка успешна
        """
        if handler_id not in self._handler_ids:
            return False

        self._handler_ids.discard(handler_id)

        # Удаляем из типизированных подписчиков
        for et in self._subscribers:
            self._subscribers[et] = [
                (hid, h, f, p)
                for hid, h, f, p in self._subscribers[et]
                if hid != handler_id
            ]

        # Удаляем из глобальных подписчиков
        self._global_subscribers = [
            (hid, h, f, p)
            for hid, h, f, p in self._global_subscribers
            if hid != handler_id
        ]

        logger.debug(f"EventBus: Handler {handler_id} unsubscribed")
        return True

    def publish(self, event: Event, async_delivery: bool = True) -> None:
        """
        Опубликовать событие.

        Args:
            event: Событие для публикации
            async_delivery: Если True, доставка будет асинхронной через Qt сигнал
        """
        self._stats["published"] += 1

        if async_delivery:
            self._internal_event.emit(event)
        else:
            self._route_event(event)

    def publish_typed(
        self,
        event_type: EventType,
        source: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        async_delivery: bool = True,
    ) -> None:
        """
        Опубликовать типизированное событие.

        Args:
            event_type: Тип события
            source: Источник события
            data: Данные события
            async_delivery: Асинхронная доставка
        """
        event = Event(event_type=event_type, source=source, data=data or {})
        self.publish(event, async_delivery)

    @Slot(Event)
    def _route_event(self, event: Event) -> None:
        """Внутренний метод маршрутизации событий."""
        # Собираем всех подписчиков (оба списка уже отсортированы по приоритету)
        type_handlers = self._subscribers.get(event.event_type, [])

        # Объединяем типизированных и глобальных подписчиков (merge двух отсортированных списков)
        handlers = []
        i, j = 0, 0
        type_h = list(type_handlers)
        global_h = list(self._global_subscribers)
        while i < len(type_h) and j < len(global_h):
            if type_h[i][3] >= global_h[j][3]:
                handlers.append(type_h[i])
                i += 1
            else:
                handlers.append(global_h[j])
                j += 1
        handlers.extend(type_h[i:])
        handlers.extend(global_h[j:])

        # Вызываем обработчики
        delivered = 0
        for handler_id, handler, event_filter, _ in handlers:
            try:
                # Проверяем фильтр
                if event_filter and not event_filter.matches(event):
                    continue

                handler(event)
                delivered += 1

            except Exception as e:
                logger.error(f"EventBus: Error in handler {handler_id}: {e}")

        self._stats["delivered"] += delivered

        if delivered == 0:
            self._stats["dropped"] += 1
            logger.debug(f"EventBus: Event {event.event_type} dropped (no handlers)")

    def get_stats(self) -> Dict[str, int]:
        """Получить статистику событий."""
        return self._stats.copy()

    def clear_stats(self) -> None:
        """Очистить статистику."""
        self._stats = {"published": 0, "delivered": 0, "dropped": 0}

    def clear_all_subscriptions(self) -> None:
        """Очистить все подписки (использовать с осторожностью)."""
        self._subscribers.clear()
        self._global_subscribers.clear()
        self._handler_ids.clear()
        logger.info("EventBus: All subscriptions cleared")
