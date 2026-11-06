"""Сборка FFmpeg-графа из монтажного плана без уплотнения времени."""

from __future__ import annotations

import math
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

from src.domain.video.metadata import ProbeMetadata, StreamMetadata
from src.domain.video.render_plan import RenderPlan

from .render_types import MediaInput, RenderGraphPlan


class RenderCommand:
    @staticmethod
    def canvas(
        plan: RenderPlan, metadata: dict[str, ProbeMetadata], params: dict[str, Any]
    ) -> tuple[int, int, Fraction]:
        reference = plan.canvas_source or (plan.video[0] if plan.video else None)
        first: ProbeMetadata | StreamMetadata = (
            metadata[reference.path] if reference else {}
        )
        streams = metadata[reference.path].get("video_streams", []) if reference else []
        first = streams[reference.stream] if streams and reference else first
        width = int(first.get("width", 640) or 640)
        height = int(first.get("height", 360) or 360)
        sar = str(first.get("sample_aspect_ratio", "1:1")).replace(":", "/")
        try:
            width = round(width * float(Fraction(sar)))
        except (ValueError, ZeroDivisionError):
            pass
        rotation = next(
            (
                float(item.get("rotation", 0))
                for item in (
                    streams[reference.stream].get("side_data_list", [])
                    if streams and reference
                    else []
                )
                if "rotation" in item
            ),
            0,
        )
        # Автоматический холст следует за итоговой ориентацией первого клипа;
        # явно заданные размеры ниже имеют приоритет.
        turned = bool(reference) and reference.rotation % 180 == 90
        if (abs(rotation) % 180 == 90) != turned:
            width, height = height, width
        wanted_w = int(params.get("width", 0) or plan.canvas.width)
        wanted_h = int(params.get("height", 0) or plan.canvas.height)
        if wanted_w and not wanted_h:
            wanted_h = max(2, round(wanted_w * height / width / 2) * 2)
        elif wanted_h and not wanted_w:
            wanted_w = max(2, round(wanted_h * width / height / 2) * 2)
        width, height = (
            wanted_w or max(2, round(width / 2) * 2),
            wanted_h or max(2, round(height / 2) * 2),
        )
        if (
            width < 2
            or height < 2
            or width > 16384
            or height > 16384
            or width % 2
            or height % 2
        ):
            raise ValueError(
                "Размеры холста должны быть чётными числами от 2 до 16384."
            )
        fps = (
            Fraction(str(params.get("fps") or 0)).limit_denominator(1001000)
            or plan.canvas.fps
        )
        if not fps:
            try:
                fps = Fraction(str(first.get("avg_frame_rate", "0/1")))
            except (ValueError, ZeroDivisionError):
                fps = Fraction(0)
        fps = fps or Fraction(str(first.get("fps", 25) or 25)).limit_denominator(
            1001000
        )
        if not 1 <= fps <= 240:
            raise ValueError("FPS холста должен находиться в диапазоне 1..240.")
        return width, height, fps

    @classmethod
    def build(
        cls,
        plan: RenderPlan,
        output: str,
        metadata: dict[str, ProbeMetadata],
        params: dict[str, Any],
    ) -> RenderGraphPlan:
        from .filter_options import build_drawtext_filters, default_fontfile
        from .service import get_media_service

        width, height, fps = cls.canvas(plan, metadata, params)
        audio_only = bool(params.get("audio_only"))
        text_filter = params.get("drawtext")
        if (
            any(item.kind == "text" and not item.rotation for item in plan.texts)
            and not text_filter
            and not audio_only
            and "drawtext" not in get_media_service().capabilities().filters
        ):
            raise ValueError(
                "В комплекте FFmpeg отсутствует drawtext; титры не будут пропущены."
            )
        rate = f"{fps.numerator}/{fps.denominator}"
        if plan.duration <= 0:
            raise ValueError("Таймлайн пуст.")
        inputs: list[MediaInput] = []
        codec = params.get("vcodec", "libx264")
        upload = ""
        filters: list[str] = []
        video_tags: list[str] = []
        audio_tags: list[str] = []
        audio_tracks: dict[str, list[str]] = {}
        layered = len({clip.track_iid for clip in plan.video}) > 1 or any(
            clip.opacity != 1
            or clip.scale != 1
            or clip.position_x
            or clip.position_y
            or clip.video_fade_in
            or clip.video_fade_out
            # concat не определяет длительность сегмента с единственным кадром.
            or clip.duration <= 1 / float(fps) + 1e-9
            for clip in plan.video
        )
        cursor = 0.0
        clips = tuple(
            clip for clip in plan.clips if not audio_only or clip.kind == "audio"
        )
        for number, clip in enumerate(clips):
            inputs.append(
                MediaInput(
                    f"{number}:{'v' if clip.kind == 'video' else 'a'}:{clip.stream}",
                    clip.path,
                    clip.kind,
                    clip.stream,
                    clip.src_in,
                    clip.src_out,
                    clip.start,
                    clip.speed,
                )
            )
            if clip.kind == "video":
                # Контейнер может быть длиннее видео из-за звукового хвоста.
                # Дополняем ДО trim: перемотка в хвост тоже получает кадр.
                streams = metadata[clip.path].get("video_streams", [])
                stream = streams[clip.stream] if streams else {}
                tail = ""
                try:
                    duration = float(stream.get("duration", 0) or 0)
                    if math.isfinite(duration) and 0 < duration <= clip.src_out:
                        tail = f",tpad=stop_mode=clone:stop_duration={clip.src_out-duration+1:.9f}"
                except (TypeError, ValueError):
                    pass
                transform = cls.orientation_filters(
                    clip.rotation, clip.flip_h, clip.flip_v
                )
                if (
                    clip.crop_left
                    or clip.crop_right
                    or clip.crop_top
                    or clip.crop_bottom
                ):
                    transform += f"crop=w='max(2,trunc(iw*{1-clip.crop_left-clip.crop_right:.9f}/2)*2)':h='max(2,trunc(ih*{1-clip.crop_top-clip.crop_bottom:.9f}/2)*2)':x='iw*{clip.crop_left:.9f}':y='ih*{clip.crop_top:.9f}',"
                layer_w, layer_h = cls.layer_size(width, height, clip.scale)
                mode = "decrease" if plan.canvas.fit == "contain" else "increase"
                geometry = (
                    cls.autorotate(stream)
                    + transform
                    + f"scale=w='max(2,trunc(iw*sar/2)*2)':h=ih,setsar=1,scale={layer_w}:{layer_h}:force_original_aspect_ratio={mode}:force_divisible_by=2"
                )
                geometry += (
                    f",pad={layer_w}:{layer_h}:(ow-iw)/2:(oh-ih)/2:color=black"
                    if plan.canvas.fit == "contain"
                    else f",crop={layer_w}:{layer_h}"
                )
                alpha = ""
                if layered:
                    alpha = f",format=rgba,colorchannelmixer=aa={clip.opacity:.9f},setpts=PTS+{clip.video_offset:.9f}/TB"
                    if clip.video_fade_in:
                        alpha += f",fade=t=in:st=0:d={clip.video_fade_in:.9f}:alpha=1"
                    if clip.video_fade_out:
                        alpha += f",fade=t=out:st={max(0, (clip.video_duration or clip.duration)-clip.video_fade_out):.9f}:d={clip.video_fade_out:.9f}:alpha=1"
                    alpha += f",setpts=PTS-STARTPTS+{clip.start:.9f}/TB"
                filters.append(
                    f"[{number}:v:{clip.stream}]setpts=PTS-{float(stream.get('start_time', 0) or 0):.9f}/TB{tail},trim=start={clip.src_in:.9f}:end={clip.src_out:.9f},setpts=(PTS-STARTPTS)/{clip.speed:.9f},{geometry},setsar=1,fps={rate},format=yuv420p,tpad=stop_mode=clone:stop_duration={clip.duration:.9f},trim=duration={clip.duration:.9f},settb=AVTB{alpha}[v{number}]"
                )
            else:
                meta = metadata[clip.path]
                streams = meta.get("audio_streams", [])
                stream = streams[clip.stream] if streams else {}
                origin = (
                    float(meta.get("vstart", meta.get("start_time", 0)))
                    if meta.get("has_video")
                    else float(meta.get("start_time", 0))
                )
                offset = (
                    float(stream.get("start_time", meta.get("astart", 0)) or 0) - origin
                )
                trim_start = max(0.0, clip.src_in - offset)
                inputs[-1] = replace(
                    inputs[-1],
                    start=trim_start,
                    end=max(trim_start, clip.src_out - offset),
                )
                delay = max(0.0, offset - clip.src_in) / clip.speed
                gain = 0.0 if clip.mute else clip.gain
                tempo = f",atempo={clip.speed:.9f}" if clip.speed != 1 else ""
                envelope = ""
                if clip.fade_in or clip.fade_out:
                    factors = []
                    if clip.fade_in:
                        factors.append(
                            f"min(1,max(0,(t+{clip.audio_offset:.9f})/{clip.fade_in:.9f}))"
                        )
                    if clip.fade_out:
                        factors.append(
                            f"min(1,max(0,({clip.audio_duration:.9f}-t-{clip.audio_offset:.9f})/{clip.fade_out:.9f}))"
                        )
                    expression = "*".join(factors)
                    envelope = (
                        f",aeval=exprs='val(0)*{expression}|val(1)*{expression}':c=same"
                    )
                filters.append(
                    f"[{number}:a:{clip.stream}]asetpts=PTS-{float(stream.get('start_time', 0) or 0):.9f}/TB,atrim=start={trim_start:.9f}:end={max(trim_start, clip.src_out - offset):.9f},asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo{tempo},volume={gain:.9f},adelay={round(delay * 48000)}S:all=1,apad,atrim=duration={clip.duration:.9f}{envelope},adelay={round(clip.start * 48000)}S:all=1[a{number}]"
                )
                audio_tags.append(f"[a{number}]")
                audio_tracks.setdefault(clip.track_iid, []).append(f"[a{number}]")
        for index, clip in enumerate(
            plan.video if not audio_only and not layered else ()
        ):
            if clip.start > cursor + 1e-9:
                filters.append(
                    f"color=c=black:s={width}x{height}:r={rate}:d={clip.start - cursor:.9f},setsar=1,format=yuv420p,settb=AVTB[gap{index}]"
                )
                video_tags.append(f"[gap{index}]")
            number = plan.clips.index(clip)
            video_tags.append(f"[v{number}]")
            cursor = clip.end
        if (
            not audio_only
            and not layered
            and (cursor < plan.duration - 1e-9 or not video_tags)
        ):
            filters.append(
                f"color=c=black:s={width}x{height}:r={rate}:d={plan.duration - cursor:.9f},setsar=1,format=yuv420p,settb=AVTB[tail]"
            )
            video_tags.append("[tail]")
        limiter = ",alimiter=limit=0.95:level=0:latency=1" if plan.limiter else ""
        if not audio_only and layered:
            filters.append(
                f"color=c=black:s={width}x{height}:r={rate}:d={plan.duration:.9f},setsar=1,format=yuv420p,settb=AVTB[base0]"
            )
            ordered = sorted(plan.video, key=lambda clip: (clip.layer, clip.start))
            for index, clip in enumerate(ordered):
                number = plan.clips.index(clip)
                # Позиция отсчитывается от центра: масштаб не сдвигает середину слоя.
                layer_w, layer_h = cls.layer_size(width, height, clip.scale)
                left = round(clip.position_x * width + (width - layer_w) / 2)
                top = round(clip.position_y * height + (height - layer_h) / 2)
                filters.append(
                    f"[base{index}][v{number}]overlay=x={left}:y={top}:eof_action=pass:repeatlast=0:enable='gte(t,{clip.start:.9f})*lt(t,{clip.end:.9f})'[base{index+1}]"
                )
            filters.append(f"[base{len(ordered)}]null[vcat]")
        elif not audio_only:
            filters.append(
                "".join(video_tags) + f"concat=n={len(video_tags)}:v=1:a=0[vcat]"
            )
        if plan.ducking:
            audio_tags = cls.ducking_graph(plan, audio_tracks, filters)
        filters.append(f"anullsrc=r=48000:cl=stereo:d={plan.duration:.9f}[silence]")
        filters.append(
            "[silence]"
            + "".join(audio_tags)
            + f"amix=inputs={len(audio_tags) + 1}:duration=longest:normalize=0,atrim=duration={plan.duration:.9f},volume={plan.master:.9f}{limiter}[aout]"
        )
        if not audio_only:
            current = "vcat"
            if text_filter:
                filters.append(f"[{current}]{text_filter}[legacytext]")
                current = "legacytext"
            raster_input = len(clips)
            for index, item in enumerate(plan.texts):
                target = f"overlay{index}"
                if item.kind == "text" and text_filter:
                    continue
                if item.kind == "text" and not item.rotation:
                    directory = Path(output).parent / f"text-{index}"
                    directory.mkdir(exist_ok=True)
                    chain = build_drawtext_filters(
                        (item,), default_fontfile(), directory
                    )
                    filters.append(f"[{current}]{chain}[{target}]")
                else:
                    from src.utils.video_overlays import (
                        graphic_image,
                        graphic_origin,
                        rotate_layer,
                    )

                    path = Path(output).parent / f"graphic-{index}.png"
                    x: float
                    y: float
                    if item.kind == "text":
                        from src.utils.video_text import text_layer

                        from .filter_options import overlay_origin

                        image, text_geometry, bleed = text_layer(item)
                        x, y = overlay_origin(
                            item.corner,
                            item.margin_x,
                            item.margin_y,
                            item.box_pad,
                            text_geometry.width,
                            text_geometry.ink.height(),
                            width,
                            height,
                        )
                        x -= item.box_pad + bleed
                        y -= item.box_pad + bleed
                    else:
                        image = graphic_image(item)
                        x, y = graphic_origin(item, width, height)
                    rotated = rotate_layer(image, item.rotation)
                    x += (image.width() - rotated.width()) / 2
                    y += (image.height() - rotated.height()) / 2
                    if not rotated.save(str(path)):
                        raise ValueError("Не удалось подготовить наложение к экспорту.")
                    number = raster_input
                    raster_input += 1
                    inputs.append(MediaInput(f"{number}:v:0", str(path), "video"))
                    enabled = f"gte(t,{item.start:.9f})" + (
                        f"*lt(t,{item.end:.9f})" if item.end else ""
                    )
                    filters.append(
                        f"[{current}][{number}:v:0]overlay=x={x}:y={y}:"
                        f"format=auto:eof_action=repeat:repeatlast=1:enable='{enabled}'[{target}]"
                    )
                current = target
            masks = [
                f"drawbox=x=iw*{mask.x:.9f}:y=ih*{mask.y:.9f}:w=iw*{mask.width:.9f}:h=ih*{mask.height:.9f}:color=black:t=fill:enable='gte(t,{mask.start:.9f})*lt(t,{mask.end:.9f})'"
                for mask in plan.masks
            ]
            filters.append(
                f"[{current}]{','.join(['format=yuv420p', *masks, *([upload] if upload else [])])}[vout]"
            )
        audio_codec = params.get("acodec", "aac")
        if audio_only:
            audio_codec = {
                ".wav": (
                    "pcm_f32le" if params.get("acodec") == "pcm_f32le" else "pcm_s16le"
                ),
                ".mp3": "libmp3lame",
                ".aac": "aac",
                ".m4a": "aac",
            }.get(Path(output).suffix.lower(), "")
            if not audio_codec:
                raise ValueError("Неподдерживаемый звуковой контейнер.")
        options = {}
        if codec in ("libx264", "libx265"):
            options["preset"] = str(params.get("preset", "medium"))
            if not params.get("vb"):
                options["crf"] = str(params.get("crf", 23))
        elif codec.endswith("_nvenc"):
            options["preset"] = "p4"
            if not params.get("vb"):
                options.update(rc="vbr", cq=str(params.get("crf", 23)))
        elif codec.endswith("_qsv"):
            options["preset"] = str(params.get("preset", "medium"))
            if not params.get("vb"):
                options["global_quality"] = str(params.get("crf", 23))
        elif codec.endswith("_amf") and not params.get("vb"):
            options.update(
                rc="cqp",
                qp_i=str(params.get("crf", 23)),
                qp_p=str(params.get("crf", 23)),
            )
        return RenderGraphPlan(
            tuple(inputs),
            ";".join(filters),
            width,
            height,
            fps,
            plan.duration,
            None if audio_only else codec,
            audio_codec,
            options,
            cls.bitrate(params.get("vb")),
            cls.bitrate(params.get("ab")),
            bool(params.get("faststart", True)),
            str(params.get("gpu_device", "")),
        )

    @staticmethod
    def orientation_filters(rotation: int, flip_h: bool, flip_v: bool) -> str:
        """Поворот, затем отражение; пустая строка или цепочка с запятой в конце."""
        chain = {
            90: "transpose=clock,",
            180: "hflip,vflip,",
            270: "transpose=cclock,",
        }.get(rotation, "")
        return chain + ("hflip," if flip_h else "") + ("vflip," if flip_v else "")

    @staticmethod
    def layer_size(width: int, height: int, scale: float) -> tuple[int, int]:
        """Чётный размер слоя клипа на холсте."""
        return (
            max(2, round(width * scale / 2) * 2),
            max(2, round(height * scale / 2) * 2),
        )

    @staticmethod
    def bitrate(value: Any) -> int | None:
        if not value:
            return None
        text = str(value).lower().strip()
        scale = 1000 if text.endswith("k") else 1000000 if text.endswith("m") else 1
        result = round(float(text[:-1] if scale != 1 else text) * scale)
        if result <= 0:
            raise ValueError("Битрейт должен быть положительным.")
        return result

    @staticmethod
    def autorotate(stream: StreamMetadata) -> str:
        rotation = (
            next(
                (float(x.get("rotation", 0)) for x in stream.get("side_data_list", [])),
                0,
            )
            % 360
        )
        return {
            90: "transpose=cclock,",
            180: "hflip,vflip,",
            270: "transpose=clock,",
        }.get(round(rotation), "")

    @staticmethod
    def ducking_graph(
        plan: RenderPlan, tracks: dict[str, list[str]], filters: list[str]
    ) -> list[str]:
        """Sidechain берётся до ducking; циклы управления не создают циклов графа."""
        rules = {
            rule.track_iid: rule
            for rule in plan.ducking
            if rule.track_iid in tracks and rule.source_iid in tracks
        }
        names = {iid: f"track{index}" for index, iid in enumerate(tracks)}
        sidechains: dict[str, str] = {}
        for iid, tags in tracks.items():
            name = names[iid]
            # Запас тишины позволяет sidechaincompress выгрузить последний
            # неполный блок до общего atrim, не обрезая хвост полезного звука.
            filters.append(
                "".join(tags)
                + f"amix=inputs={len(tags)}:normalize=0,apad,atrim=duration={plan.duration+1:.9f}[{name}raw]"
            )
            destinations = [
                target for target, rule in rules.items() if rule.source_iid == iid
            ]
            outputs = [f"[{name}main]"] + [
                f"[{name}side{index}]" for index in range(len(destinations))
            ]
            filters.append(f"[{name}raw]asplit={len(outputs)}" + "".join(outputs))
            for index, target in enumerate(destinations):
                sidechains[target] = f"[{name}side{index}]"
        result = []
        for iid in tracks:
            name = names[iid]
            if iid in rules:
                rule = rules[iid]
                filters.append(
                    f"[{name}main]{sidechains[iid]}sidechaincompress=threshold={rule.threshold:.9f}:ratio={rule.ratio:.9f}:attack={rule.attack:.9f}:release={rule.release:.9f}:makeup=1:link=maximum:detection=peak[{name}duck]"
                )
                result.append(f"[{name}duck]")
            else:
                result.append(f"[{name}main]")
        return result
