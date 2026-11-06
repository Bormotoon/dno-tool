"""Независимые от Qt снимки системной информации и правила полноты."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from src.domain.device_identity import DeviceKey


class InfoStatus(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    COLLECTING = "collecting"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    UNSUPPORTED = "unsupported"
    PERMISSION_DENIED = "permission_denied"
    NO_DATA = "no_data"
    TIMEOUT = "timeout"
    TRUNCATED = "truncated"


STATUS_LABELS = {
    InfoStatus.IDLE: "Не запрашивалось",
    InfoStatus.CONNECTING: "Подключение",
    InfoStatus.COLLECTING: "Сбор",
    InfoStatus.COMPLETE: "Готово",
    InfoStatus.PARTIAL: "Частичные данные",
    InfoStatus.FAILED: "Ошибка",
    InfoStatus.CANCELLING: "Отмена",
    InfoStatus.CANCELLED: "Отменено",
    InfoStatus.UNSUPPORTED: "Не поддерживается",
    InfoStatus.PERMISSION_DENIED: "Нет прав",
    InfoStatus.NO_DATA: "Объекты не найдены",
    InfoStatus.TIMEOUT: "Истекло время",
    InfoStatus.TRUNCATED: "Вывод усечён",
}
GOOD_STATUSES = {InfoStatus.COMPLETE, InfoStatus.NO_DATA}


def utc_now() -> str:
    """Время источника в переносимом формате UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    status: InfoStatus = InfoStatus.COMPLETE
    elapsed_sec: float = 0.0

    @classmethod
    def from_output(
        cls,
        command_id: str,
        stdout: str,
        stderr: str,
        returncode: int,
        truncated: bool = False,
        elapsed_sec: float = 0.0,
    ) -> CommandResult:
        """Отличить отсутствие объектов от ошибки, прав и лимитов."""
        error = stderr.lower()
        if truncated:
            status = InfoStatus.TRUNCATED
        elif returncode in (124, 137):
            status = InfoStatus.TIMEOUT
        elif returncode in (126, 127) or any(
            text in error
            for text in (
                "command not found",
                "unrecognized option",
                "invalid option",
                "no such file or directory",
            )
        ):
            status = InfoStatus.UNSUPPORTED
        elif any(
            text in error
            for text in (
                "permission denied",
                "access denied",
                "operation not permitted",
                "password is required",
                "not in the sudoers",
                "incorrect password",
                "authentication failure",
                "a terminal is required",
            )
        ):
            status = InfoStatus.PERMISSION_DENIED
        elif returncode != 0:
            status = InfoStatus.FAILED
        elif not stdout.strip():
            status = InfoStatus.NO_DATA
        elif stderr.strip():
            status = InfoStatus.PARTIAL
        else:
            status = InfoStatus.COMPLETE
        return cls(command_id, stdout, stderr, returncode, status, elapsed_sec)


@dataclass(frozen=True)
class InfoCell:
    display: str
    value: str | float | int | None = None
    kind: str = "text"
    unit: str = ""


@dataclass(frozen=True)
class CategoryResult:
    category_id: str
    name: str
    columns: tuple[str, ...]
    rows: tuple[tuple[InfoCell, ...], ...] = ()
    commands: tuple[CommandResult, ...] = ()
    status: InfoStatus = InfoStatus.IDLE
    timestamp: str = field(default_factory=utc_now)
    expected_commands: int = 0
    diagnostic: str = ""


@dataclass
class DeviceInfoSnapshot:
    device_key: DeviceKey
    name: str
    host: str
    owner: str
    transport: str
    endpoint: str
    profile: str = "brief"
    request_id: str = field(default_factory=lambda: uuid4().hex)
    schema_version: int = 2
    started_at: str = field(default_factory=utc_now)
    finished_at: str = ""
    status: InfoStatus = InfoStatus.IDLE
    categories: dict[str, CategoryResult] = field(default_factory=dict)
    expected_categories: tuple[str, ...] = ()
    diagnostic: str = ""
    metrics: dict[str, float | int] = field(default_factory=dict)

    def finish(self, *, cancelled: bool = False, transport_ok: bool = True) -> None:
        """Один раздел не может подтвердить полноту всего снимка."""
        self.finished_at = utc_now()
        results = [
            self.categories[name]
            for name in self.expected_categories
            if name in self.categories
        ]
        complete = len(results) == len(self.expected_categories) and bool(results)
        good = all(result.status in GOOD_STATUSES for result in results)
        if cancelled:
            self.status = InfoStatus.CANCELLED
        elif transport_ok and complete and good:
            self.status = InfoStatus.COMPLETE
        elif any(result.rows or result.status in GOOD_STATUSES for result in results):
            self.status = InfoStatus.PARTIAL
        else:
            self.status = InfoStatus.FAILED

    @property
    def problem_count(self) -> int:
        return sum(
            name not in self.categories
            or self.categories[name].status not in GOOD_STATUSES
            for name in self.expected_categories
        )


def category_data(cat: CategoryResult) -> dict[str, Any]:
    """Значения одного раздела без stdout и stderr команд."""
    return {
        "category_id": cat.category_id,
        "status": cat.status.value,
        "timestamp": cat.timestamp,
        "columns": cat.columns,
        "rows": [
            [
                {
                    "display": cell.display,
                    "value": cell.value,
                    "kind": cell.kind,
                    "unit": cell.unit,
                }
                for cell in row
            ]
            for row in cat.rows
        ],
        "expected_commands": cat.expected_commands,
        "received_commands": len(cat.commands),
        "commands": [
            {
                "command_id": cmd.command_id,
                "returncode": cmd.returncode,
                "status": cmd.status.value,
                "elapsed_sec": cmd.elapsed_sec,
            }
            for cmd in cat.commands
        ],
        "diagnostic": cat.diagnostic,
    }


def snapshot_data(
    snapshot: DeviceInfoSnapshot, names: set[str] | None = None
) -> dict[str, Any]:
    """Экспортировать значения и безопасную диагностику без сырого вывода."""
    return {
        "schema_version": snapshot.schema_version,
        "device_key": snapshot.device_key,
        "name": snapshot.name,
        "host": snapshot.host,
        "owner": snapshot.owner,
        "transport": snapshot.transport,
        "endpoint": snapshot.endpoint,
        "request_id": snapshot.request_id,
        "profile": snapshot.profile,
        "started_at": snapshot.started_at,
        "finished_at": snapshot.finished_at,
        "status": snapshot.status.value,
        "diagnostic": snapshot.diagnostic,
        "expected_categories": snapshot.expected_categories,
        "metrics": snapshot.metrics,
        "categories": {
            name: category_data(cat)
            for name, cat in snapshot.categories.items()
            if names is None or name in names
        },
    }
