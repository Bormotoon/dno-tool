"""Совместимые утилиты редактора поверх единственного локального движка PyAV."""

from __future__ import annotations
import threading
from pathlib import Path
from typing import Any
from collections.abc import Sequence
from src.domain.video.metadata import ProbeMetadata, RawProbeMetadata
from src.services.media import get_media_service, OperationContext
from src.services.media.filter_options import (
    default_fontfile,
    escape_drawtext,
    _filter_option,
    _norm_drawtext_color,
    _escape_drawtext_value,
    _clamp_margin,
    _clamp_pad,
    overlay_rect,
    overlay_origin,
    build_drawtext_filter,
    build_drawtext_filters,
)


def media_available() -> bool:
    from importlib.util import find_spec
    from src.utils.system.media_runtime import media_runtime_root

    return (
        find_spec("av") is not None
        and (media_runtime_root() / "media-runtime.json").is_file()
    )


def detect_weird(data: RawProbeMetadata) -> list[str]:
    return [] if data.get("streams") else ["потоки не найдены"]


def probe_media(
    path: str, timeout: float = 30.0, cancelled: threading.Event | None = None
) -> ProbeMetadata:
    return get_media_service().probe(
        path, OperationContext.with_timeout(timeout, cancelled)
    )


def repair_file(path: str, cancel: threading.Event | None = None) -> tuple[bool, str]:
    from .repair import ContainerRepair

    return ContainerRepair.run(path, cancel)


def detect_hw_encoders() -> dict[str, bool]:
    encoders = get_media_service().capabilities().encoders
    return {
        f"{backend}_{codec}": f"{codec}_{backend}" in encoders
        for backend in ("nvenc", "qsv", "amf", "vaapi")
        for codec in ("h264", "hevc")
    }


def check_drawtext() -> bool:
    return "drawtext" in get_media_service().capabilities().filters


def format_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    return f"{int(seconds//3600):02d}:{int(seconds%3600//60):02d}:{seconds%60:06.3f}"


def build_reencode_command(
    segments: Sequence[
        tuple[str, float, float]
        | tuple[str, float, float, float]
        | tuple[str, float, float, float, bool]
    ],
    out_path: str,
    params: dict[str, Any],
) -> Any:
    """Совместимый вход для последовательных сегментов через общий план."""
    from src.domain.video.render_plan import RenderCanvas, RenderClip, RenderPlan
    from .render_command import RenderCommand

    metadata = dict(params.get("metadata") or {})
    clips = []
    cursor = 0.0
    for index, segment in enumerate(segments):
        path, start, end = segment[:3]
        gain = float(segment[3]) if len(segment) > 3 else 1.0
        mute = bool(segment[4]) if len(segment) > 4 else False
        meta = metadata.get(path)
        if meta is None:
            meta = probe_media(path)
            metadata[path] = meta
        if not meta.get("has_video"):
            raise ValueError("Исходник не содержит видеопотока.")
        clips.append(
            RenderClip(f"v{index}", "video", path, "video", cursor, start, end)
        )
        if (params.get("audio_presence") or {}).get(path, meta.get("has_audio", False)):
            clips.append(
                RenderClip(
                    f"a{index}",
                    "audio",
                    path,
                    "audio",
                    cursor,
                    start,
                    end,
                    gain=gain,
                    mute=mute,
                )
            )
        cursor += end - start
    plan = RenderPlan(
        tuple(clips), cursor, RenderCanvas(), float(params.get("master_volume", 1))
    )
    return RenderCommand.build(plan, out_path, metadata, params)
