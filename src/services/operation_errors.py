"""Адресная ошибка операции для сервисов-синглтонов с полем _last_error.

Параллельные операции перезаписывают общее поле сервиса, поэтому ошибка другого
хоста могла попасть в отчёт. Дескриптор сохраняет прежнее значение «последняя
ошибка в процессе» и дополнительно ведёт копию на поток выполнения.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_thread_errors = threading.local()


def _values() -> dict[tuple[int, str], str]:
    values = getattr(_thread_errors, "values", None)
    if values is None:
        values = _thread_errors.values = {}
    return values


class ScopedLastError:
    """Поле ошибки: общее значение для прежних читателей и копия текущего потока."""

    def __init__(self) -> None:
        self._name = ""
        self._slot = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self._name = name
        self._slot = f"_scoped{name}"

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self
        return instance.__dict__.get(self._slot, "")

    def __set__(self, instance: Any, value: str) -> None:
        instance.__dict__[self._slot] = value
        _values()[(id(instance), self._name)] = value


@dataclass(frozen=True)
class OperationOutcome:
    """Результат операции вместе с ошибкой именно этого вызова."""

    value: Any
    error: str = ""
    detail: str = ""

    def __bool__(self) -> bool:
        return bool(self.value)


def run_capturing_errors(service: object, call: Callable[[], Any]) -> OperationOutcome:
    """Выполнить операцию в текущем потоке и вернуть её результат и ошибку.

    Копия потока сбрасывается до вызова: поток пула переиспользуется, и ошибка
    предыдущей задачи не должна попасть в отчёт об успешной.
    """
    values = _values()
    key = id(service)
    values.pop((key, "_last_error"), None)
    values.pop((key, "_last_error_detail"), None)
    value = call()
    if value:
        return OperationOutcome(value)
    return OperationOutcome(
        value,
        values.get((key, "_last_error"), "") or "",
        values.get((key, "_last_error_detail"), "") or "",
    )
