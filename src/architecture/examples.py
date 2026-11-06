"""
Examples - примеры использования архитектурных компонентов.

Демонстрирует интеграцию EventBus, DialogManager, ViewState и WorkerBridge
с существующими компонентами pyktool через интерфейсы и mixins.
"""

from typing import Dict, Any, Optional, List
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QDialog, QPushButton,
    QVBoxLayout, QLabel, QMessageBox, QLineEdit
)
from PySide6.QtCore import QObject, Signal

# Импорты архитектурных компонентов
from src.architecture import (
    EventBus, EventType, Event,
    DialogManager, DialogConfig, DialogResult,
    ViewState, StateChangeEvent,
    WorkerBridge,
    IEventHandler, IDialogClient, IStateObserver, IWorkerClient,
    ICommand, IService, IComponentLifecycle,
    EventHandlerMixin, DialogClientMixin, StateObserverMixin,
    WorkerClientMixin, ComponentLifecycleMixin,
    EventFilter, CommandHistory, WidgetDataBinding, PersistentStateManager
)

# Импорты существующих классов pyktool
from src.domain.models.device import DeviceModel
from src.workers import CommandWorker, Command
from src.ui.dialogs.common.common import DeviceEditDialog, ConfigDialog
from src.ui.dialogs.command.result import CommandResultDialog


# =============================================================================
# Пример 1: EventHandlerMixin — автоматическая подписка/отписка
# =============================================================================

class DeviceTracker(EventHandlerMixin):
    """
    Компонент, отслеживающий события устройств.
    
    EventHandlerMixin автоматически управляет подписками:
    - subscribe() сохраняет handler_id для авто-отписки
    - unsubscribe_all_tracked() отписывает все обработчики
    - publish() — шорткат для публикации событий
    """

    def __init__(self, event_bus: EventBus):
        super().__init__()
        self._set_event_bus(event_bus)
        self._tracked_devices: List[str] = []

        # Подписка — ID сохраняется автоматически
        self.subscribe(EventType.DEVICE_ADDED, self._on_device_added)
        self.subscribe(EventType.DEVICE_REMOVED, self._on_device_removed)
        self.subscribe(EventType.DEVICE_UPDATED, self._on_device_updated)

    def _on_device_added(self, event: Event):
        device_id = event.get('device_id', 'unknown')
        self._tracked_devices.append(device_id)
        print(f"[DeviceTracker] Device added: {device_id}")

    def _on_device_removed(self, event: Event):
        device_id = event.get('device_id', 'unknown')
        if device_id in self._tracked_devices:
            self._tracked_devices.remove(device_id)
        print(f"[DeviceTracker] Device removed: {device_id}")

    def _on_device_updated(self, event: Event):
        device_id = event.get('device_id', 'unknown')
        print(f"[DeviceTracker] Device updated: {device_id}")

    def cleanup(self):
        """Автоматическая отписка от всех событий."""
        self.unsubscribe_all_tracked()


# =============================================================================
# Пример 2: DialogClientMixin — централизованная обработка диалогов
# =============================================================================

class MainWindowWithDialogs(QMainWindow, IDialogClient, DialogClientMixin):
    """
    MainWindow с использованием DialogClientMixin.
    
    Предоставляет:
    - open_dialog() — шорткат для открытия через DialogManager
    - open_typed_dialog() — шорткат для открытия по фабричному типу
    - close_all_dialogs() — закрытие всех диалогов
    - on_dialog_result() — централизованная обработка результатов
    """

    def __init__(self):
        super().__init__()
        QMainWindow.__init__(self)
        DialogClientMixin.__init__(self)
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("MainWindow with DialogClientMixin")
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        btn_config = QPushButton("Open Config (Singleton)")
        btn_config.clicked.connect(self._open_config_dialog)
        layout.addWidget(btn_config)

    def initialize(self):
        """Вызывается после инициализации DI-контейнера."""
        from src.di import get_container
        container = get_container()
        self._set_dialog_manager(container.resolve(DialogManager))

    def _open_config_dialog(self):
        """Открыть диалог настроек через DialogClientMixin."""
        self.open_dialog(
            dialog_class=ConfigDialog,
            modal=True,
            singleton=True,
            title="Application Settings"
        )

    def on_dialog_result(self, dialog_id: str, result: DialogResult, data: Any) -> None:
        """Централизованная обработка результатов диалогов."""
        if result == DialogResult.ACCEPTED:
            print(f"Dialog {dialog_id} accepted")
        else:
            print(f"Dialog {dialog_id} rejected")


# =============================================================================
# Пример 3: StateObserverMixin — наблюдение за состоянием
# =============================================================================

class DeviceListPanel(QWidget, IStateObserver, StateObserverMixin):
    """
    Панель списка устройств с привязкой к ViewState.
    
    StateObserverMixin предоставляет:
    - observe_state() — подписка на изменения состояния
    - get_state() / set_state() — шорткаты для работы с ViewState
    - stop_all_observing() — авто-отписка
    """

    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        StateObserverMixin.__init__(self, parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        self.label = QLabel("No devices selected")
        layout.addWidget(self.label)

    def initialize(self):
        """Вызывается после инициализации DI-контейнера."""
        from src.di import get_container
        container = get_container()
        self._set_view_state(container.resolve(ViewState))

        # Начать наблюдение за состоянием
        self.observe_state('selected_devices', default_value={'devices': []})

    def on_state_changed(self, state_id: str, event: StateChangeEvent) -> None:
        """Вызывается при изменении наблюдаемого состояния."""
        if state_id == 'selected_devices':
            devices = self.get_state('selected_devices', 'devices', [])
            self.label.setText(f"Selected: {len(devices)} devices")


# =============================================================================
# Пример 4: WorkerClientMixin — централизованная обработка воркеров
# =============================================================================

class MainWindowWithWorkers(QMainWindow, IWorkerClient, WorkerClientMixin):
    # Без этого атрибута WorkerBridge не пересылает прогресс клиенту.
    wants_worker_progress = True

    """
    MainWindow с использованием WorkerClientMixin.
    
    WorkerClientMixin предоставляет:
    - register_threaded_worker() — регистрация с авто-управлением потоком
    - register_worker() — регистрация без выделенного потока
    - abort_worker() / abort_all_workers() — прерывание
    - is_worker_running() — проверка состояния
    """

    def __init__(self):
        super().__init__()
        QMainWindow.__init__(self)
        WorkerClientMixin.__init__(self)
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("MainWindow with WorkerClientMixin")
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        btn_execute = QPushButton("Execute Command")
        btn_execute.clicked.connect(self._execute_command)
        layout.addWidget(btn_execute)

        btn_abort = QPushButton("Abort All")
        btn_abort.clicked.connect(lambda: self.abort_all_workers())
        layout.addWidget(btn_abort)

        self.status_label = QLabel("Ready")
        layout.addWidget(self.status_label)

    def initialize(self):
        """Вызывается после инициализации DI-контейнера."""
        from src.di import get_container
        container = get_container()
        self._set_worker_bridge(container.resolve(WorkerBridge))

    def _execute_command(self):
        devices = [DeviceModel({"name": "Device 1", "host": "192.168.1.1"})]
        commands = [Command({"type": "ssh", "text": "uptime"})]
        worker = CommandWorker(devices, commands)
        self.register_threaded_worker(worker, 'cmd_001')
        self.status_label.setText("Command started...")

    # Реализация IWorkerClient

    def on_worker_started(self, worker_id: str, context: Dict[str, Any]) -> None:
        self.status_label.setText(f"Worker {worker_id} started")

    def on_worker_progress(self, worker_id: str, progress: Any) -> None:
        print(f"Worker {worker_id} progress: {progress}")

    def on_worker_finished(self, worker_id: str, result: Any) -> None:
        self.status_label.setText(f"Worker {worker_id} finished")

    def on_worker_error(self, worker_id: str, error: str) -> None:
        QMessageBox.critical(self, "Worker Error", f"Worker {worker_id}: {error}")


# =============================================================================
# Пример 5: ComponentLifecycleMixin — управление жизненным циклом
# =============================================================================

class MyService(ComponentLifecycleMixin, IService):
    """
    Сервис с управлением жизненным циклом.
    
    ComponentLifecycleMixin предоставляет:
    - initialize() — однократная инициализация с отслеживанием
    - cleanup() — однократная очистка с отслеживанием
    - is_initialized / is_cleaned_up — свойства состояния
    """

    def __init__(self):
        super().__init__()
        self._data = None

    def start(self) -> None:
        if not self.is_initialized:
            self.initialize()
        self._data = self._load_data()

    def stop(self) -> None:
        if self.is_initialized and not self.is_cleaned_up:
            self._save_data()
            self.cleanup()

    def get_name(self) -> str:
        return 'MyService'

    def _load_data(self):
        return {}

    def _save_data(self):
        pass


# =============================================================================
# Пример 6: CommandHistory — undo/redo
# =============================================================================

class AddDeviceCommand(ICommand):
    """Команда добавления устройства с поддержкой undo."""

    def __init__(self, device_data: dict, event_bus: EventBus = None):
        self.device_data = device_data
        self.device_id = device_data.get('iid', 'unknown')
        self.event_bus = event_bus
        self._added = False

    @property
    def description(self) -> str:
        return f"Add device {self.device_data.get('name', 'unknown')}"

    def execute(self) -> None:
        # Здесь была бы реальная логика добавления
        self._added = True
        if self.event_bus:
            self.event_bus.publish_typed(
                EventType.DEVICE_ADDED,
                source='AddDeviceCommand',
                data={'device_id': self.device_id}
            )

    def undo(self) -> None:
        # Здесь была бы реальная логика удаления
        self._added = False
        if self.event_bus:
            self.event_bus.publish_typed(
                EventType.DEVICE_REMOVED,
                source='AddDeviceCommand',
                data={'device_id': self.device_id}
            )

    def redo(self) -> None:
        self.execute()


def demo_command_history():
    """Демонстрация CommandHistory с undo/redo."""
    history = CommandHistory(max_size=20)

    cmd1 = AddDeviceCommand({'name': 'Device 1', 'host': '192.168.1.1', 'iid': 'dev1'})
    cmd2 = AddDeviceCommand({'name': 'Device 2', 'host': '192.168.1.2', 'iid': 'dev2'})

    history.execute(cmd1)
    history.execute(cmd2)

    print(f"Can undo: {history.can_undo}")  # True
    print(f"Undo descriptions: {history.get_undo_descriptions()}")

    history.undo()  # Отменить cmd2
    print(f"Can redo: {history.can_redo}")  # True

    history.redo()  # Повторить cmd2


# =============================================================================
# Пример 7: WidgetDataBinding — двусторонняя привязка
# =============================================================================

class SettingsWindow(QWidget):
    """
    Окно настроек с двусторонней привязкой данных.
    
    WidgetDataBinding автоматически синхронизирует:
    - виджет -> состояние (при изменении пользователем)
    - состояние -> виджет (при программном изменении)
    """

    def __init__(self, view_state: ViewState):
        super().__init__()
        self.view_state = view_state
        self._bindings: List[WidgetDataBinding] = []
        self._setup_ui()
        self._setup_bindings()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self.hostname_edit = QLineEdit()
        self.hostname_edit.setObjectName("hostname")
        layout.addWidget(QLabel("Hostname:"))
        layout.addWidget(self.hostname_edit)

    def _setup_bindings(self):
        # Регистрируем состояние
        self.view_state.register('settings', default_value={'hostname': ''})

        # Создаём привязку
        binding = WidgetDataBinding(
            view_state=self.view_state,
            state_id='settings',
            key='hostname',
            widget=self.hostname_edit,
            widget_property='text'
        )
        binding.bind()
        self._bindings.append(binding)

    def cleanup(self):
        """Отвязать все привязки при закрытии."""
        for binding in self._bindings:
            binding.unbind()
        self._bindings.clear()


# =============================================================================
# Пример 8: PersistentStateManager — сохранение состояния между сессиями
# =============================================================================

def setup_persistence(view_state: ViewState):
    """
    Подключение PersistentStateManager к ViewState.
    
    После этого view_state.register('main_window', persist=True)
    будет загружать/сохранять данные через QSettings.
    """
    manager = PersistentStateManager(organization="DNO", application="DNOTool")
    manager.bind(view_state)
    return manager


# =============================================================================
# Пример 9: EventFilter — сложная фильтрация событий
# =============================================================================

class DeviceCommandFilter:
    """
    Комбинированный фильтр: события команд для конкретного устройства.
    """

    def __init__(self, device_id: str):
        self.device_id = device_id
        self._filter = EventFilter(
            event_types={EventType.COMMAND_STARTED, EventType.COMMAND_FINISHED,
                         EventType.COMMAND_PROGRESS},
            data_filter=lambda data: data.get('device_id') == self.device_id
        )

    def matches(self, event: Event) -> bool:
        return self._filter.matches(event)

    @property
    def filter(self) -> EventFilter:
        return self._filter


# =============================================================================
# Пример 10: Полная интеграция всех компонентов
# =============================================================================

class FullyIntegratedMainWindow(
    QMainWindow,
    IEventHandler, IDialogClient, IStateObserver, IWorkerClient,
    EventHandlerMixin, DialogClientMixin, StateObserverMixin,
    WorkerClientMixin, ComponentLifecycleMixin
):
    """
    Полностью интегрированное MainWindow со всеми mixins.
    
    Сочетает все архитектурные компоненты через стандартные интерфейсы
    и mixin-классы.
    """

    def __init__(self):
        QMainWindow.__init__(self)
        EventHandlerMixin.__init__(self)
        DialogClientMixin.__init__(self)
        StateObserverMixin.__init__(self)
        WorkerClientMixin.__init__(self)
        ComponentLifecycleMixin.__init__(self)

        self._setup_ui()
        self._command_history = CommandHistory(max_size=50)

    def _setup_ui(self):
        self.setWindowTitle("Fully Integrated MainWindow")
        self.resize(1024, 768)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        label = QLabel("This window uses all architecture components")
        layout.addWidget(label)

    def initialize(self):
        """Вызывается после инициализации DI-контейнера."""
        from src.di import get_container
        container = get_container()

        # Подключаем компоненты через setters
        self._set_event_bus(container.resolve(EventBus))
        self._set_dialog_manager(container.resolve(DialogManager))
        self._set_view_state(container.resolve(ViewState))
        self._set_worker_bridge(container.resolve(WorkerBridge))

        # Подключаем PersistentStateManager к ViewState
        self._persistence_manager = PersistentStateManager()
        self._persistence_manager.bind(self._view_state)

        # Подписываемся на события
        self.subscribe(EventType.DEVICE_SELECTED, self._on_device_selected_event)
        self.subscribe(EventType.COMMAND_FINISHED, self._on_command_finished_event)

        # Наблюдаем за состояниями
        self.observe_state('main_window', default_value={})
        self.observe_state('selected_devices', default_value={'devices': []})

        # Регистрируем фабрики диалогов
        self._register_dialog_factories()

        # Отмечаем инициализацию
        ComponentLifecycleMixin.initialize(self)

    def _register_dialog_factories(self):
        self._dialog_manager.register_factory(
            'device_edit',
            lambda parent=None, **kwargs: DeviceEditDialog(kwargs.get('device'), parent)
        )

    # Реализация интерфейсов

    def handle_event(self, event: Event) -> None:
        pass

    def on_dialog_result(self, dialog_id: str, result: DialogResult, data: Any) -> None:
        self.publish(EventType.DIALOG_RESULT, source='MainWindow',
                     data={'dialog_id': dialog_id, 'result': result.name})

    def on_state_changed(self, state_id: str, event: StateChangeEvent) -> None:
        self.publish(EventType.UI_STATE_CHANGED, source='MainWindow',
                     data={'state_id': state_id, 'change': event.change_type.name})

    def on_worker_started(self, worker_id: str, context: Dict[str, Any]) -> None:
        self.set_state('main_window', f'last_worker_{worker_id}', 'started')

    def on_worker_progress(self, worker_id: str, progress: Any) -> None:
        pass

    def on_worker_finished(self, worker_id: str, result: Any) -> None:
        self.set_state('main_window', f'last_result_{worker_id}', result)

    def on_worker_error(self, worker_id: str, error: str) -> None:
        self.publish(EventType.WORKER_ERROR, source='MainWindow',
                     data={'worker_id': worker_id, 'error': error})

    # Обработчики событий

    def _on_device_selected_event(self, event: Event):
        device = event.get('device')
        if device:
            devices = self.get_state('selected_devices', 'devices', default=[])
            devices.append(device.to_dict() if hasattr(device, 'to_dict') else device)
            self.set_state('selected_devices', 'devices', devices)

    def _on_command_finished_event(self, event: Event):
        pass

    def closeEvent(self, event):
        """Очистка при закрытии — все mixins отписываются автоматически."""
        self.unsubscribe_all_tracked()
        self.stop_all_observing()
        self.abort_all_workers()
        self.close_all_dialogs()
        if hasattr(self, '_persistence_manager'):
            self._persistence_manager.save_all()
        super().closeEvent(event)


# =============================================================================
# Запуск демонстрации
# =============================================================================

if __name__ == "__main__":
    import sys
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)

    # Демонстрация CommandHistory (не требует Qt event loop)
    print("=== CommandHistory Demo ===")
    demo_command_history()

    # Создаём окно
    window = FullyIntegratedMainWindow()
    window.show()

    sys.exit(app.exec())