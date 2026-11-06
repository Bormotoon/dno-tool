"""Распознавание завершённых запросов по последней отображаемой строке PTY."""

import re

_TAIL_SIZE = 2048
PROMPT_SETTLE_SECONDS = 0.3

# Полное совпадение строки исключает упоминания пароля и вопросов внутри журнала.
_SECRET = (
    r"(?:password|passphrase|pin(?:\s+code)?|пароль|парольную\s+фразу|"
    r"пин(?:-?код)?|verification\s+code|auth(?:entication)?\s+code|otp|token|code|"
    r"код(?:\s+(?:подтверждения|аутентификации))?|одноразовый\s+код)"
)
_REQUEST = r"(?:(?:please\s+)?(?:enter|type|input)\s+|(?:пожалуйста,?\s+)?(?:введите|укажите)\s+)"
_ANSWERS = r"(?:y\s*/\s*n|yes\s*/\s*no(?:\s*/\s*fingerprint)?|д\s*/\s*н|да\s*/\s*нет)"
_PROMPT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        (
            rf"(?:(?:\[sudo\]|sudo:)\s*)?(?:{_REQUEST})?(?:your\s+|свой\s+)?{_SECRET}"
            r"(?:\s+(?:for|для)\s+[^:\n]+)?\s*:"
        ),
        r"\S+@\S+'s\s+password\s*:",
        rf"[^\n]*[\[(]\s*{_ANSWERS}\s*[\])]\s*[:?]?",
        r"(?:continue|proceed|confirm|overwrite)\b[^\n?]*\?",
        r"(?:ok|is\s+this\s+ok)\s*\?",
        r"(?:are\s+you\s+sure|do\s+you\s+(?:really\s+)?(?:want|wish))\b[^\n?]*\?",
        (
            r"(?:продолжить|подтвердить|перезаписать|вы\s+уверены|хотите\s+продолжить|"
            r"продолжить\s+подключение)\b[^\n?]*\?"
        ),
        (
            r"(?:please\s+)?(?:press|hit)\s+(?:[<\[]?(?:enter|return)[>\]]?|any\s+key|a\s+key)"
            r"(?:\s+to\s+(?:continue|proceed|exit|return))?\s*(?:[:.!]|\.{3})?"
        ),
        (
            r"нажмите\s+(?:(?:клавишу\s+)?[<\[]?(?:enter|return|ввод)[>\]]?|любую\s+(?:клавишу|кнопку))"
            r"(?:\s+для\s+(?:продолжения|выхода))?\s*(?:[:.!]|\.{3}|…)?"
        ),
        rf"{_REQUEST}[^\n:?!]+\s*:",
        r"(?:please\s+)?(?:select|choose)\s+[^\n:?!]+\s*:",
        r"(?:пожалуйста,?\s+)?выберите\s+[^\n:?!]+\s*:",
        r"(?:choice|selection|your\s+choice|ваш\s+выбор)\s*:",
    )
)


class PromptTail:
    """Ограниченная последняя строка с потоковой обработкой ANSI, CR и backspace.

    Предыдущая строка сохраняется только до первого символа следующей: запрос
    может заканчиваться переводом строки. Переполненная строка не распознаётся,
    чтобы обрезанный префикс журнала не превратил его конец в ложный запрос.
    """

    def __init__(self) -> None:
        self._line: list[str] = []
        self._previous = ""
        self._column = 0
        self._overflow = False
        self._mode = "text"
        self._parameters = ""

    @property
    def line(self) -> str:
        """Вернуть строку для распознавания, без управляющих последовательностей."""
        if self._overflow:
            return ""
        return "".join(self._line) if self._line else self._previous

    def clear(self) -> None:
        """Исключить обслуженный запрос, сохранив состояние ANSI-парсера."""
        self._line.clear()
        self._previous = ""
        self._column = 0
        self._overflow = False

    def feed(self, chunk: str) -> bool:
        """Принять фрагмент; вернуть признак нового отображаемого текста."""
        changed = False
        if self._mode == "text" and "\x1b" not in chunk:
            # Без ESC режим разбора не меняется, а строка для распознавания
            # зависит лишь от текста после предпоследнего перевода строки.
            last = chunk.rfind("\n")
            cut = chunk.rfind("\n", 0, last) if last > 0 else -1
            if cut >= 0:
                self.clear()
                changed = True
                chunk = chunk[cut + 1 :]
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
                    changed = self._control(ch) or changed
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
                self._previous = "" if self._overflow else "".join(self._line)
                self._line.clear()
                self._column = 0
                self._overflow = False
                changed = True
            elif ch == "\b":
                self._column = max(0, self._column - 1)
            elif ch == "\t" or (ch >= " " and ch != "\x7f"):
                self._previous = ""
                if self._column < _TAIL_SIZE:
                    if self._column >= len(self._line):
                        self._line.extend(
                            " " for _ in range(self._column - len(self._line) + 1)
                        )
                    self._line[self._column] = ch
                else:
                    self._overflow = True
                self._column = min(_TAIL_SIZE + 1, self._column + 1)
                changed = True
        return changed

    def _control(self, final: str) -> bool:
        """Применить поддерживаемое изменение последней строки."""
        if not re.fullmatch(r"[0-9]*(;[0-9]*)*", self._parameters):
            return False
        n = int(self._parameters.split(";")[0] or "0")
        if final == "K":
            before = (self.line, self._overflow)
            self._previous = ""
            if n == 0:
                del self._line[self._column :]
                if self._column <= _TAIL_SIZE:
                    self._overflow = False
            elif n == 1:
                for i in range(min(self._column + 1, len(self._line))):
                    self._line[i] = " "
            elif n == 2:
                self._line.clear()
                self._overflow = False
            return before != (self.line, self._overflow)
        if final == "G":
            self._column = min(_TAIL_SIZE + 1, max(1, n) - 1)
        elif final == "C":
            self._column = min(_TAIL_SIZE + 1, self._column + max(1, n))
        elif final == "D":
            self._column = max(0, self._column - max(1, n))
        return False


def matches_input_prompt(line: str) -> bool:
    """Проверить завершённую форму запроса в уже отображаемой строке."""
    line = line.strip()
    return bool(line) and any(pattern.fullmatch(line) for pattern in _PROMPT_PATTERNS)


def is_input_prompt(buffer: str) -> bool:
    """Проверить последнюю отображаемую строку готового фрагмента вывода."""
    tail = PromptTail()
    tail.feed(buffer)
    return matches_input_prompt(tail.line)
