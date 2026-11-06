"""Нормализация метаданных с сохранением совместимости монтажных проектов."""

from __future__ import annotations

import hashlib
import json

from src.domain.video.metadata import ProbeMetadata, RawProbeMetadata


def normalize_metadata(path: str, data: RawProbeMetadata) -> ProbeMetadata:
    result: ProbeMetadata = {"path": path}
    result["video_streams"] = [
        stream
        for stream in data.get("streams", [])
        if stream.get("codec_type") == "video"
    ]
    result["audio_streams"] = [
        stream
        for stream in data.get("streams", [])
        if stream.get("codec_type") == "audio"
    ]
    result["stream_counts"] = {
        kind: sum(
            stream.get("codec_type") == kind for stream in data.get("streams", [])
        )
        for kind in ("video", "audio", "subtitle", "data", "attachment")
    }
    fmt = data.get("format", {})
    try:
        result["duration"] = float(fmt.get("duration", 0) or 0)
    except ValueError:
        result["duration"] = 0.0
    try:
        result["start_time"] = float(fmt.get("start_time", 0) or 0)
    except ValueError:
        result["start_time"] = 0.0
    for s in data.get("streams", []):
        ctype = s.get("codec_type")
        if ctype == "video" and "width" not in result:
            result["width"] = int(s.get("width", 0) or 0)
            result["height"] = int(s.get("height", 0) or 0)
            result["vcodec"] = s.get("codec_name", "")
            fps_raw = s.get("avg_frame_rate", "0/1")
            try:
                num, den = fps_raw.split("/")
                result["fps"] = float(num) / float(den) if float(den) else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                result["fps"] = 0.0
            result["has_video"] = True
            try:
                result["vstart"] = float(s.get("start_time", 0) or 0)
            except ValueError:
                result["vstart"] = 0.0
        elif ctype == "audio":
            # Первый аудиопоток задаёт кодек; каналы берём из известной раскладки.
            if "acodec" not in result:
                result["acodec"] = s.get("codec_name", "")
                result["has_audio"] = True
                try:
                    result["astart"] = float(s.get("start_time", 0) or 0)
                except ValueError:
                    result["astart"] = 0.0
            try:
                ch = int(s.get("channels", 0) or 0)
            except (TypeError, ValueError):
                ch = 0
            layout = str(s.get("channel_layout") or s.get("ch_layout") or "")
            if ch > 0 and result.get("channels", 0) in (0, None):
                result["channels"] = ch
                result["channel_layout"] = layout
    result.setdefault("has_video", False)
    result.setdefault("has_audio", False)
    result.setdefault("channels", 0)
    result.setdefault("channel_layout", "")
    result.setdefault("start_time", 0.0)
    result.setdefault("vstart", 0.0)
    result.setdefault("astart", 0.0)
    result["weird"] = [] if data.get("streams") else ["потоки не найдены"]
    video = next(
        (
            stream
            for stream in data.get("streams", [])
            if stream.get("codec_type") == "video"
        ),
        None,
    )
    audio = next(
        (
            stream
            for stream in data.get("streams", [])
            if stream.get("codec_type") == "audio"
        ),
        None,
    )
    signatures: list[dict[str, object] | None] = []
    complete = video is not None
    for stream, required, optional in (
        (
            video,
            (
                "codec_name",
                "width",
                "height",
                "time_base",
                "pix_fmt",
                "avg_frame_rate",
                "sample_aspect_ratio",
            ),
            (
                "codec_tag_string",
                "profile",
                "level",
                "r_frame_rate",
                "color_space",
                "color_range",
                "field_order",
            ),
        ),
        (
            audio,
            ("codec_name", "sample_rate", "channels", "time_base"),
            (
                "codec_tag_string",
                "profile",
                "sample_fmt",
                "channel_layout",
                "ch_layout",
                "frame_size",
            ),
        ),
    ):
        if stream is None:
            signatures.append(None)
            continue
        if any(stream.get(key) in (None, "", "N/A", "0/0") for key in required):
            complete = False
        extra = int(stream.get("extradata_size", 0) or 0)
        if extra > 0 and not stream.get("extradata_hash"):
            complete = False
        signature: dict[str, object] = {
            key: stream.get(key)
            for key in (*required, *optional, "extradata_size", "extradata_hash")
        }
        signature["rotation"] = [
            item.get("rotation")
            for item in stream.get("side_data_list", [])
            if "rotation" in item
        ]
        signatures.append(signature)
    result["copy_signature"] = (
        hashlib.sha256(
            json.dumps(signatures, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if complete
        else ""
    )
    return result
