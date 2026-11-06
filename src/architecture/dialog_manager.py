"""
Dialog Manager - централизованное управление диалогами.

Предоставляет механизм создания, отображения и управления жизненным циклом
диалоговых окон с поддержкой:
- Синглтон-диалогов (несколько экземпляров запрещено)
- Модальных и немодальных диалогов
- Передачи данных между диалогами
- Отслеживания состояния диалогов
- Интеграции с EventBus
"""

from enum import Enum, auto
from typing import Dict, List, Optional, Type, Any, Callable, Union, TYPE_CHECKING
from dataclasses import dataclass, field
from PySide6.QtWidgets import QDialog, QWidget, QApplication
from PySide6.QtCore import QObject, Signal, Slot, Qt
import weakref
from src.logger import logger

if TYPE_CHECKING:
    from .event_bus import EventBus, Event


class DialogResult(Enum):
    """Результат работы диалога."""
    ACCEPTED = auto()
    REJECTED = auto()
    CLOSED = auto()
    ERROR = auto()


@dataclass
class DialogConfig:
    """Конфигурация диалога."""
    dialog_class: Type[QDialog]
    singleton: bool = False  # Только один экземпляр
    modal: bool = True
    parent: Optional[QWidget] = None
    title: Optional[str] = None
    size: Optional[tuple] = None  # (width, height)
    data: Dict[str, Any] = field(default_factory=dict)
    on_result: Optional[Callable[[DialogResult, Any], None]] = None
    on_close: Optional[Callable[[], None]] = None
    destroy_on_close: bool = True


@dataclass
class DialogInfo:
    """Информация об открытом диалоге."""
    dialog_id: str
    dialog: QDialog
    config: DialogConfig
    result: Optional[DialogResult] = None
    data_out: Optional[Any] = None
    is_open: bool = True


class DialogManager(QObject):
    """
    Централизованный менеджер диалогов.
    
    Управляет созданием, отображением и жизненным циклом диалоговых окон.
    Интегрируется с EventBus для уведомлений о событиях диалогов.
    """
    
    # Сигналы
    dialog_opened = Signal(str, str)  # dialog_id, dialog_type
    dialog_closed = Signal(str, DialogResult)  # dialog_id, result
    dialog_result_ready = Signal(str, DialogResult, object)  # dialog_id, result, data
    
    def __init__(self, event_bus: Optional['EventBus'] = None):
        super().__init__()
        
        self._event_bus = event_bus
        self._dialogs: Dict[str, DialogInfo] = {}
        self._singleton_instances: Dict[Type[QDialog], str] = {}  # class -> dialog_id
        self._dialog_counter = 0
        self._factories: Dict[str, Callable[..., QDialog]] = {}
    
    def _generate_id(self, dialog_class: Type[QDialog]) -> str:
        """Генерация уникального ID для диалога."""
        self._dialog_counter += 1
        class_name = dialog_class.__name__
        return f"{class_name}_{self._dialog_counter}_{__import__('uuid').uuid4().hex[:4]}"
    
    def register_factory(self, dialog_type: str, factory: Callable[..., QDialog]) -> None:
        """
        Зарегистрировать фабричную функцию для создания диалога.
        
        Args:
            dialog_type: Тип диалога (строковый идентификатор)
            factory: Функция, создающая экземпляр диалога
        """
        self._factories[dialog_type] = factory
        logger.debug(f"DialogManager: Factory registered for {dialog_type}")
    
    def open(self, config: DialogConfig) -> str:
        """
        Открыть диалог с указанной конфигурацией.
        
        Args:
            config: Конфигурация диалога
            
        Returns:
            ID открытого диалога
            
        Raises:
            RuntimeError: Если синглтон-диалог уже открыт
        """
        dialog_class = config.dialog_class
        
        # Проверяем синглтон
        if config.singleton:
            if dialog_class in self._singleton_instances:
                existing_id = self._singleton_instances[dialog_class]
                logger.warning(f"DialogManager: Singleton {dialog_class.__name__} already open (id={existing_id})")
                # Поднимаем существующий диалог
                if existing_id in self._dialogs:
                    self._dialogs[existing_id].dialog.raise_()
                    self._dialogs[existing_id].dialog.activateWindow()
                return existing_id
        
        # Создаем диалог
        dialog_id = self._generate_id(dialog_class)
        
        try:
            # Создаем экземпляр диалога
            if config.data:
                dialog = dialog_class(config.parent, **config.data)
            else:
                dialog = dialog_class(config.parent)
            
            # Устанавливаем заголовок
            if config.title:
                dialog.setWindowTitle(config.title)
            
            # Устанавливаем размер
            if config.size:
                dialog.resize(*config.size)
            
            # Сохраняем информацию о диалоге
            dialog_info = DialogInfo(
                dialog_id=dialog_id,
                dialog=dialog,
                config=config
            )
            self._dialogs[dialog_id] = dialog_info
            
            if config.singleton:
                self._singleton_instances[dialog_class] = dialog_id
            
            # Подключаем сигналы
            dialog.finished.connect(lambda result: self._on_dialog_finished(dialog_id, result))
            
            # Публикуем событие
            self._publish_event('DIALOG_OPENED', {
                'dialog_id': dialog_id,
                'dialog_type': dialog_class.__name__,
                'singleton': config.singleton,
                'modal': config.modal
            })
            
            # Показываем диалог
            if config.modal:
                dialog.exec()
            else:
                dialog.show()
            
            self.dialog_opened.emit(dialog_id, dialog_class.__name__)
            logger.debug(f"DialogManager: Opened {dialog_class.__name__} (id={dialog_id})")
            
            return dialog_id
            
        except Exception as e:
            logger.error(f"DialogManager: Error opening {dialog_class.__name__}: {e}")
            raise RuntimeError(f"Failed to open dialog {dialog_class.__name__}: {e}")
    
    def open_typed(
        self,
        dialog_type: str,
        modal: bool = True,
        parent: Optional[QWidget] = None,
        data: Optional[Dict[str, Any]] = None,
        on_result: Optional[Callable[[DialogResult, Any], None]] = None
    ) -> Optional[str]:
        """
        Открыть диалог по зарегистрированному типу.
        
        Args:
            dialog_type: Тип диалога
            modal: Модальный режим
            parent: Родительское окно
            data: Данные для передачи в диалог
            on_result: Callback для результата
            
        Returns:
            ID диалога или None если фабрика не найдена
        """
        if dialog_type not in self._factories:
            logger.error(f"DialogManager: No factory registered for {dialog_type}")
            return None
        
        factory = self._factories[dialog_type]
        
        try:
            dialog = factory(parent=parent, **(data or {}))
            
            dialog_id = self._generate_id(type(dialog))
            
            try:
                if modal:
                    dialog.setWindowTitle(dialog.windowTitle())
                
                dialog_info = DialogInfo(
                    dialog_id=dialog_id,
                    dialog=dialog,
                    config=DialogConfig(
                        dialog_class=type(dialog),
                        modal=modal,
                        parent=parent,
                        data=data or {},
                        on_result=on_result,
                        singleton=False,
                        destroy_on_close=True
                    )
                )
                self._dialogs[dialog_id] = dialog_info
                
                dialog.finished.connect(lambda result, did=dialog_id: self._on_dialog_finished(did, result))
                
                self._publish_event('DIALOG_OPENED', {
                    'dialog_id': dialog_id,
                    'dialog_type': type(dialog).__name__,
                    'singleton': False,
                    'modal': modal
                })
                
                if modal:
                    dialog.exec()
                else:
                    dialog.show()
                
                self.dialog_opened.emit(dialog_id, type(dialog).__name__)
                logger.debug(f"DialogManager: Opened {type(dialog).__name__} (id={dialog_id}) via factory")
                
                return dialog_id
                
            except Exception as e:
                logger.error(f"DialogManager: Error setting up {dialog_type}: {e}")
                try:
                    dialog.deleteLater()
                except Exception:
                    pass
                return None
            
        except Exception as e:
            logger.error(f"DialogManager: Error creating {dialog_type}: {e}")
            return None
    
    def close(self, dialog_id: str, result: DialogResult = DialogResult.CLOSED) -> bool:
        """
        Закрыть диалог по ID.
        
        Args:
            dialog_id: ID диалога
            result: Результат закрытия
            
        Returns:
            True если диалог найден и закрыт
        """
        if dialog_id not in self._dialogs:
            logger.warning(f"DialogManager: Dialog {dialog_id} not found")
            return False
        
        dialog_info = self._dialogs[dialog_id]
        
        try:
            dialog_info.dialog.done(
                QDialog.Accepted if result == DialogResult.ACCEPTED else QDialog.Rejected
            )
            return True
        except Exception as e:
            logger.error(f"DialogManager: Error closing dialog {dialog_id}: {e}")
            return False
    
    def close_all(self, dialog_type: Optional[Type[QDialog]] = None) -> int:
        """
        Закрыть все диалоги или диалоги определенного типа.
        
        Args:
            dialog_type: Тип диалога для закрытия (None = все)
            
        Returns:
            Количество закрытых диалогов
        """
        closed = 0
        to_close = []
        
        for dialog_id, dialog_info in self._dialogs.items():
            if dialog_type is None or isinstance(dialog_info.dialog, dialog_type):
                to_close.append(dialog_id)
        
        for dialog_id in to_close:
            if self.close(dialog_id):
                closed += 1
        
        return closed
    
    def get_dialog(self, dialog_id: str) -> Optional[QDialog]:
        """Получить экземпляр диалога по ID."""
        if dialog_id in self._dialogs:
            return self._dialogs[dialog_id].dialog
        return None
    
    def get_dialog_info(self, dialog_id: str) -> Optional[DialogInfo]:
        """Получить информацию о диалоге."""
        return self._dialogs.get(dialog_id)
    
    def is_open(self, dialog_id: str) -> bool:
        """Проверить, открыт ли диалог."""
        return dialog_id in self._dialogs and self._dialogs[dialog_id].is_open
    
    def is_singleton_open(self, dialog_class: Type[QDialog]) -> bool:
        """Проверить, открыт ли синглтон-диалог данного типа."""
        return dialog_class in self._singleton_instances
    
    def get_open_dialogs(self, dialog_type: Optional[Type[QDialog]] = None) -> List[str]:
        """
        Получить список ID открытых диалогов.
        
        Args:
            dialog_type: Фильтр по типу (None = все)
            
        Returns:
            Список ID диалогов
        """
        return [
            dialog_id for dialog_id, info in self._dialogs.items()
            if info.is_open and (dialog_type is None or isinstance(info.dialog, dialog_type))
        ]
    
    def bring_to_front(self, dialog_id: str) -> bool:
        """Поднять диалог на передний план."""
        dialog = self.get_dialog(dialog_id)
        if dialog:
            dialog.raise_()
            dialog.activateWindow()
            return True
        return False
    
    def _on_dialog_finished(self, dialog_id: str, qt_result: int) -> None:
        """Обработчик завершения диалога."""
        if dialog_id not in self._dialogs:
            return
        
        dialog_info = self._dialogs[dialog_id]
        dialog_info.is_open = False
        
        # Определяем результат
        if qt_result == QDialog.Accepted:
            result = DialogResult.ACCEPTED
        elif qt_result == QDialog.Rejected:
            result = DialogResult.REJECTED
        else:
            result = DialogResult.CLOSED
        
        dialog_info.result = result
        
        try:
            if hasattr(dialog_info.dialog, 'get_data') and callable(dialog_info.dialog.get_data):
                dialog_info.data_out = dialog_info.dialog.get_data()
        except Exception as e:
            logger.error(f"DialogManager: Error getting data from dialog {dialog_id}: {e}")
        
        if dialog_info.config.on_result:
            try:
                dialog_info.config.on_result(result, dialog_info.data_out)
            except Exception as e:
                logger.error(f"DialogManager: Error in on_result callback: {e}")
        
        if dialog_info.config.on_close:
            try:
                dialog_info.config.on_close()
            except Exception as e:
                logger.error(f"DialogManager: Error in on_close callback: {e}")
        
        # Публикуем событие
        self._publish_event('DIALOG_CLOSED', {
            'dialog_id': dialog_id,
            'dialog_type': type(dialog_info.dialog).__name__,
            'result': result.name,
            'data': dialog_info.data_out
        })
        
        # Эмитируем сигналы
        self.dialog_closed.emit(dialog_id, result)
        self.dialog_result_ready.emit(dialog_id, result, dialog_info.data_out)
        
        # Очищаем
        if dialog_info.config.destroy_on_close:
            self._cleanup_dialog(dialog_id)
        
        logger.debug(f"DialogManager: Dialog {dialog_id} finished with {result.name}")
    
    def _cleanup_dialog(self, dialog_id: str) -> None:
        """Очистка ресурсов диалога."""
        if dialog_id not in self._dialogs:
            return
        
        dialog_info = self._dialogs[dialog_id]
        
        # Удаляем из синглтонов
        dialog_class = type(dialog_info.dialog)
        if dialog_class in self._singleton_instances:
            if self._singleton_instances[dialog_class] == dialog_id:
                del self._singleton_instances[dialog_class]
        
        # Удаляем диалог
        try:
            dialog_info.dialog.deleteLater()
        except Exception as e:
            logger.error(f"DialogManager: Error deleting dialog {dialog_id}: {e}")
        
        del self._dialogs[dialog_id]
    
    def _publish_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Публикация события в EventBus."""
        if self._event_bus:
            try:
                from .event_bus import EventType
                et = EventType.DIALOG_OPENED if event_type == 'DIALOG_OPENED' else EventType.DIALOG_CLOSED
                self._event_bus.publish_typed(
                    event_type=et,
                    source='DialogManager',
                    data=data
                )
            except Exception as e:
                logger.error(f"DialogManager: Error publishing event: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику менеджера."""
        return {
            'open_dialogs': len([d for d in self._dialogs.values() if d.is_open]),
            'total_dialogs': len(self._dialogs),
            'singletons': len(self._singleton_instances),
            'registered_factories': len(self._factories)
        }



