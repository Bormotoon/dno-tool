"""Базовый класс для всех фоновых workers приложения."""

import threading
from PySide6.QtCore import QObject, Signal, QMetaObject, Qt


class BaseWorker(QObject):
    """Абстрактный базовый класс фонового исполнителя. Предоставляет сигналы finished, error, started и механизм прерывания через threading.Event."""

    finished = Signal()
    error = Signal(str)
    started = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._abort_event = threading.Event()

    def execute(self) -> None:
        """Запустить выполнение задачи. Должен быть переопределён в подклассе."""
        raise NotImplementedError

    def abort(self) -> None:
        """Установить флаг прерывания."""
        self._abort_event.set()

    @property
    def is_aborting(self) -> bool:
        """Проверить, запрошено ли прерывание."""
        return self._abort_event.is_set()
