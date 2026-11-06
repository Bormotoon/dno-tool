"""Человекочитаемые длительности без зависимости от Qt."""

from __future__ import annotations

import math


def format_duration(value: object, *, milliseconds: bool = True) -> str:
    """Показать секунды как длительность, сохраняя точность коротких операций."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return "нет данных"
    try:
        seconds = float(value)
    except (ValueError, OverflowError):
        return "нет данных"
    if not math.isfinite(seconds) or seconds < 0:
        return "нет данных"
    if seconds == 0:
        return "0 с"
    if seconds < 1 and milliseconds:
        value_ms = round(seconds * 1000)
        return f"{value_ms} мс" if value_ms < 1000 else "1 с"
    if seconds < 60:
        rounded = round(seconds, 1)
        if rounded < 60:
            return f"{rounded:g} с".replace(".", ",")
        seconds = rounded
    total = int(seconds)
    parts: list[str] = []
    for size, unit in ((86400, "д"), (3600, "ч"), (60, "мин"), (1, "с")):
        count, total = divmod(total, size)
        if count:
            parts.append(f"{count} {unit}")
    return " ".join(parts) or "0 с"
