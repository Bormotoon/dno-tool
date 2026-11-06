"""Базовый класс для всех диалоговых окон приложения с унифицированной структурой инициализации."""

"""
Base Dialog - Базовый класс для всех диалогов.

Предоставляет единую структуру инициализации и обработки результатов
для всех диалоговых окон приложения.
"""

from abc import abstractmethod
from typing import Optional, Dict, Any, Tuple
from PySide6.QtWidgets import QDialog, QWidget, QVBoxLayout, QDialogButtonBox
from PySide6.QtCore import Qt
from src.logger import logger


def enable_size_grip(dialog: QDialog) -> None:
    """Включить уголок растяжения, если окно реально растягивается.

    Фиксированные окна (minimumSize == maximumSize) пропускаются -
    им грип не нужен.
    """
    if dialog.minimumSize() == dialog.maximumSize():
        return
    dialog.setSizeGripEnabled(True)


class BaseDialog(QDialog):
    """
    Базовый класс для всех диалогов приложения.

    Предоставляет:
    - Единую структуру инициализации (setup_ui, load_data, connect_signals)
    - Стандартные кнопки (OK/Cancel)
    - Метод для получения результатов
    - Логирование жизненного цикла диалога
    - Автоматическое применение macOS-style

    Пример использования:
        class ConfigDialog(BaseDialog):
            def _setup_ui(self):
                # Создание виджетов
                self.username_edit = QLineEdit()
                self.layout.addWidget(self.username_edit)

            def _load_data(self, **kwargs):
                # Загрузка начальных данных
                self.username_edit.setText(kwargs.get('username', ''))

            def _connect_signals(self):
                # Подключение сигналов
                self.username_edit.textChanged.connect(self._on_username_changed)

            def get_data(self) -> Dict[str, Any]:
                # Возврат результатов
                return {'username': self.username_edit.text()}
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        title: Optional[str] = None,
        size: Optional[Tuple[int, int]] = None,
        modal: bool = True,
        **kwargs,
    ):
        """
        Инициализация диалога.

        Args:
            parent: Родительское окно
            title: Заголовок окна
            size: Размер окна (width, height)
            modal: Модальный режим
            **kwargs: Дополнительные данные для загрузки
        """
        super().__init__(parent)
        enable_size_grip(self)

        # Настройка окна
        if title:
            self.setWindowTitle(title)
        if size:
            self.resize(*size)
        if modal:
            self.setWindowModality(Qt.WindowModality.WindowModal)

        # Создаем основной layout
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(10, 10, 10, 10)
        self.layout.setSpacing(10)

        # Логирование
        logger.debug(f"BaseDialog: Creating {self.__class__.__name__}")

        # Инициализация в правильном порядке
        self._setup_ui()
        self._load_data(**kwargs)
        self._connect_signals()

        # Добавляем кнопки
        self._setup_buttons()

        logger.debug(f"BaseDialog: {self.__class__.__name__} initialized")

    @abstractmethod
    def _setup_ui(self) -> None:
        """
        Создать виджеты диалога.

        Должен быть реализован в подклассе.
        Виджеты должны добавляться в self.layout.
        """
        pass

    def _load_data(self, **kwargs) -> None:
        """
        Загрузить начальные данные в виджеты.

        Args:
            **kwargs: Данные для загрузки

        Может быть переопределен в подклассе.
        """
        pass

    def _connect_signals(self) -> None:
        """
        Подключить сигналы виджетов.

        Может быть переопределен в подклассе.
        """
        pass

    def _setup_buttons(self) -> None:
        """
        Создать кнопки диалога.

        По умолчанию создает OK и Cancel кнопки.
        Может быть переопределен для кастомизации.
        """
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.layout.addWidget(self.button_box)

    def get_data(self) -> Optional[Dict[str, Any]]:
        """
        Получить данные из диалога.

        Returns:
            Словарь с данными или None

        Может быть переопределен в подклассе.
        """
        return None

    def validate(self) -> Tuple[bool, str]:
        """
        Валидировать данные диалога.

        Returns:
            (is_valid, error_message)

        Может быть переопределен в подклассе.
        """
        return True, ""

    def accept(self) -> None:
        """
        Обработка нажатия OK.

        Выполняет валидацию перед закрытием.
        """
        is_valid, error_msg = self.validate()

        if not is_valid:
            logger.warning(f"BaseDialog: Validation failed - {error_msg}")
            # Показываем сообщение об ошибке
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(self, "Ошибка валидации", error_msg)
            return

        logger.debug(f"BaseDialog: {self.__class__.__name__} accepted")
        super().accept()

    def reject(self) -> None:
        """Обработка нажатия Cancel."""
        logger.debug(f"BaseDialog: {self.__class__.__name__} rejected")
        super().reject()

    def closeEvent(self, event) -> None:
        """Обработка закрытия диалога."""
        logger.debug(f"BaseDialog: {self.__class__.__name__} closing")
        super().closeEvent(event)
