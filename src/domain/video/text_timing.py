"""Абсолютные интервалы титров для таймлайна без изменения схемы проекта."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import Project, TextOverlay


@dataclass(frozen=True)
class TextInterval:
    iid: str
    start: float
    end: float
    text: str
    enabled: bool


class TextTiming:
    @staticmethod
    def remove_time(
        texts: list[TextOverlay], intervals: list[tuple[float, float]], duration: float
    ) -> list[TextOverlay]:
        """Сдвинуть свободные титры вместе с вырезанным временем проекта."""

        def mapped(time: float) -> float:
            return time - sum(
                max(0, min(time, end) - start)
                for start, end in intervals
                if time > start
            )

        result = []
        for text in texts:
            if text.clip_iid:
                result.append(text)
                continue
            start, end = mapped(text.start), mapped(text.end or duration)
            if end > start + 1e-9:
                result.append(replace(text, start=start, end=end if text.end else 0.0))
        return result

    @staticmethod
    def keep_time(
        texts: list[TextOverlay], start: float, end: float, duration: float
    ) -> list[TextOverlay]:
        """Обрезать свободные титры при команде «Оставить фрагмент»."""
        result = []
        for text in texts:
            if text.clip_iid:
                result.append(text)
                continue
            left, right = max(start, text.start), min(end, text.end or duration)
            if right > left + 1e-9:
                result.append(
                    replace(
                        text, start=left - start, end=right - start if text.end else 0.0
                    )
                )
        return result

    @staticmethod
    def intervals(project: Project) -> list[TextInterval]:
        """Разрешить конец 0 и привязку к клипу в координаты проекта."""
        clips = {clip.iid: clip for track in project.tracks for clip in track.clips}
        duration = project.duration()
        result = []
        for text in project.text_overlays:
            start, end = text.start, text.end
            if text.clip_iid:
                clip = clips.get(text.clip_iid)
                if clip is None:
                    continue
                start += clip.timeline_start
                end = clip.timeline_start + min(
                    text.end or clip.duration, clip.duration
                )
            elif not end:
                end = duration if duration > start else start + 5
            if end > start:
                result.append(
                    TextInterval(text.iid, start, end, text.label, text.enabled)
                )
        return result
