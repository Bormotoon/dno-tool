"""RTSP TCP через avformat с тайм-аутами и владением контейнером в потоке."""

from __future__ import annotations

import time
from collections.abc import Generator
from fractions import Fraction
from typing import Any, BinaryIO

from src.utils.system.camera_decoding import CameraDecoder
from src.utils.system.media_runtime import load_pyav

from .camera_presentation import camera_frame_size
from .types import MediaProgress, OperationContext, RecordOptions


def open_camera(url: str, timeout: float, *, hwaccel: Any = None) -> Any:
    av = load_pyav()
    if not url.lower().startswith(("rtsp://", "rtsps://")):
        raise ValueError("Для камеры требуется адрес RTSP.")
    return av.open(
        url,
        hwaccel=hwaccel,
        timeout=(timeout, timeout),
        options={
            "rtsp_transport": "tcp",
            "timeout": str(round(timeout * 1000000)),
            "threads": "1",
        },
    )


def camera_frames(
    url: str,
    size: tuple[int, int] | None,
    context: OperationContext,
    timeout: float,
    *,
    decoder: CameraDecoder | None = None,
) -> Generator[Any, None, None]:
    """Удерживать аппаратные кадры и контейнер в потоке до отбора для показа."""
    while True:
        context.check()
        hardware = bool(decoder and decoder.policy.device_type and not decoder.failed)
        try:
            acceleration = decoder.acceleration() if decoder else None
            with open_camera(url, timeout, hwaccel=acceleration) as container:
                if not container.streams.video:
                    raise ValueError("RTSP-поток не содержит видео.")
                stream = container.streams.video[0]
                stream.codec_context.thread_count = 1
                for frame in container.decode(stream):
                    context.check()
                    if hardware and not stream.codec_context.is_hwaccel:
                        assert decoder is not None
                        decoder.fallback()
                        hardware = False
                    if size:
                        width, height = camera_frame_size(
                            (frame.width, frame.height), size, size
                        )
                        frame = frame.reformat(
                            width=width, height=height, format="rgb24", threads=1
                        )
                    yield frame
            return
        except InterruptedError:
            raise
        except Exception:
            if not hardware or decoder is None:
                raise
            decoder.fallback()


_PCM_FALLBACK_AUDIO = ("pcm_alaw", "pcm_mulaw")
_VIDEO_ENCODERS = {
    "h264": ("h264_nvenc", "h264_qsv", "h264_amf", "libx264"),
    "h265": ("hevc_nvenc", "hevc_qsv", "hevc_amf", "libx265"),
}
_AUDIO_ENCODERS = {"aac": "aac", "opus": "libopus", "flac": "flac"}
_OPUS_RATES = (8000, 12000, 16000, 24000, 48000)
_TIME_BASE = Fraction(1, 90000)


def _signature(selected: list[Any]) -> list[tuple[int, str, str]]:
    """Индекс, тип и кодек потоков: по ним проверяется совместимость при переподключении."""
    return [
        (stream.index, stream.type, stream.codec_context.name) for stream in selected
    ]


def _select_streams(container: Any, audio: bool = True) -> list[Any]:
    kinds = ("video", "audio") if audio else ("video",)
    selected = [stream for stream in container.streams if stream.type in kinds]
    if not any(stream.type == "video" for stream in selected):
        raise ValueError("RTSP-поток не содержит видео.")
    return selected


def _layout_name(stream: Any) -> str:
    """Стандартное имя раскладки: у G.711 она приходит как «1 channels»."""
    name = stream.codec_context.layout.name
    head = name.split()[0] if name else ""
    if head.isdigit():
        return {"1": "mono", "2": "stereo"}.get(head, name)
    return name


_SOFT_PRESETS = {"fast": "veryfast", "balanced": "medium", "quality": "slow"}
_NVENC_PRESETS = {"fast": "p2", "balanced": "p4", "quality": "p6"}
_QSV_PRESETS = {"fast": "veryfast", "balanced": "medium", "quality": "slower"}
_AMF_QUALITY = {"fast": "speed", "balanced": "balanced", "quality": "quality"}


def _encoder_options(name: str, options: RecordOptions) -> dict[str, str]:
    """Параметры кодера; у аппаратных они называются по-разному.

    Битрейт задаётся отдельно через ``bit_rate`` контекста: при ненулевом
    ``video_bitrate`` параметры постоянного качества не передаются.
    """
    crf = str(options.crf)
    by_bitrate = options.video_bitrate > 0
    preset = options.preset
    if name.endswith("_nvenc"):
        result = {"preset": _NVENC_PRESETS[preset], "rc": "vbr"}
        if not by_bitrate:
            result["cq"] = crf
        return result
    if name.endswith("_qsv"):
        result = {"preset": _QSV_PRESETS[preset]}
        if not by_bitrate:
            result["global_quality"] = crf
        return result
    if name.endswith("_amf"):
        result = {"quality": _AMF_QUALITY[preset]}
        if by_bitrate:
            result["rc"] = "vbr_latency"
        else:
            result.update({"rc": "cqp", "qp_i": crf, "qp_p": crf})
        return result
    result = {"preset": _SOFT_PRESETS[preset]}
    if not by_bitrate:
        result["crf"] = crf
    if name == "libx265":
        result["x265-params"] = "log-level=error"
    return result


def _fit_size(width: int, height: int, limit: str) -> tuple[int, int]:
    """Вписать кадр в потолок ``WxH`` без увеличения; стороны чётные для yuv420p."""
    if limit != "source":
        max_w, max_h = (int(part) for part in limit.split("x"))
        scale = min(1.0, max_w / width, max_h / height)
        width, height = int(width * scale), int(height * scale)
    return max(2, width // 2 * 2), max(2, height // 2 * 2)


def _pick_encoder(av: Any, options: RecordOptions, width: int, height: int) -> str:
    """Выбрать первый кодер, который реально открывается; программный — запасной."""
    names = _VIDEO_ENCODERS[options.video]
    candidates = names if options.hardware else names[-1:]
    for name in candidates:
        if name not in av.codecs_available:
            continue
        probe = None
        try:
            probe = av.codec.CodecContext.create(name, "w")
            probe.width, probe.height = width, height
            probe.pix_fmt = "yuv420p"
            probe.time_base = _TIME_BASE
            probe.framerate = Fraction(25)
            probe.options = _encoder_options(name, options)
            if options.video_bitrate > 0:
                probe.bit_rate = options.video_bitrate * 1000
            probe.open()
            return name
        except Exception:
            # Нет устройства, драйвера или свободной сессии кодирования
            # (у видеокарт их число ограничено) — берём следующий кодер.
            continue
        finally:
            probe = None
    raise ValueError(
        "Кодер для записи недоступен: "
        + ("H.265" if options.video == "h265" else "H.264")
    )


def _reconnect(
    old: Any,
    url: str,
    timeout: float,
    context: OperationContext,
    signature: list[tuple[int, str, str]],
    audio: bool,
    deadline: float,
) -> Any | None:
    """Переоткрыть RTSP до ``deadline``; None, если не вышло или кодеки изменились."""
    try:
        old.close()
    except Exception:
        pass
    delay = 1.0
    while time.monotonic() < deadline:
        # Ожидание прерывается отменой записи.
        if context.cancelled.wait(delay):
            raise InterruptedError("Обработка отменена.")
        context.check()
        delay = min(delay * 2, 5.0)
        try:
            container = open_camera(url, timeout)
        except Exception:
            continue
        try:
            if _signature(_select_streams(container, audio)) != signature:
                container.close()
                return None
            return container
        except Exception:
            try:
                container.close()
            except Exception:
                pass
            return None
    return None


def record_camera(
    url: str,
    output_file: BinaryIO,
    context: OperationContext,
    timeout: float,
    options: RecordOptions | None = None,
) -> None:
    """Записать RTSP в MKV.

    Видео: ``copy`` берёт кодек из потока без перекодирования, ``h264``/``h265``
    перекодируют (программно или аппаратно). Звук: ``auto`` копирует то, что
    принимает Matroska (G.711 и прочее приводится к PCM), ``aac``/``pcm``
    перекодируют, ``none`` отбрасывает. При обрыве в течение
    ``reconnect_grace`` секунд поток открывается заново и запись продолжается
    в тот же файл с сохранением реального времени; новый файл нужен, только
    если связь не вернулась или кодеки потока изменились.
    """
    options = options or RecordOptions()
    av = load_pyav()
    context.check()
    use_audio = options.audio != "none"
    container = open_camera(url, timeout)
    try:
        selected = _select_streams(container, use_audio)
        signature = _signature(selected)
        with av.open(
            output_file,
            "w",
            format="matroska",
            options={
                "avoid_negative_ts": "make_zero",
                "flush_packets": "1",
                "cluster_time_limit": "1000",
            },
        ) as output:
            streams: dict[int, Any] = {}
            audio_converters: dict[int, Any] = {}
            video_size: dict[int, tuple[int, int]] = {}
            min_frame_gap: dict[int, Fraction] = {}

            def add_audio(stream: Any, codec: str) -> None:
                rate = options.audio_rate or stream.codec_context.sample_rate
                if codec == "libopus" and rate not in _OPUS_RATES:
                    rate = 48000  # Opus не принимает 44,1 и 32 кГц
                layout = {1: "mono", 2: "stereo"}.get(
                    options.audio_channels, _layout_name(stream)
                )
                target = output.add_stream(codec, rate=rate)
                target.layout = layout
                if codec in ("aac", "libopus"):
                    target.codec_context.bit_rate = options.audio_bitrate * 1000
                streams[stream.index] = target
                if codec == "aac":
                    audio_converters[stream.index] = av.AudioResampler(
                        format="fltp", layout=layout, rate=rate, frame_size=1024
                    )
                elif codec == "libopus":
                    audio_converters[stream.index] = av.AudioResampler(
                        format="s16", layout=layout, rate=rate, frame_size=rate // 50
                    )
                else:
                    audio_converters[stream.index] = av.AudioResampler(
                        format="s16", layout=layout, rate=rate
                    )

            for stream in selected:
                if stream.type == "video":
                    if options.video == "copy":
                        streams[stream.index] = output.add_stream_from_template(stream)
                        continue
                    context_ = stream.codec_context
                    width, height = _fit_size(
                        context_.width, context_.height, options.resolution
                    )
                    name = _pick_encoder(av, options, width, height)
                    rate = stream.average_rate or Fraction(25)
                    if options.max_fps > 0 and rate > options.max_fps:
                        rate = Fraction(options.max_fps)
                        # Пропускаем кадры, пришедшие раньше 90% периода.
                        min_frame_gap[stream.index] = (
                            Fraction(1) / rate / _TIME_BASE * Fraction(9, 10)
                        )
                    target =output.add_stream(name, rate=rate)
                    target.width, target.height = width, height
                    target.pix_fmt = "yuv420p"
                    target.codec_context.time_base = _TIME_BASE
                    target.codec_context.gop_size = max(
                        1, round(float(rate) * options.gop)
                    )
                    target.codec_context.options = _encoder_options(name, options)
                    if options.video_bitrate > 0:
                        target.codec_context.bit_rate = options.video_bitrate * 1000
                    streams[stream.index] = target
                    video_size[stream.index] = (width, height)
                elif options.audio in _AUDIO_ENCODERS:
                    add_audio(stream, _AUDIO_ENCODERS[options.audio])
                elif (
                    options.audio == "pcm"
                    or options.audio_rate
                    or options.audio_channels
                    or stream.codec_context.name in _PCM_FALLBACK_AUDIO
                ):
                    # Matroska не поддерживает G.711; декодирование в PCM
                    # сохраняет звук без дополнительного сжатия с потерями.
                    add_audio(stream, "pcm_s16le")
                else:
                    try:
                        streams[stream.index] = output.add_stream_from_template(stream)
                    except Exception:
                        add_audio(stream, "pcm_s16le")

            last_pts: dict[int, int] = {}

            def encode_video(packet: Any, index: int) -> None:
                width, height = video_size[index]
                target = streams[index]
                for frame in packet.decode():
                    if frame.pts is None:
                        continue
                    pts = int(Fraction(frame.pts * frame.time_base) / _TIME_BASE)
                    previous = last_pts.get(index)
                    if previous is not None and pts - previous < max(
                        1, min_frame_gap.get(index, 1)
                    ):
                        continue
                    last_pts[index] = pts
                    frame = frame.reformat(width=width, height=height, format="yuv420p")
                    frame.pts, frame.time_base = pts, _TIME_BASE
                    frame.pict_type = 0
                    for encoded in target.encode(frame):
                        output.mux(encoded)

            origin = None
            started = False
            latest = Fraction(0)  # Позиция выходной шкалы (секунды от начала)
            out_last = Fraction(0)  # Абсолютная выходная позиция последнего пакета
            last_dts: dict[int, Fraction] = {}
            shift = Fraction(0)
            packets = 0
            last_packet_at = time.monotonic()
            while True:
                try:
                    for packet in container.demux(selected):
                        context.check()
                        if packet.size == 0:
                            # Пустой пакет в конце потока сбрасывает кадры декодера.
                            if started and packet.stream.index in video_size:
                                encode_video(packet, packet.stream.index)
                            continue
                        if packet.dts is None:
                            if packet.pts is None:
                                continue
                            packet.dts = packet.pts
                        if not started:
                            if packet.stream.type != "video" or not packet.is_keyframe:
                                continue
                            started = True
                            if origin is not None:
                                # Продолжение после обрыва: сохраняем реальный разрыв.
                                gap = Fraction(time.monotonic() - last_packet_at)
                                source = Fraction(packet.dts * packet.time_base)
                                shift = out_last + max(gap, Fraction(1, 25)) - source
                        base = packet.time_base
                        if shift:
                            ticks = round(shift / base)
                            packet.dts += ticks
                            if packet.pts is not None:
                                packet.pts += ticks
                        timestamp = Fraction(packet.dts * base)
                        index = packet.stream.index
                        if index in last_dts and timestamp <= last_dts[index]:
                            continue
                        last_dts[index] = timestamp
                        if origin is None:
                            origin = timestamp
                        out_last = max(out_last, timestamp)
                        latest = max(latest, out_last - origin)
                        target = streams[index]
                        if index in video_size:
                            encode_video(packet, index)
                        elif index in audio_converters:
                            for frame in packet.decode():
                                for converted in audio_converters[index].resample(
                                    frame
                                ):
                                    for encoded in target.encode(converted):
                                        output.mux(encoded)
                        else:
                            packet.stream = target
                            output.mux(packet)
                        packets += 1
                        last_packet_at = time.monotonic()
                        context.report(
                            MediaProgress(
                                "record",
                                float(latest),
                                None,
                                packets,
                                output_file.tell(),
                            )
                        )
                    # Штатный конец потока: без окна переподключения — обычное
                    # завершение, иначе трактуется как обрыв.
                    if not packets or options.reconnect_grace <= 0:
                        break
                    raise EOFError("Поток завершён")
                except InterruptedError:
                    # Закрытие output выполняется здесь, в потоке владельца.
                    break
                except Exception as error:
                    if not packets or options.reconnect_grace <= 0:
                        raise
                    reopened = _reconnect(
                        container,
                        url,
                        timeout,
                        context,
                        signature,
                        use_audio,
                        last_packet_at + options.reconnect_grace,
                    )
                    if reopened is None:
                        container = None
                        if isinstance(error, EOFError):
                            break
                        raise
                    container = reopened
                    selected = _select_streams(container, use_audio)
                    started = False
            for index, converter in audio_converters.items():
                target = streams[index]
                for frame in converter.resample(None):
                    for encoded in target.encode(frame):
                        output.mux(encoded)
                for encoded in target.encode(None):
                    output.mux(encoded)
            for index in video_size:
                for encoded in streams[index].encode(None):
                    output.mux(encoded)
    finally:
        try:
            if container is not None:
                container.close()
        except Exception:
            pass
