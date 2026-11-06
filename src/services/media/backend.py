"""Локальные операции PyAV; контейнеры и кадры освобождает поток операции."""

from __future__ import annotations

import hashlib
import math
import struct
from fractions import Fraction
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from av import VideoFrame
    from PySide6.QtGui import QImage

from src.domain.video.media_cache import MediaDiskCache
from src.domain.video.metadata import ProbeMetadata, RawProbeMetadata, StreamMetadata
from src.utils.system.media_runtime import load_pyav

from .metadata import normalize_metadata
from .types import OperationContext


def rotation_of(stream: Any) -> int:
    """Считать display matrix через поддерживаемое свойство PyAV 19."""
    data = stream.codec_context.coded_side_data.get("display_matrix")
    if data and len(data) == 36:
        matrix = struct.unpack("=9i", data)
        return round(-math.degrees(math.atan2(matrix[1], matrix[0]))) % 360
    try:
        return round(float(stream.metadata.get("rotate", 0))) % 360
    except (TypeError, ValueError):
        return 0


def stream_metadata(stream: Any) -> StreamMetadata:
    ctx = stream.codec_context
    result: dict[str, Any] = {
        "index": stream.index,
        "codec_type": stream.type,
        "codec_name": ctx.name if ctx else "",
        "time_base": str(stream.time_base),
        "tags": dict(stream.metadata),
    }
    if ctx is None:
        return cast(StreamMetadata, result)
    extra = ctx.extradata or b""
    result.update(
        codec_tag_string=ctx.codec_tag,
        profile=ctx.profile,
        extradata_size=len(extra),
        extradata_hash="SHA256:" + hashlib.sha256(extra).hexdigest() if extra else "",
    )
    for key in ("start_time", "duration"):
        value = getattr(stream, key)
        if value is not None and stream.time_base:
            result[key] = str(float(value * stream.time_base))
    if stream.type == "video":
        result.update(
            width=ctx.width,
            height=ctx.height,
            pix_fmt=ctx.format.name if ctx.format else "",
            avg_frame_rate=str(stream.average_rate or Fraction(0)),
            r_frame_rate=str(stream.base_rate or Fraction(0)),
            sample_aspect_ratio=f"{Fraction(stream.sample_aspect_ratio or 1).numerator}:{Fraction(stream.sample_aspect_ratio or 1).denominator}",
            level=ctx.level,
            color_space={
                0: "gbr",
                1: "bt709",
                2: "unknown",
                4: "fcc",
                5: "bt470bg",
                6: "smpte170m",
                7: "smpte240m",
                8: "ycgco",
                9: "bt2020nc",
                10: "bt2020c",
                11: "smpte2085",
                14: "ictcp",
            }.get(ctx.colorspace, str(ctx.colorspace)),
            color_range={0: "unknown", 1: "tv", 2: "pc"}.get(
                ctx.color_range, str(ctx.color_range)
            ),
            color_primaries=ctx.color_primaries,
            color_transfer=ctx.color_trc,
            chroma_location=ctx.chroma_sample_location,
            field_order={
                0: "unknown",
                1: "progressive",
                2: "tt",
                3: "bb",
                4: "tb",
                5: "bt",
            }.get(ctx.field_order, str(ctx.field_order)),
            side_data_list=[{"rotation": rotation_of(stream)}],
        )
    elif stream.type == "audio":
        result.update(
            sample_rate=str(ctx.sample_rate),
            channels=ctx.channels,
            channel_layout=ctx.layout.name,
            sample_fmt=ctx.format.name if ctx.format else "",
            frame_size=ctx.frame_size,
        )
    return cast(StreamMetadata, result)


class PyAVBackend:
    """Единственная реализация локального движка, без поиска CLI."""

    def probe(self, path: str, context: OperationContext) -> ProbeMetadata:
        av = load_pyav()
        context.check()
        fingerprint = MediaDiskCache.fingerprint(path)
        with av.open(
            path,
            timeout=(5.0, 1.0),
            options={"probesize": "33554432", "analyzeduration": "10000000"},
        ) as container:
            context.check()
            data: RawProbeMetadata = {
                "streams": [stream_metadata(s) for s in container.streams],
                "format": {"format_name": container.format.name},
            }
            if container.duration is not None:
                data["format"]["duration"] = str(container.duration / av.time_base)
            if container.start_time is not None:
                data["format"]["start_time"] = str(container.start_time / av.time_base)
            result = normalize_metadata(path, data)
            result["duration_known"] = container.duration is not None
        context.check()
        if MediaDiskCache.fingerprint(path) != fingerprint:
            raise ValueError("Исходник изменился во время анализа.")
        result["_source_fingerprint"] = fingerprint
        return result

    def frame(
        self,
        path: str,
        seconds: float,
        stream_index: int,
        context: OperationContext,
        *,
        filter_chain: str = "",
    ) -> VideoFrame:
        """Вернуть принадлежащий вызывающему PyAV-кадр без NumPy/Pillow.

        Seek идёт к предыдущему ключевому кадру; отсчёт позиции совпадает
        с началом выбранного видеопотока. QImage на границе делает copy().
        """
        av = load_pyav()
        from .graph import FilterGraph

        context.check()
        fingerprint = MediaDiskCache.fingerprint(path)
        with av.open(path, timeout=(5.0, 1.0)) as container:
            if not 0 <= stream_index < len(container.streams.video):
                raise ValueError("Выбранный видеопоток отсутствует.")
            stream = container.streams.video[stream_index]
            stream.codec_context.thread_count = 1
            target = (
                Fraction(str(max(0.0, seconds)))
                + (stream.start_time or 0) * stream.time_base
            )
            if (
                stream.duration is not None
                and target
                >= ((stream.start_time or 0) + stream.duration) * stream.time_base
            ):
                raise ValueError("Выбранная позиция находится за концом видеопотока.")
            container.seek(
                int(target / stream.time_base),
                stream=stream,
                backward=True,
                any_frame=False,
            )
            selected = None
            for frame in container.decode(stream):
                context.check()
                selected = frame
                if frame.pts is not None and frame.pts * frame.time_base >= target:
                    break
            if selected is None:
                raise ValueError("В выбранной позиции нет доступного кадра.")
            rotate = rotation_of(stream)
            transforms = {
                90: "transpose=cclock",
                180: "hflip,vflip",
                270: "transpose=clock",
            }
            filters = [transforms[rotate]] if rotate in transforms else []
            if filter_chain:
                filters.append(filter_chain)
            if filters:
                graph = FilterGraph(
                    "[in]" + ",".join(filters) + "[out]",
                    {"in": selected},
                    {"out": "video"},
                    threads=1,
                    video_properties={
                        "in": (
                            Fraction(stream.base_rate or stream.average_rate or 25),
                            Fraction(stream.sample_aspect_ratio or 1),
                        )
                    },
                )
                graph.sources["in"].push(selected)
                graph.sources["in"].push(None)
                selected = graph.sinks["out"].pull()
        context.check()
        if MediaDiskCache.fingerprint(path) != fingerprint:
            raise ValueError("Исходник изменился при чтении кадра.")
        return cast("VideoFrame", selected)

    @staticmethod
    def write_png(frame: VideoFrame, output: str, context: OperationContext) -> None:
        av = load_pyav()
        context.check()
        with av.open(output, "w", format="image2") as container:
            stream = container.add_stream("png", rate=1)
            stream.width, stream.height = frame.width, frame.height
            pixel_format = (
                "rgba"
                if any(component.is_alpha for component in frame.format.components)
                else "rgb24"
            )
            stream.pix_fmt = pixel_format
            rgb = frame.reformat(format=pixel_format)
            rgb.pts, rgb.time_base = 0, Fraction(1)
            for packet in stream.encode(rgb):
                context.check()
                container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)

    def waveform(
        self, path: str, output: str, stream_index: int, context: OperationContext
    ) -> None:
        """Два прохода и 600 колонок вместо хранения всего аудио внутри showwavespic.

        Суммируются модули S16, как в showwavespic FFmpeg 9: последняя
        колонка получает остаток. Сам растр по-прежнему создаёт libavfilter.
        """
        from array import array

        from .graph import FilterGraph

        av = load_pyav()
        fingerprint = MediaDiskCache.fingerprint(path)
        with av.open(path, timeout=(5.0, 1.0)) as container:
            if not 0 <= stream_index < len(container.streams.audio):
                raise ValueError("Выбранный аудиопоток отсутствует.")
            stream = container.streams.audio[stream_index]
            stream.codec_context.thread_count = 1
            total = 0
            for frame in container.decode(stream):
                context.check()
                total += frame.samples
            width = 600
            if total < width:
                raise ValueError("Недостаточно звуковых отсчётов для формы волны.")
            per_column = total // width
            channels = stream.codec_context.channels
            if not 1 <= channels <= 64:
                raise ValueError("Число аудиоканалов превышает допустимое для волны.")
            means = array("h", [0]) * (width * channels)
            sums = [0] * channels
            column = used = seen = 0
            resampler = av.AudioResampler(
                format="s16", layout=stream.codec_context.layout
            )

            def consume(frame: Any) -> None:
                nonlocal column, used, seen
                seen += frame.samples
                values = memoryview(frame.planes[0]).cast("h")
                index = 0
                while index < frame.samples and column < width:
                    context.check()
                    size = (
                        per_column
                        if column < width - 1
                        else total - per_column * (width - 1)
                    )
                    take = min(frame.samples - index, size - used)
                    for channel in range(channels):
                        sums[channel] += sum(
                            map(
                                abs,
                                values[
                                    index * channels
                                    + channel : (index + take) * channels : channels
                                ],
                            )
                        )
                    used += take
                    index += take
                    if used == size:
                        for channel in range(channels):
                            value = sums[channel] // size
                            means[column * channels + channel] = (
                                value if value < 32768 else -32768
                            )
                            sums[channel] = 0
                        column += 1
                        used = 0

            # Новый demuxer повторно применяет codec delay/skip_samples.
            # Seek в начало MKV может выдать дополнительный priming-кадр AAC.
            with av.open(path, timeout=(5.0, 1.0)) as second:
                selected = second.streams.audio[stream_index]
                selected.codec_context.thread_count = 1
                for frame in second.decode(selected):
                    context.check()
                    for converted in resampler.resample(frame):
                        consume(converted)
            for converted in resampler.resample(None):
                consume(converted)
            if column != width or seen != total:
                raise ValueError("Число отсчётов изменилось между проходами анализа.")
            summary = av.AudioFrame(
                format="s16", layout=stream.codec_context.layout.name, samples=width
            )
            summary.sample_rate = stream.codec_context.sample_rate
            summary.time_base = Fraction(1, summary.sample_rate)
            summary.pts = 0
            payload = means.tobytes()
            summary.planes[0].update(
                payload + bytes(summary.planes[0].buffer_size - len(payload))
            )
            graph = FilterGraph(
                "[in]showwavespic=s=600x80:colors=#1565c0[out]",
                {"in": summary},
                {"out": "video"},
                threads=1,
            )
            graph.sources["in"].push(summary)
            graph.sources["in"].push(None)
            self.write_png(graph.sinks["out"].pull(), output, context)
        if MediaDiskCache.fingerprint(path) != fingerprint:
            raise ValueError("Исходник изменился при подготовке формы волны.")

    def peak(
        self,
        path: str,
        stream_index: int,
        start: float,
        end: float,
        context: OperationContext,
    ) -> float:
        """Взять измеренный astats пик из метаданных кадров, не из stderr."""
        from .graph import FilterGraph

        av = load_pyav()
        fingerprint = MediaDiskCache.fingerprint(path)
        peak = -math.inf
        with av.open(path, timeout=(5.0, 1.0)) as container:
            stream = container.streams.audio[stream_index]
            stream.codec_context.thread_count = 1
            origin = (stream.start_time or 0) * stream.time_base
            container.seek(
                int((origin + Fraction(str(start))) / stream.time_base),
                stream=stream,
                backward=True,
            )
            graph = None
            for frame in container.decode(stream):
                context.check()
                if graph is None:
                    graph = FilterGraph(
                        f"[in]asetpts=PTS-{float(origin):.9f}/TB,atrim=start={start:.9f}:end={end:.9f},astats=metadata=1:reset=0[out]",
                        {"in": frame},
                        {"out": "audio"},
                        threads=1,
                    )
                try:
                    graph.sources["in"].push(frame)
                except av.error.EOFError:
                    break
                while True:
                    context.check()
                    try:
                        analyzed = graph.sinks["out"].pull()
                    except (av.error.BlockingIOError, av.error.EOFError):
                        break
                    value = analyzed.metadata.get("lavfi.astats.Overall.Peak_level")
                    if value is not None:
                        peak = float(value)
                if (
                    frame.pts is not None
                    and frame.pts * frame.time_base - origin >= end
                ):
                    break
        if MediaDiskCache.fingerprint(path) != fingerprint:
            raise ValueError("Исходник изменился во время измерения.")
        return peak


def frame_to_qimage(frame: VideoFrame) -> QImage:
    """Независимая копия буфера нужна Qt после освобождения кадра в воркере."""
    from PySide6.QtGui import QImage

    rgb = frame.reformat(format="rgb24", threads=1)
    return QImage(
        memoryview(rgb.planes[0]),
        rgb.width,
        rgb.height,
        rgb.planes[0].line_size,
        QImage.Format.Format_RGB888,
    ).copy()
