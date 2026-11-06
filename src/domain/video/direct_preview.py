"""Быстрый предпросмотр обычной нарезки без промежуточного кодирования.

Поворот и отражение применяются к готовому кадру в окне, поэтому прямое
воспроизведение они не отключают.
"""

from __future__ import annotations

from dataclasses import dataclass

from .render_plan import RenderClip, RenderPlan


@dataclass(frozen=True)
class DirectPreview:
    video: tuple[RenderClip, ...]
    audio: tuple[RenderClip | None, ...]

    @classmethod
    def from_plan(cls, plan: RenderPlan) -> DirectPreview | None:
        """Допустить последовательные исходники с их собственным звуком."""
        video = plan.video
        if not video or plan.ducking or plan.masks or plan.limiter:
            return None
        if plan.canvas.width or plan.canvas.height or plan.canvas.fit != "contain":
            return None
        audio = [clip for clip in plan.clips if clip.kind == "audio"]
        paired: list[RenderClip | None] = []
        cursor = 0.0
        for clip in video:
            if clip.start < cursor - 1e-6 or clip.stream or clip.speed != 1:
                return None
            if (
                clip.opacity != 1
                or clip.scale != 1
                or clip.position_x
                or clip.position_y
                or clip.crop_left
                or clip.crop_right
                or clip.crop_top
                or clip.crop_bottom
                or clip.video_fade_in
                or clip.video_fade_out
            ):
                return None
            mate = next(
                (
                    item
                    for item in audio
                    if (
                        item.path == clip.path
                        and item.stream == 0
                        and item.speed == 1
                        and abs(item.start - clip.start) < 1e-6
                        and abs(item.src_in - clip.src_in) < 1e-6
                        and abs(item.src_out - clip.src_out) < 1e-6
                    )
                ),
                None,
            )
            if mate:
                if (
                    mate.fade_in
                    or mate.fade_out
                    or (not mate.mute and mate.gain * plan.master > 1)
                ):
                    return None
                audio.remove(mate)
            paired.append(mate)
            cursor = clip.end
        if audio or cursor > plan.duration + 1e-6:
            return None
        return cls(video, tuple(paired))

    def segments(self) -> list[tuple[float, float, str, float]]:
        return [(clip.start, clip.end, clip.path, clip.src_in) for clip in self.video]

    def volume_at(self, position: float, master: float) -> float:
        for clip, audio in zip(self.video, self.audio):
            if clip.start <= position < clip.end:
                return audio.gain * master if audio and not audio.mute else 0.0
        return 0.0
