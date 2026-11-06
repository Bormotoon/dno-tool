"""Именованные точки и диапазоны, привязанные к времени проекта."""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass, field


@dataclass
class VideoMarker:
    iid: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Фрагмент"
    start: float = 0.0
    end: float = 0.0
    color: str = "#ffb300"
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self, duration: float) -> None:
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in (self.start, self.end)
        ):
            raise ValueError("Метки должны содержать конечное неотрицательное время.")
        if (
            self.start > duration + 1e-6
            or self.end > duration + 1e-6
            or (self.end and self.end <= self.start)
        ):
            raise ValueError(
                "Диапазон маркера должен лежать внутри проекта; конец 0 означает точку."
            )
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or len(self.name) > 1000
            or not isinstance(self.note, str)
            or len(self.note) > 10000
        ):
            raise ValueError(
                "Укажите имя фрагмента (до 1000 символов) и заметку (до 10000)."
            )


class MarkerRules:
    @staticmethod
    def keep(markers: list[VideoMarker], start: float, end: float) -> list[VideoMarker]:
        from dataclasses import replace

        result = []
        for marker in markers:
            if marker.end:
                left, right = max(start, marker.start), min(end, marker.end)
                if right > left:
                    result.append(
                        replace(marker, start=left - start, end=right - start)
                    )
            elif start <= marker.start <= end:
                result.append(replace(marker, start=marker.start - start))
        return result

    @staticmethod
    def remove(
        markers: list[VideoMarker], intervals: list[tuple[float, float]]
    ) -> list[VideoMarker]:
        from dataclasses import replace

        def mapped(time: float) -> float:
            return time - sum(
                max(0, min(time, end) - start)
                for start, end in intervals
                if time > start
            )

        result = []
        for marker in markers:
            if marker.end:
                start, end = mapped(marker.start), mapped(marker.end)
                if end > start + 1e-9:
                    result.append(replace(marker, start=start, end=end))
            elif not any(start <= marker.start < end for start, end in intervals):
                result.append(replace(marker, start=mapped(marker.start)))
        return result
