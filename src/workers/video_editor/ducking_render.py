"""Непрерывный sidechain для пакетного монтажа без сброса детектора на стыках."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

from src.domain.video.render_plan import RenderClip


class DuckingRender:
    @staticmethod
    def run(owner) -> bool:
        from .export_worker import ExportWorker, ExportParams

        plan = owner._plan
        root = Path(owner._params.out_path).parent
        tracks = list(
            dict.fromkeys(clip.track_iid for clip in plan.clips if clip.kind == "audio")
        )
        if len(tracks) > 24:
            raise ValueError(
                "Для непрерывного ducking поддерживается до 24 активных звуковых дорожек. Сведите часть дорожек."
            )
        needed = int((len(tracks) + 1) * plan.duration * 48000 * 8 * 1.2)
        if shutil.disk_usage(root).free < needed + 8 * 1048576:
            raise ValueError("Недостаточно временного места для непрерывного ducking.")

        def render(stage, path: Path, parameters: ExportParams) -> bool:
            worker = ExportWorker(stage, parameters)
            worker._cancel_event = owner._cancel_event
            worker._control.cancelled = owner._cancel_event
            worker._metadata = dict(owner._metadata)
            errors = []
            worker.error.connect(errors.append)
            worker.progress.connect(
                lambda percent, text: owner.progress.emit(
                    min(99, percent), "Подготовка ducking: " + text
                )
            )
            worker._run()
            owner._control.check()
            if errors:
                owner.error.emit(errors[0])
                return False
            return path.is_file()

        stems = []
        for index, iid in enumerate(tracks):
            path = root / f"stem-{index}.wav"
            stage = replace(
                plan,
                clips=tuple(
                    clip
                    for clip in plan.clips
                    if clip.kind == "audio" and clip.track_iid == iid
                ),
                texts=(),
                masks=(),
                ducking=(),
                master=1,
                limiter=False,
            )
            if not render(
                stage,
                path,
                ExportParams(
                    mode="reencode",
                    out_path=str(path),
                    audio_only=True,
                    acodec="pcm_f32le",
                ),
            ):
                return False
            stems.append(
                RenderClip(
                    f"stem-{index}",
                    iid,
                    str(path),
                    "audio",
                    0,
                    0,
                    plan.duration,
                    audio_duration=plan.duration,
                )
            )
        mixed = root / "ducking-mix.wav"
        stage = replace(plan, clips=tuple(stems), texts=(), masks=(), limiter=False)
        if not render(
            stage,
            mixed,
            ExportParams(
                mode="reencode",
                out_path=str(mixed),
                audio_only=True,
                acodec="pcm_f32le",
            ),
        ):
            return False
        composite = replace(
            plan,
            clips=tuple(clip for clip in plan.clips if clip.kind == "video")
            + (
                RenderClip(
                    "ducking-mix",
                    "ducking-mix",
                    str(mixed),
                    "audio",
                    0,
                    0,
                    plan.duration,
                    audio_duration=plan.duration,
                ),
            ),
            ducking=(),
            master=1,
        )
        return render(composite, Path(owner._params.out_path), owner._params)
