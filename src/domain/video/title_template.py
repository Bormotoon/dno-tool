"""Редактируемая подпись выбранного кадра по достоверным данным источника."""

from __future__ import annotations

from datetime import datetime
from string import Formatter

from .models import MediaAsset


class TitleTemplate:
    @staticmethod
    def expand(template: str, asset: MediaAsset, source_time: float) -> str:
        values = {"source": asset.name, "timecode": "", "recorded_at": ""}
        milliseconds = max(0, round(source_time * 1000))
        seconds, ms = divmod(milliseconds, 1000)
        minutes, sec = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        values["timecode"] = f"{hours:02d}:{minute:02d}:{sec:02d}.{ms:03d}"
        for _, key, spec, conversion in Formatter().parse(template):
            if key is None:
                continue
            if key not in values or spec or conversion:
                raise ValueError(
                    "Допустимы только {source}, {timecode} и {recorded_at} без дополнительных форматов."
                )
            if key == "recorded_at":
                if not asset.recording_started:
                    raise ValueError(
                        "У источника нет достоверного времени начала записи. Укажите его явно; имя файла не используется."
                    )
                values[key] = (
                    datetime.fromtimestamp(asset.recording_started + source_time)
                    .astimezone()
                    .isoformat(timespec="seconds")
                )
        return template.format_map(values)
