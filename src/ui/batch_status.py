"""Общий отчёт о ходе пакетной операции. Без Qt, вызывается из потоков пула."""

from __future__ import annotations

import threading
from collections.abc import Callable

from src.utils.text.text_utils import plural_ru


def plural_errors(count: int) -> str:
    """Правильное русское склонение слова «ошибка» по числу.

    1 → ошибка, 2-4 → ошибки, 5-20 → ошибок, 21 → ошибка, 22-24 → ошибки и т.д.
    """
    return plural_ru(count, "ошибка", "ошибки", "ошибок")


class BatchStatusAggregator:
    """Счётчики батча и итоговый статус; порядок сообщений строго монотонный.

    Подсчёт и отправка выполняются под одной блокировкой, иначе промежуточное
    «⏳ 7/8» могло прийти после итога и оставить строку статуса в ожидании.
    report из чужого потока только ставит сигнал в очередь и не блокируется.
    """

    def __init__(
        self,
        total: int,
        label: str,
        report: Callable[[str, str, str], None],
        *,
        suffix: str = "",
    ) -> None:
        self._total = total
        self._title = f"{label}{suffix}"
        self._report = report
        self._lock = threading.Lock()
        self._done = 0
        self._ok = 0
        self._fail = 0
        self._last_error = ""
        self._last_detail = ""

    def record(self, ok: bool, error: str = "", detail: str = "") -> None:
        with self._lock:
            self._done += 1
            if ok:
                self._ok += 1
            else:
                self._fail += 1
                if error:
                    self._last_error = error
                if detail:
                    self._last_detail = detail
            self._report(*self._message())

    def _message(self) -> tuple[str, str, str]:
        if self._done < self._total:
            progress = f"{self._done}/{self._total} (✅{self._ok} ❌{self._fail})"
            return f"⏳ {self._title}: {progress}", "busy", ""
        if self._fail == 0:
            return f"✅ {self._title}: {self._ok}/{self._total}", "ok", ""
        text = (
            f"⚠ {self._title}: {self._ok}/{self._total} успешно, "
            f"{self._fail} {plural_errors(self._fail)}"
        )
        if self._last_error:
            text += f" — {self._last_error}"
        return text, "error", self._last_detail
