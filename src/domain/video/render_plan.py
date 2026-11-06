"""Неизменяемый монтажный план: единая временная ось изображения и звука."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from fractions import Fraction

from .models import Project


@dataclass(frozen=True)
class RenderClip:
    iid: str
    track_iid: str
    path: str
    kind: str
    start: float
    src_in: float
    src_out: float
    stream: int = 0
    gain: float = 1.0
    mute: bool = False
    fade_in: float = 0.0
    fade_out: float = 0.0
    audio_offset: float = 0.0
    audio_duration: float = 0.0
    layer: int = 0
    opacity: float = 1.0
    scale: float = 1.0
    position_x: float = 0.0
    position_y: float = 0.0
    rotation: int = 0
    crop_left: float = 0.0
    crop_top: float = 0.0
    crop_right: float = 0.0
    crop_bottom: float = 0.0
    video_fade_in: float = 0.0
    video_fade_out: float = 0.0
    video_offset: float = 0.0
    video_duration: float = 0.0
    speed: float = 1.0
    flip_h: bool = False
    flip_v: bool = False

    @property
    def duration(self) -> float:
        return (self.src_out - self.src_in) / self.speed

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass(frozen=True)
class RenderCanvas:
    width: int = 0
    height: int = 0
    fps: Fraction = Fraction(0)
    fit: str = "contain"


@dataclass(frozen=True)
class RenderText:
    text: str
    corner: str
    fontsize: int
    color: str
    font_family: str
    margin_x: int
    margin_y: int
    back_color: str
    back_alpha: int
    box_pad: int
    enabled: bool = True
    start: float = 0.0
    end: float = 0.0
    fontfile: str = ""
    kind: str = "text"
    image_path: str = ""
    width: int = 240
    height: int = 120
    opacity: int = 100
    stroke_width: int = 6
    filled: bool = False
    arrow_direction: str = "right"
    rotation: int = 0


@dataclass(frozen=True)
class RenderDucking:
    track_iid: str
    source_iid: str
    threshold: float = 0.03
    ratio: float = 8.0
    attack: float = 20.0
    release: float = 300.0


@dataclass(frozen=True)
class RenderMask:
    start: float
    end: float
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class RenderPlan:
    clips: tuple[RenderClip, ...]
    duration: float
    canvas: RenderCanvas
    master: float = 1.0
    copy_reasons: tuple[str, ...] = ()
    texts: tuple[RenderText, ...] = ()
    limiter: bool = False
    canvas_source: RenderClip | None = None
    ducking: tuple[RenderDucking, ...] = ()
    masks: tuple[RenderMask, ...] = ()

    @classmethod
    def from_project(cls, project: Project) -> RenderPlan:
        clips = []
        solo = any(track.solo for track in project.audio_tracks())
        for layer, track in enumerate(project.tracks):
            if track.kind not in ("video", "audio"):
                raise ValueError("Неизвестный тип дорожки.")
            if track.hidden or (
                track.kind == "audio" and (track.mute or (solo and not track.solo))
            ):
                continue
            for clip in track.sorted_clips():
                asset = project.asset_by_iid(clip.asset_iid)
                if asset is None:
                    raise ValueError("Клип ссылается на отсутствующий исходник.")
                values = (clip.timeline_start, clip.src_in, clip.src_out, clip.volume)
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in values
                ):
                    raise ValueError("Некорректные границы или громкость клипа.")
                if (
                    clip.timeline_start < 0
                    or clip.src_in < 0
                    or clip.src_out <= clip.src_in
                    or not 0 <= clip.volume <= 2
                ):
                    raise ValueError("Клип имеет недопустимые границы или громкость.")
                if clip.effects or clip.transitions:
                    raise ValueError(
                        "Эффекты и переходы пока не поддерживаются движком."
                    )
                if (
                    any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        or value < 0
                        for value in (
                            clip.fade_in,
                            clip.fade_out,
                            clip.normalization_gain,
                            clip.audio_offset,
                            clip.audio_duration,
                        )
                    )
                    or max(clip.fade_in, clip.fade_out)
                    > (clip.audio_duration or clip.duration)
                    or clip.normalization_gain > 100
                ):
                    raise ValueError("Некорректные затухания или нормализация клипа.")
                clips.append(
                    RenderClip(
                        clip.iid,
                        track.iid,
                        asset.path,
                        track.kind,
                        clip.timeline_start,
                        clip.src_in,
                        clip.src_out,
                        (
                            asset.video_stream
                            if track.kind == "video"
                            else asset.audio_stream
                        ),
                        clip.volume * clip.normalization_gain,
                        clip.mute,
                        clip.fade_in,
                        clip.fade_out,
                        clip.audio_offset,
                        clip.audio_duration or clip.duration,
                        layer,
                        **{
                            key: getattr(clip, key)
                            for key in (
                                "opacity",
                                "scale",
                                "position_x",
                                "position_y",
                                "rotation",
                                "crop_left",
                                "crop_top",
                                "crop_right",
                                "crop_bottom",
                                "video_fade_in",
                                "video_fade_out",
                                "video_offset",
                                "video_duration",
                                "speed",
                                "flip_h",
                                "flip_v",
                            )
                        },
                    )
                )
        video = sorted(
            (clip for clip in clips if clip.kind == "video"),
            key=lambda clip: clip.start,
        )
        for iid in {clip.track_iid for clip in video}:
            on_track = [clip for clip in video if clip.track_iid == iid]
            for previous, following in zip(on_track, on_track[1:]):
                if following.start < previous.end - 1e-9:
                    raise ValueError(
                        "Перекрытие клипов на одной видеодорожке не поддерживается. Используйте отдельный слой."
                    )
        if (
            not math.isfinite(project.master_volume)
            or not 0 <= project.master_volume <= 2
        ):
            raise ValueError("Некорректная мастер-громкость.")
        canvas = project.canvas
        return cls(
            tuple(clips),
            project.duration(),
            RenderCanvas(
                canvas.width,
                canvas.height,
                Fraction(canvas.fps_num, canvas.fps_den),
                canvas.fit,
            ),
            project.effective_master_volume,
            tuple(project.copy_reasons()),
            tuple(
                RenderText(
                    **{
                        key: getattr(overlay, key)
                        for key in RenderText.__dataclass_fields__
                        if key not in ("start", "end")
                    },
                    start=overlay.start + (bound.timeline_start if bound else 0),
                    end=(
                        (
                            bound.timeline_start
                            + min(overlay.end or bound.duration, bound.duration)
                        )
                        if bound
                        else overlay.end
                    ),
                )
                for overlay in project.text_overlays
                for bound in [
                    next(
                        (
                            clip
                            for track in project.tracks
                            for clip in track.clips
                            if clip.iid == overlay.clip_iid
                        ),
                        None,
                    )
                ]
                if overlay.enabled
                and overlay.has_content
                and (
                    not bound
                    or (
                        bound.iid in {clip.iid for clip in clips}
                        and overlay.start < bound.duration
                    )
                )
            ),
            project.audio_limiter,
            video[0] if video else None,
            tuple(
                RenderDucking(
                    track.iid,
                    track.duck_source_iid,
                    track.duck_threshold,
                    track.duck_ratio,
                    track.duck_attack,
                    track.duck_release,
                )
                for track in project.audio_tracks()
                if track.duck_source_iid
            ),
            tuple(
                RenderMask(
                    **{
                        key: getattr(mask, key)
                        for key in RenderMask.__dataclass_fields__
                    }
                )
                for mask in project.masks
            ),
        )

    def section(self, start: float, end: float) -> RenderPlan:
        """Ограниченный диапазон с переносом начала в ноль для preview."""
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
            or end > self.duration + 1e-9
        ):
            raise ValueError("Некорректный диапазон монтажа.")

        clips = tuple(
            replace(
                clip,
                start=max(clip.start, start) - start,
                src_in=clip.src_in + max(0, start - clip.start) * clip.speed,
                src_out=clip.src_out - max(0, clip.end - end) * clip.speed,
                audio_offset=clip.audio_offset + max(0, start - clip.start),
                video_offset=clip.video_offset + max(0, start - clip.start),
                video_duration=clip.video_duration or clip.duration,
            )
            for clip in self.clips
            if clip.start < end - 1e-9 and clip.end > start + 1e-9
        )
        return RenderPlan(
            clips,
            end - start,
            self.canvas,
            self.master,
            ("Выбран диапазон монтажа",),
            tuple(
                replace(
                    text,
                    start=max(0, text.start - start),
                    end=min(end, text.end or self.duration) - start,
                )
                for text in self.texts
                if text.start < end and (text.end or self.duration) > start
            ),
            self.limiter,
            self.canvas_source or (self.video[0] if self.video else None),
            self.ducking,
            tuple(
                replace(
                    mask,
                    start=max(0, mask.start - start),
                    end=min(end, mask.end) - start,
                )
                for mask in self.masks
                if mask.start < end and mask.end > start
            ),
        )

    @property
    def video(self) -> tuple[RenderClip, ...]:
        return tuple(
            sorted(
                (clip for clip in self.clips if clip.kind == "video"),
                key=lambda clip: clip.start,
            )
        )

    @property
    def sources(self) -> tuple[str, ...]:
        paths = [clip.path for clip in self.clips]
        paths.extend(item.image_path for item in self.texts if item.kind == "image")
        if self.canvas_source:
            paths.append(self.canvas_source.path)
        return tuple(dict.fromkeys(paths))
