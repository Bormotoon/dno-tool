"""Рабочий поток одной операции: результат применяется после QThread.finished."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from src.services.hosts.kde_service import (
    KdeConnection,
    KdeOperationControl,
    KdeOperationError,
    KdeService,
)


@dataclass(frozen=True)
class KdeOutcome:
    """Результат живёт до штатного завершения потока."""

    data: dict[str, Any] | None = None
    code: str = ""
    message: str = ""
    details: str = ""


class KDEConfigWorker(QThread):
    """Владеет одной SSH-операцией и не получает объекты виджетов."""

    progress = Signal(str)

    def __init__(
        self,
        service: KdeService,
        connection: KdeConnection,
        request: dict[str, Any],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.connection = connection
        self.request = dict(request)
        self.control = KdeOperationControl()
        self.outcome = KdeOutcome(code="pending", message="Операция ещё не завершена.")

    def run(self) -> None:
        try:
            result = self.service.request(
                self.connection, self.request, self.control, self.progress.emit
            )
            self.outcome = KdeOutcome(data=result)
        except KdeOperationError as error:
            self.outcome = KdeOutcome(
                code=error.code, message=str(error), details=error.details
            )
        except Exception as error:  # noqa: BLE001 — граница рабочего потока
            self.outcome = KdeOutcome(
                code="internal",
                message=f"Внутренняя ошибка операции: {type(error).__name__}.",
            )

    def abort(self) -> None:
        self.control.cancel()
