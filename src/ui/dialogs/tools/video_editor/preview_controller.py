"""Подготовка preview с одним заданием, поколениями и ограниченным диапазоном."""

from __future__ import annotations

import tempfile
import time
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, QUrl, Slot
from PySide6.QtGui import QImage, QColor

from src.domain.video.render_plan import RenderPlan, RenderText
from src.domain.video.direct_preview import DirectPreview
from src.utils.system.qt_multimedia import stop_media_player
from src.workers.video_editor.preview_worker import PreviewRenderWorker

if TYPE_CHECKING:
    from .preview_widget import PreviewWidget

# Длина подготовленного участка и запас перед курсором: шаг назад не требует
# нового кодирования.
PREVIEW_RANGE = 20.0
PREVIEW_LOOKBACK = 3.0


class PreviewController(QObject):
    def __init__(self, view: PreviewWidget) -> None:
        super().__init__(view)
        self.view = view
        self.plan: RenderPlan | None = None
        self.direct: DirectPreview | None = None
        self._active_texts: tuple[RenderText, ...] = ()
        self.text: str | None = None
        self.position = 0.0
        self.resume = False
        self.generation = 0
        self.worker: PreviewRenderWorker | None = None
        self.prepared: tuple[float, float, str] | None = None
        self.directory: tempfile.TemporaryDirectory[str] | None = None
        self.retired: list[tempfile.TemporaryDirectory[str]] = []
        self.closed = False
        self.in_gap = False
        self._continuing = False
        self._gap_start = 0.0
        self._gap_position = 0.0
        self._gap_timer = QTimer(self)
        self._gap_timer.setInterval(16)
        self._gap_timer.timeout.connect(self._gap_tick)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._prepare)
        view.timeline_pos.connect(self._position_changed)
        view.range_ended.connect(self._range_ended)

    def configure(
        self, plan: RenderPlan | None, text: str | None, force: bool = False
    ) -> None:
        if self.closed or (not force and plan == self.plan and text == self.text):
            return
        # Текст рисуется в GUI поверх кадра. Его правка не меняет медиа,
        # позицию плеера и уже подготовленный диапазон сложного монтажа.
        previous = self.plan
        if (
            not force
            and plan is not None
            and previous is not None
            and text == self.text
            and replace(plan, texts=(), copy_reasons=())
            == replace(previous, texts=(), copy_reasons=())
        ):
            self.plan = plan
            self._update_live()
            return
        direct = (
            DirectPreview.from_plan(plan) if plan is not None and text is None else None
        )
        if (
            direct is not None
            and self.direct is not None
            and direct.segments() == self.direct.segments()
        ):
            self.plan, self.text, self.direct = plan, text, direct
            self._update_live()
            # Поворот применяется к уже показанному кадру, без плеера и кодирования.
            self.view._compose()
            return
        if direct is not None and plan is not None:
            was_direct = self.direct is not None
            self.resume = self.resume or self.view.player.playbackState().value == 1
            self.plan, self.text, self.direct = plan, text, direct
            self.position = min(self.position, max(0, plan.duration - 0.001))
            self.generation += 1
            self.timer.stop()
            self.prepared = None
            if self.worker is not None:
                self.worker.cancel()
            if not was_direct:
                self.view._cancel_prime()
                stop_media_player(self.view.player)
                self.view.player.setSource(QUrl())
            if self.directory is not None:
                self.retired.append(self.directory)
                self.directory = None
            self.view._cancel_frame_save()
            self.in_gap = False
            self._gap_timer.stop()
            self.view.set_timeline(
                direct.segments(), plan.duration, float(plan.canvas.fps or 25)
            )
            self.view.hide_preparing()
            self.view.err_label.hide()
            self.view.seek_timeline(self.position)
            self.view._display_position(self.position)
            self._update_live()
            if self.resume:
                self.view._cancel_prime()
                self.view._resume_after_load = True
                self.view.player.play()
            return
        # Последний кадр остаётся на экране до готовности нового участка.
        self.view.bake_frame_transform()
        self.direct = None
        self.in_gap = False
        self._gap_timer.stop()
        self.resume = self.resume or self.view.player.playbackState().value == 1
        self.plan, self.text = plan, text
        self.position = min(self.position, max(0, plan.duration - 0.001)) if plan else 0
        self.generation += 1
        self.prepared = None
        self.view._cancel_frame_save()
        self.view._cancel_prime()
        stop_media_player(self.view.player)
        self.view.player.setSource(QUrl())
        self.view._segments = []
        if self.directory is not None:
            self.retired.append(self.directory)
            self.directory = None
        if self.worker is not None:
            self.worker.cancel()
        if plan and plan.duration > 0:
            self.view._timeline_len = plan.duration
            self.view.show_preparing()
            self.timer.start()
        else:
            self.timer.stop()
            self.view.hide_preparing()
            self.view.set_timeline([], 0)

    def toggle(self) -> None:
        if (
            self.plan is not None
            and self.position >= self.plan.duration - 1e-6
            and self.direct is not None
        ):
            self.view.seek_timeline(0)
        if self.in_gap:
            self.resume = not self.resume
            if self.resume:
                self._start_gap_clock()
            else:
                self._gap_timer.stop()
            return
        if self.prepared or self.direct is not None:
            self.view._cancel_prime()
            if self.view.player.playbackState().value == 1:
                self.resume = False
                self.view.player.pause()
            else:
                self.resume = True
                self.view.player.play()
        else:
            self.resume = not self.resume
            self.timer.start()

    def stop(self) -> None:
        """Прекратить воспроизведение, включая ход времени в пустом участке."""
        self.resume = False
        self._gap_timer.stop()

    def seek(self, position: float) -> bool:
        if not self.plan or not self.plan.duration:
            return False
        self.position = min(max(0, position), max(0, self.plan.duration - 1e-6))
        if self.direct is not None:
            self._update_live()
            if not any(
                clip.start <= self.position < clip.end for clip in self.direct.video
            ):
                self._enter_gap()
                return False
            self.in_gap = False
            self._gap_timer.stop()
            return True
        if self.prepared and self.prepared[0] <= self.position < self.prepared[1]:
            return True
        self.resume = self.resume or self.view.player.playbackState().value == 1
        self.view.player.pause()
        self.view._cancel_prime()
        self.prepared = None
        self.generation += 1
        if self.worker is not None:
            self.worker.cancel()
        self._continuing = False
        self.view.show_preparing()
        self.timer.start()
        return False

    def frame_transform(self) -> tuple[int, bool, bool]:
        """Поворот и отражение кадра, который плеер отдаёт напрямую из исходника."""
        if self.direct is not None and 0 <= self.view._seg_idx < len(self.direct.video):
            clip = self.direct.video[self.view._seg_idx]
            return clip.rotation, clip.flip_h, clip.flip_v
        return 0, False, False

    @Slot(float)
    def _position_changed(self, position: float) -> None:
        self.position = position
        self._update_live()

    def _update_live(self) -> None:
        if self.plan is None or self.closed:
            return
        active = (
            tuple(
                item
                for item in self.plan.texts
                if item.enabled
                and item.start <= self.position
                and (not item.end or self.position < item.end)
            )
            if self.text is None
            else ()
        )
        if active != self._active_texts:
            self._active_texts = active
            self.view.set_overlays([asdict(item) for item in active])
        if self.direct is not None:
            self.view.audio.setVolume(
                self.direct.volume_at(self.position, self.plan.master)
            )

    @Slot(bool)
    def _range_ended(self, was_playing: bool) -> None:
        if self.closed:
            return
        if self.direct is not None:
            self.resume = was_playing
            end = self.direct.video[self.view._seg_idx].end
            if self.plan is not None and end < self.plan.duration - 1e-6:
                self.view.seek_timeline(end)
                if was_playing and not self.in_gap:
                    self.view._cancel_prime()
                    self.view._resume_after_load = True
                    self.view.player.play()
            else:
                self.resume = False
            return
        if self.plan and self.prepared and self.prepared[1] < self.plan.duration - 1e-6:
            self.resume = was_playing
            self.seek(self.prepared[1])
            # Воспроизведение идёт вперёд: запас перед курсором не нужен.
            self._continuing = True
        else:
            self.resume = False

    def _enter_gap(self) -> None:
        self.resume = self.resume or self.view.player.playbackState().value == 1
        self.in_gap = True
        self.view._cancel_prime()
        self.view.player.pause()
        previous = self.view._last_frame
        width = previous.width() if previous is not None else 640
        height = previous.height() if previous is not None else 360
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill(QColor("black"))
        self.view._last_frame = image
        self.view._compose()
        self.view._display_position(self.position)
        if self.resume:
            self._start_gap_clock()

    def _start_gap_clock(self) -> None:
        self._gap_position = self.position
        self._gap_start = time.monotonic()
        self._gap_timer.start()

    def _gap_tick(self) -> None:
        if not self.in_gap or not self.resume or self.closed or self.plan is None:
            self._gap_timer.stop()
            return
        position = min(
            self.plan.duration, self._gap_position + time.monotonic() - self._gap_start
        )
        if self.direct is not None and any(
            clip.start <= position < clip.end for clip in self.direct.video
        ):
            self.view.seek_timeline(position)
            self.view._cancel_prime()
            self.view._resume_after_load = True
            self.view.player.play()
            return
        self.view._display_position(position)
        if position >= self.plan.duration:
            self.resume = False
            self._gap_timer.stop()

    @Slot()
    def _prepare(self) -> None:
        if (
            self.closed
            or self.direct is not None
            or self.worker is not None
            or not self.plan
            or not self.plan.duration
        ):
            return
        start = min(self.position, max(0, self.plan.duration - 0.04))
        if not self._continuing:
            start = max(0, start - PREVIEW_LOOKBACK)
        self._continuing = False
        end = min(self.plan.duration, start + PREVIEW_RANGE)
        # Прогреть детектор sidechain до запрошенной позиции; возврат
        # ограничен пятью временами release и не меняет позицию курсора.
        if self.plan.ducking:
            start = max(
                0, start - max(rule.release for rule in self.plan.ducking) / 1000 * 5
            )
        retired, self.retired = self.retired, []
        worker = PreviewRenderWorker(
            replace(self.plan, texts=()),
            self.text,
            start,
            end,
            self.generation,
            retired,
            self,
        )
        self.worker = worker
        worker.progress.connect(self._progress)
        worker.finished.connect(self._finished)
        worker.start()

    @Slot(int)
    def _progress(self, percent: int) -> None:
        worker = self.worker
        if worker is not None and worker.generation == self.generation:
            self.view.show_preparing(percent)

    @Slot()
    def _finished(self) -> None:
        worker = self.worker
        if worker is None:
            return
        self.worker = None
        self.retired.extend(worker.retired)
        if worker.generation != self.generation or self.closed:
            if worker.directory is not None:
                self.retired.append(worker.directory)
            if not self.closed and self.direct is None:
                self.timer.start()
        elif worker.error:
            self.view.hide_preparing()
            self.view.clear_frame()
            self.view.err_label.setText(
                f"Предпросмотр недоступен: {worker.error[:300]}"
            )
            self.view.err_label.show()
            self.resume = False
        elif worker.directory is not None and self.plan:
            if self.directory is not None:
                self.retired.append(self.directory)
            self.directory = worker.directory
            self.prepared = (worker.start_time, worker.end_time, worker.output)
            self.view.hide_preparing()
            self.view._boundary_reached = False
            self._active_texts = ()
            self.view.set_overlays([])
            self._update_live()
            self.view.set_video_size(*worker.size)
            self.view.set_timeline(
                [(worker.start_time, worker.end_time, worker.output, 0)],
                self.plan.duration,
                worker.fps,
            )
            self.view.audio.setVolume(1)
            self.view.seek_timeline(self.position)
            self.view._resume_after_load = self.resume
        worker.deleteLater()
        if self.closed:
            self._cleanup()

    def close(self) -> None:
        self.closed = True
        self._gap_timer.stop()
        self.timer.stop()
        self.view._cancel_prime()
        stop_media_player(self.view.player)
        self.view.player.setSource(QUrl())
        if self.worker is not None:
            self.worker.cancel()
        else:
            self._cleanup()

    def _cleanup(self) -> None:
        if self.directory is not None:
            self.retired.append(self.directory)
            self.directory = None
        for directory in list(self.retired):
            try:
                directory.cleanup()
                self.retired.remove(directory)
            except OSError:
                pass
