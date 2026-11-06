"""Ограниченное накопление вывода команд и прореживание живого прогресса.

Без Qt и без собственных потоков. Экземпляр принадлежит одному потоку.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable

# Начало накопленного текста сдвигается к переводу строки, если он близко:
# так снижается риск показать разорванную ANSI-последовательность.
_REALIGN_WINDOW_CHARS = 4096


def _output_marker(count: int) -> str:
    return f"\n[… пропущено {count} символов вывода …]\n"


def _progress_marker(count: int) -> str:
    return (
        f"\n[… пропущено {count} символов живого вывода, "
        "полный результат будет показан по завершении …]\n"
    )


class BoundedOutput:
    """Накопитель вывода: сохраняет начало и конец, середину отбрасывает."""

    def __init__(self, head_chars: int, tail_chars: int) -> None:
        self._head: list[str] = []
        self._head_free = max(0, head_chars)
        self._tail: deque[str] = deque()
        self._tail_chars = 0
        self._tail_limit = max(0, tail_chars)
        self._dropped = 0

    def append(self, text: str) -> None:
        if not text:
            return
        if self._head_free > 0:
            piece = text[: self._head_free]
            self._head.append(piece)
            self._head_free -= len(piece)
            if len(piece) == len(text):
                return
            text = text[len(piece) :]
        self._tail.append(text)
        self._tail_chars += len(text)
        self._evict()

    def _evict(self) -> None:
        excess = self._tail_chars - self._tail_limit
        while excess > 0:
            first = self._tail[0]
            if len(first) <= excess:
                self._tail.popleft()
                removed = len(first)
            else:
                self._tail[0] = first[excess:]
                removed = excess
            self._tail_chars -= removed
            self._dropped += removed
            excess -= removed

    def text(self) -> str:
        head = "".join(self._head)
        tail = "".join(self._tail)
        if self._dropped:
            return head + _output_marker(self._dropped) + tail
        return head + tail

    @property
    def dropped_chars(self) -> int:
        return self._dropped

    def __len__(self) -> int:
        return sum(map(len, self._head)) + self._tail_chars

    def __bool__(self) -> bool:
        return bool(self._head or self._tail)


class ProgressCoalescer:
    """Ограничить частоту и объём живого вывода одного хоста."""

    def __init__(
        self,
        emit: Callable[[str], None],
        interval: float,
        max_chars: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._emit = emit
        self._interval = interval
        self._max_chars = max(1, max_chars)
        self._clock = clock
        self._pending: deque[str] = deque()
        self._pending_chars = 0
        self._dropped = 0
        self._last_emit = -math.inf

    def push(self, text: str) -> None:
        """Накопить фрагмент; отправить, если подошёл срок."""
        if not text:
            return
        self._pending.append(text)
        self._pending_chars += len(text)
        excess = self._pending_chars - self._max_chars
        while excess > 0:
            first = self._pending[0]
            if len(first) <= excess:
                self._pending.popleft()
                removed = len(first)
            else:
                self._pending[0] = first[excess:]
                removed = excess
            self._pending_chars -= removed
            self._dropped += removed
            excess -= removed
        self.flush_if_due()

    def flush_if_due(self) -> None:
        """Вызывается на каждой итерации цикла чтения."""
        if self._pending and self._clock() - self._last_emit >= self._interval:
            self.flush()

    def flush(self) -> None:
        """Отправить всё накопленное немедленно."""
        if not self._pending:
            return
        text = "".join(self._pending)
        dropped = self._dropped
        self._pending.clear()
        self._pending_chars = 0
        self._dropped = 0
        self._last_emit = self._clock()
        if dropped:
            newline = text.find("\n", 0, _REALIGN_WINDOW_CHARS)
            if newline >= 0:
                dropped += newline + 1
                text = text[newline + 1 :]
            text = _progress_marker(dropped) + text
        self._emit(text)
