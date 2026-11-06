"""Преобразование результатов команд в семантические поля и строки."""

from __future__ import annotations

import re
from typing import Any

from src.domain.device_info import (
    GOOD_STATUSES,
    STATUS_LABELS,
    CategoryResult,
    CommandResult,
    InfoCell,
    InfoStatus,
)

from .protocol import category_id


def semantic_cell(value: str, unit: str = "") -> InfoCell:
    """Числа и размеры сортируются по значению, отсутствие не становится нулём."""
    text = str(value)
    raw = text.strip()
    try:
        if unit in {"bytes", "kb"}:
            amount = float(raw) * (1024 if unit == "kb" else 1)
            for scale, label in (
                (1024**4, "TiB"),
                (1024**3, "GiB"),
                (1024**2, "MiB"),
                (1024, "KiB"),
            ):
                if abs(amount) >= scale:
                    return InfoCell(
                        f"{amount / scale:.2f} {label}", amount, "bytes", "bytes"
                    )
            return InfoCell(f"{amount:g} B", amount, "bytes", "bytes")
        if raw.endswith("%"):
            return InfoCell(text, float(raw[:-1]), "percent", "%")
        if re.fullmatch(r"[+-]?\d+", raw):
            return InfoCell(text, int(raw), "integer")
        if re.fullmatch(r"[+-]?\d+\.\d+", raw):
            return InfoCell(text, float(raw), "number")
        match = re.fullmatch(
            r"(\d+(?:\.\d+)?)\s*(B|bytes|KiB|MiB|GiB|TiB|kB|КБ|МБ|ГБ|ТБ|Б)",
            raw,
            re.IGNORECASE,
        )
        if match:
            power = {
                "b": 0,
                "bytes": 0,
                "б": 0,
                "kb": 1,
                "kib": 1,
                "кб": 1,
                "mib": 2,
                "мб": 2,
                "gib": 3,
                "гб": 3,
                "tib": 4,
                "тб": 4,
            }[match[2].lower()]
            return semantic_cell(str(float(match[1]) * 1024**power), "bytes")
        if re.match(r"\d{4}-\d{2}-\d{2}", raw):
            return InfoCell(text, raw, "timestamp")
        timestamp = re.fullmatch(
            r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d+)\s+(\d{2}:\d{2}(?::\d{2})?)\s+(\d{4})",
            raw,
        )
        if timestamp:
            month = [
                "Jan",
                "Feb",
                "Mar",
                "Apr",
                "May",
                "Jun",
                "Jul",
                "Aug",
                "Sep",
                "Oct",
                "Nov",
                "Dec",
            ].index(timestamp[1]) + 1
            return InfoCell(
                text,
                f"{timestamp[4]}-{month:02d}-{int(timestamp[2]):02d}T{timestamp[3]}",
                "timestamp",
                "source_local",
            )
    except (ValueError, KeyError):
        pass
    return InfoCell(text, text if text else None)


def parse_category(
    name: str, commands: dict[str, CommandResult], catalog: dict[str, Any]
) -> CategoryResult:
    """Общие правила полноты и парсинга для обоих транспортов."""
    config = catalog[name]
    rows: list[tuple[Any, ...]] = []
    parse_errors: list[str] = []
    for cmd, result in commands.items():
        parser = config.get("parse", {}).get(cmd)
        if parser is None or result.status in {
            InfoStatus.UNSUPPORTED,
            InfoStatus.PERMISSION_DENIED,
        }:
            continue
        if result.stdout.strip() or (
            cmd in config.get("parse_empty_commands", [])
            and result.status in GOOD_STATUSES
        ):
            try:
                parsed = parser(result.stdout)
                if (
                    not parsed
                    and result.status == InfoStatus.COMPLETE
                    and cmd not in config.get("allow_empty_parsed", [])
                ):
                    parse_errors.append(cmd)
                rows.extend(tuple(row) for row in parsed)
            except Exception:  # noqa: BLE001
                # Внешний парсер не должен разрушать жизненный цикл запроса.
                parse_errors.append(cmd)
    fields = config.get("fields_order", [])
    if fields:
        order = {key: i for i, key in enumerate(fields)}
        rows.sort(key=lambda row: order.get(row[0], len(order)))
    columns = tuple(config.get("table_columns") or ("Свойство", "Значение"))
    sizes = config.get("size_columns", {})
    field_units = config.get("field_units", {})
    cells = tuple(
        tuple(
            semantic_cell(
                str(row[i]) if i < len(row) else "",
                sizes.get(i, "") or (field_units.get(row[0], "") if i == 1 else ""),
            )
            for i in range(len(columns))
        )
        for row in rows
    )
    statuses = {result.status for result in commands.values()}
    expected = len(config["commands"])
    if len(commands) < expected or parse_errors:
        status = InfoStatus.PARTIAL
    elif statuses <= GOOD_STATUSES:
        status = InfoStatus.COMPLETE if cells else InfoStatus.NO_DATA
    elif len(statuses) == 1:
        status = next(iter(statuses))
    else:
        status = InfoStatus.PARTIAL
    issues = [
        f"{cmd}: {STATUS_LABELS[result.status]}"
        for cmd, result in commands.items()
        if result.status not in GOOD_STATUSES
    ]
    issues += [f"{cmd}: формат ответа не распознан" for cmd in parse_errors]
    if len(commands) < expected:
        issues.append(f"Команд получено {len(commands)} из {expected}")
    if config.get("description"):
        issues.append(config.get("description", ""))
    return CategoryResult(
        category_id(name),
        name,
        columns,
        cells,
        tuple(commands.values()),
        status,
        expected_commands=expected,
        diagnostic="; ".join(issues),
    )
