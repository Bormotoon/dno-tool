"""Undo/Redo видеопроекта с ограничением числа шагов и памяти снимков."""

from __future__ import annotations

import json
import zlib

from PySide6.QtGui import QUndoCommand, QUndoStack


class VideoEditSnapshot(dict):
    """Выделение хранится отдельно от ключей проекта и сериализуется только историей."""

    def __init__(self, project: dict, selection: list[str | None]) -> None:
        super().__init__(project)
        self.selection = selection


class SnapshotCommand(QUndoCommand):
    """Команда-снапшот: хранит состояние проекта до и после."""

    def __init__(
        self,
        editor: "VideoUndoMixin",
        before: dict,
        after: dict,
        text: str = "",
        refresh: bool = True,
    ) -> None:
        super().__init__(text)
        self._editor = editor
        self._before = self.pack(before)
        self._after = self.pack(after)
        self._initial_redo = True
        self._refresh = refresh
        self._replaying = False

    def undo(self) -> None:
        self._editor._restore_snapshot(self.unpack(self._before))

    def redo(self) -> None:
        if self._replaying:
            return
        if self._initial_redo:
            self._initial_redo = False
            self._editor._on_edit_committed(self._refresh)
            return
        self._editor._restore_snapshot(self.unpack(self._after))

    @staticmethod
    def pack(snapshot: dict) -> bytes:
        if isinstance(snapshot, VideoEditSnapshot):
            snapshot = dict(snapshot, _selection=snapshot.selection)
        return zlib.compress(
            json.dumps(
                snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8"),
            1,
        )

    @staticmethod
    def unpack(snapshot: bytes) -> dict:
        return json.loads(zlib.decompress(snapshot))

    @property
    def memory_bytes(self) -> int:
        return len(self._before) + len(self._after)


class VideoUndoStack(QUndoStack):
    """Хранит только последние команды, помещающиеся в бюджет снимков."""

    def __init__(self, parent: VideoUndoMixin, budget: int) -> None:
        super().__init__(parent)
        self.memory_budget = budget
        self.pruned_steps = 0

    @property
    def memory_bytes(self) -> int:
        return sum(self.command(index).memory_bytes for index in range(self.count()))

    def push(self, command: SnapshotCommand) -> None:
        # После новой правки ветка Redo всё равно исчезает. Сохраняем данные
        # до clear(): Qt удаляет C++-объекты старых команд вместе со стеком.
        commands = [self.command(index) for index in range(self.index())] + [command]
        total = sum(item.memory_bytes for item in commands)
        if total <= self.memory_budget:
            super().push(command)
            return
        removed = 0
        while commands and total > self.memory_budget:
            total -= commands.pop(0).memory_bytes
            removed += 1
        records = [
            (item._before, item._after, item.text(), item._refresh) for item in commands
        ]
        clean_index = self.cleanIndex() - removed
        self.pruned_steps += removed
        previous = self.blockSignals(True)
        try:
            self.clear()
            self.resetClean()
            if clean_index == 0:
                self.setClean()
            for index, (before, after, title, refresh) in enumerate(records, 1):
                item = SnapshotCommand(command._editor, {}, {}, title, refresh)
                item._before, item._after = before, after
                # Пересоздание стека не выполняет монтаж и не обновляет UI.
                item._initial_redo = False
                item._replaying = True
                super().push(item)
                item._replaying = False
                if index == clean_index:
                    self.setClean()
        finally:
            self.blockSignals(previous)
        # Все сохранённые команды уже применены в живой модели.
        command._editor._on_edit_committed(command._refresh)
        self.indexChanged.emit(self.index())
        self.canUndoChanged.emit(self.canUndo())
        self.canRedoChanged.emit(self.canRedo())
        self.undoTextChanged.emit(self.undoText())
        self.redoTextChanged.emit(self.redoText())
        self.cleanChanged.emit(self.isClean())


class VideoUndoMixin:
    """Миксин для диалога: снапшотный undo через QUndoStack."""

    UNDO_LIMIT = 100
    UNDO_MEMORY_BYTES = 32 * 1024 * 1024

    def _init_undo(self) -> None:
        self.undo_stack = VideoUndoStack(self, self.UNDO_MEMORY_BYTES)
        self.undo_stack.setUndoLimit(self.UNDO_LIMIT)

    def _take_snapshot(self) -> dict:
        return self.project.to_dict()

    def _restore_snapshot(self, snap: dict) -> None:
        from .models import Project

        self.project = Project.from_dict(snap)
        self._on_project_restored()

    def _on_project_restored(self) -> None:
        """Переопределяется в диалоге: перерисовать панели."""

    def _on_edit_committed(self, refresh: bool) -> None:
        """Первое redo фиксирует уже применённую правку без замены модели."""
        if refresh:
            self._on_project_restored()

    def push_undo(self, before: dict, text: str = "", *, refresh: bool = True) -> bool:
        after = self._take_snapshot()
        if before == after:
            return False
        self.undo_stack.push(SnapshotCommand(self, before, after, text, refresh))
        return True
