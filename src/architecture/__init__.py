"""
Архитектурный модуль DNO Tool.

Предоставляет инфраструктуру для слабосвязанного взаимодействия компонентов:
- EventBus — шина событий (pub/sub) для асинхронной коммуникации;
- DialogManager — менеджер жизненного цикла диалоговых окон;
- ViewState — централизованное управление состоянием UI с поддержкой undo/redo;
- WorkerBridge — мост между фоновыми workers и системой событий;
- Интерфейсы (IEventHandler, IDialogClient, IStateObserver, IWorkerClient и др.)
  для стандартизации контрактов между компонентами;
- Mixin-классы (EventHandlerMixin, DialogClientMixin, StateObserverMixin,
  WorkerClientMixin, ComponentLifecycleMixin) для быстрой интеграции;
- Конкретные классы (EventFilter, DialogFactory, CommandHistory,
  WidgetDataBinding, PersistentStateManager).
"""

from .event_bus import EventBus, EventType, Event
from .dialog_manager import DialogManager, DialogResult, DialogConfig
from .view_state import ViewState, StateChangeEvent
from .interfaces import (
    # Абстрактные интерфейсы
    IEventHandler,
    IDialogClient,
    IStateObserver,
    IWorkerClient,
    ICommand,
    IService,
    IComponentLifecycle,
    IDataBinding,
    # Mixin-классы
    EventHandlerMixin,
    DialogClientMixin,
    StateObserverMixin,
    WorkerClientMixin,
    ComponentLifecycleMixin,
    # Конкретные реализации
    EventFilter,
    DialogFactory,
    CommandHistory,
    WidgetDataBinding,
    PersistentStateManager,
    # Типы
    HandlerId,
    DialogId,
    StateId,
    WorkerId,
)
from .worker_bridge import WorkerBridge, WorkerEventAdapter

__all__ = [
    # Event Bus
    'EventBus',
    'EventType',
    'Event',

    # Dialog Manager
    'DialogManager',
    'DialogResult',
    'DialogConfig',

    # View State
    'ViewState',
    'StateChangeEvent',

    # Worker Bridge
    'WorkerBridge',
    'WorkerEventAdapter',

    # Interfaces (ABC)
    'IEventHandler',
    'IDialogClient',
    'IStateObserver',
    'IWorkerClient',
    'ICommand',
    'IService',
    'IComponentLifecycle',
    'IDataBinding',

    # Mixin implementations
    'EventHandlerMixin',
    'DialogClientMixin',
    'StateObserverMixin',
    'WorkerClientMixin',
    'ComponentLifecycleMixin',

    # Concrete implementations
    'EventFilter',
    'DialogFactory',
    'CommandHistory',
    'WidgetDataBinding',
    'PersistentStateManager',

    # Type aliases
    'HandlerId',
    'DialogId',
    'StateId',
    'WorkerId',
]