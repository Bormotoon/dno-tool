"""Read-only command output rendering, matching the web's line renderer.

This is a log with a mutable last line, not a terminal screen. Raw streams
remain owned by the caller; escape sequences can span any number of feeds.
"""

import re
from dataclasses import dataclass

# Обычный текст обрабатывается без посимвольного цикла. \t и \n разрешены,
# \r в составе \r\n нормализуется заранее.
_NON_PLAIN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Строка без перевода не должна расти без предела.
MAX_LINE_CHARS = 1024 * 1024
# Удаление середины выполняется пакетами, а не на каждую новую строку.
_RETAIN_SLACK_LINES = 1024


@dataclass(frozen=True)
class OutputPreview:
    """Первые и последние строки текста и их общее число без сборки всего текста."""

    head: tuple[str, ...]
    tail: tuple[str, ...]
    newlines: int
    empty: bool


class CommandOutputText:
    def __init__(
        self, text: str = "", *, retain_lines: tuple[int, int] | None = None
    ) -> None:
        """retain_lines=(начало, конец): хранить только эти строки, память O(1).

        В таком режиме text() не содержит середину. Режим рассчитан на превью,
        которому нужны лишь первые и последние строки и их число.
        """
        self._retain_lines = retain_lines
        self.reset(text)

    def reset(self, text: str = "") -> None:
        self._completed: list[str] = []
        self._line: list[str] = []
        self._column = 0
        self._mode = "text"
        self._parameters = ""
        self._dropped_lines = 0
        self.feed(text)

    def feed(self, chunk: str) -> None:
        if self._mode == "text" and self._column == len(self._line):
            # Под PTY строки заканчиваются \r\n. Для посимвольного разбора это
            # равносильно \n, пока \r не остался последним символом фрагмента:
            # такой \r попадает в регулярное выражение и идёт медленным путём.
            normalized = chunk.replace("\r\n", "\n")
            if _NON_PLAIN.search(normalized) is None:
                self._feed_plain(normalized)
                self._enforce_limits()
                return
        self._feed_slow(chunk)
        self._enforce_limits()

    def _feed_plain(self, text: str) -> None:
        first, separator, rest = text.partition("\n")
        if not separator:
            self._line.extend(first)
            self._column = len(self._line)
            return
        self._completed.append("".join(self._line) + first + "\n")
        self._line = []
        if rest:
            parts = rest.split("\n")
            last = parts.pop()
            self._completed.extend(part + "\n" for part in parts)
            self._line = list(last)
        self._column = len(self._line)

    def _feed_slow(self, chunk: str) -> None:
        for ch in chunk:
            if self._mode in ("osc", "oscEscape"):
                if ch == "\x07" or (self._mode == "oscEscape" and ch == "\\"):
                    self._mode = "text"
                else:
                    self._mode = "oscEscape" if ch == "\x1b" else "osc"
                continue
            if self._mode == "escape":
                self._mode = {"[": "csi", "]": "osc"}.get(ch, "text")
                self._parameters = ""
                continue
            if self._mode == "csi":
                if "@" <= ch <= "~":
                    self._control(ch)
                    self._mode = "text"
                elif len(self._parameters) < 128:
                    self._parameters += ch
                else:
                    self._mode = "text"
                continue
            if ch == "\x1b":
                self._mode = "escape"
            elif ch == "\r":
                self._column = 0
            elif ch == "\n":
                self._completed.append("".join(self._line) + "\n")
                self._line = []
                self._column = 0
            elif ch == "\b":
                self._column = max(0, self._column - 1)
            elif ch == "\t" or (ch >= " " and ch != "\x7f"):
                if self._column >= len(self._line):
                    self._line.extend(" " for _ in range(self._column - len(self._line) + 1))
                self._line[self._column] = ch
                self._column += 1

    def _enforce_limits(self) -> None:
        if len(self._line) > MAX_LINE_CHARS:
            del self._line[MAX_LINE_CHARS:]
            self._column = min(self._column, MAX_LINE_CHARS)
        if self._retain_lines is None:
            return
        head, tail = self._retain_lines
        count = len(self._completed)
        if count > head + tail + _RETAIN_SLACK_LINES:
            del self._completed[head : count - tail]
            self._dropped_lines += count - tail - head

    def _control(self, final: str) -> None:
        # SGR colours and unsupported controls are consumed without rendering.
        if not re.fullmatch(r"[0-9]*(;[0-9]*)*", self._parameters):
            return
        n = int(self._parameters.split(";")[0] or "0")
        if final == "K":
            if n == 0:
                del self._line[self._column:]
            elif n == 1:
                for i in range(min(self._column + 1, len(self._line))):
                    self._line[i] = " "
            elif n == 2:
                self._line = []
        elif final == "G":
            self._column = min(16384, max(1, n) - 1)
        elif final == "C":
            self._column = min(16384, self._column + max(1, n))
        elif final == "D":
            self._column = max(0, self._column - max(1, n))

    def text(self) -> str:
        return "".join(self._completed) + "".join(self._line)

    def current_line(self) -> str:
        """Незавершённая последняя строка: единственная часть, которая меняется."""
        return "".join(self._line)

    def drain_completed(self) -> list[str]:
        """Забрать завершённые строки, чтобы показать их без полной копии текста."""
        lines, self._completed = self._completed, []
        return lines

    def head_text(self, limit: int) -> str:
        """Вернуть начало текста, не собирая его целиком."""
        parts: list[str] = []
        remaining = limit
        for line in self._completed:
            piece = line[:remaining]
            parts.append(piece)
            remaining -= len(piece)
            if remaining <= 0:
                return "".join(parts)
        parts.append("".join(self._line[:remaining]))
        return "".join(parts)

    def preview_parts(
        self, head_lines: int, tail_lines: int, line_chars: int
    ) -> OutputPreview:
        """Вернуть первые и последние строки и их число за O(head_lines + tail_lines)."""

        def segment(line: str) -> str:
            return line[: min(len(line) - 1, line_chars)]

        completed = self._completed
        current = "".join(self._line[:line_chars])
        head = [segment(line) for line in completed[:head_lines]]
        if len(head) < head_lines and not self._dropped_lines:
            head.append(current)
        tail = [segment(line) for line in completed[-max(0, tail_lines - 1) :]] if tail_lines > 1 else []
        tail.append(current)
        return OutputPreview(
            head=tuple(head),
            tail=tuple(tail),
            newlines=self._dropped_lines + len(completed),
            empty=not completed and not self._dropped_lines and not self._line,
        )
