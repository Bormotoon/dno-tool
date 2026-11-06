"""Проверка формата задач и дерева commands.json.

Единая точка для загрузки, импорта, редактора и запуска. Ошибка - ValueError
с понятным пользователю текстом, путь к задаче указывается в начале.

Формат:
- категория: словарь из одного ключа со списком дочерних элементов;
- задача: словарь с ``name`` и ``commands``;
- шаг: словарь ``{"type": "ssh"|"sftp"|"local", "text": str, ...}``
  или устаревшая строка (тип определяется по префиксу).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from src.domain.task_options import (
    background_enabled,
    desktop_session_enabled,
    desktop_session_for_step,
)

STEP_TYPES = ("ssh", "sftp", "local")


def is_task(item: Any) -> bool:
    """Задача - словарь с полями name и commands."""
    return isinstance(item, Mapping) and "name" in item and "commands" in item


def is_category(item: Any) -> bool:
    """Категория - словарь из одного ключа со списком."""
    return (
        isinstance(item, Mapping)
        and len(item) == 1
        and isinstance(next(iter(item.values())), list)
    )


def validate_step(step: Any) -> None:
    """Проверить один шаг задачи."""
    if isinstance(step, str):
        return
    if not isinstance(step, Mapping):
        raise ValueError("шаг должен быть объектом с полями type и text")
    step_type = step.get("type", "ssh")
    if step_type not in STEP_TYPES:
        raise ValueError(
            f"неизвестный тип шага «{step_type}», допустимы: {', '.join(STEP_TYPES)}"
        )
    if not isinstance(step.get("text", ""), str):
        raise ValueError("текст шага должен быть строкой")
    background_enabled(step)
    desktop_session_for_step(step)


def validate_task(task: Any) -> None:
    """Проверить задачу целиком, включая все шаги."""
    if not isinstance(task, Mapping):
        raise ValueError("задача должна быть объектом")
    name = task.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("у задачи нет имени")
    commands = task.get("commands")
    if not isinstance(commands, list):
        raise ValueError("поле commands должно быть списком")
    desktop_session_enabled(task)
    timeout = task.get("timeout", 0)
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError("таймаут должен быть неотрицательным числом секунд")
    params = task.get("params", [])
    if params is None:
        params = []
    if not isinstance(params, list) or not all(isinstance(p, str) for p in params):
        raise ValueError("поле params должно быть списком строк")
    for field in ("description", "iid"):
        if field in task and task[field] is not None and not isinstance(task[field], str):
            raise ValueError(f"поле {field} должно быть строкой")
    for number, step in enumerate(commands, 1):
        try:
            validate_step(step)
        except ValueError as error:
            raise ValueError(f"шаг {number}: {error}") from None


def task_error(task: Any) -> str:
    """Текст ошибки задачи или пустая строка."""
    try:
        validate_task(task)
    except ValueError as error:
        return str(error)
    return ""


def validate_command_tree(items: Any, path: Sequence[str] = ()) -> None:
    """Проверить дерево категорий и задач; ошибка указывает путь к элементу."""
    if not isinstance(items, list):
        raise ValueError("список задач должен быть массивом")
    for item in items:
        if is_task(item):
            try:
                validate_task(item)
            except ValueError as error:
                where = "/".join([*path, str(item.get("name") or "без имени")])
                raise ValueError(f"Задача «{where}»: {error}") from None
        elif is_category(item):
            name, children = next(iter(item.items()))
            validate_command_tree(children, (*path, str(name)))
        else:
            where = "/".join(path) or "корень"
            raise ValueError(
                f"Категория «{where}»: элемент не является ни задачей, ни категорией"
            )
