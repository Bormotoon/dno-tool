"""Ограниченный, обратимый формат сбора через SSH и совместимый Node execute."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import shlex
from collections.abc import Callable
from typing import Any

from src.domain.device_info import CommandResult

from .catalog import CATEGORY_IDS, INFO_CATEGORIES

MAX_COMMAND_BYTES = 128 * 1024
MAX_ERROR_BYTES = 16 * 1024
MAX_CATEGORY_BYTES = 512 * 1024
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_FRAME_BYTES = 210 * 1024


def category_id(name: str) -> str:
    """Стабильный ID, в том числе для тестового расширения каталога."""
    return str(CATEGORY_IDS.get(name, hashlib.sha256(name.encode()).hexdigest()[:16]))


def manifest(categories: dict[str, Any]) -> list[list[str]]:
    return [
        [category_id(name), command]
        for name, category in categories.items()
        for command in category["commands"]
    ]


def build_node_info_script(
    categories: dict[str, Any] | None = None,
    *,
    request_id: str = "",
    command_timeout: int = 30,
    total_timeout: int = 180,
    local_sudo: bool | None = None,
) -> str:
    """Собрать один ограниченный батч; секрет локального sudo передаётся по stdin.

    Кодируются файлы, а не shell-подстановка: завершающие переводы строк,
    пустые поля и CRLF сохраняются. У каждой команды свой rc и два потока.
    FIFO ограничивают объём временных файлов даже у непрерывного вывода.
    """
    categories = INFO_CATEGORIES if categories is None else categories
    if not request_id:
        from uuid import uuid4

        request_id = uuid4().hex
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", request_id):
        raise ValueError("Недопустимый request_id")
    command_timeout = max(1, min(int(command_timeout), 120))
    total_timeout = max(1, min(int(total_timeout), 600))
    encoded_manifest = base64.b64encode(
        json.dumps(manifest(categories), separators=(",", ":")).encode()
    ).decode()
    parts = [
        "D=DNOINFO2",
        f"R={shlex.quote(request_id)}",
        'for x in bash base64 timeout head mktemp mkfifo wc tr; do command -v "$x" >/dev/null || { printf \'%s|%s|UNSUPPORTED|%s\\n\' "$D" "$R" "$x"; exit 127; }; done',
        "export LC_ALL=C LANG=C",
        "umask 077",
        "T=$(mktemp -d) || exit 1",
        "trap 'rm -rf -- \"$T\"' EXIT",
        "trap 'exit 143' HUP TERM INT PIPE",
        'mkfifo "$T/o" "$T/e" || exit 1',
        "S=$SECONDS",
        "N=0",
        "B=0",
        'dno_now() { V=${EPOCHREALTIME/./}; [ -n "$V" ] || V=$((SECONDS * 1000000)); }',
    ]
    if local_sudo is not None:
        parts += ["IFS= read -r P || P=''"]
    parts += [f"printf '%s|%s|BEGIN|%s\\n' \"$D\" \"$R\" '{encoded_manifest}'"]
    cache: dict[tuple[str, bool], tuple[str, str]] = {}
    index = 0
    for name, cat in categories.items():
        parts.append("C=0")
        for cmd_id, text in cat["commands"].items():
            if not re.fullmatch(r"[a-zA-Z0-9_]+", cmd_id):
                raise ValueError("Недопустимый command_id")
            elevated = cmd_id in cat.get("sudo_commands", [])
            cache_key = text, elevated
            cached = cache.get(cache_key)
            if cached:
                file_id, rc_var = cached
            else:
                index += 1
                file_id, rc_var = str(index), f"r{index}"
                cache[cache_key] = file_id, rc_var
                plain = "bash -o pipefail -c " + shlex.quote(text)
                if elevated:
                    if local_sudo is None:
                        sudo = "%sudo%" + plain
                    elif local_sudo:
                        sudo = "sudo -S -p '' -- " + plain
                    else:
                        sudo = plain
                    invocation = f'if [ "$(id -u)" = 0 ]; then {plain}; else {sudo}; fi'
                else:
                    invocation = plain
                parts.extend(
                    [
                        f"L=$(({total_timeout} - (SECONDS - S)))",
                        """if [ "$L" -le 0 ]; then printf '%s|%s|LIMIT|time\\n' "$D" "$R"; exit 124; fi""",
                        f'[ "$L" -le {command_timeout} ] || L={command_timeout}',
                        f'head -c {MAX_COMMAND_BYTES + 1} <"$T/o" >"$T/{file_id}.o" & O=$!',
                        f'head -c {MAX_ERROR_BYTES + 1} <"$T/e" >"$T/{file_id}.e" & E=$!',
                        "dno_now; K=$V",
                    ]
                )
                password_pipe = (
                    "printf '%s\\n' \"$P\" | " if local_sudo is not None else ""
                )
                parts.extend(
                    [
                        f'{password_pipe}timeout -k 1 "$L" bash -c {shlex.quote(invocation)} >"$T/o" 2>"$T/e"',
                        f"{rc_var}=$?",
                        f"dno_now; t{file_id}=$(((V - K) / 1000))",
                        'wait "$O"; wait "$E"',
                    ]
                )
            parts.extend(
                [
                    f'X=$(wc -c <"$T/{file_id}.o"); Y=$(wc -c <"$T/{file_id}.e"); Z=0',
                    f'[ "$X" -le {MAX_COMMAND_BYTES} ] && [ "$Y" -le {MAX_ERROR_BYTES} ] || Z=1',
                    f"""if [ "$((B + X + Y))" -gt {MAX_SNAPSHOT_BYTES} ] || [ "$((C + X + Y))" -gt {MAX_CATEGORY_BYTES} ]; then printf '%s|%s|LIMIT|bytes\\n' "$D" "$R"; exit 124; fi""",
                    "B=$((B + X + Y)); C=$((C + X + Y))",
                    f'''printf '%s|%s|RESULT|%s|%s|%s|%s|%s|' "$D" "$R" '{category_id(name)}' '{cmd_id}' "${rc_var}" "$Z" "$t{file_id}"''',
                    f"head -c {MAX_COMMAND_BYTES} \"$T/{file_id}.o\" | base64 | tr -d '\\r\\n'",
                    "printf '|'",
                    f"head -c {MAX_ERROR_BYTES} \"$T/{file_id}.e\" | base64 | tr -d '\\r\\n'",
                    "printf '\\n'",
                    "N=$((N + 1))",
                ]
            )
    parts.append('printf \'%s|%s|END|%s\\n\' "$D" "$R" "$N"')
    # Начальный true сохраняет точечную семантику auto-sudo старых нод.
    return "true; bash -c " + shlex.quote("; ".join(parts))


class InfoFrameParser:
    """Инкрементальный разбор полных строк; эхо и чужие запросы игнорируются."""

    def __init__(
        self,
        categories: dict[str, Any],
        request_id: str,
        on_command: Callable[[str, CommandResult], None] | None = None,
    ) -> None:
        self.categories = categories
        self.request_id = request_id
        self.expected = manifest(categories)
        self._names = {category_id(name): name for name in categories}
        self.results: dict[str, dict[str, CommandResult]] = {}
        self._buffer = ""
        self.started = False
        self.ended = False
        self.invalid = False
        self.unsupported = False
        self.limited = False
        self.limit_reason = ""
        self.bytes_received = 0
        self.on_command = on_command

    def feed(self, chunk: str) -> None:
        self.bytes_received += len(chunk.encode("utf-8"))
        if self.bytes_received > 4 * MAX_SNAPSHOT_BYTES:
            self.invalid = True
            raise ValueError("Превышен лимит снимка")
        self._buffer += chunk
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if len(line) > MAX_FRAME_BYTES:
                self.invalid = True
                continue
            self._line(line.rstrip("\r"))
        if len(self._buffer) > MAX_FRAME_BYTES:
            self.invalid = True
            raise ValueError("Превышен лимит фрейма")

    def _line(self, line: str) -> None:
        prefix = f"DNOINFO2|{self.request_id}|"
        if not line.startswith(prefix):
            return
        parts = line[len(prefix) :].split("|")
        try:
            if parts[0] == "UNSUPPORTED":
                self.unsupported = True
            elif parts[0] == "LIMIT":
                self.limited = True
                self.limit_reason = parts[1] if len(parts) > 1 else "bytes"
            elif parts[0] == "BEGIN" and len(parts) == 2:
                if (
                    self.started
                    or self.ended
                    or json.loads(base64.b64decode(parts[1], validate=True))
                    != self.expected
                ):
                    self.invalid = True
                else:
                    self.started = True
            elif (
                parts[0] == "RESULT"
                and len(parts) == 8
                and self.started
                and not self.ended
            ):
                _, cat_id, cmd, rc, trunc, elapsed_ms, out, err = parts
                name = self._names[cat_id]
                if cmd not in self.categories[name]["commands"] or trunc not in {
                    "0",
                    "1",
                }:
                    raise ValueError("Неизвестная команда")
                stdout = base64.b64decode(out, validate=True)
                stderr = base64.b64decode(err, validate=True)
                if len(stdout) > MAX_COMMAND_BYTES or len(stderr) > MAX_ERROR_BYTES:
                    raise ValueError("Превышен лимит команды")
                result = CommandResult.from_output(
                    cmd,
                    stdout.decode("utf-8", "replace"),
                    stderr.decode("utf-8", "replace"),
                    int(rc),
                    trunc == "1",
                    max(0, int(elapsed_ms)) / 1000,
                )
                group = self.results.setdefault(name, {})
                if cmd in group:
                    self.invalid = True
                else:
                    group[cmd] = result
                    if self.on_command:
                        self.on_command(name, result)
            elif (
                parts[0] == "END"
                and len(parts) == 2
                and self.started
                and not self.ended
            ):
                count = sum(len(g) for g in self.results.values())
                self.ended = True
                if int(parts[1]) != count:
                    self.invalid = True
            else:
                self.invalid = True
        except (ValueError, KeyError, binascii.Error, UnicodeError):
            self.invalid = True

    @property
    def complete(self) -> bool:
        return (
            self.started
            and self.ended
            and not self.invalid
            and not self.limited
            and not self.unsupported
            and sum(map(len, self.results.values())) == len(self.expected)
        )
