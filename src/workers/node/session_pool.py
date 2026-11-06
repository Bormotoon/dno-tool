"""Ограниченный набор HTTP-сессий одной ноды. Без Qt.

Новая сессия на каждую операцию означает новое TCP- и TLS-соединение: миниатюры и
статусы платят за рукопожатие на каждый запрос. Сессия не потокобезопасна, поэтому
одновременно принадлежит одному потоку. Набор общий для потоков пула.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any


class NodeSessionPool:
    """LIFO-набор сессий: последняя использованная вероятнее всего ещё подключена."""

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        max_idle: int = 8,
        idle_ttl_sec: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = factory
        self._max_idle = max_idle
        self._idle_ttl = idle_ttl_sec
        self._clock = clock
        self._lock = threading.Lock()
        self._idle: list[tuple[Any, float]] = []
        self._closed = False

    def acquire(self) -> Any:
        """Выдать сессию в монопольное пользование вызывающему потоку."""
        with self._lock:
            expired = self._take_expired_locked()
            session = self._idle.pop()[0] if self._idle else None
        for stale in expired:
            stale.close()
        return session if session is not None else self._factory()

    def release(self, session: Any, *, broken: bool = False) -> None:
        """Вернуть сессию; сломанная или лишняя закрывается вне блокировки."""
        with self._lock:
            expired = self._take_expired_locked()
            keep = not broken and not self._closed and len(self._idle) < self._max_idle
            if keep:
                self._idle.append((session, self._clock()))
        for stale in expired:
            stale.close()
        if not keep:
            session.close()

    def _take_expired_locked(self) -> list[Any]:
        now = self._clock()
        expired = []
        while self._idle and now - self._idle[0][1] >= self._idle_ttl:
            expired.append(self._idle.pop(0)[0])
        return expired

    def close(self) -> None:
        """Закрыть простаивающие сессии; возвращённые позже закроются сразу."""
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for session, _released_at in idle:
            session.close()
