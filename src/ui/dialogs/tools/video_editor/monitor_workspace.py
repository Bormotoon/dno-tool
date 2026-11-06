"""Компоновка просмотра исходника и результата монтажа."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QEvent, QObject, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from src.ui.icons import set_themed_icon

from .preview_widget import PreviewWidget, _mk_tool_btn
from .source_monitor import SourceMonitor


class MonitorWorkspace(QWidget):
    """Переключает компоновку двух независимых плееров без их пересоздания."""

    MODES = {
        "dual": "Два просмотра рядом",
        "source": "Только исходник",
        "program": "Только монтаж",
    }
    mode_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = "dual"
        self._sizes: dict[str, list[int]] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.source = SourceMonitor(self)
        self.program_column = QWidget(self)
        program_layout = QVBoxLayout(self.program_column)
        program_layout.setContentsMargins(0, 0, 0, 0)
        program_layout.addWidget(QLabel("Монтаж — результат таймлайна", self))
        self.program = PreviewWidget(self)
        program_layout.addWidget(self.program, 1)
        self.splitter.addWidget(self.source)
        self.splitter.addWidget(self.program_column)
        self.splitter.setSizes([500, 500])
        layout.addWidget(self.splitter)
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.timeout.connect(self._sync_controls)
        self.source.controls.installEventFilter(self)
        self.program.controls.installEventFilter(self)
        self._sync_controls()
        self.mode_buttons = QWidget(self)
        row = QHBoxLayout(self.mode_buttons)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.buttons: dict[str, QToolButton] = {}
        for key, title in self.MODES.items():
            button = _mk_tool_btn(f"ve_view_{key}", title, self.mode_buttons)
            set_themed_icon(button, f"ve_view_{key}", toggle=True)
            button.setCheckable(True)
            button.setChecked(key == self.mode)
            self.mode_group.addButton(button)
            button.clicked.connect(lambda checked=False, mode=key: self.set_mode(mode))
            row.addWidget(button)
            self.buttons[key] = button
        self.source.playback_started.connect(self.program.pause)
        self.program.player.playbackStateChanged.connect(self._program_playback)

    def _program_playback(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if (
            self.program.player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
            and not self.program._priming
            and not self.source._priming
        ):
            self.source.pause()

    def _sync_controls(self) -> None:
        source_layout = self.source.controls.layout()
        program_layout = self.program.controls.layout()
        assert source_layout is not None and program_layout is not None
        height = max(
            source_layout.sizeHint().height(),
            program_layout.sizeHint().height(),
        )
        for controls in (self.source.controls, self.program.controls):
            if controls.minimumHeight() != height:
                controls.setMinimumHeight(height)
        width = max(
            self.source.minimumSizeHint().width(),
            self.program_column.minimumSizeHint().width(),
        )
        self.source.setMinimumWidth(width)
        self.program_column.setMinimumWidth(width)
        self.splitter.setMinimumWidth(
            width * (2 if self.mode == "dual" else 1)
            + (self.splitter.handleWidth() if self.mode == "dual" else 0)
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            self._sync_timer.start(0)
        return super().eventFilter(watched, event)

    def set_mode(self, mode: str) -> None:
        if not isinstance(mode, str) or mode not in self.MODES:
            mode = "dual"
        if mode != self.mode and self.mode == "dual":
            sizes = self.splitter.sizes()
            if all(sizes):
                self._sizes[self.mode] = sizes
        self.mode = mode
        self._sync_controls()
        if mode == "program":
            self.source.pause()
        elif mode == "source":
            self.program.pause()
        self.source.setVisible(mode != "program")
        self.program_column.setVisible(mode != "source")
        if mode == "dual":
            sizes = self._sizes.get(mode, [500, 500])
            available = max(1, self.splitter.width() - self.splitter.handleWidth())
            # Сначала масштабируем доли: иначе Qt зажимает исходные числа
            # минимумами панелей ещё до распределения доступной ширины.
            self.splitter.setSizes(
                [round(value * available / sum(sizes)) for value in sizes]
            )
        self.buttons[mode].setChecked(True)
        self.mode_changed.emit(mode)

    def layout_sizes(self) -> dict[str, list[int]]:
        if self.mode == "dual":
            self._sizes[self.mode] = self.splitter.sizes()
        return {key: list(value) for key, value in self._sizes.items()}

    def restore_sizes(self, values: object) -> None:
        if not isinstance(values, dict):
            return
        for mode in ("dual",):
            sizes = values.get(mode)
            if (
                isinstance(sizes, list)
                and len(sizes) == 2
                and all(type(value) is int and value > 0 for value in sizes)
            ):
                self._sizes[mode] = list(sizes)
