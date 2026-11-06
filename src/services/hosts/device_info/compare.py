"""Сравнение самостоятельных снимков без дополнительных сетевых запросов."""

from __future__ import annotations

from collections import Counter

from src.domain.device_info import DeviceInfoSnapshot


def compare_snapshots(
    before: DeviceInfoSnapshot, after: DeviceInfoSnapshot
) -> list[tuple[str, str, str, str, str]]:
    """Отсутствие раздела обозначается отдельно от исчезновения объекта."""
    changes: list[tuple[str, str, str, str, str]] = []
    for name in dict.fromkeys((*before.categories, *after.categories)):
        old, new = before.categories.get(name), after.categories.get(name)
        if old is None or new is None:
            changes.append(
                (
                    name,
                    "Раздел",
                    old.status.value if old else "Не получен",
                    new.status.value if new else "Не получен",
                    "Различается полнота",
                )
            )
            continue
        if old.status != new.status:
            changes.append(
                (name, "Статус", old.status.value, new.status.value, "Изменено")
            )
        if old.columns != new.columns:
            changes.append(
                (
                    name,
                    "Схема",
                    " | ".join(old.columns),
                    " | ".join(new.columns),
                    "Изменено",
                )
            )
        if old.columns == new.columns == ("Свойство", "Значение"):
            old_fields = {row[0].display: row[1].display for row in old.rows}
            new_fields = {row[0].display: row[1].display for row in new.rows}
            for key in dict.fromkeys((*old_fields, *new_fields)):
                a, b = old_fields.get(key), new_fields.get(key)
                if a != b:
                    changes.append(
                        (
                            name,
                            key,
                            a or "",
                            b or "",
                            (
                                "Изменено"
                                if a is not None and b is not None
                                else "Добавлено" if a is None else "Удалено"
                            ),
                        )
                    )
        else:
            old_rows = Counter(tuple(cell.display for cell in row) for row in old.rows)
            new_rows = Counter(tuple(cell.display for cell in row) for row in new.rows)
            changes.extend(
                (name, row[0], " | ".join(row), "", "Удалено")
                for row, count in (old_rows - new_rows).items()
                for _ in range(count)
            )
            changes.extend(
                (name, row[0], "", " | ".join(row), "Добавлено")
                for row, count in (new_rows - old_rows).items()
                for _ in range(count)
            )
    return changes
