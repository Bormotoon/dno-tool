"""Последовательный lossless-рендер с ограниченным числом декодеров."""

from __future__ import annotations

import math
import shutil
from dataclasses import replace
from pathlib import Path

from src.domain.video.render_plan import RenderCanvas, RenderPlan
from .render_command import RenderCommand
from src.services.media import get_media_service, OperationContext
from src.domain.video.render_plan import RenderClip


class BatchRender:
    MAX_INPUTS = 24

    @classmethod
    def sections(cls, plan: RenderPlan, fps: float) -> list[tuple[float, float]]:
        result = []
        start = 0.0
        while start < plan.duration - 1e-9:
            clips = sorted(
                (clip for clip in plan.clips if clip.end > start + 1e-9),
                key=lambda clip: clip.start,
            )
            end = plan.duration
            if len(clips) > cls.MAX_INPUTS:
                end = min(
                    end, math.floor(clips[cls.MAX_INPUTS].start * fps + 1e-7) / fps
                )
                if end <= start + 1e-9:
                    raise ValueError(
                        f"Одновременно требуется более {cls.MAX_INPUTS} входов. Сведите часть звуковых дорожек или уменьшите диапазон."
                    )
            result.append((start, end))
            start = end
        return result

    @classmethod
    def run(cls, owner) -> bool:
        from .export_worker import ExportParams, ExportWorker

        plan = owner._plan
        parameters = owner._params
        width, height, fps = RenderCommand.canvas(
            plan, owner._metadata, vars(parameters)
        )
        if parameters.audio_only:
            width, height = 2, 2
            plan = replace(
                plan,
                clips=tuple(clip for clip in plan.clips if clip.kind == "audio"),
                texts=(),
            )
        sections = cls.sections(plan, float(fps))
        # Консервативная оценка lossless-временных файлов; не обещаем
        # размер конечного CRF-результата по сжимаемости исходника.
        needed = int(
            (width * height * 1.5 * float(fps) + 48000 * 8) * plan.duration * 1.2
        )
        root = Path(parameters.out_path).parent
        if shutil.disk_usage(root).free < needed + 8 * 1048576:
            raise ValueError(
                f"Для пакетного рендера нужно до {needed / 1048576:.0f} МиБ временного места. Выберите меньший диапазон или другой диск."
            )
        outputs = []
        for index, (start, end) in enumerate(sections):
            owner._control.check()
            section = replace(
                plan.section(start, end),
                canvas=RenderCanvas(width, height, fps, plan.canvas.fit),
                limiter=False,
            )
            output = root / f"batch-{index:05d}.mkv"
            worker = ExportWorker(
                section,
                ExportParams(
                    mode="reencode",
                    out_path=str(output),
                    vcodec="ffv1",
                    acodec="pcm_f32le",
                    faststart=False,
                    verify_decode=parameters.verify_decode,
                ),
            )
            worker._cancel_event = owner._cancel_event
            worker._control.cancelled = owner._cancel_event
            worker._metadata = dict(owner._metadata)
            errors = []
            worker.error.connect(errors.append)
            worker.progress.connect(
                lambda percent, stage, offset=start, length=end - start: (
                    owner.progress.emit(
                        min(
                            89,
                            int((offset + length * percent / 100) / plan.duration * 90),
                        ),
                        "Подготовка частей: " + stage,
                    )
                )
            )
            worker._run()
            owner._control.check()
            if errors or not output.is_file():
                raise ValueError(
                    errors[0] if errors else "Не удалось подготовить часть монтажа."
                )
            outputs.append(str(output))
        joined = root / "joined.mkv"
        get_media_service().remux(
            [(path, 0, end - start) for path, (start, end) in zip(outputs, sections)],
            str(joined),
            OperationContext.with_timeout(1800, owner._cancel_event),
        )
        final_plan = replace(
            plan,
            clips=(
                RenderClip("video", "video", str(joined), "video", 0, 0, plan.duration),
                RenderClip("audio", "audio", str(joined), "audio", 0, 0, plan.duration),
            ),
            texts=(),
            masks=(),
            ducking=(),
        )
        if parameters.audio_only:
            final_plan = replace(
                final_plan,
                clips=tuple(c for c in final_plan.clips if c.kind == "audio"),
            )
        final = ExportWorker(final_plan, parameters)
        final._cancel_event = owner._cancel_event
        final._control.cancelled = owner._cancel_event
        errors = []
        final.error.connect(errors.append)
        final.progress.connect(
            lambda percent, text: owner.progress.emit(
                min(99, 90 + int(percent * 0.09)), text
            )
        )
        final._run()
        owner._control.check()
        if errors:
            raise ValueError(errors[0])
        return True
