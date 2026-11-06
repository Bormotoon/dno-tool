"""Описание входов и связанного графа, не содержащее аргументов CLI."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class MediaInput:
    label: str
    path: str
    kind: str
    stream: int = 0
    start: float = 0.0
    end: float | None = None
    timeline_start: float = 0.0
    speed: float = 1.0


@dataclass(frozen=True)
class RenderGraphPlan:
    inputs: tuple[MediaInput, ...]
    description: str
    width: int
    height: int
    fps: Fraction
    duration: float
    video_codec: str | None
    audio_codec: str | None
    video_options: dict[str, str]
    video_bitrate: int | None
    audio_bitrate: int | None
    faststart: bool
    device: str = ""
    threads: int = 4
