"""Реестр источников для монитора пулов, не являющихся исполнителями Python.

Каждый источник предоставляет label и get_snapshot() в формате ApplicationPool.
Модуль не зависит от Qt и от пакета src.workers.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from typing import Any, Protocol

import psutil


class SnapshotSource(Protocol):
    @property
    def label(self) -> str: ...

    def get_snapshot(self) -> dict[str, Any]: ...


_lock = threading.Lock()
_sources: list[SnapshotSource] = []


def register_pool(source: SnapshotSource) -> None:
    with _lock:
        if source not in _sources:
            _sources.append(source)


def unregister_pool(source: SnapshotSource) -> None:
    with _lock:
        if source in _sources:
            _sources.remove(source)


def get_registered_pools() -> tuple[SnapshotSource, ...]:
    with _lock:
        return tuple(_sources)


class QtPoolSnapshot:
    """Показать QThreadPool в мониторе: очередь и задачи Qt не раскрывает."""

    def __init__(self, pool: Any, label: str) -> None:
        self._pool = pool
        self.label = label

    def get_snapshot(self) -> dict[str, Any]:
        return {
            "stats": {
                "label": self.label,
                "max_workers": self._pool.maxThreadCount(),
                "active": self._pool.activeThreadCount(),
                "pending": 0,
                "submitted": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "total_tasks": 0,
            },
            "tasks": [],
        }


def process_thread_summary() -> tuple[int, str]:
    """Вернуть число потоков ОС и разбивку потоков Python по префиксу имени."""
    prefixes = Counter(
        re.sub(r"[-_]?\d+.*$", "", thread.name) or thread.name
        for thread in threading.enumerate()
    )
    lines = [f"{name}: {count}" for name, count in sorted(prefixes.items())]
    return psutil.Process().num_threads(), "\n".join(lines)
