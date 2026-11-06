"""Строгая проверка необязательных режимов задания."""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


def background_enabled(step: Mapping[str, Any]) -> bool:
    """Проверить фоновый режим, допустимый только для SSH-шага."""
    value = step.get("background", False)
    if type(value) is not bool:
        raise ValueError("Поле background должно быть boolean")
    if value and step.get("type", "ssh") != "ssh":
        raise ValueError("Фоновый запуск доступен только для SSH-шагов")
    return value


def validate_background_steps(task: Mapping[str, Any]) -> None:
    """Проверить режимы всех шагов задания до выполнения."""
    for step in task.get("commands", []):
        if isinstance(step, Mapping):
            background_enabled(step)
            desktop_session_for_step(step)


def desktop_session_for_step(step: Mapping[str, Any], default: bool = False) -> bool:
    """Выбрать права исполнения SSH-шага с наследованием старого флага задачи."""
    if type(default) is not bool:
        raise ValueError("desktop_session задания должен быть boolean")
    if "desktop_session" in step and type(step["desktop_session"]) is not bool:
        raise ValueError("Поле desktop_session шага должно быть boolean")
    if step.get("type", "ssh") != "ssh":
        if step.get("desktop_session") is True:
            raise ValueError("Окружение рабочего стола доступно только для SSH-шагов")
        return False
    return step.get("desktop_session", default)


def desktop_session_enabled(task: Mapping[str, Any]) -> bool:
    """Прочитать флаг, не преобразуя строки и числа в boolean."""
    value = task.get("desktop_session", False)
    if type(value) is not bool:
        raise ValueError("Поле desktop_session должно быть boolean")
    return value


def move_desktop_session_to_steps(task: Mapping[str, Any]) -> dict[str, Any]:
    """Перенести старый флаг задачи в её SSH-шаги без изменения оригинала."""
    result = deepcopy(dict(task))
    if "desktop_session" in result:
        default = desktop_session_enabled(result)
        steps = []
        for step in result.get("commands", []):
            if isinstance(step, str):
                step = {"type": "ssh", "text": step}
            if isinstance(step, dict) and step.get("type", "ssh") == "ssh":
                step.setdefault("desktop_session", default)
            steps.append(step)
        result["commands"] = steps
        result.pop("desktop_session")
    return result


def validate_desktop_session_tree(items: list[Any]) -> None:
    """Проверить флаг во всём дереве, включая вложенные категории."""
    for item in items:
        if not isinstance(item, dict):
            continue
        if "commands" in item:
            desktop_session_enabled(item)
            validate_background_steps(item)
        elif len(item) == 1:
            children = next(iter(item.values()))
            if isinstance(children, list):
                validate_desktop_session_tree(children)
