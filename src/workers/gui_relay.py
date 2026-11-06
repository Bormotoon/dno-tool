"""Доставка результатов фоновых потоков в GUI через объект со временем жизни процесса.

Сигнал нельзя отправлять из чужого потока на QObject, который GUI может удалить:
гонка с деструктором роняет процесс, а ``except RuntimeError`` ловит только уже
удалённый объект. Поэтому отправитель здесь один и никогда не удаляется, а получатель
проверяется в GUI-потоке, где он и удаляется.

QThread и воркер, перенесённый в свой поток, отправлять собственные сигналы могут: на
время работы потока они живы. Правило касается колбэков пулов, Future и обычных потоков.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, Slot
from shiboken6 import isValid

from src.logger import logger

_Call = tuple[QObject, Callable[..., Any], tuple[Any, ...], Callable[[], Any] | None]


class _GuiRelay(QObject):
    """Выполнить вызов в GUI-потоке, если его получатель ещё существует."""

    _deliver = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._deliver.connect(self._run, Qt.ConnectionType.QueuedConnection)
        application = QCoreApplication.instance()
        if application is not None and self.thread() is not application.thread():
            self.moveToThread(application.thread())

    def post(self, call: _Call) -> None:
        self._deliver.emit(call)

    @Slot(object)
    def _run(self, call: _Call) -> None:
        target, callback, args, on_dropped = call
        try:
            if isValid(target):
                callback(*args)
            elif on_dropped is not None:
                on_dropped()
        except Exception:  # noqa: BLE001 - сбой одного получателя не должен мешать остальным
            logger.exception("Ошибка обработки результата фоновой операции")


_lock = threading.Lock()
_relay: _GuiRelay | None = None


def _get_relay() -> _GuiRelay:
    """Вернуть ретранслятор процесса; он не удаляется до выхода."""
    global _relay
    with _lock:
        if _relay is None:
            _relay = _GuiRelay()
        return _relay


def post_to_gui(
    target: QObject,
    callback: Callable[..., Any],
    *args: Any,
    on_dropped: Callable[[], Any] | None = None,
) -> None:
    """Вызвать ``callback(*args)`` в GUI-потоке, если ``target`` к этому моменту жив.

    Безопасно из любого потока. ``callback`` нужно получить заранее в GUI-потоке или
    передать обычной функцией: чужой поток не должен обращаться к объекту Qt.

    ``on_dropped`` вызывается в GUI-потоке вместо ``callback``, если получателя уже нет:
    он нужен, когда результат держит ресурс, который иначе некому вернуть.
    """
    try:
        _get_relay().post((target, callback, args, on_dropped))
    except RuntimeError:
        pass  # Процесс завершается: доставлять результат некому.
