"""
View State - управление состоянием представлений.

Предоставляет централизованное управление состоянием UI компонентов:
- Сохранение и восстановление состояния
- Отслеживание изменений
- Связь с EventBus
- Поддержка undo/redo
"""

from enum import Enum, auto
from typing import Dict, List, Optional, Any, Callable, Set, Union
from dataclasses import dataclass, field, asdict
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QSplitter, QHeaderView, QTableWidget,
    QTreeWidget, QListWidget, QComboBox, QLineEdit,
    QSpinBox, QCheckBox, QTextEdit, QAbstractItemView
)
import json
import copy
import time
from src.logger import logger


class StateChangeType(Enum):
    """Тип изменения состояния."""
    VALUE_CHANGED = auto()
    SELECTION_CHANGED = auto()
    EXPANSION_CHANGED = auto()
    GEOMETRY_CHANGED = auto()
    CUSTOM = auto()


@dataclass
class StateChangeEvent:
    """Событие изменения состояния."""
    state_id: str
    change_type: StateChangeType
    old_value: Any = None
    new_value: Any = None
    source: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class ViewStateSnapshot:
    """Снимок состояния представления."""
    state_id: str
    data: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    description: Optional[str] = None


class StateObserver(QObject):
    """Базовый класс для наблюдателей за состоянием. Реализует IStateObserver."""

    state_changed = Signal(str, object)  # state_id, new_value

    def on_state_changed(self, state_id: str, event: StateChangeEvent) -> None:
        """Вызывается при изменении состояния."""
        pass


class ViewState(QObject):
    """
    Централизованное управление состоянием представлений.
    
    Позволяет сохранять, восстанавливать и отслеживать состояние
    UI компонентов с поддержкой истории изменений.
    """
    
    state_changed = Signal(str, StateChangeEvent)
    state_saved = Signal(str)  # state_id
    state_restored = Signal(str)  # state_id
    
    def __init__(self):
        super().__init__()
        
        # Хранилище состояний
        self._states: Dict[str, Dict[str, Any]] = {}
        self._observers: Dict[str, List[StateObserver]] = {}
        
        # История для undo/redo
        self._history: Dict[str, List[ViewStateSnapshot]] = {}
        self._history_index: Dict[str, int] = {}
        self._max_history_size = 50
        
        # Привязки к виджетам
        self._widget_bindings: Dict[str, QWidget] = {}
        self._binding_handlers: Dict[str, Callable] = {}

        # Менеджер персистентности (подменяется PersistentStateManager)
        self._pm_load: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None
        self._pm_save: Optional[Callable[[str, Dict[str, Any]], None]] = None
    
    def register(
        self,
        state_id: str,
        default_value: Optional[Dict[str, Any]] = None,
        persist: bool = False
    ) -> None:
        """
        Зарегистрировать состояние.
        
        Args:
            state_id: Уникальный идентификатор состояния
            default_value: Значение по умолчанию
            persist: Сохранять ли состояние между сессиями
        """
        if state_id not in self._states:
            self._states[state_id] = default_value or {}
            self._history[state_id] = []
            self._history_index[state_id] = -1
            
            if persist:
                # Загружаем сохраненное состояние
                saved = self._load_persistent(state_id)
                if saved is not None:
                    self._states[state_id] = saved
            
            logger.debug(f"ViewState: Registered state '{state_id}'")
    
    def get(self, state_id: str, key: Optional[str] = None, default: Any = None) -> Any:
        """
        Получить значение состояния.
        
        Args:
            state_id: ID состояния
            key: Ключ в состоянии (None = вернуть всё состояние)
            default: Значение по умолчанию
            
        Returns:
            Значение состояния или default
        """
        if state_id not in self._states:
            return default
        
        state = self._states[state_id]
        
        if key is None:
            return copy.deepcopy(state)
        
        return state.get(key, default)
    
    def set(
        self,
        state_id: str,
        key: str,
        value: Any,
        track_change: bool = True,
        source: Optional[str] = None
    ) -> None:
        """
        Установить значение состояния.
        
        Args:
            state_id: ID состояния
            key: Ключ
            value: Новое значение
            track_change: Отслеживать изменение для undo/redo
            source: Источник изменения
        """
        if state_id not in self._states:
            self.register(state_id)
        
        old_value = self._states[state_id].get(key)
        
        if old_value == value:
            return
        
        # Сохраняем в историю
        if track_change:
            self._push_to_history(state_id)
        
        # Устанавливаем новое значение
        self._states[state_id][key] = copy.deepcopy(value)
        
        # Создаем событие
        event = StateChangeEvent(
            state_id=state_id,
            change_type=StateChangeType.VALUE_CHANGED,
            old_value=old_value,
            new_value=value,
            source=source
        )
        
        # Уведомляем наблюдателей
        self._notify_observers(state_id, event)
        self.state_changed.emit(state_id, event)
        
        logger.debug(f"ViewState: {state_id}.{key} = {value}")
    
    def update(
        self,
        state_id: str,
        values: Dict[str, Any],
        track_change: bool = True,
        source: Optional[str] = None
    ) -> None:
        """
        Обновить несколько значений состояния.
        
        Args:
            state_id: ID состояния
            values: Словарь значений
            track_change: Отслеживать изменение
            source: Источник изменения
        """
        if state_id not in self._states:
            self.register(state_id)
        
        if track_change:
            self._push_to_history(state_id)
        
        old_state = copy.deepcopy(self._states[state_id])
        self._states[state_id].update(copy.deepcopy(values))
        
        event = StateChangeEvent(
            state_id=state_id,
            change_type=StateChangeType.VALUE_CHANGED,
            old_value=old_state,
            new_value=self._states[state_id],
            source=source
        )
        
        self._notify_observers(state_id, event)
        self.state_changed.emit(state_id, event)
    
    def bind_widget(
        self,
        state_id: str,
        widget: QWidget,
        widget_property: Optional[str] = None,
        state_key: Optional[str] = None
    ) -> None:
        """
        Привязать виджет к состоянию (двусторонняя связь).
        
        Args:
            state_id: ID состояния
            widget: Виджет для привязки
            widget_property: Свойство виджета (None = автоопределение)
            state_key: Ключ состояния (None = использовать objectName виджета)
        """
        if state_id not in self._states:
            self.register(state_id)
        
        binding_id = f"{state_id}_{id(widget)}"
        self._widget_bindings[binding_id] = widget
        
        key = state_key or widget.objectName() or f"widget_{id(widget)}"
        
        prop = widget_property or self._detect_widget_property(widget)
        
        if not prop:
            logger.warning(f"ViewState: Cannot detect property for {type(widget).__name__}")
            return
        
        handler = self._create_widget_handler(state_id, key, widget, prop)
        self._binding_handlers[binding_id] = handler
        
        self._connect_widget_signals(widget, handler)
        
        value = self.get(state_id, key)
        if value is not None:
            self._set_widget_value(widget, prop, value)
        
        widget.destroyed.connect(lambda obj=None, bid=binding_id: self._on_widget_destroyed(bid))
        
        logger.debug(f"ViewState: Bound {type(widget).__name__} to {state_id}.{key}")
    
    def unbind_widget(self, state_id: str, widget: QWidget) -> None:
        """Отвязать виджет от состояния."""
        binding_id = f"{state_id}_{id(widget)}"
        
        if binding_id in self._widget_bindings:
            del self._widget_bindings[binding_id]
        
        if binding_id in self._binding_handlers:
            del self._binding_handlers[binding_id]
    
    def add_observer(self, state_id: str, observer: StateObserver) -> None:
        """Добавить наблюдателя за состоянием."""
        if state_id not in self._observers:
            self._observers[state_id] = []
        self._observers[state_id].append(observer)
    
    def remove_observer(self, state_id: str, observer: StateObserver) -> None:
        """Удалить наблюдателя."""
        if state_id in self._observers:
            self._observers[state_id] = [o for o in self._observers[state_id] if o != observer]
    
    def can_undo(self, state_id: str) -> bool:
        """Проверить возможность отмены."""
        return state_id in self._history_index and self._history_index[state_id] > 0
    
    def can_redo(self, state_id: str) -> bool:
        """Проверить возможность повтора."""
        if state_id not in self._history_index:
            return False
        history = self._history.get(state_id, [])
        index = self._history_index[state_id]
        return index < len(history) - 1
    
    def undo(self, state_id: str) -> bool:
        """Отменить последнее изменение."""
        if not self.can_undo(state_id):
            return False
        
        self._history_index[state_id] -= 1
        snapshot = self._history[state_id][self._history_index[state_id]]
        
        self._states[state_id] = copy.deepcopy(snapshot.data)
        
        event = StateChangeEvent(
            state_id=state_id,
            change_type=StateChangeType.VALUE_CHANGED,
            new_value=self._states[state_id],
            source='undo'
        )
        
        self._notify_observers(state_id, event)
        self.state_changed.emit(state_id, event)
        
        logger.debug(f"ViewState: Undo {state_id}")
        return True
    
    def redo(self, state_id: str) -> bool:
        """Повторить отмененное изменение."""
        if not self.can_redo(state_id):
            return False
        
        self._history_index[state_id] += 1
        snapshot = self._history[state_id][self._history_index[state_id]]
        
        self._states[state_id] = copy.deepcopy(snapshot.data)
        
        event = StateChangeEvent(
            state_id=state_id,
            change_type=StateChangeType.VALUE_CHANGED,
            new_value=self._states[state_id],
            source='redo'
        )
        
        self._notify_observers(state_id, event)
        self.state_changed.emit(state_id, event)
        
        logger.debug(f"ViewState: Redo {state_id}")
        return True
    
    def clear_history(self, state_id: Optional[str] = None) -> None:
        """Очистить историю изменений."""
        if state_id:
            self._history[state_id] = []
            self._history_index[state_id] = -1
        else:
            self._history.clear()
            self._history_index.clear()
    
    def save_to_file(self, state_id: str, filepath: str) -> bool:
        """Сохранить состояние в файл."""
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(self._states.get(state_id, {}), f, indent=2, ensure_ascii=False)
            self.state_saved.emit(state_id)
            return True
        except Exception as e:
            logger.error(f"ViewState: Error saving {state_id}: {e}")
            return False
    
    def load_from_file(self, state_id: str, filepath: str) -> bool:
        """Загрузить состояние из файла."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._states[state_id] = data
            self.state_restored.emit(state_id)
            return True
        except Exception as e:
            logger.error(f"ViewState: Error loading {state_id}: {e}")
            return False
    
    def _push_to_history(self, state_id: str) -> None:
        if state_id not in self._history:
            self._history[state_id] = []
            self._history_index[state_id] = -1
        
        index = self._history_index[state_id]
        self._history[state_id] = self._history[state_id][:index + 1]
        
        snapshot = ViewStateSnapshot(
            state_id=state_id,
            data=copy.deepcopy(self._states[state_id])
        )
        self._history[state_id].append(snapshot)
        self._history_index[state_id] += 1
        
        if len(self._history[state_id]) > self._max_history_size:
            self._history[state_id].pop(0)
            self._history_index[state_id] -= 1
    
    def _notify_observers(self, state_id: str, event: StateChangeEvent) -> None:
        """Уведомить наблюдателей об изменении."""
        if state_id in self._observers:
            for observer in self._observers[state_id]:
                try:
                    observer.on_state_changed(state_id, event)
                except Exception as e:
                    logger.error(f"ViewState: Error notifying observer: {e}")
    
    def _detect_widget_property(self, widget: QWidget) -> Optional[str]:
        """Автоматически определить свойство виджета для привязки."""
        widget_type = type(widget)
        
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
    
    def _set_widget_value(self, widget: QWidget, prop: str, value: Any) -> None:
        """Установить значение свойства виджета."""
        try:
            if prop == 'text' and hasattr(widget, 'setText'):
                widget.setText(str(value))
            elif prop == 'plainText' and hasattr(widget, 'setPlainText'):
                widget.setPlainText(str(value))
            elif prop == 'value' and hasattr(widget, 'setValue'):
                widget.setValue(value)
            elif prop == 'checked' and hasattr(widget, 'setChecked'):
                widget.setChecked(bool(value))
            elif prop == 'currentIndex' and hasattr(widget, 'setCurrentIndex'):
                widget.setCurrentIndex(value)
            elif prop == 'sizes' and hasattr(widget, 'setSizes'):
                widget.setSizes(value)
        except Exception as e:
            logger.error(f"ViewState: Error setting widget value: {e}")
    
    def _create_widget_handler(
        self,
        state_id: str,
        key: str,
        widget: QWidget,
        prop: str
    ) -> Callable:
        """Создать обработчик изменений виджета."""
        def handler():
            try:
                value = None
                if prop == 'text' and hasattr(widget, 'text'):
                    value = widget.text()
                elif prop == 'plainText' and hasattr(widget, 'toPlainText'):
                    value = widget.toPlainText()
                elif prop == 'value' and hasattr(widget, 'value'):
                    value = widget.value()
                elif prop == 'checked' and hasattr(widget, 'isChecked'):
                    value = widget.isChecked()
                elif prop == 'currentIndex' and hasattr(widget, 'currentIndex'):
                    value = widget.currentIndex()
                elif prop == 'sizes' and hasattr(widget, 'sizes'):
                    value = widget.sizes()
                
                if value is not None:
                    self.set(state_id, key, value, track_change=False, source='widget')
            except Exception as e:
                logger.error(f"ViewState: Error in widget handler: {e}")
        
        return handler
    
    def _connect_widget_signals(self, widget: QWidget, handler: Callable) -> None:
        """Подключить сигналы виджета к обработчику."""
        widget_type = type(widget)
        
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
                signal = getattr(widget, signal_name, None)
                if signal:
                    signal.connect(handler)
                break
    
    def _on_widget_destroyed(self, binding_id: str) -> None:
        if binding_id in self._widget_bindings:
            del self._widget_bindings[binding_id]
        if binding_id in self._binding_handlers:
            del self._binding_handlers[binding_id]

    def bind_persistence(self, load_func: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
                         save_func: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> None:
        """Привязать менеджер персистентного состояния.

        Вызывается PersistentStateManager.bind(view_state) для подмены
        загрузки/сохранения состояния между сессиями.

        Args:
            load_func: Функция загрузки состояния (state_id -> dict | None)
            save_func: Функция сохранения состояния (state_id, data -> None)
        """
        self._pm_load = load_func
        self._pm_save = save_func

    def _load_persistent(self, state_id: str) -> Optional[Dict[str, Any]]:
        """Загрузить персистентное состояние.

        Использует PersistentDataManager если привязан через bind_persistence(),
        иначе возвращает None (состояние не сохраняется между сессиями).
        """
        if self._pm_load is not None:
            return self._pm_load(state_id)
        return None

    def _save_persistent(self, state_id: str, data: Dict[str, Any]) -> None:
        """Сохранить персистентное состояние."""
        if self._pm_save is not None:
            self._pm_save(state_id, data)



