"""Сериализация отчётов и атомарная замена выбранного файла."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path

from src.domain.device_info import STATUS_LABELS, DeviceInfoSnapshot, snapshot_data


def serialize_report(
    snapshot: DeviceInfoSnapshot, *, format: str = "txt", names: set[str] | None = None
) -> str:
    """Сохранить любые схемы таблиц, источник, время и частичность."""
    if format == "json":
        return json.dumps(snapshot_data(snapshot, names), ensure_ascii=False, indent=2)
    categories = [
        cat
        for name, cat in snapshot.categories.items()
        if names is None or name in names
    ]
    if format in {"csv", "tsv"}:
        if len(categories) != 1:
            raise ValueError("Для CSV/TSV выберите один раздел")
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter="\t" if format == "tsv" else ",")
        selected_category = categories[0]
        writer.writerow(selected_category.columns)
        writer.writerows(
            [[cell.display for cell in row] for row in selected_category.rows]
        )
        return stream.getvalue()
    if format != "txt":
        raise ValueError("Неизвестный формат отчёта")
    lines = [
        f"Информация об устройстве: {snapshot.name} ({snapshot.host})",
        f"Владелец: {snapshot.owner}",
        f"Источник: {snapshot.transport}",
        f"SSH endpoint: {snapshot.endpoint}",
        f"Начало: {snapshot.started_at}",
        f"Окончание: {snapshot.finished_at or 'Сбор не завершён'}",
        f"Статус: {STATUS_LABELS[snapshot.status]}",
        f"Проблемных разделов: {snapshot.problem_count}",
        f"Запрос: {snapshot.request_id}",
        f"Профиль: {snapshot.profile}",
        snapshot.diagnostic,
        "",
    ]
    expected = snapshot.expected_categories if names is None else tuple(names)
    for name in dict.fromkeys((*expected, *(cat.name for cat in categories))):
        if names is not None and name not in names:
            continue
        cat = snapshot.categories.get(name)
        lines.append(f"--- {name} ---")
        if cat is None:
            lines.append("Не получено")
            continue
        lines += [
            f"Статус: {STATUS_LABELS[cat.status]}; время: {cat.timestamp}",
            cat.diagnostic,
        ]
        if cat.columns == ("Свойство", "Значение"):
            for row in cat.rows:
                parts = row[1].display.splitlines() or [""]
                lines.append(f"  {row[0].display}: {parts[0]}")
                lines.extend(f"    {line}" for line in parts[1:])
        else:
            stream = io.StringIO(newline="")
            writer = csv.writer(stream, delimiter="\t")
            writer.writerow(cat.columns)
            writer.writerows([[cell.display for cell in row] for row in cat.rows])
            lines.append(stream.getvalue().rstrip())
        if not cat.rows:
            lines.append("Нет строк данных")
        lines.append("")
    return "\n".join(lines)


def write_report(path: str | Path, text: str) -> None:
    """Ошибка формирования или записи не повреждает предыдущий файл."""
    target = Path(path).resolve()
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=target.parent,
            prefix=".dno-info-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
