"""Своя команда: разовый SSH-скрипт без записи в commands.json.

Модуль без Qt. ``CustomCommand`` описывает то, что пользователь ввёл в окне,
и собирает из этого обычную задачу для ``MainWindow.execute_command``:
дальше она идёт тем же конвейером, что и задачи из файла (локальные хосты,
ноды, таблица результатов, отмена).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

CUSTOM_TASK_NAME = "Своя команда"
MIN_TIMEOUT_SEC = 1
MAX_TIMEOUT_SEC = 7 * 24 * 3600

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def env_name_error(name: str) -> str:
    """Текст ошибки имени переменной окружения или пустая строка."""
    if not name:
        return "у переменной окружения нет имени"
    if not _ENV_NAME_RE.match(name):
        return (
            f"недопустимое имя переменной «{name}»: только латинские буквы, "
            "цифры и «_», первой не может быть цифра"
        )
    return ""


def format_duration(seconds: int) -> str:
    """Короткая запись длительности: «90 с» -> «1 мин 30 с»."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [
        f"{value} {unit}"
        for value, unit in ((days, "д"), (hours, "ч"), (minutes, "мин"), (secs, "с"))
        if value
    ]
    return " ".join(parts[:2]) or "0 с"


@dataclass(frozen=True)
class CustomCommand:
    """Скрипт и параметры разового запуска по SSH."""

    script: str = ""
    desktop_session: bool = False
    background: bool = False
    timeout: int = 300
    env: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def errors(self) -> list[str]:
        """Причины, по которым запуск невозможен; пустой список - можно."""
        problems: list[str] = []
        if not self.script.strip():
            problems.append("введите текст команды")
        if not MIN_TIMEOUT_SEC <= self.timeout <= MAX_TIMEOUT_SEC:
            problems.append(
                f"таймаут должен быть от {MIN_TIMEOUT_SEC} с до "
                f"{format_duration(MAX_TIMEOUT_SEC)}"
            )
        seen: set[str] = set()
        for name, _value in self.env:
            error = env_name_error(name)
            if error:
                problems.append(error)
            elif name in seen:
                problems.append(f"переменная «{name}» указана дважды")
            seen.add(name)
        return problems

    def script_text(self) -> str:
        """Текст шага: экспорт переменных, затем скрипт.

        Строка ``export`` первой заставляет авто-sudo обернуть весь текст
        в ``sudo -- bash -c``, а не только первую команду.
        """
        if not self.env:
            return self.script
        exports = "\n".join(
            f"export {name}={shlex.quote(value)}" for name, value in self.env
        )
        return f"{exports}\n{self.script}"

    def build_task(self) -> dict[str, Any]:
        """Задача в формате commands.json; перед вызовом проверить ``errors``."""
        return {
            "name": CUSTOM_TASK_NAME,
            "description": "Разовая команда из окна «Своя команда»",
            "commands": [
                {
                    "type": "ssh",
                    "text": self.script_text(),
                    "desktop_session": self.desktop_session,
                    "background": self.background,
                }
            ],
            "params": [],
            "timeout": self.timeout,
        }
