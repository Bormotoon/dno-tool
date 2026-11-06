"""
Integration Interfaces - интерфейсы и mixin-классы для слабосвязанной интеграции компонентов.

Предоставляет абстракции и готовые реализации (mixins) для стандартизации
контрактов между компонентами архитектуры приложения.

Абстрактные интерфейсы (ABC):
- IEventHandler — реакция на события EventBus
- IDialogClient — реакция на результаты диалогов
- IStateObserver — реакция на изменение состояния ViewState
- IWorkerClient — реакция на события воркеров
- ICommand — паттерн Command с undo/redo
- IService — жизненный цикл сервиса (start/stop)
- IComponentLifecycle — инициализация и очистка компонентов
- IDataBinding — двусторонняя привязка данных

Mixin-классы с готовой реализацией:
- EventHandlerMixin — автоматическая подписка/отписка на события
- DialogClientMixin — центральная обработка результатов диалогов
- StateObserverMixin — наблюдение за изменениями состояния
- WorkerClientMixin — централизованная обработка событий воркеров
- ComponentLifecycleMixin — управление жизненным циклом

Concrete-классы:
- EventFilter — фильтрация событий по типу/источнику/данным
- DialogFactory — абстрактная фабрика диалогов
- CommandHistory — история команд с undo/redo
- WidgetDataBinding — двусторонняя привязка данных виджета к состоянию
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, Callable, Union, Type, Set, TYPE_CHECKING
from PySide6.QtWidgets import QDialog, QWidget
from PySide6.QtCore import QObject, Signal, QSettings
from dataclasses import dataclass, field
import time
import weakref
from src.logger import logger

if TYPE_CHECKING:
    from .event_bus import Event, EventType
    from .dialog_manager import DialogConfig, DialogResult, DialogManager
    from .view_state import StateChangeEvent, ViewState
    from .worker_bridge import WorkerBridge


# =============================================================================
# Абстрактные интерфейсы (ABC)
# =============================================================================

class IEventHandler(ABC):
    """Интерфейс компонента, обрабатывающего события EventBus."""

    @abstractmethod
    def handle_event(self, event: 'Event') -> None:
        """Обработать событие."""
        pass


class IDialogClient(ABC):
    """Интерфейс компонента, получающего результаты диалогов."""

    @abstractmethod
    def on_dialog_result(self, dialog_id: str, result: 'DialogResult', data: Any) -> None:
        """Вызывается при получении результата от диалога."""
        pass


class IStateObserver(ABC):
    """Интерфейс компонента, наблюдающего за изменениями ViewState."""

    @abstractmethod
    def on_state_changed(self, state_id: str, event: 'StateChangeEvent') -> None:
        """Вызывается при изменении состояния."""
        pass


class IWorkerClient(ABC):
    """Интерфейс компонента, получающего события от WorkerBridge."""

    @abstractmethod
    def on_worker_started(self, worker_id: str, context: Dict[str, Any]) -> None:
        pass

    @abstractmethod
    def on_worker_progress(self, worker_id: str, progress: Any) -> None:
        pass

    @abstractmethod
    def on_worker_finished(self, worker_id: str, result: Any) -> None:
        pass

    @abstractmethod
    def on_worker_error(self, worker_id: str, error: str) -> None:
        pass


class ICommand(ABC):
    """Интерфейс команды (паттерн Command) с поддержкой undo/redo."""

    @abstractmethod
    def execute(self) -> None:
        pass

    @abstractmethod
    def undo(self) -> None:
        pass

    @abstractmethod
    def redo(self) -> None:
        pass

    def can_execute(self) -> bool:
        return True

    def can_undo(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return self.__class__.__name__


class IService(ABC):
    """Интерфейс сервиса приложения."""

    @abstractmethod
    def start(self) -> None:
        pass

    @abstractmethod
    def stop(self) -> None:
        pass

    @abstractmethod
    def get_name(self) -> str:
        pass

    def is_running(self) -> bool:
        return False


class IComponentLifecycle(ABC):
    """Интерфейс жизненного цикла компонента."""

    @abstractmethod
    def initialize(self) -> None:
        pass

    @abstractmethod
    def cleanup(self) -> None:
        pass


class IDataBinding(ABC):
    """Интерфейс двусторонней привязки данных."""

    @abstractmethod
    def bind(self, source: Any, target: Any) -> None:
        pass

    @abstractmethod
    def unbind(self) -> None:
        pass

    @abstractmethod
    def sync_to_target(self) -> None:
        pass

    @abstractmethod
    def sync_to_source(self) -> None:
        pass


# =============================================================================
# Mixin-классы с готовой реализацией
# =============================================================================

class EventHandlerMixin:
    """
    Mixin для автоматической подписки/отписки на события EventBus.

    Подкласс вызывает subscribe()/subscribe_all() в initialize(),
    отписка происходит автоматически в cleanup().

    Пример:
        class MyComponent(EventHandlerMixin):
            def initialize(self):
                self.event_bus = container.resolve(EventBus)
                self.subscribe(EventType.DATA_CHANGED, self._on_data_changed)

            def _on_data_changed(self, event):
                ...
    """

    def __init__(self):
        self._event_bus: Optional[Any] = None
        self._handler_ids: List[str] = []
        self._global_handler_id: Optional[str] = None

    def _set_event_bus(self, event_bus: Any) -> None:
        """Установить EventBus для миксина."""
        self._event_bus = event_bus

    def subscribe(
        self,
        event_type: Union['EventType', List['EventType']],
        handler: Callable[['Event'], None],
        event_filter: Optional['EventFilter'] = None,
        priority: int = 0
    ) -> str:
        """
        Подписаться на событие. ID сохраняется для автоматической отписки.

        Returns:
            handler_id для ручной отписки
        """
        if self._event_bus is None:
            logger.warning(f"{self.__class__.__name__}: EventBus not set, cannot subscribe")
            return ''
        handler_id = self._event_bus.subscribe(event_type, handler, event_filter, priority)
        self._handler_ids.append(handler_id)
        return handler_id

    def subscribe_all(
        self,
        handler: Callable[['Event'], None],
        event_filter: Optional['EventFilter'] = None,
        priority: int = 0
    ) -> str:
        """Подписаться на все события."""
        if self._event_bus is None:
            logger.warning(f"{self.__class__.__name__}: EventBus not set, cannot subscribe_all")
            return ''
        self._global_handler_id = self._event_bus.subscribe_all(handler, event_filter, priority)
        return self._global_handler_id

    def unsubscribe(self, handler_id: str) -> bool:
        """Отписаться от конкретного обработчика."""
        if self._event_bus is None:
            return False
        result = self._event_bus.unsubscribe(handler_id)
        if result and handler_id in self._handler_ids:
            self._handler_ids.remove(handler_id)
        return result

    def unsubscribe_all_tracked(self) -> None:
        """Отписаться от всех сохранённых обработчиков."""
        if self._event_bus is None:
            return
        for handler_id in self._handler_ids[:]:
            self._event_bus.unsubscribe(handler_id)
        self._handler_ids.clear()
        if self._global_handler_id:
            self._event_bus.unsubscribe(self._global_handler_id)
            self._global_handler_id = None

    def publish(self, event_type: 'EventType', source: Optional[str] = None,
                data: Optional[Dict[str, Any]] = None) -> None:
        """Опубликовать событие через EventBus."""
        if self._event_bus is None:
            logger.warning(f"{self.__class__.__name__}: EventBus not set, cannot publish")
            return
        self._event_bus.publish_typed(event_type=event_type, source=source, data=data or {})


IEventHandler.register(EventHandlerMixin)


class DialogClientMixin:
    """
    Mixin для централизованной обработки результатов диалогов.

    Предоставляет удобные методы для открытия диалогов через DialogManager
    и маршрутизации результатов в on_dialog_result.

    Пример:
        class MyWindow(DialogClientMixin):
            def initialize(self):
                self.dialog_manager = container.resolve(DialogManager)

            def show_settings(self):
                self.open_dialog(
                    dialog_class=ConfigDialog,
                    modal=True,
                    singleton=True
                )

            def on_dialog_result(self, dialog_id, result, data):
                if result == DialogResult.ACCEPTED:
                    ...
    """

    def __init__(self):
        self._dialog_manager: Optional[Any] = None
        self._pending_dialogs: Dict[str, str] = {}

    def _set_dialog_manager(self, dialog_manager: Any) -> None:
        """Установить DialogManager для миксина."""
        self._dialog_manager = dialog_manager

    def open_dialog(
        self,
        dialog_class: Type[QDialog],
        modal: bool = True,
        singleton: bool = False,
        title: Optional[str] = None,
        size: Optional[tuple] = None,
        data: Optional[Dict[str, Any]] = None,
        on_result: Optional[Callable[['DialogResult', Any], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
        destroy_on_close: bool = True
    ) -> Optional[str]:
        """
        Открыть диалог через DialogManager.

        Если on_result не указан, результат будет направлен в on_dialog_result.

        Returns:
            ID диалога или None
        """
        if self._dialog_manager is None:
            logger.warning(f"{self.__class__.__name__}: DialogManager not set, cannot open dialog")
            return None

        from .dialog_manager import DialogConfig, DialogResult as DR

        parent = self if isinstance(self, QWidget) else None

        callback = on_result
        if callback is None and isinstance(self, IDialogClient):
            callback = lambda r, d: self.on_dialog_result('', r, d)

        config = DialogConfig(
            dialog_class=dialog_class,
            parent=parent,
            modal=modal,
            singleton=singleton,
            title=title,
            size=size,
            data=data or {},
            on_result=callback,
            on_close=on_close,
            destroy_on_close=destroy_on_close
        )

        dialog_id = self._dialog_manager.open(config)
        return dialog_id

    def open_typed_dialog(
        self,
        dialog_type: str,
        modal: bool = True,
        data: Optional[Dict[str, Any]] = None,
        on_result: Optional[Callable[['DialogResult', Any], None]] = None
    ) -> Optional[str]:
        """Открыть диалог по зарегистрированному типу."""
        if self._dialog_manager is None:
            logger.warning(f"{self.__class__.__name__}: DialogManager not set")
            return None

        parent = self if isinstance(self, QWidget) else None

        callback = on_result
        if callback is None and isinstance(self, IDialogClient):
            callback = lambda r, d: self.on_dialog_result(dialog_type, r, d)

        return self._dialog_manager.open_typed(
            dialog_type=dialog_type,
            modal=modal,
            parent=parent,
            data=data,
            on_result=callback
        )

    def close_all_dialogs(self) -> int:
        """Закрыть все открытые диалоги."""
        if self._dialog_manager is None:
            return 0
        return self._dialog_manager.close_all()


IDialogClient.register(DialogClientMixin)


class StateObserverMixin(QObject):
    """
    Mixin для наблюдения за изменениями ViewState.

    Автоматически подключает on_state_changed как наблюдателя
    и уведомляет о пересечениях с EventBus.

    Пример:
        class MyPanel(StateObserverMixin):
            def initialize(self):
                self.view_state = container.resolve(ViewState)
                self.observe_state('selected_devices')

            def on_state_changed(self, state_id, event):
                if state_id == 'selected_devices':
                    self.update_list()
    """

    state_changed_signal = Signal(str, object)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._view_state: Optional[Any] = None
        self._observed_states: List[str] = []

    def _set_view_state(self, view_state: Any) -> None:
        """Установить ViewState для миксина."""
        self._view_state = view_state

    def observe_state(self, state_id: str, default_value: Optional[Dict[str, Any]] = None) -> None:
        """Начать наблюдение за состоянием."""
        if self._view_state is None:
            logger.warning(f"{self.__class__.__name__}: ViewState not set, cannot observe")
            return
        if state_id not in self._view_state._states:
            self._view_state.register(state_id, default_value=default_value)
        self._view_state.add_observer(state_id, self)
        self._observed_states.append(state_id)

    def stop_observing(self, state_id: str) -> None:
        """Прекратить наблюдение за состоянием."""
        if self._view_state is None:
            return
        self._view_state.remove_observer(state_id, self)
        if state_id in self._observed_states:
            self._observed_states.remove(state_id)

    def stop_all_observing(self) -> None:
        """Прекратить наблюдение за всеми состояниями."""
        if self._view_state is None:
            return
        for state_id in self._observed_states[:]:
            self._view_state.remove_observer(state_id, self)
        self._observed_states.clear()

    def get_state(self, state_id: str, key: Optional[str] = None, default: Any = None) -> Any:
        """Получить значение состояния."""
        if self._view_state is None:
            return default
        return self._view_state.get(state_id, key, default)

    def set_state(self, state_id: str, key: str, value: Any, source: Optional[str] = None) -> None:
        """Установить значение состояния."""
        if self._view_state is None:
            return
        self._view_state.set(state_id, key, value, source=source)

    def on_state_changed(self, state_id: str, event: 'StateChangeEvent') -> None:
        """Реализация по умолчанию — эмитирует Qt-сигнал."""
        self.state_changed_signal.emit(state_id, event)


IStateObserver.register(StateObserverMixin)


class WorkerClientMixin:
    """
    Mixin для централизованной обработки событий WorkerBridge.

    Автоматически регистрирует/отменяет воркеры и маршрутизирует
    их события в on_worker_* коллбэки.

    Пример:
        class MainWindow(WorkerClientMixin):
            def execute_command(self, devices, commands):
                worker = CommandWorker(devices, commands)
                self.register_threaded_worker(worker, 'cmd_001')

            def on_worker_finished(self, worker_id, result):
                self.update_result_table(worker_id, result)
    """

    def __init__(self):
        self._worker_bridge: Optional[Any] = None
        self._registered_workers: Dict[str, Any] = {}

    def _set_worker_bridge(self, worker_bridge: Any) -> None:
        """Установить WorkerBridge для миксина."""
        self._worker_bridge = worker_bridge

    def register_threaded_worker(
        self,
        worker: Any,
        worker_id: str,
        context: Optional[Dict[str, Any]] = None,
        auto_start: bool = True
    ) -> Optional[Any]:
        """Зарегистрировать воркер с выделенным потоком."""
        if self._worker_bridge is None:
            logger.warning(f"{self.__class__.__name__}: WorkerBridge not set, cannot register worker")
            return None

        client = self if isinstance(self, IWorkerClient) else None
        ctx = self._worker_bridge.register_threaded_worker(
            worker=worker,
            worker_id=worker_id,
            client=client,
            context=context,
            auto_start=auto_start
        )
        self._registered_workers[worker_id] = worker
        return ctx

    def register_worker(
        self,
        worker: Any,
        worker_id: str,
        context: Optional[Dict[str, Any]] = None
    ) -> Optional[Any]:
        """Зарегистрировать воркер без выделенного потока."""
        if self._worker_bridge is None:
            logger.warning(f"{self.__class__.__name__}: WorkerBridge not set, cannot register worker")
            return None

        client = self if isinstance(self, IWorkerClient) else None
        ctx = self._worker_bridge.register_worker(
            worker=worker,
            worker_id=worker_id,
            client=client,
            context=context
        )
        self._registered_workers[worker_id] = worker
        return ctx

    def abort_worker(self, worker_id: str) -> bool:
        """Прервать воркер."""
        if self._worker_bridge is None:
            return False
        result = self._worker_bridge.abort_worker(worker_id)
        if result and worker_id in self._registered_workers:
            del self._registered_workers[worker_id]
        return result

    def abort_all_workers(self) -> int:
        """Прервать все зарегистрированные воркеры."""
        if self._worker_bridge is None:
            return 0
        aborted = self._worker_bridge.abort_all()
        self._registered_workers.clear()
        return aborted

    def get_worker_context(self, worker_id: str) -> Optional[Any]:
        """Получить контекст воркера."""
        if self._worker_bridge is None:
            return None
        return self._worker_bridge.get_context(worker_id)

    def is_worker_running(self, worker_id: str) -> bool:
        """Проверить, выполняется ли воркер."""
        if self._worker_bridge is None:
            return False
        return self._worker_bridge.is_running(worker_id)

    def unregister_worker(self, worker_id: str) -> bool:
        """Отменить регистрацию воркера."""
        if self._worker_bridge is None:
            return False
        result = self._worker_bridge.unregister_worker(worker_id)
        if result and worker_id in self._registered_workers:
            del self._registered_workers[worker_id]
        return result


IWorkerClient.register(WorkerClientMixin)


class ComponentLifecycleMixin:
    """
    Mixin для управления жизненным циклом компонента.

    Предоставляет стандартный паттерн initialize()/cleanup()
    с отслеживанием состояния и логированием.

    Пример:
        class MyService(ComponentLifecycleMixin, IService):
            def initialize(self):
                self._load_data()
                self._setup_connections()
            def cleanup(self):
                self._save_data()
                self._disconnect()
    """

    def __init__(self):
        self._initialized = False
        self._cleaned_up = False

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def is_cleaned_up(self) -> bool:
        return self._cleaned_up

    def initialize(self) -> None:
        """Инициализировать компонент. Вызывается один раз."""
        if self._initialized:
            logger.warning(f"{self.__class__.__name__}: Already initialized")
            return
        self._initialized = True
        self._cleaned_up = False
        logger.debug(f"{self.__class__.__name__}: Initialized")

    def cleanup(self) -> None:
        """Очистить ресурсы компонента. Вызывается один раз."""
        if self._cleaned_up:
            logger.warning(f"{self.__class__.__name__}: Already cleaned up")
            return
        if not self._initialized:
            logger.warning(f"{self.__class__.__name__}: Cannot cleanup, not initialized")
            return
        self._cleaned_up = True
        logger.debug(f"{self.__class__.__name__}: Cleaned up")


IComponentLifecycle.register(ComponentLifecycleMixin)


# =============================================================================
# Конкретные классы-реализации
# =============================================================================

class EventFilter:
    """
    Фильтр событий на основе типа, источника или данных.

    Может использоваться с EventBus.subscribe() для отбора событий
    до передачи обработчику.

    Пример:
        device_filter = EventFilter(
            event_types={EventType.DEVICE_UPDATED},
            data_filter=lambda data: data.get('device_id') == 'dev_001'
        )
        bus.subscribe(EventType.DEVICE_UPDATED, handler, event_filter=device_filter)
    """

    def __init__(
        self,
        event_types: Optional[Set['EventType']] = None,
        sources: Optional[Set[str]] = None,
        data_filter: Optional[Callable[[Dict[str, Any]], bool]] = None
    ):
        self.event_types = event_types
        self.sources = sources
        self.data_filter = data_filter

    def matches(self, event: 'Event') -> bool:
        """Проверить, соответствует ли событие фильтру."""
        if self.event_types and event.event_type not in self.event_types:
            return False
        if self.sources and event.source not in self.sources:
            return False
        if self.data_filter and not self.data_filter(event.data):
            return False
        return True


class DialogFactory(ABC):
    """Абстрактная фабрика для создания диалогов."""

    @abstractmethod
    def create_dialog(self, parent: Optional[QWidget] = None, **kwargs) -> QDialog:
        pass


class CommandHistory:
    """
    История команд с поддержкой undo/redo.

    Работает с любыми объектами, реализующими интерфейс ICommand.

    Пример:
        history = CommandHistory(max_size=50)
        cmd = DeleteDeviceCommand(device_id, ...)
        history.execute(cmd)   # выполняет и добавляет в историю
        history.undo()         # отменяет
        history.redo()         # повторяет
    """

    def __init__(self, max_size: int = 50):
        self._undo_stack: List[ICommand] = []
        self._redo_stack: List[ICommand] = []
        self._max_size = max_size

    @property
    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    @property
    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0

    @property
    def undo_count(self) -> int:
        return len(self._undo_stack)

    @property
    def redo_count(self) -> int:
        return len(self._redo_stack)

    def execute(self, command: ICommand) -> None:
        """Выполнить команду и добавить в историю."""
        if not command.can_execute():
            logger.warning(f"CommandHistory: Cannot execute {command.description}")
            return
        command.execute()
        self._undo_stack.append(command)
        self._redo_stack.clear()
        if len(self._undo_stack) > self._max_size:
            self._undo_stack.pop(0)
        logger.debug(f"CommandHistory: Executed {command.description}")

    def undo(self) -> bool:
        """Отменить последнюю команду."""
        if not self.can_undo:
            return False
        command = self._undo_stack.pop()
        if not command.can_undo():
            self._undo_stack.append(command)
            logger.warning(f"CommandHistory: Cannot undo {command.description}")
            return False
        command.undo()
        self._redo_stack.append(command)
        logger.debug(f"CommandHistory: Undone {command.description}")
        return True

    def redo(self) -> bool:
        """Повторить последнюю отменённую команду."""
        if not self.can_redo:
            return False
        command = self._redo_stack.pop()
        command.redo()
        self._undo_stack.append(command)
        logger.debug(f"CommandHistory: Redone {command.description}")
        return True

    def clear(self) -> None:
        """Очистить историю."""
        self._undo_stack.clear()
        self._redo_stack.clear()

    def get_undo_descriptions(self) -> List[str]:
        """Получить описания команд, доступных для отмены."""
        return [cmd.description for cmd in reversed(self._undo_stack)]

    def get_redo_descriptions(self) -> List[str]:
        """Получить описания команд, доступных для повтора."""
        return [cmd.description for cmd in reversed(self._redo_stack)]


class WidgetDataBinding:
    """
    Двусторонняя привязка данных виджета к ViewState.

    Связывает Qt-виджет с ключом в состоянии. При изменении виджета
    обновляется состояние, при изменении состояния — обновляется виджет.

    Пример:
        binding = WidgetDataBinding(
            view_state=view_state,
            state_id='settings',
            key='hostname',
            widget=hostname_edit,
            widget_property='text'
        )
        binding.bind()
        # ... позже ...
        binding.unbind()
    """

    def __init__(
        self,
        view_state: 'ViewState',
        state_id: str,
        key: str,
        widget: QWidget,
        widget_property: Optional[str] = None,
        transform_to_state: Optional[Callable[[Any], Any]] = None,
        transform_to_widget: Optional[Callable[[Any], Any]] = None
    ):
        self._view_state = view_state
        self._state_id = state_id
        self._key = key
        self._widget = widget
        self._widget_property = widget_property or self._detect_property(widget)
        self._transform_to_state = transform_to_state
        self._transform_to_widget = transform_to_widget
        self._bound = False
        self._observer = None
        self._handler = None

    @staticmethod
    def _detect_property(widget: QWidget) -> Optional[str]:
        """Автоопределение свойства виджета."""
        from PySide6.QtWidgets import QLineEdit, QTextEdit, QSpinBox, QCheckBox, QComboBox, QSplitter
        property_map = {
            QLineEdit: 'text',
            QTextEdit: 'plainText',
            QSpinBox: 'value',
            QCheckBox: 'checked',
            QComboBox: 'currentIndex',
            QSplitter: 'sizes',
        }
        for wtype, prop in property_map.items():
            if isinstance(widget, wtype):
                return prop
        return None

    @staticmethod
    def _detect_signal(widget: QWidget, prop: str) -> Optional[Any]:
        """Автоопределение сигнала виджета."""
        from PySide6.QtWidgets import QLineEdit, QTextEdit, QSpinBox, QCheckBox, QComboBox, QSplitter
        signal_map = {
            QLineEdit: 'textChanged',
            QTextEdit: 'textChanged',
            QSpinBox: 'valueChanged',
            QCheckBox: 'stateChanged',
            QComboBox: 'currentIndexChanged',
            QSplitter: 'splitterMoved',
        }
        for wtype, signal_name in signal_map.items():
            if isinstance(widget, wtype):
                return getattr(widget, signal_name, None)
        return None

    def bind(self, source: Any = None, target: Any = None) -> None:
        """Установить привязку."""
        if self._bound:
            logger.warning(f"WidgetDataBinding: Already bound for {self._state_id}.{self._key}")
            return

        if not self._widget_property:
            logger.warning(f"WidgetDataBinding: Cannot detect property for {type(self._widget).__name__}")
            return

        if self._state_id not in self._view_state._states:
            self._view_state.register(self._state_id)

        self._connect_widget_signal()

        current_value = self._view_state.get(self._state_id, self._key)
        if current_value is not None:
            self._apply_to_widget(current_value)

        self._observer = _BindingStateObserver(self._state_id, self._key, self._widget,
                                                self._widget_property,
                                                self._transform_to_widget)
        self._view_state.add_observer(self._state_id, self._observer)
        self._bound = True

    def unbind(self) -> None:
        """Удалить привязку."""
        if not self._bound:
            return
        if self._observer and self._view_state:
            self._view_state.remove_observer(self._state_id, self._observer)
        if self._handler and self._widget:
            signal = self._detect_signal(self._widget, self._widget_property or '')
            if signal:
                try:
                    signal.disconnect(self._handler)
                except Exception:
                    pass
        self._bound = False

    def sync_to_target(self) -> None:
        """Синхронизировать состояние -> виджет."""
        value = self._view_state.get(self._state_id, self._key)
        if value is not None:
            self._apply_to_widget(value)

    def sync_to_source(self) -> None:
        """Синхронизировать виджет -> состояние."""
        value = self._read_from_widget()
        if value is not None:
            self._view_state.set(self._state_id, self._key, value, track_change=False, source='binding')

    def _connect_widget_signal(self) -> None:
        """Подключить сигнал виджета к обработчику."""
        if not self._widget_property:
            return
        signal = self._detect_signal(self._widget, self._widget_property)
        if signal and hasattr(signal, 'connect'):
            self._handler = self._create_handler()
            signal.connect(self._handler)

    def _create_handler(self) -> Callable:
        """Создать обработчик изменения виджета."""
        def handler(*args):
            if not self._bound:
                return
            value = self._read_from_widget_args(args)
            if value is not None:
                transformed = self._transform_to_state(value) if self._transform_to_state else value
                self._view_state.set(self._state_id, self._key, transformed,
                                     track_change=False, source='widget')
        return handler

    def _read_from_widget(self) -> Optional[Any]:
        """Прочитать текущее значение из виджета."""
        prop = self._widget_property
        if not prop:
            return None
        getters = {
            'text': 'text', 'plainText': 'toPlainText', 'value': 'value',
            'checked': 'isChecked', 'currentIndex': 'currentIndex', 'sizes': 'sizes'
        }
        getter_name = getters.get(prop)
        if getter_name and hasattr(self._widget, getter_name):
            return getattr(self._widget, getter_name)()
        return None

    def _read_from_widget_args(self, args: tuple) -> Optional[Any]:
        """Прочитать значение из аргументов сигнала или виджета."""
        if args:
            return args[0]
        return self._read_from_widget()

    def _apply_to_widget(self, value: Any) -> None:
        """Установить значение в виджет."""
        transformed = self._transform_to_widget(value) if self._transform_to_widget else value
        prop = self._widget_property
        if not prop:
            return
        setters = {
            'text': 'setText', 'plainText': 'setPlainText', 'value': 'setValue',
            'checked': 'setChecked', 'currentIndex': 'setCurrentIndex', 'sizes': 'setSizes'
        }
        setter_name = setters.get(prop)
        if setter_name and hasattr(self._widget, setter_name):
            try:
                getattr(self._widget, setter_name)(transformed)
            except Exception as e:
                logger.error(f"WidgetDataBinding: Error setting widget value: {e}")


class _BindingStateObserver:
    """Внутренний наблюдатель для WidgetDataBinding."""

    def __init__(self, state_id: str, key: str, widget: QWidget,
                 widget_property: str, transform_to_widget: Optional[Callable] = None):
        self._state_id = state_id
        self._key = key
        self._widget = widget
        self._widget_property = widget_property
        self._transform_to_widget = transform_to_widget

    def on_state_changed(self, state_id: str, event: Any) -> None:
        """Реагирует на изменение состояния, обновляя виджет."""
        if state_id != self._state_id:
            return
        if not hasattr(event, 'new_value') or event.new_value is None:
            return

        key = getattr(event, 'key', None)
        if self._key and key and key != self._key:
            return

        value = event.new_value
        if isinstance(value, dict) and self._key in value:
            value = value[self._key]

        transformed = self._transform_to_widget(value) if self._transform_to_widget else value

        setters = {
            'text': 'setText', 'plainText': 'setPlainText', 'value': 'setValue',
            'checked': 'setChecked', 'currentIndex': 'setCurrentIndex', 'sizes': 'setSizes'
        }
        setter_name = setters.get(self._widget_property)
        if setter_name and hasattr(self._widget, setter_name):
            try:
                getattr(self._widget, setter_name)(transformed)
            except Exception as e:
                logger.error(f"WidgetDataBinding: Error applying state to widget: {e}")


class PersistentStateManager:
    """
    Менеджер персистентного состояния на основе QSettings.

    Обеспечивает сохранение и восстановление состояния ViewState
    между сессиями приложения. Заменяет заглушку _load_persistent
    в ViewState.

    Пример:
        manager = PersistentStateManager()
        manager.bind(view_state)

        # Теперь view_state.register('main_window', persist=True)
        # будет загружать/сохранять данные автоматически.
    """

    def __init__(self, organization: str = "DNO", application: str = "DNOTool"):
        self._settings = QSettings(organization, application)
        self._view_state: Optional['ViewState'] = None
        self._bound = False

    def bind(self, view_state: 'ViewState') -> None:
        """Привязать к ViewState и установить хуки сохранения."""
        self._view_state = view_state
        view_state._load_persistent = self._load_persistent_impl
        view_state._save_persistent_impl = self._save_persistent_impl
        view_state.state_changed.connect(self._on_state_changed)
        self._bound = True
        logger.debug("PersistentStateManager: Bound to ViewState")

    def unbind(self) -> None:
        """Отвязать от ViewState."""
        if self._view_state and self._bound:
            self._view_state.state_changed.disconnect(self._on_state_changed)
        self._bound = False

    def save_all(self) -> None:
        """Сохранить все зарегистрированные состояния с persist=True."""
        if self._view_state is None:
            return
        for state_id, state_data in self._view_state._states.items():
            self._settings.setValue(f"ViewState/{state_id}", state_data)
        self._settings.sync()
        logger.debug("PersistentStateManager: All states saved")

    def load_state(self, state_id: str) -> Optional[Dict[str, Any]]:
        """Загрузить состояние из QSettings."""
        value = self._settings.value(f"ViewState/{state_id}")
        if value is not None:
            if isinstance(value, dict):
                return value
        return None

    def _load_persistent_impl(self, state_id: str) -> Optional[Dict[str, Any]]:
        """Загрузить персистентное состояние (используется как заменитель в ViewState)."""
        return self.load_state(state_id)

    def _save_persistent_impl(self, state_id: str, data: Dict[str, Any]) -> None:
        """Сохранить персистентное состояние."""
        self._settings.setValue(f"ViewState/{state_id}", data)
        self._settings.sync()
        logger.debug(f"PersistentStateManager: State '{state_id}' saved")

    def _on_state_changed(self, state_id: str, event: Any) -> None:
        """Автосохранение при изменении состояния."""
        self._settings.setValue(f"ViewState/{state_id}",
                                self._view_state._states.get(state_id, {}))
        self._settings.sync()


# =============================================================================
# Типы для аннотаций
# =============================================================================

HandlerId = str
DialogId = str
StateId = str
WorkerId = str