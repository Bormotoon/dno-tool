"""Непрозрачная статичная маска в нормированных координатах холста."""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass, field


@dataclass
class VideoMask:
    iid: str = field(default_factory=lambda: uuid.uuid4().hex)
    start: float = 0
    end: float = 1
    x: float = 0.1
    y: float = 0.1
    width: float = 0.3
    height: float = 0.2

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self, duration: float) -> None:
        values = (self.start, self.end, self.x, self.y, self.width, self.height)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in values
        ):
            raise ValueError("Параметры маски должны быть конечными числами.")
        if not 0 <= self.start < self.end <= duration + 1e-9:
            raise ValueError("Интервал маски должен лежать внутри проекта.")
        if not (
            self.x >= 0
            and self.y >= 0
            and self.width > 0
            and self.height > 0
            and self.x + self.width <= 1
            and self.y + self.height <= 1
        ):
            raise ValueError(
                "Маска должна лежать внутри холста; координаты задаются долями 0..1."
            )
