"""Разбор завершённого блока FFmpeg -progress без зависимости от Qt."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressSample:
    """Одна согласованная выборка времени, кадров и скорости."""

    seconds: float | None
    frame: int | None
    fps: float | None
    speed: float | None

    @staticmethod
    def _number(value: str | None) -> float | None:
        try:
            number = float(value) if value is not None else float("nan")
        except (ValueError, OverflowError):
            return None
        return number if math.isfinite(number) and number >= 0 else None

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> ProgressSample:
        """Оба поля out_time_us/out_time_ms содержат микросекунды."""
        timestamp = cls._number(fields.get("out_time_us"))
        if timestamp is None:
            timestamp = cls._number(fields.get("out_time_ms"))
        frame = cls._number(fields.get("frame"))
        return cls(
            seconds=timestamp / 1_000_000 if timestamp is not None else None,
            frame=int(frame) if frame is not None and frame.is_integer() else None,
            fps=cls._number(fields.get("fps")),
            speed=cls._number(fields.get("speed", "").removesuffix("x")),
        )
