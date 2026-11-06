"""Экспорт проекта через FFmpeg в QThread с прогрессом и отменой."""

from __future__ import annotations

from .metadata import ProbeMetadata

import os
import math
import tempfile
import threading
import shutil
from typing import Any
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Qt

from src.logger import logger
from src.utils.filesystem.fs_utils import ensure_user_owned
from .ffmpeg_utils import (
    build_reencode_command,
    probe_media,
)
from .progress import ProgressSample
from src.services.media import get_media_service, OperationContext, MediaProgress
from src.domain.video.file_output import AtomicOutput
from src.domain.video.render_plan import RenderPlan
from src.domain.video.output_lease import OutputLease
from src.domain.video.failures import VideoFailure, error_detail
from .render_command import RenderCommand


@dataclass
class ExportParams:
    mode: str = "copy"  # copy | reencode
    out_path: str = ""
    vcodec: str = "libx264"
    acodec: str = "aac"
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vb: str = ""
    ab: str = ""
    drawtext: str | None = None
    master_volume: float = 1.0
    container: str = ""
    overwrite: bool = False
    audio_only: bool = False
    crf: int = 23
    preset: str = "medium"
    faststart: bool = True
    gpu_device: str = ""
    # Полное декодирование результата; временный файл предпросмотра его не требует.
    verify_decode: bool = True


class ExportWorker(QObject):
    progress = Signal(int, str)  # percent, stage
    finished = Signal(str)  # out_path
    error = Signal(str)

    def __init__(
        self,
        segments: list[tuple[str, float, float, float, bool]] | RenderPlan,
        params: ExportParams,
        parent: QObject | None = None,
        source_paths: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._plan = segments if isinstance(segments, RenderPlan) else None
        self._segments = (
            [
                (clip.path, clip.src_in, clip.src_out, clip.gain, clip.mute)
                for clip in segments.video
            ]
            if isinstance(segments, RenderPlan)
            else segments
        )
        self._params = replace(params)
        self._source_paths = list(
            source_paths
            or (
                self._plan.sources
                if self._plan
                else [segment[0] for segment in self._segments]
            )
        )
        if self._plan:
            self._source_paths = list(
                dict.fromkeys([*self._source_paths, *self._plan.sources])
            )
        self._cancelled = False
        self._cancel_event = threading.Event()
        self._control = OperationContext(cancelled=self._cancel_event)
        self._commit_lock = threading.RLock()
        self._metadata: dict[str, ProbeMetadata] = {}
        self._fingerprints: dict[str, tuple[int, int, int, int]] = {}
        self._thread: QThread | None = None
        self.failure: VideoFailure | None = None
        self._ffmpeg_version = "не проверена"
        self._stage = "preflight"
        self.error.connect(self._record_failure, Qt.ConnectionType.DirectConnection)
        self.progress.connect(self._record_stage, Qt.ConnectionType.DirectConnection)

    def _record_stage(self, percent: int, text: str) -> None:
        for prefix, stage in (
            ("Подготовка", "preflight"),
            ("Нарезка", "segments"),
            ("Склейка", "concat"),
            ("Кодирование", "encode"),
            ("Перекодирование", "encode"),
            ("Проверка", "verify"),
            ("Финализация", "finalize"),
        ):
            if text.startswith(prefix):
                self._stage = stage
                break

    def _record_failure(self, message: str) -> None:
        if self.failure is None:
            self.failure = VideoFailure.classify(message, self._stage)

    def diagnostic_report(self) -> str:
        from dataclasses import asdict
        import platform
        from PySide6.QtCore import qVersion

        parameters = asdict(self._params)
        parameters["ffmpeg_version"] = self._ffmpeg_version
        parameters.update(
            sources=len(self._source_paths),
            clips=len(self._plan.clips) if self._plan else len(self._segments),
            qt=qVersion(),
            platform=platform.system(),
        )
        return (self.failure or VideoFailure.classify("", self._stage)).report(
            parameters
        )

    def start(self, parent: QObject | None = None) -> None:
        """Запустить экспорт в отдельном QThread.

        Потоку задается родитель, чтобы сборщик мусора Python не удалил
        живой QThread (было: QtFatal "Destroyed while thread is still
        running" после конца кодирования). Завершение: finished/error
        гасят цикл потока и ставят воркер в deleteLater, сам поток
        удаляется по собственному finished. Ссылку на воркер владелец
        должен сбрасывать только по finished потока (см. wait).
        """
        th = QThread(parent)
        from .thread_lifetime import VideoThreadReaper

        self._reaper = VideoThreadReaper(th, self, parent)
        self._thread = th
        self.moveToThread(th)
        th.started.connect(self._run)
        self.finished.connect(th.quit)
        self.error.connect(th.quit)
        self.finished.connect(self.deleteLater)
        self.error.connect(self.deleteLater)
        th.start()

    def thread(self) -> QThread:
        """Поток экспорта (None до start)."""
        return self._thread if self._thread is not None else super().thread()

    def wait(self, timeout_ms: int = 8000) -> bool:
        """Попросить поток завершиться и дождаться (для closeEvent/reject).

        Безопасен после штатного завершения: quit идемпотентен, доступ
        к уже удаленному потоку дает RuntimeError - считаем успехом.
        """
        th = self._thread
        if th is None:
            return True
        try:
            th.quit()
        except RuntimeError:
            return True
        except Exception:
            pass
        try:
            return bool(th.wait(timeout_ms))
        except RuntimeError:
            return True
        except Exception as e:
            logger.error(f"ExportWorker wait: {e}")
            return False

    def cancel(self) -> None:
        with self._commit_lock:
            self._cancelled = True
            self._cancel_event.set()

    def _run(self) -> None:
        try:
            self._control.check()
            self._ffmpeg_version = get_media_service().capabilities().version
        except Exception as error:
            self.error.emit(str(error))
            return
        self._execute_plan()

    def _execute_plan(self) -> None:
        original = self._params
        lease: OutputLease | None = None
        try:
            self._control.check()
            target = self._validate_target()
            lease = OutputLease(target)
            lease.__enter__()
            self.progress.emit(0, "Подготовка: проверка исходников")
            for source in (
                self._plan.sources
                if self._plan
                else dict.fromkeys(segment[0] for segment in self._segments)
            ):
                self._control.check()
                self._fingerprints[source] = self._fingerprint(source)
                meta = self._metadata.get(source) or probe_media(
                    source, cancelled=self._cancel_event
                )
                if not meta or not (meta.get("has_video") or meta.get("has_audio")):
                    raise ValueError(
                        f"Не удалось проверить медиаисточник: {Path(source).name}"
                    )
                self._metadata[source] = meta
            if self._plan:
                for clip in self._plan.clips:
                    meta = self._metadata[clip.path]
                    streams = (
                        meta.get("video_streams", [])
                        if clip.kind == "video"
                        else meta.get("audio_streams", [])
                    )
                    if clip.stream < 0 or clip.stream >= len(streams):
                        raise ValueError(
                            f"Выбранный {clip.kind}-поток отсутствует: {Path(clip.path).name}"
                        )
                    if clip.src_out > float(meta.get("duration", 0)) + 0.05:
                        raise ValueError(
                            f"Клип выходит за длительность исходника: {Path(clip.path).name}"
                        )
                    if clip.kind == "video":
                        try:
                            stream_duration = float(
                                streams[clip.stream].get("duration", 0) or 0
                            )
                        except (TypeError, ValueError):
                            stream_duration = 0
                        if (
                            math.isfinite(stream_duration)
                            and stream_duration > 0
                            and clip.src_out > stream_duration + 0.05
                            and original.mode == "copy"
                        ):
                            raise ValueError(
                                "Видеопоток короче фрагмента. Выберите перекодирование, чтобы сохранить последний кадр до конца звука."
                            )
            if original.mode == "copy":
                if self._plan and (
                    self._plan.copy_reasons
                    or any(clip.stream for clip in self._plan.clips)
                ):
                    raise ValueError(
                        "; ".join(self._plan.copy_reasons)
                        or "Выбор другого потока требует перекодирования."
                    )
                if (
                    original.master_volume != 1
                    or original.drawtext
                    or any(
                        volume != 1 or mute for _, _, _, volume, mute in self._segments
                    )
                ):
                    raise ValueError("Текст и обработка звука требуют перекодирования.")
                compatible, reason = self.check_compat(list(self._metadata.values()))
                if not compatible:
                    raise ValueError(reason)
                from .copy_preflight import check_copy_container

                self.progress.emit(0, "Подготовка: проверка контейнера Copy")
                check_copy_container(
                    list(self._metadata.values()),
                    target.suffix.lower()[1:],
                    self._cancel_event,
                )
            self._check_disk_space(target.parent)
            with tempfile.TemporaryDirectory(
                prefix=".dno-export-", dir=target.parent
            ) as job:
                self._params = replace(
                    original, out_path=str(Path(job) / f"render{target.suffix.lower()}")
                )
                ok = (
                    self._run_copy()
                    if original.mode == "copy"
                    else self._run_reencode()
                )
                if not ok:
                    return
                self._control.check()
                if any(
                    self._fingerprint(source) != fingerprint
                    for source, fingerprint in self._fingerprints.items()
                ):
                    raise RuntimeError(
                        "Исходник изменился во время экспорта; результат не опубликован."
                    )
                # При отсутствии согласия не перезаписываем и файл, появившийся
                # уже после preflight. Для публикации нового имени — hard link.
                with open(self._params.out_path, "r+b") as ready:
                    os.fsync(ready.fileno())
                with self._commit_lock:
                    self._control.check()
                    self._publish(self._params.out_path, target, original.overwrite)
            self.progress.emit(100, "Готово")
            self.finished.emit(str(target))
        except InterruptedError:
            self.progress.emit(0, "")
            self.error.emit("Экспорт отменен")
        except Exception as e:
            logger.error(
                f"ExportWorker: сбой {type(e).__name__}, этап {self._stage}: "
                f"{error_detail(e)}; {self._plan_summary()}"
            )
            self.failure = VideoFailure.classify(str(e), self._stage, e)
            self.error.emit(str(e))
        finally:
            self._params = original
            if lease is not None:
                lease.__exit__()

    def _plan_summary(self) -> str:
        """Параметры задания для журнала, без путей и текста наложений."""
        params = self._params
        parts = [f"режим {params.mode}", f"кодек {params.vcodec}"]
        if self._plan is None:
            return ", ".join(parts + [f"сегментов {len(self._segments)}"])
        video = self._plan.video
        canvas = self._plan.canvas
        parts += [
            f"клипов {len(self._plan.clips)}",
            f"видео {len(video)}",
            f"длительность {self._plan.duration:.3f}",
            f"холст {canvas.width}x{canvas.height}",
            f"повороты {sorted({clip.rotation for clip in video})}",
            f"наложений {len(self._plan.texts)}",
            f"масок {len(self._plan.masks)}",
        ]
        return ", ".join(parts)

    @staticmethod
    def _fingerprint(path: str) -> tuple[int, int, int, int]:
        stat = Path(path).stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    @staticmethod
    def _publish(staging: str, target: Path, overwrite: bool) -> None:
        """Атомарная публикация; новое имя не заменяет чужой поздний файл."""
        AtomicOutput.publish(staging, target, overwrite)

    def _validate_target(self) -> Path:
        if not self._segments and not (self._plan and self._plan.duration > 0):
            raise ValueError("Нечего экспортировать.")
        if (
            self._params.mode not in ("copy", "reencode")
            or type(self._params.overwrite) is not bool
        ):
            raise ValueError("Некорректный режим экспорта или флаг перезаписи.")
        target = Path(self._params.out_path).absolute()
        suffixes = (
            (".wav", ".mp3", ".aac", ".m4a")
            if self._params.audio_only
            else (".mp4", ".mkv")
        )
        if target.suffix.lower() not in suffixes:
            raise ValueError("Расширение не соответствует выбранному типу результата.")
        if self._params.audio_only and (
            self._params.mode != "reencode"
            or not self._plan
            or not any(clip.kind == "audio" for clip in self._plan.clips)
        ):
            raise ValueError(
                "Для звукового экспорта нужны аудиоклипы и режим перекодирования."
            )
        if (
            self._params.container
            and target.suffix.lower() != f".{self._params.container.lower()}"
        ):
            raise ValueError("Расширение файла не совпадает с выбранным контейнером.")
        if not target.parent.is_dir() or target.is_dir():
            raise ValueError(
                "Каталог результата не существует или путь указывает на каталог."
            )
        for source in self._source_paths:
            original = Path(source)
            if original.resolve() == target.resolve() or (
                original.exists()
                and target.exists()
                and os.path.samefile(original, target)
            ):
                raise ValueError("Результат нельзя записать поверх исходника.")
        if target.exists() and not self._params.overwrite:
            raise ValueError("Перезапись существующего результата не подтверждена.")
        return target

    def _check_disk_space(self, parent: Path) -> None:
        estimate = 0.0
        for source, start, end, _volume, _mute in self._segments:
            duration = float(self._metadata[source].get("duration", 0) or 0)
            if duration > 0:
                estimate += Path(source).stat().st_size * max(0, end - start) / duration
        estimate *= 2 if self._params.mode == "copy" else 1
        needed = int(estimate) + 8 * 1024 * 1024
        if shutil.disk_usage(parent).free < needed:
            raise OSError(
                f"Недостаточно места: оценка временного результата {needed / 1048576:.1f} МиБ."
            )

    def _media_progress(self, progress: MediaProgress) -> None:
        percent = (
            min(98, int(progress.media_time / progress.duration * 98))
            if progress.duration
            else 0
        )
        self.progress.emit(
            percent,
            "Кодирование" if progress.stage == "encode" else "Нарезка и склейка",
        )

    def _run_copy(self) -> bool:
        service = get_media_service()
        context = OperationContext.with_timeout(
            1800, self._cancel_event, self._media_progress
        )
        service.remux(
            [(path, start, end) for path, start, end, _, _ in self._segments],
            self._params.out_path,
            context,
        )
        self.progress.emit(99, "Проверка результата")
        self._verify_output(sum(end - start for _, start, end, _, _ in self._segments))
        ensure_user_owned(self._params.out_path)
        return True

    def _verify_output(self, total: float) -> None:
        """Не сообщать 100%, пока не проверен непустой видеорезультат."""
        out = Path(self._params.out_path)
        if not out.is_file() or out.stat().st_size == 0:
            raise RuntimeError("Результат экспорта не создан или пуст.")
        meta = probe_media(str(out), cancelled=self._cancel_event)
        duration = float(meta.get("duration", 0) or 0)
        if (
            not meta.get("has_audio" if self._params.audio_only else "has_video")
            or not math.isfinite(duration)
            or duration <= 0
        ):
            raise RuntimeError(
                "Не удалось подтвердить видеопоток и длительность результата."
            )
        if (
            self._plan is not None
            and self._params.mode == "reencode"
            and not meta.get("has_audio")
        ):
            raise RuntimeError("Не удалось подтвердить звуковой поток результата.")
        if self._params.mode == "reencode":
            fps = float(meta.get("fps", 0) or 0)
            tolerance = max(
                0.15, 1 / fps if fps > 0 and not self._params.audio_only else 0.15
            )
            if abs(duration - total) > tolerance:
                raise RuntimeError(
                    "Длительность перекодированного результата не совпадает с монтажом; файл не опубликован."
                )
        if self._params.verify_decode:
            get_media_service().verify(
                str(out), OperationContext(cancelled=self._cancel_event)
            )
        logger.info(
            f"ffmpeg export done: total={total:.1f}s "
            f"max_out={getattr(self, '_max_out_s', 0.0):.1f}s result={duration:.1f}s"
        )
        if total > 5 and abs(duration - total) > max(5.0, total * 0.05):
            logger.warning(
                f"ffmpeg export: длительность результата ({duration:.1f}s) "
                f"заметно отличается от таймлайна ({total:.1f}s)"
            )

    def _run_reencode(self) -> bool:
        from .batch_render import BatchRender
        from .capabilities import FFmpegCapabilities
        from .encoder_hardware import EncoderHardware

        if not self._params.audio_only and EncoderHardware.is_hardware(
            self._params.vcodec
        ):
            FFmpegCapabilities.verify_encoder(
                self._params.vcodec, self._cancel_event, self._params.gpu_device
            )

        if self._plan and len(self._plan.clips) > BatchRender.MAX_INPUTS:
            if self._plan.ducking:
                from .ducking_render import DuckingRender

                return DuckingRender.run(self)
            return BatchRender.run(self)
        self.progress.emit(0, "Подготовка: анализ исходников")
        total = (
            self._plan.duration
            if self._plan
            else sum(o - i for _, i, o, _v, _m in self._segments)
        )
        # Наличие звука по уникальным файлам: входы без звука добиваются
        # тишиной в команде (иначе [i:a] роняет весь фильтр).
        # Заодно забираем fps/стартовые метки для базы прогресса и лога.
        presence: dict[str, bool] = {}
        metas: dict[str, ProbeMetadata] = dict(self._metadata)
        for src in {str(p) for p, _i, _o, _v, _m in self._segments}:
            self._control.check()
            meta = metas.get(src) or probe_media(src, cancelled=self._cancel_event)
            if meta:
                metas[src] = meta
                presence[src] = bool(meta.get("has_audio", False))
        n_audio = sum(
            1 for p, _i, _o, _v, _m in self._segments if presence.get(p, True)
        )
        # Только явно выбранный выходной CFR задаёт число кадров.
        # Средний FPS исходника не определяет длительность VFR-экспорта.
        fps_out = float(self._params.fps or 0)
        total_frames = total * fps_out if fps_out > 0 else 0.0
        basis = f"frames@{fps_out:.2f}fps" if total_frames > 0 else "time"
        params = {
            "vcodec": self._params.vcodec,
            "acodec": self._params.acodec,
            "width": self._params.width,
            "height": self._params.height,
            "fps": self._params.fps,
            "vb": self._params.vb,
            "ab": self._params.ab,
            "drawtext": self._params.drawtext,
            "gpu_device": self._params.gpu_device,
            "master_volume": self._params.master_volume,
            "audio_presence": presence,
            "metadata": metas,
            "cancelled": self._cancel_event,
            "audio_only": self._params.audio_only,
            "crf": self._params.crf,
            "preset": self._params.preset,
            "faststart": self._params.faststart,
        }
        logger.info(
            f"ffmpeg export reencode: total={total:.1f}s "
            f"segs={len(self._segments)} audio={n_audio}/{len(self._segments)} "
            f"vcodec={self._params.vcodec} basis={basis}"
        )
        cmd = (
            RenderCommand.build(self._plan, self._params.out_path, metas, params)
            if self._plan
            else build_reencode_command(self._segments, self._params.out_path, params)
        )
        context = OperationContext(
            cancelled=self._cancel_event, progress=self._media_progress
        )
        get_media_service().render(cmd, self._params.out_path, context)
        # Хвост после EOF прогресса (муксинг, запись трейлера, смена
        # владельца) виден отдельной стадией, а не зависшими 99%.
        self.progress.emit(99, "Финализация")
        ensure_user_owned(self._params.out_path)
        self.progress.emit(99, "Проверка результата")
        self._verify_output(total)
        self._control.check()
        return True

    @staticmethod
    def check_compat(assets_meta: list[ProbeMetadata]) -> tuple[bool, str]:
        """Проверить состав и сигнатуры потоков; незнание не означает совместимость."""
        if not assets_meta:
            return False, "Нет данных о потоках"
        first = assets_meta[0]
        if any(
            not meta.get("vcodec")
            or any(
                type(meta.get(key)) is not int or meta[key] <= 0
                for key in ("width", "height")
            )
            or type(meta.get("has_audio")) is not bool
            for meta in assets_meta
        ):
            return False, "Параметры потоков ещё не проверены"
        for m in assets_meta[1:]:
            if m["has_audio"] != first["has_audio"]:
                return False, "Разное наличие аудиопотоков — требуется перекодирование"
            if m.get("vcodec") != first.get("vcodec"):
                return False, "Разные видеокодеки - нужен режим перекодирования"
            if (m.get("width"), m.get("height")) != (
                first.get("width"),
                first.get("height"),
            ):
                return False, "Разное разрешение - нужен режим перекодирования"
        if first.get("path") and all(
            meta.get("path") == first["path"] for meta in assets_meta
        ):
            return True, ""
        signature = first.get("copy_signature")
        if not signature or any(
            meta.get("copy_signature") != signature for meta in assets_meta
        ):
            return (
                False,
                "Параметры потоков различаются или не проверены (time base, codec data, FPS, звук)",
            )
        return True, ""
