"""Перенос пакетов с единой временной осью и сохранением смещения потоков."""

# Замыкание записи выполняется синхронно до следующей итерации сегментов.
# ruff: noqa: B023

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO

from src.utils.system.media_runtime import load_pyav

from .types import MediaProgress, OperationContext


def remux_segments(
    segments: list[tuple[str, float, float | None]],
    output_path: str | BinaryIO,
    context: OperationContext,
    *,
    all_streams: bool = False,
    repair: bool = False,
    format_name: str | None = None,
    max_bytes: int | None = None,
) -> None:
    """PTS/DTS пересчитывает muxer PyAV, смещение задаётся в исходном time base.

    Обрезка Copy начинается с предыдущего ключевого кадра. Все потоки
    сегмента сдвигаются одинаково, отрицательные DTS сохраняют порядок B-frames.
    """
    av = load_pyav()
    context.check()
    total = sum(max(0, end - start) for _, start, end in segments if end is not None)
    options = {"avoid_negative_ts": "make_zero"}
    with av.open(output_path, "w", format=format_name, options=options) as output:
        templates: list[Any] = []
        cursor = Fraction(0)
        count = 0
        written = 0
        for source, start, end in segments:
            context.check()
            input_options = (
                {"fflags": "+genpts", "err_detect": "ignore_err"} if repair else {}
            )
            with av.open(
                source, timeout=(5.0, 1.0), options=input_options
            ) as container:
                selected = (
                    list(container.streams)
                    if all_streams
                    else [*container.streams.video[:1], *container.streams.audio[:1]]
                )
                if not selected:
                    raise ValueError("В исходнике нет доступных потоков для Copy.")
                if not templates:
                    templates = [
                        output.add_stream_from_template(stream) for stream in selected
                    ]
                if len(selected) != len(templates):
                    raise ValueError("Состав потоков сегментов различается.")
                streams = {
                    stream.index: template
                    for stream, template in zip(selected, templates)
                }
                origin = Fraction(container.start_time or 0, av.time_base)
                reference = next(
                    (s for s in selected if s.type == "video"), selected[0]
                )
                if start:
                    container.seek(
                        int((origin + Fraction(str(start))) / reference.time_base),
                        stream=reference,
                        backward=True,
                        any_frame=False,
                    )
                target_end = origin + Fraction(str(end)) if end is not None else None
                packets = iter(container.demux(selected))
                initial: list[Any] = []
                seen: set[int] = set()
                media_indices = {
                    stream.index
                    for stream in selected
                    if stream.type in ("video", "audio")
                }
                initial_bytes = 0
                for packet in packets:
                    context.check()
                    if packet.dts is None:
                        continue
                    initial.append(packet)
                    seen.add(packet.stream.index)
                    initial_bytes += packet.size
                    if media_indices <= seen:
                        break
                    if len(initial) >= 1024 or initial_bytes > 16 * 1024**2:
                        raise ValueError(
                            "Не удалось синхронизировать начало потоков Copy."
                        )
                if not initial:
                    raise ValueError("В выбранном фрагменте нет пакетов.")
                video_start = [
                    Fraction(p.pts * p.time_base)
                    for p in initial
                    if p.stream.type == "video" and p.pts is not None
                ]
                base = (
                    min(video_start) if video_start else origin + Fraction(str(start))
                )
                end_time = cursor
                ended: set[int] = set()
                filters: dict[int, Any] = {}
                # Muxer включает auto_bsf; явная конверсия AAC нужна ADTS → MP4.
                if (
                    Path(str(output_path)).suffix.lower() in (".mp4", ".m4a")
                    and container.format.name == "aac"
                ):
                    for stream in selected:
                        if (
                            stream.type == "audio"
                            and stream.codec_context.name == "aac"
                        ):
                            filters[stream.index] = av.BitStreamFilterContext(
                                "aac_adtstoasc", stream, streams[stream.index]
                            )

                def write(packet: Any) -> None:
                    nonlocal end_time, count, written
                    context.check()
                    index = packet.stream.index
                    if packet.dts is None or packet.time_base is None:
                        return
                    timestamp = Fraction(packet.dts * packet.time_base)
                    if (
                        not repair
                        and packet.stream.type == "audio"
                        and packet.pts is not None
                        and packet.pts * packet.time_base
                        < origin + Fraction(str(start))
                    ):
                        return
                    if target_end is not None and timestamp >= target_end:
                        ended.add(index)
                        return
                    shift = round((cursor - base) / packet.time_base)
                    packet.dts += shift
                    if packet.pts is not None:
                        packet.pts += shift
                    end_time = max(
                        end_time,
                        Fraction(
                            (
                                (packet.pts if packet.pts is not None else packet.dts)
                                + (packet.duration or 0)
                            )
                            * packet.time_base
                        ),
                    )
                    packet.stream = streams[index]
                    filtered = (
                        filters[index].filter(packet) if index in filters else [packet]
                    )
                    for item in filtered:
                        context.check()
                        written += item.size
                        if max_bytes is not None and written > max_bytes:
                            raise ValueError("Проверка Copy превысила бюджет записи.")
                        output.mux(item)
                    count += 1
                    if count % 64 == 0:
                        context.report(
                            MediaProgress(
                                "copy", float(end_time), total or None, count, written
                            )
                        )

                for packet in initial:
                    write(packet)
                for packet in packets:
                    write(packet)
                    if media_indices <= ended:
                        break
                for index, bsf in filters.items():
                    for packet in bsf.filter(None):
                        packet.stream = streams[index]
                        output.mux(packet)
                cursor = end_time
                context.check()
