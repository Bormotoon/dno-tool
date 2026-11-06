"""План имён серии фрагментов с явными коллизиями и политикой перезаписи."""

from __future__ import annotations

import os
import re
import string
from dataclasses import dataclass
from pathlib import Path

from .markers import VideoMarker


@dataclass(frozen=True)
class SeriesTarget:
    start: float
    end: float
    path: str
    skip: bool


class SeriesPlanner:
    MAX_JOBS = 1000

    @classmethod
    def plan(
        cls,
        markers: list[VideoMarker],
        directory: str,
        template: str,
        suffix: str,
        overwrite: bool,
    ) -> list[SeriesTarget]:
        root = Path(directory).absolute()
        if not root.is_dir() or suffix not in (
            "mp4",
            "mkv",
            "wav",
            "mp3",
            "aac",
            "m4a",
        ):
            raise ValueError("Выберите существующую папку и поддерживаемый формат.")
        if len(template) > 512:
            raise ValueError("Шаблон имени не должен превышать 512 символов.")
        for _, field, spec, conversion in string.Formatter().parse(template):
            if (
                field not in (None, "index", "name", "start", "end")
                or conversion not in (None, "s")
                or (
                    field is not None
                    and not re.fullmatch(r"\d{0,3}(?:\.\d{1,2})?[dfs]?", spec)
                )
            ):
                raise ValueError(
                    "Используйте {index:03d}, {name}, {start:.3f}, {end:.3f}; ширина поля не более 999."
                )
        ranges = [marker for marker in markers if marker.end > marker.start]
        if not ranges or len(ranges) > cls.MAX_JOBS:
            raise ValueError(f"Укажите от 1 до {cls.MAX_JOBS} именованных диапазонов.")
        result, seen = [], set()
        for index, marker in enumerate(ranges, 1):
            stem = template.format(
                index=index, name=marker.name, start=marker.start, end=marker.end
            )
            stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")[:120]
            if not stem or stem.split(".")[0].upper() in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *[f"COM{i}" for i in range(1, 10)],
                *[f"LPT{i}" for i in range(1, 10)],
            }:
                raise ValueError("Шаблон создаёт недопустимое имя файла.")
            path = root / f"{stem}.{suffix}"
            key = os.path.normcase(str(path))
            if key in seen:
                raise ValueError(
                    "Шаблон создаёт повторяющиеся имена. Добавьте {index:03d}."
                )
            if path.is_dir():
                raise ValueError("Имя результата совпадает с папкой.")
            seen.add(key)
            result.append(
                SeriesTarget(
                    marker.start, marker.end, str(path), path.exists() and not overwrite
                )
            )
        return result
