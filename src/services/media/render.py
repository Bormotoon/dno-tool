"""Потоковый рендер связанного графа без служебных процессов и очередей кадров."""

from __future__ import annotations

import heapq
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from src.utils.system.media_runtime import load_pyav

from .graph import FilterGraph
from .render_types import MediaInput, RenderGraphPlan
from .types import MediaProgress, OperationContext


@dataclass
class InputFrames:
    spec: MediaInput
    stream: Any
    frames: Iterator[Any]
    origin: Fraction
    context: OperationContext

    def next(self) -> Any | None:
        self.context.check()
        for frame in self.frames:
            self.context.check()
            if frame.pts is None:
                raise ValueError("В декодированном потоке отсутствуют временные метки.")
            timestamp = float(frame.pts * frame.time_base - self.origin)
            if self.spec.end is not None and timestamp > self.spec.end + 0.1:
                return None
            return frame
        return None

    def scheduled(self, frame: Any) -> float:
        return (
            self.spec.timeline_start
            + max(0, float(frame.pts * frame.time_base - self.origin) - self.spec.start)
            / self.spec.speed
        )


def open_input(
    spec: MediaInput, stack: ExitStack, context: OperationContext
) -> InputFrames:
    av = load_pyav()
    context.check()
    container = stack.enter_context(av.open(spec.path, timeout=(5.0, 1.0)))
    streams = (
        container.streams.video if spec.kind == "video" else container.streams.audio
    )
    if not 0 <= spec.stream < len(streams):
        raise ValueError("Выбранный поток исходника отсутствует.")
    stream = streams[spec.stream]
    stream.codec_context.thread_count = 1
    origin = Fraction((stream.start_time or 0) * stream.time_base)
    if spec.start > 0:
        position = max(0.0, spec.start - 0.1)
        if spec.kind == "video" and stream.duration:
            position = min(
                position, max(0.0, float(stream.duration * stream.time_base) - 0.1)
            )
        container.seek(
            int((origin + Fraction(str(position))) / stream.time_base),
            stream=stream,
            backward=True,
        )
    return InputFrames(spec, stream, iter(container.decode(stream)), origin, context)


def encoder_acceleration(codec: str | None, device: str) -> Any:
    """PyAV сам загружает кадры в контекст выбранного кодировщика."""
    import os
    import re

    from av.codec.hwaccel import HWAccel

    if codec and codec.endswith(("_vaapi", "_qsv", "_d3d12va")):
        kind = codec.rsplit("_", 1)[1]
        selected = device or None
        if os.name != "nt" and kind in ("vaapi", "qsv"):
            selected = device or "/dev/dri/renderD128"
            if not re.fullmatch(r"/dev/dri/renderD[0-9]+", selected) or not os.access(
                selected, os.R_OK | os.W_OK
            ):
                raise ValueError("Выбранное DRM-устройство отсутствует или недоступно.")
        elif device:
            raise ValueError("Явный путь DRM-устройства поддерживается только в Linux.")
        return HWAccel(kind, device=selected, allow_software_fallback=False)
    if device:
        raise ValueError(
            "Явное DRM-устройство не применяется к выбранному кодировщику."
        )
    return None


def configure_video(output: Any, plan: RenderGraphPlan) -> Any:
    codec = plan.video_codec
    if codec is None:
        return None
    acceleration = encoder_acceleration(codec, plan.device)
    stream = output.add_stream(
        codec, rate=plan.fps, options=dict(plan.video_options), hwaccel=acceleration
    )
    stream.width, stream.height = plan.width, plan.height
    stream.time_base = 1 / plan.fps
    stream.codec_context.time_base = 1 / plan.fps
    stream.codec_context.thread_count = plan.threads
    stream.pix_fmt = "yuv420p"
    if acceleration is not None:
        stream.codec_context.sw_format = "nv12"
        stream.pix_fmt = "nv12"
    if plan.video_bitrate:
        stream.bit_rate = plan.video_bitrate
    stream.codec_context.open()
    if stream.codec_context.options:
        raise ValueError(
            "Кодировщик не применил параметры: "
            + ", ".join(sorted(stream.codec_context.options))
            + "."
        )
    return stream


def render_graph(
    plan: RenderGraphPlan,
    output_path: str,
    context: OperationContext,
    *,
    max_bytes: int | None = None,
) -> None:
    av = load_pyav()
    context.check()
    with ExitStack() as stack:
        sources = [open_input(spec, stack, context) for spec in plan.inputs]
        first = {source.spec.label: source.next() for source in sources}
        for source in sources:
            if first[source.spec.label] is None and source.spec.kind == "audio":
                codec = source.stream.codec_context
                silence = av.AudioFrame(
                    format=codec.format.name, layout=codec.layout.name, samples=1
                )
                silence.sample_rate = codec.sample_rate
                silence.time_base = Fraction(1, codec.sample_rate)
                silence.pts = round(
                    (source.origin + Fraction(str(source.spec.start)))
                    * codec.sample_rate
                )
                for plane in silence.planes:
                    plane.update(bytes(plane.buffer_size))
                first[source.spec.label] = silence
        if any(frame is None for frame in first.values()):
            raise ValueError(
                "Один из выбранных фрагментов не содержит доступных кадров."
            )
        outputs = {"aout": "audio"} if plan.audio_codec else {}
        if plan.video_codec:
            outputs["vout"] = "video"
        properties = {
            source.spec.label: (
                Fraction(
                    source.stream.base_rate or source.stream.average_rate or plan.fps
                ),
                Fraction(source.stream.sample_aspect_ratio or 1),
            )
            for source in sources
            if source.spec.kind == "video"
        }
        graph = FilterGraph(
            plan.description, first, outputs, video_properties=properties
        )
        options = (
            {"movflags": "+faststart"}
            if plan.faststart and Path(output_path).suffix.lower() in (".mp4", ".m4a")
            else {}
        )
        output = stack.enter_context(av.open(output_path, "w", options=options))
        video = configure_video(output, plan)
        audio = None
        resampler = None
        if plan.audio_codec:
            audio = output.add_stream(plan.audio_codec, rate=48000)
            audio.layout = "stereo"
            formats = audio.codec_context.codec.audio_formats
            audio.codec_context.format = next(
                (fmt.name for fmt in formats or [] if fmt.name == "fltp"),
                formats[0].name if formats else "s16",
            )
            if plan.audio_bitrate and not plan.audio_codec.startswith("pcm_"):
                audio.bit_rate = plan.audio_bitrate
            audio.codec_context.open()
            frame_size = audio.codec_context.frame_size
            if frame_size:
                graph.graph.set_audio_frame_size(frame_size)
            resampler = av.AudioResampler(
                format=audio.codec_context.format.name, layout="stereo", rate=48000
            )
        streams = {"aout": audio, "vout": video}
        pending: list[tuple[float, int, Any]] = []
        for index, source in enumerate(sources):
            graph.sources[source.spec.label].push(first[source.spec.label])
            frame = source.next()
            if frame is None:
                graph.sources[source.spec.label].push(None)
            else:
                heapq.heappush(pending, (source.scheduled(frame), index, frame))
        completed: set[str] = set()
        last_time = 0.0
        frames_count = 0
        last_advanced = time.monotonic()
        last_report = 0.0

        def encode(frame: Any, label: str) -> None:
            nonlocal last_time, frames_count, last_advanced
            context.check()
            timestamp = (
                float(frame.pts * frame.time_base) if frame.pts is not None else 0.0
            )
            if timestamp >= plan.duration - 1e-9:
                return
            if timestamp > last_time:
                last_time, last_advanced = timestamp, time.monotonic()
            stream = streams[label]
            if label == "vout":
                frame.pict_type = 0
                frame.pts = round(Fraction(frame.pts) * frame.time_base * plan.fps)
                frame.time_base = 1 / plan.fps
                frames_count += 1
                packets = stream.encode(frame)
                for packet in packets:
                    context.check()
                    output.mux(packet)
            else:
                assert resampler is not None
                for converted in resampler.resample(frame):
                    for packet in stream.encode(converted):
                        context.check()
                        output.mux(packet)

        while len(completed) < len(outputs):
            context.check()
            produced = False
            for label in outputs:
                if label in completed:
                    continue
                try:
                    frame = graph.sinks[label].pull()
                except av.error.BlockingIOError:
                    continue
                except av.error.EOFError:
                    completed.add(label)
                    continue
                encode(frame, label)
                produced = True
            now = time.monotonic()
            if now - last_advanced > 120:
                raise TimeoutError(
                    "Рендер более 120 с не продвигается по медиавремени."
                )
            if now - last_report > 0.1:
                written = (
                    Path(output_path).stat().st_size
                    if Path(output_path).exists()
                    else 0
                )
                if max_bytes is not None and written > max_bytes:
                    raise ValueError("Производный файл превысил допустимый размер.")
                context.report(
                    MediaProgress(
                        "encode",
                        min(plan.duration, last_time),
                        plan.duration,
                        frames_count,
                        written,
                    )
                )
                last_report = now
            if produced:
                continue
            if not pending:
                if len(completed) != len(outputs):
                    raise ValueError(
                        "Граф не завершился после конца всех входов: "
                        + ", ".join(
                            label for label in outputs if label not in completed
                        )
                        + "."
                    )
                break
            _, index, frame = heapq.heappop(pending)
            source = sources[index]
            try:
                graph.sources[source.spec.label].push(frame)
            except av.error.EOFError:
                continue
            following = source.next()
            if following is None:
                try:
                    graph.sources[source.spec.label].push(None)
                except av.error.EOFError:
                    pass
            else:
                heapq.heappush(pending, (source.scheduled(following), index, following))
        if resampler is not None:
            assert audio is not None
            for converted in resampler.resample(None):
                for packet in audio.encode(converted):
                    context.check()
                    output.mux(packet)
        for stream in (video, audio):
            if stream is not None:
                for packet in stream.encode(None):
                    context.check()
                    output.mux(packet)
        context.check()
    context.check()
    if max_bytes is not None and Path(output_path).stat().st_size > max_bytes:
        raise ValueError("Производный файл превысил допустимый размер.")
