"""Ползунок числа потоков: единый вид и шаги для всех настроек потоков."""

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QSlider, QWidget

from src.config.settings import THREAD_COUNT_STEPS


class ThreadCountSlider(QWidget):
    """Выбор числа потоков из степеней двойки; каждая настройка хранит своё значение.

    Значение вне списка из старых настроек сохраняется точно, пока пользователь не
    сдвинет ползунок: открытие окна и «ОК» настройку не меняют.
    """

    valueChanged = Signal(int)

    def __init__(
        self,
        value: int = THREAD_COUNT_STEPS[0],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._value = value
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, len(THREAD_COUNT_STEPS) - 1)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(1)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider.setMinimumWidth(100)
        self.label = QLabel(self)
        self.label.setMinimumWidth(
            max(28, self.fontMetrics().horizontalAdvance(str(THREAD_COUNT_STEPS[-1])) + 4)
        )
        self.label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.label)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusProxy(self.slider)
        self.slider.valueChanged.connect(self._slider_moved)
        self._sync()

    def _sync(self) -> None:
        nearest = min(
            range(len(THREAD_COUNT_STEPS)),
            key=lambda index: abs(THREAD_COUNT_STEPS[index] - self._value),
        )
        with QSignalBlocker(self.slider):
            self.slider.setValue(nearest)
        self.label.setNum(self._value)

    def _slider_moved(self, position: int) -> None:
        self.setValue(THREAD_COUNT_STEPS[position])

    def value(self) -> int:
        """Вернуть выбранное число потоков."""
        return self._value

    def setValue(self, value: int) -> None:
        """Показать значение точно; шаг выбирает только движение ползунка."""
        changed = value != self._value
        self._value = value
        self._sync()
        if changed:
            self.valueChanged.emit(value)
