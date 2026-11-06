"""Таймкоды и монтажные ориентиры без зависимости от интерфейса."""

from __future__ import annotations

import math
import re
from fractions import Fraction


class VideoNavigation:
    @staticmethod
    def parse_timecode(value: str, fps: Fraction) -> float:
        value = value.strip().replace(",", ".")
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            result = float(value)
        else:
            match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d)(?:([.:])(\d+))?", value)
            if not match:
                raise ValueError("Введите секунды, ЧЧ:ММ:СС.мс или ЧЧ:ММ:СС:кадры.")
            hours, minutes, seconds, separator, part = match.groups()
            result = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
            if part:
                if separator == ":":
                    if fps <= 0 or int(part) >= math.ceil(fps):
                        raise ValueError("Номер кадра выходит за FPS проекта.")
                    result += float(Fraction(int(part), 1) / fps)
                else:
                    result += float("0." + part)
        if not math.isfinite(result):
            raise ValueError("Таймкод должен быть конечным числом.")
        return result

    @staticmethod
    def next_boundary(
        boundaries: list[float], position: float, direction: int
    ) -> float:
        candidates = [
            value for value in boundaries if (value - position) * direction > 1e-6
        ]
        return (
            (min(candidates) if direction > 0 else max(candidates))
            if candidates
            else position
        )

    @staticmethod
    def snap(
        position: float,
        duration: float,
        targets: list[float],
        tolerance: float,
        edge: str = "move",
    ) -> float:
        edges = (0.0, duration) if edge == "move" else (0.0,)
        candidates = [
            (abs(target - position - offset), target - offset)
            for offset in edges
            for target in targets
            if abs(target - position - offset) <= tolerance and target - offset >= 0
        ]
        return min(candidates)[1] if candidates else position
