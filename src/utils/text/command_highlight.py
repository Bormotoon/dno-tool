"""Подсветка текста команд без Qt, HTML и исполнения содержимого."""

from __future__ import annotations

import platform
import re
from typing import Any

from pygments.lexers.shell import (  # type: ignore[import-untyped]
    BashLexer,
    BatchLexer,
    PowerShellLexer,
)
from pygments.token import (  # type: ignore[import-untyped]
    Comment,
    Keyword,
    Name,
    Number,
    Operator,
    String,
)

_PARAMETER = re.compile(r"%\w+%")


def _role(token: Any) -> str:
    """Использовать те же смысловые цвета, что в редакторе Qt."""
    for family, role in (
        (Comment, "comment"),
        (Keyword, "keyword"),
        (Name.Variable, "variable"),
        (Name.Builtin, "command"),
        (Name.Function, "command"),
        (String, "string"),
        (Number, "number"),
        (Operator, "operator"),
    ):
        if token in family:
            return role
    return "plain"


def highlight_command(text: str, kind: str = "ssh") -> dict[str, Any]:
    """Вернуть фрагменты с исходными пробелами и переводами строк."""
    language = "bash"
    lexer: BashLexer | BatchLexer | PowerShellLexer = BashLexer()
    if kind == "local":
        if re.search(
            r"(?im)^\s*(?:powershell|pwsh|\$env:|\$ErrorActionPreference|Get-|Set-|Write-)",
            text,
        ):
            language, lexer = "powershell", PowerShellLexer()
        elif platform.system() == "Windows":
            language, lexer = "batch", BatchLexer()
    parameters = list(_PARAMETER.finditer(text))
    cursor = 0
    fragments: list[dict[str, str]] = []

    def append(value: str, role: str) -> None:
        if not value:
            return
        if fragments and fragments[-1]["kind"] == role:
            fragments[-1]["text"] += value
        else:
            fragments.append({"text": value, "kind": role})

    try:
        for start, token, value in lexer.get_tokens_unprocessed(text):
            end = start + len(value)
            while cursor < len(parameters) and parameters[cursor].end() <= start:
                cursor += 1
            position, index = start, cursor
            while index < len(parameters) and parameters[index].start() < end:
                match = parameters[index]
                left, right = max(position, match.start()), min(end, match.end())
                append(text[position:left], _role(token))
                append(text[left:right], "parameter")
                position, index = right, index + 1
            append(text[position:end], _role(token))
            if len(fragments) > 20000:
                break
    except Exception:  # noqa: BLE001
        # Ошибка подсветки не должна запрещать просмотр или попадать в лог с текстом команды.
        fragments = []
    if "".join(part["text"] for part in fragments) != text:
        fragments = [{"text": text, "kind": "plain"}]
    return {"language": language, "tokens": fragments}


def highlight_commands(commands: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Разобрать ограниченный пакет в рабочем потоке веб-сервера."""
    return [highlight_command(item["text"], item["type"]) for item in commands]
