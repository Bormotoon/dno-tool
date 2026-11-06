"""Исходники проекта: импорт, анализ, миниатюры, ремонт и свойства файла."""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, Slot
from PySide6.QtGui import (
    QIcon,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QMessageBox,
)

from src.domain.video.models import (
    AUDIO_EXTS,
    VIDEO_EXTS,
    Clip,
    MediaAsset,
    SourceSelection,
    new_iid,
)
from src.logger import logger
from src.workers.video_editor.ffmpeg_utils import (
    repair_file,
)
from src.workers.video_editor.metadata import ProbeMetadata
from src.workers.video_editor.render_command import RenderCommand
from src.workers.video_editor.thumbnail_worker import (
    STRIP_N,
)


ALL_EXTS = " ".join(f"*{e}" for e in VIDEO_EXTS + AUDIO_EXTS)
# Пауза перед повтором неуспешной генерации волны: refresh_all ходит
# часто, без паузы каждый проход создавал бы новое задание.
_WAVE_RETRY_S = 60.0


class VideoAssetMixin:
    """Часть VideoEditorDialog: состояние и виджеты принадлежат диалогу."""

    def import_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Импорт медиа", "", f"Медиа ({ALL_EXTS})"
        )
        self.import_paths(paths)

    def import_paths(
        self,
        paths: list[str],
        captions: dict[str, str] | None = None,
        recording_started: dict[str, float] | None = None,
    ) -> None:
        """Импортировать пачку одной правкой; метаданные дозагрузятся в фоне."""
        self._finish_edit_sessions()
        before = self._take_snapshot()
        known = {
            os.path.normcase(os.path.realpath(asset.path))
            for asset in self.project.assets
        }
        skipped = 0
        imported: list[MediaAsset] = []
        for value in paths:
            path = Path(value)
            key = os.path.normcase(os.path.realpath(value))
            kind = (
                "video"
                if path.suffix.lower() in VIDEO_EXTS
                else "audio" if path.suffix.lower() in AUDIO_EXTS else ""
            )
            if not kind or not path.is_file():
                skipped += 1
                continue
            if key not in known:
                imported.append(
                    MediaAsset(
                        path=str(path),
                        kind=kind,
                        name=(captions or {}).get(value) or path.name,
                        duration=0,
                        recording_started=(recording_started or {}).get(value, 0),
                    )
                )
                known.add(key)
        self.edits.import_assets(imported)
        self.push_undo(before, "Импорт файлов")
        if skipped:
            self.panel.summary.setText(
                f"Не импортировано: {skipped}. Файл отсутствует или формат не поддерживается."
            )

    def _import_one(self, path: str) -> None:
        ext = Path(path).suffix.lower()
        kind = (
            "video" if ext in VIDEO_EXTS else ("audio" if ext in AUDIO_EXTS else None)
        )
        if kind is None:
            QMessageBox.warning(self, "Импорт", f"Неподдерживаемый формат: {path}")
            return
        if not os.path.exists(path):
            return
        # дедуп: тот же файл не добавлять дважды, reuse старого ассета.
        # На таймлайн импорт больше не кладет: только через меню списка,
        # DnD на таймлайн или "Добавить все" (иначе каждый импорт рвал
        # текущую выкладку).
        key = os.path.normcase(os.path.abspath(path))
        for a in self.project.assets:
            if os.path.normcase(os.path.abspath(a.path)) == key:
                self.refresh_all()
                return
        before = self._take_snapshot()
        a = MediaAsset(
            iid=new_iid(), path=path, name=Path(path).name, kind=kind, duration=0.0
        )
        # .duration узнаем асинхронно через probe
        self.edits.import_assets([a])
        self._assets[a.iid] = self.project.assets[-1]
        self.push_undo(before, "Импорт")

    def _queue_probe(self, iid: str, path: str) -> None:
        """Новый токен анализа изолирует поздние ответы и повторные запросы."""
        pending = self._probe_latest.get(iid)
        if pending in self._probe_requests and self._probe_requests[pending] == (
            iid,
            path,
        ):
            return
        token = new_iid()
        self._probe_latest[iid] = token
        self._probe_requests[token] = (iid, path)
        self._probe_requested.emit(token, path)

    def _on_probed(self, token: str, meta: ProbeMetadata) -> None:
        if self._closing:
            return
        request = self._probe_requests.pop(token, None)
        if request is None:
            return
        iid, path = request
        if self._probe_latest.get(iid) != token:
            return
        self._probe_cache[(iid, path)] = deepcopy(meta)
        self._probe_errors.pop((iid, path), None)
        a = self._assets.get(iid)
        if a is None or a.path != path:
            return
        self._apply_probe_metadata(a, meta)
        self._after_probed(a, meta)

    @staticmethod
    def _apply_probe_metadata(a: MediaAsset, meta: ProbeMetadata) -> None:
        """Производные данные анализа живут отдельно от истории монтажа."""
        a.duration = float(meta.get("duration", 0) or 0)
        if not math.isfinite(a.duration) or a.duration < 0:
            a.duration = 0.0
        a.width = int(meta.get("width", 0) or 0)
        a.height = int(meta.get("height", 0) or 0)
        a.fps = float(meta.get("fps", 0) or 0)
        a.vcodec = str(meta.get("vcodec", "") or "")
        a.acodec = str(meta.get("acodec", "") or "")
        a.has_video = bool(meta.get("has_video", a.kind == "video"))
        a.has_audio = bool(meta.get("has_audio", False))
        try:
            a.channels = int(meta.get("channels", 0) or 0)
        except (TypeError, ValueError):
            a.channels = 0
        a.channel_layout = str(meta.get("channel_layout", "") or "")
        a.probe_error = ""
        videos, audios = meta.get("video_streams", []), meta.get("audio_streams", [])
        if videos and a.video_stream < len(videos):
            video = videos[a.video_stream]
            a.width, a.height = int(video.get("width", 0) or 0), int(
                video.get("height", 0) or 0
            )
            a.vcodec = str(video.get("codec_name", a.vcodec))
            from fractions import Fraction

            try:
                a.fps = float(
                    Fraction(
                        str(
                            video.get("avg_frame_rate")
                            or video.get("r_frame_rate")
                            or a.fps
                        )
                    )
                )
            except (ValueError, ZeroDivisionError):
                pass
        if audios and a.audio_stream < len(audios):
            audio = audios[a.audio_stream]
            a.channels = int(audio.get("channels", 0) or 0)
            a.channel_layout = str(audio.get("channel_layout", "") or "")
            a.acodec = str(audio.get("codec_name", a.acodec))
        if (videos and a.video_stream >= len(videos)) or (
            audios and a.audio_stream >= len(audios)
        ):
            a.probe_error = (
                "Выбранный поток недоступен. Выберите другой в просмотре исходника."
            )
        a.copy_signature = str(meta.get("copy_signature", "") or "")
        if a.duration <= 0:
            weird = meta.get("weird") or []
            a.probe_error = "; ".join(weird) if weird else "длительность не определена"

    def _after_probed(self, a: MediaAsset, meta: ProbeMetadata) -> None:
        iid = a.iid
        # Реально битый файл: предложить ремонт при импорте.
        # Уже пробовали и не помогло - больше не спрашиваем.
        weird = meta.get("weird") or []
        # Анализ выполнен, но длительности нет: для редактора это отказ.
        # Фиксируем понятную ошибку - иначе в списке вечно "анализ..."
        if a.duration <= 0:
            a.probe_error = "; ".join(weird) if weird else "длительность не определена"
            self.refresh_all()
            return
        if weird and a.path not in self._repair_failed:
            self._queue_repair_offer(iid, weird)
        # импорт на таймлайн не кладет: выкладка только вручную
        # (меню списка, DnD, "Добавить все"). Здесь лишь кэши превью.
        # Строку списка обновить сразу: длительность/разрешение видны
        # без выкладки (раньше их обновлял _auto_place через refresh_all,
        # без него список вечно висел на "анализ...").
        self.panel.update_asset(iid)
        if self.panel.current_iid() == iid:
            self._preview_current_source()
        # миниатюра первого кадра (видео) + форма волны (чистое аудио
        # и звук видеофайлов: связанная пара кладет его клипом на A1)
        if a.kind == "video" and a.has_video:
            self._request_thumb(iid)
        if a.kind == "audio" or a.has_audio:
            # волна нужна и звуку видеофайла: при выкладке на таймлайн
            # связанная пара положит его клипом на A1, волна уже готова
            self._request_wave(iid)

    def _request_thumb(self, iid: str) -> None:
        if iid not in self._visible_asset_iids or not self._media_allowed("frame", iid):
            return
        """Первый кадр в список файлов: один запрос на ассет (дедуп)."""
        a = self._assets.get(iid)
        if a is None or not os.path.exists(a.path):
            return
        if iid in self._thumbs or iid in self._thumb_pending:
            self._touch_media("frame", iid)
            return
        self._thumb_pending.add(iid)
        if not self._thumb_pool.request(
            iid, a.path, 0.0, mode="frame", extra={"stream": a.video_stream}
        ):
            self._thumb_pending.discard(iid)
            self._schedule_media_retry()

    def _request_wave(self, iid: str) -> None:
        """Форма волны для аудиоклипов таймлайна: один запрос на ассет."""
        if iid not in self._visible_audio_assets or not self._media_allowed(
            "wave", iid
        ):
            return
        a = self._assets.get(iid)
        if a is None or not os.path.exists(a.path):
            return
        if iid in self._waves or iid in self._wave_pending:
            self._touch_media("wave", iid)
            return
        failed_at = self._wave_failed.get(iid)
        if failed_at is not None:
            # уже падала: повтор не раньше таймаута, иначе очередь повторных заданий
            if time.monotonic() - failed_at < _WAVE_RETRY_S:
                return
            del self._wave_failed[iid]
        self._wave_pending.add(iid)
        if not self._thumb_pool.request(
            iid, a.path, 0.0, mode="wave", extra={"stream": a.audio_stream}
        ):
            self._wave_pending.discard(iid)
            self._schedule_media_retry()

    def _request_strip(self, clip: Clip, asset: MediaAsset) -> None:
        """Лента превью видеоклипа: N кадров across src_in..src_out."""
        if clip.iid not in self._visible_video_clips or not self._media_allowed(
            "strip", clip.iid
        ):
            return
        if asset is None or not os.path.exists(asset.path):
            return
        if not bool(getattr(asset, "has_video", False)):
            return
        dur = max(0.0, float(clip.src_out - clip.src_in))
        if dur <= 0:
            return
        orientation = RenderCommand.orientation_filters(
            clip.rotation, clip.flip_h, clip.flip_v
        )
        bounds = (clip.src_in, clip.src_out, orientation)
        if self._strip_bounds.get(clip.iid) == bounds and (
            clip.iid in self._strip_pix
        ):
            self._touch_media("strip", clip.iid)
            return
        if clip.iid in self._strip_pending:
            # trim во время генерации: дождаться готового, потом перезапросить
            return
        n = max(1, int(STRIP_N or 6))
        times = [clip.src_in + (i + 0.5) / n * dur for i in range(n)]
        self._strip_bounds[clip.iid] = bounds
        self._strip_pending.add(clip.iid)
        if not self._thumb_pool.request(
            clip.iid,
            asset.path,
            clip.src_in,
            mode="strip",
            extra={
                "times": times,
                "stream": asset.video_stream,
                "orientation": orientation,
            },
        ):
            self._strip_pending.discard(clip.iid)
            self._strip_bounds.pop(clip.iid, None)
            self._schedule_media_retry()

    def _forget_media_cache(self, iid: str) -> None:
        """Сбросить кэши миниатюры/волны (ремонт, повторный анализ)."""
        self._forget_media_ram(iid)
        self._probe_latest.pop(iid, None)
        for cache in (self._probe_cache, self._probe_errors):
            for key in [key for key in cache if key[0] == iid]:
                cache.pop(key, None)
        self._thumbs.pop(iid, None)
        self._waves.pop(iid, None)
        self._thumb_pending.discard(iid)
        self._wave_pending.discard(iid)
        self._wave_failed.pop(iid, None)
        # ленты клипов этого ассета тоже протухли
        for tr in self.project.tracks:
            for c in tr.clips:
                if c.asset_iid == iid:
                    self._forget_media_ram(c.iid)
                    self._strip_pix.pop(c.iid, None)
                    self._strip_bounds.pop(c.iid, None)
                    self._strip_pending.discard(c.iid)
        a = self._assets.get(iid)
        if a is not None:
            a.wave_error = ""

    def _on_thumb_ok(self, key: str, png: str, mode: str) -> None:
        """Готовый кадр/волна/лента из пула: в кэш + обновить виджет."""
        if self._closing:
            return
        if mode == "strip":
            self._strip_pending.discard(key)
            pix = QPixmap(png)
            if pix.isNull():
                self._strip_bounds.pop(key, None)
                return
            self._strip_pix[key] = pix
            self._remember_media_pixmap(mode, key, pix)
            self.timeline.update_media(key, mode)
            # Клип могли повернуть или подрезать, пока лента готовилась.
            for track in self.project.video_tracks():
                for clip in track.clips:
                    if clip.iid == key:
                        asset = self._assets.get(clip.asset_iid)
                        if asset is not None:
                            self._request_strip(clip, asset)
        elif mode == "wave":
            self._wave_pending.discard(key)
            pix = QPixmap(png)
            if pix.isNull():
                return
            self._waves[key] = pix
            self._remember_media_pixmap(mode, key, pix)
            self._wave_failed.pop(key, None)
            a = self._assets.get(key)
            if a is not None and a.wave_error:
                a.wave_error = ""
                self.panel.update_asset(a.iid)
            self.timeline.update_media(key, mode)
        else:
            self._thumb_pending.discard(key)
            pix = QPixmap(png)
            if pix.isNull():
                return
            icon = QIcon(pix)
            self._thumbs[key] = icon
            self.panel.set_thumb(key, icon)
            self._remember_media_pixmap(mode, key, pix)
        self._schedule_media_retry()

    def _on_thumb_failed(self, key: str, mode: str, err: str) -> None:
        """Кадр/волна не сгенерировались: снять pending, чтобы повторный
        анализ или refresh смогли запросить заново (иначе застревает)."""
        if self._closing:
            return
        self._media_failed(mode, key)
        if mode == "wave":
            self._wave_pending.discard(key)
            self._wave_failed[key] = time.monotonic()
        elif mode == "strip":
            # лента не собралась: снять pending и границы, следующий
            # refresh_all запросит заново; плейсхолдер (кадры...) остается
            self._strip_pending.discard(key)
            self._strip_bounds.pop(key, None)
        else:
            self._thumb_pending.discard(key)
        logger.error(f"thumbnail {mode} {key}: {err}")
        if mode == "wave":
            # причину видно прямо в списке файлов и свойствах, не только в логе
            a = self._assets.get(key)
            if a is not None:
                a.wave_error = err or "ошибка генерации"
                self.panel.update_asset(a.iid)

    def _on_probe_failed(self, token: str, err: str) -> None:
        if self._closing:
            return
        request = self._probe_requests.pop(token, None)
        if request is None:
            return
        iid, path = request
        if self._probe_latest.get(iid) != token:
            return
        self._probe_errors[(iid, path)] = err or "ошибка анализа"
        self._probe_cache.pop((iid, path), None)
        logger.error(f"probe {iid}: {err}")
        a = self._assets.get(iid)
        if a is not None and a.path == path:
            a.probe_error = err or "ошибка анализа"
            self.refresh_all()

    def _queue_repair_offer(self, iid: str, weird: list[str]) -> None:
        """Ремонт-промпты по одному: очередь вместо стека модалок."""
        if self._closing:
            return
        self._repair_queue.append((iid, weird))
        if not self._repair_in_progress:
            QTimer.singleShot(0, self._next_repair_offer)

    def _next_repair_offer(self) -> None:
        if (
            self._closing
            or self._project_io_thread is not None
            or self._repair_in_progress
            or not self._repair_queue
        ):
            return
        iid, weird = self._repair_queue.pop(0)
        self._offer_repair(iid, weird)

    def append_all_to_timeline(self) -> None:
        """Все файлы списка в конец таймлайна (порядок списка, без дублей).

        Без длительности (еще анализируются) молча пропускаем - иначе
        пачка модалок; уже лежащие на таймлайне не дублируем.
        """
        placed = {c.asset_iid for tr in self.project.tracks for c in tr.clips}
        eligible = [iid for iid in self.panel.ordered_iids() if iid not in placed]
        self.append_assets_to_timeline(eligible)

    def append_assets_to_timeline(self, asset_iids: list[str]) -> None:
        self._finish_edit_sessions()
        pending = [
            self._assets[iid]
            for iid in asset_iids
            if iid in self._assets and self._assets[iid].duration <= 0
        ]
        eligible = [
            iid
            for iid in asset_iids
            if iid in self._assets and self._assets[iid].duration > 0
        ]
        if pending:
            self.panel.summary.setText(
                f"Не добавлено: {len(pending)}. Дождитесь анализа или повторите его для файлов с ошибкой, затем нажмите «Добавить все»."
            )
        if not eligible:
            QMessageBox.information(
                self,
                "Таймлайн",
                "Нечего добавлять: все файлы уже на таймлайне или еще анализируются.",
            )
            return
        before = self._take_snapshot()
        try:
            added = self.project.append_assets(eligible)
        except ValueError as error:
            QMessageBox.warning(self, "Таймлайн", str(error))
            return
        self.push_undo(before, f"Добавить файлы ({len(added)})")
        if added:
            self._select_clip(*added[-1])
        if pending:
            self.panel.summary.setText(
                f"Добавлено: {len(added)} · ожидают анализа/исправления: {len(pending)}. «Добавить все» дозагрузит их без дублей."
            )

    def remove_assets(self, asset_iids: list[str]) -> None:
        self._finish_edit_sessions()
        removed = set(asset_iids) & self._assets.keys()
        before = self._take_snapshot()
        try:
            self.edits.remove_assets(removed)
        except ValueError as error:
            QMessageBox.warning(self, "Удалить файлы", str(error))
            return
        self.push_undo(before, f"Удалить файлы ({len(removed)})")

    def reprobe_assets(self, asset_iids: list[str]) -> None:
        for iid in asset_iids:
            if iid in self._assets:
                self.reprobe_asset(iid)

    def make_asset_title(self, asset_iid: str) -> None:
        from src.domain.video.models import TextOverlay

        asset = self.project.asset_by_iid(asset_iid)
        if asset is None:
            return
        self._finish_edit_sessions()
        clips = [
            clip
            for track in self.project.tracks
            for clip in track.clips
            if clip.asset_iid == asset_iid
        ]
        before = self._take_snapshot()
        self.edits.texts(
            [
                *self.project.text_overlays,
                TextOverlay(
                    text=asset.name,
                    start=min((clip.timeline_start for clip in clips), default=0),
                    end=max((clip.timeline_end for clip in clips), default=0),
                ),
            ]
        )
        self.push_undo(before, "Титр из названия источника")

    def preview_source(self, asset_iid: str) -> None:
        asset = self.project.asset_by_iid(asset_iid)
        if asset is None or asset.duration <= 0:
            return
        if not Path(asset.path).is_file() or (
            asset.probe_error and (asset.iid, asset.path) not in self._probe_cache
        ):
            QMessageBox.warning(
                self,
                "Исходник",
                asset.probe_error + "\nПовторите анализ или перепривяжите файл.",
            )
            return
        if self.monitors.mode == "program":
            self.monitors.set_mode("dual")
        self.source_monitor.load_asset(
            asset, self._probe_cache.get((asset.iid, asset.path))
        )
        self.source_monitor.btn_play.setFocus()

    def _preview_current_source(self) -> None:
        if self._closing or not hasattr(self, "source_monitor"):
            return
        iid = self.panel.current_iid()
        asset = self.project.asset_by_iid(iid) if iid else None
        if asset is not None and asset.duration > 0 and Path(asset.path).is_file():
            self.source_monitor.load_asset(
                asset, self._probe_cache.get((asset.iid, asset.path))
            )

    @Slot(str, object)
    def append_source_selection(
        self, asset_iid: str, selection: SourceSelection
    ) -> None:
        if not self._project_can_start_io():
            return
        asset = self.project.asset_by_iid(asset_iid)
        if asset is None:
            return
        self._finish_edit_sessions()
        source_in, source_out, video_stream, audio_stream = selection
        metadata = self._probe_cache.get((asset.iid, asset.path), {})
        videos = metadata.get("video_streams", [])
        duration = 0.0
        if 0 <= video_stream < len(videos):
            try:
                duration = float(videos[video_stream].get("duration", 0) or 0)
            except (TypeError, ValueError):
                duration = 0
            if duration > 0 and any(
                clip.asset_iid == asset.iid and clip.src_out > duration + 0.05
                for track in self.project.video_tracks()
                for clip in track.clips
            ):
                QMessageBox.warning(
                    self,
                    "Исходник",
                    "Выбранный видеопоток короче уже используемых фрагментов. Сначала измените их границы; поток не заменён.",
                )
                return
        before = self._take_snapshot()
        try:
            added = self.edits.append_source(
                asset.iid, source_in, source_out, video_stream, audio_stream, duration
            )
        except ValueError as error:
            QMessageBox.warning(self, "Исходник", str(error))
            return
        self._forget_media_cache(asset.iid)
        self.push_undo(before, "Добавить фрагмент исходника")
        if added:
            self._select_clip(*added[-1])
            if self.monitors.mode == "source":
                self.monitors.set_mode("dual")

    def _offer_repair(self, iid: str, weird: list[str]) -> None:
        """Вопрос при подозрении на сломанный контейнер (блокирует GUI,
        поэтому вызывается строго по одному из очереди)."""
        if self._closing:
            return
        a = self._assets.get(iid)
        if a is None:
            if self._repair_queue:
                QTimer.singleShot(0, self._next_repair_offer)
            return
        reasons = "\n".join(f"- {w}" for w in weird[:4])
        r = QMessageBox.question(
            self,
            "Подозрение на сломанный файл",
            f"{a.name}\n\nПризнаки некорректного хронометража:\n{reasons}\n\n"
            "Создать исправленную копию без перекодирования и заменить ссылку "
            "в проекте? Оригинал останется на месте.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if r == QMessageBox.StandardButton.Yes:
            self.repair_asset(iid, after=self._next_repair_offer)
        elif self._repair_queue:
            QTimer.singleShot(0, self._next_repair_offer)

    def repair_asset(self, iid: str, after: Callable[[], None] | None = None) -> None:
        """Remux без перекодирования: пересборка индекса, genpts."""
        if self._closing:
            return
        a = self._assets.get(iid)
        if (
            a is None
            or self._repair_in_progress
            or self._thread_is_running(self._export_thread)
            or self._audio_analysis is not None
            or (self._series is not None and self._series.active)
        ):
            if after:
                after()
            return
        self._repair_in_progress = True
        self._op_cancel.clear()
        self._show_progress("Ремонт контейнера...", cancel=True)
        result: dict[str, Any] = {}

        def work() -> None:
            try:
                ok, out = repair_file(a.path, cancel=self._op_cancel)
                result["ok"] = ok
                result["out"] = out
            except Exception as e:
                result["ok"] = False
                result["out"] = str(e)

        def done() -> None:
            self._repair_in_progress = False
            self._hide_progress()
            self.btn_cancel.setEnabled(True)
            self.progress.setRange(0, 100)
            out = result.get("out", "")
            if result.get("ok"):
                if out.startswith("already_ok:"):
                    QMessageBox.information(
                        self, "Ремонт", "Файл уже корректен, изменений не требуется."
                    )
                    # больше не предлагать ремонт для этого файла
                    self._repair_failed.add(out.split(":", 1)[1])
                else:
                    old = self._assets.get(iid)
                    if old is not None:
                        self._start_project_io(
                            "relink", "", self._take_snapshot(), replacements={iid: out}
                        )
                    QMessageBox.information(self, "Ремонт", f"Готово: {out}")
            elif out == "отменено":
                # отмена - не провал: без модалки, очередь промптов продолжается
                self._repair_failed.discard(a.path if a else "")
            else:
                self._repair_failed.add(a.path if a else "")
                QMessageBox.critical(self, "Ремонт", f"Не удалось починить:\n{out}")
            if after:
                after()

        th = threading.Thread(target=work, daemon=True)
        self._repair_thread = th
        timer = QTimer(self)
        self._repair_timer = timer
        timer.setInterval(25)

        def poll_repair() -> None:
            if th.is_alive():
                return
            timer.stop()
            timer.deleteLater()
            self._repair_thread = None
            self._repair_timer = None
            if not self._closing:
                done()

        timer.timeout.connect(poll_repair)
        th.start()
        timer.start()

    def append_asset_to_timeline(self, asset_iid: str, select: bool = False) -> None:
        """Добавить файл в конец подходящей дорожки."""
        a = self._assets.get(asset_iid)
        if a is None:
            return
        if a.duration <= 0:
            self._warn_no_duration(a)
            return
        cands = [t for t in self.project.tracks if t.kind == a.kind]
        if not cands:
            return
        track = cands[0]
        end = max([c.timeline_end for c in track.clips], default=0.0)
        if a.kind == "video" and bool(getattr(a, "has_audio", False)):
            # связанная пара: старт за концом обеих дорожек
            for at in self.project.tracks:
                if at.kind == "audio":
                    end = max(end, max([c.timeline_end for c in at.clips], default=0.0))
                    break
        self.add_asset_to_timeline(asset_iid, track.iid, end, select=select)

    def reprobe_asset(self, asset_iid: str) -> None:
        """Повторный анализ файла после сбоя или обновления исходника."""
        a = self._assets.get(asset_iid)
        if a is None or self._repair_in_progress:
            return
        a.probe_error = ""
        # файл мог измениться (ремонт): миниатюру/волну перегенерировать
        self._forget_media_cache(asset_iid)
        self.refresh_all()
        self._queue_probe(asset_iid, a.path)

    def _warn_no_duration(self, a: MediaAsset) -> None:
        """Сообщение при попытке добавить файл без длительности."""
        if getattr(a, "probe_error", ""):
            QMessageBox.warning(
                self,
                "Таймлайн",
                f"Анализ не удался: {a.probe_error}\n\n"
                "Проверьте медиабиблиотеки в диагностике FFmpeg / PyAV и повторите "
                "анализ: ПКМ по файлу в списке - Повторить анализ.",
            )
        else:
            QMessageBox.information(
                self, "Таймлайн", "Метаданные еще анализируются, подождите секунду."
            )

    def show_asset_in_folder(self, asset_iid: str) -> None:
        """Открыть папку с файлом в проводнике/файловом менеджере."""
        a = self._assets.get(asset_iid)
        if a is None or not os.path.exists(a.path):
            QMessageBox.warning(self, "Файл", "Файл не найден на диске.")
            return
        try:
            if os.name == "nt":
                os.startfile(
                    os.path.dirname(os.path.abspath(a.path)) or "."
                )  # noqa: S606
            else:
                import subprocess

                from src.utils.system.subprocess_env import (
                    build_clean_env,
                    safe_cwd_for_user,
                    wrap_as_user,
                )

                kwargs: dict[str, Any] = dict(
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                clean_env = build_clean_env()
                if clean_env is not None:
                    kwargs["env"] = clean_env
                kwargs["cwd"] = safe_cwd_for_user()
                subprocess.Popen(
                    wrap_as_user(
                        ["xdg-open", os.path.dirname(os.path.abspath(a.path)) or "."]
                    ),
                    **kwargs,
                )
        except Exception as e:
            QMessageBox.critical(self, "Файл", f"Не удалось открыть папку: {e}")

    def show_asset_info(self, asset_iid: str) -> None:
        """Свойства файла: путь, метаданные probe."""
        a = self._assets.get(asset_iid)
        if a is None:
            return
        rows = [
            f"Имя: {a.name}",
            f"Тип: {'видео' if a.kind == 'video' else 'аудио'}",
            f"Путь: {a.path}",
        ]
        if a.duration > 0:
            mm, ss = divmod(int(a.duration), 60)
            hh, mm = divmod(mm, 60)
            rows.append(f"Длительность: {hh:02d}:{mm:02d}:{ss:02d}")
            if a.width:
                rows.append(f"Разрешение: {a.width}x{a.height}")
            if a.fps:
                rows.append(f"FPS: {a.fps:.2f}")
            if a.vcodec:
                rows.append(f"Видеокодек: {a.vcodec}")
            if a.acodec:
                rows.append(f"Аудиокодек: {a.acodec}")
            if getattr(a, "channels", 0):
                layout = (getattr(a, "channel_layout", "") or "").strip()
                ch = f"{a.channels} кан."
                if layout and layout.lower() not in ("unknown",):
                    ch += f" ({layout})"
                rows.append(f"Каналы: {ch}")
            if getattr(a, "wave_error", ""):
                rows.append(f"Волна: {a.wave_error[:300]}")
        elif a.probe_error:
            rows.append(f"Ошибка анализа: {a.probe_error}")
        else:
            rows.append("Статус: анализируется...")
        QMessageBox.information(self, "Свойства файла", "\n".join(rows))

    def rename_asset(self, asset_iid: str) -> None:
        """Переименовать файл в списке проекта (только метка, не на диске)."""
        a = self._assets.get(asset_iid)
        if a is None:
            return
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(
            self, "Переименовать", "Название в проекте:", text=a.name
        )
        if not ok:
            return
        name = name.strip()
        if not name or name == a.name:
            return
        before = self._take_snapshot()
        self.edits.rename_asset(a.iid, name)
        self.push_undo(before, "Переименовать")

    def remove_asset(self, asset_iid: str) -> None:
        """Убрать файл из проекта: его клипы с таймлайна + сам ассет."""
        a = self._assets.get(asset_iid)
        if a is None:
            return
        if any(
            track.locked and any(clip.asset_iid == asset_iid for clip in track.clips)
            for track in self.project.tracks
        ):
            QMessageBox.warning(
                self,
                "Удалить файл",
                "Исходник используется на заблокированной дорожке.",
            )
            return
        before = self._take_snapshot()
        self.edits.remove_assets({asset_iid})
        del self._assets[asset_iid]
        self._forget_media_cache(asset_iid)
        if self._sel_clip and not any(
            c.iid == self._sel_clip for tr in self.project.tracks for c in tr.clips
        ):
            self._sel_clip = None
        self.push_undo(before, "Удалить файл")

    def delete_selected_asset(self) -> None:
        """Убрать из проекта файл, выбранный в списке."""
        iid = self.panel.current_iid()
        if iid:
            self.remove_asset(iid)

    def clear_assets(self) -> None:
        """Очистить список файлов; таймлайн при этом тоже очищается."""
        if not self.project.assets and not any(tr.clips for tr in self.project.tracks):
            return
        before = self._take_snapshot()
        for asset in self.project.assets:
            self._forget_media_ram(asset.iid)
        for track in self.project.tracks:
            for clip in track.clips:
                self._forget_media_ram(clip.iid)
        self._clear_media_ram()
        try:
            self.edits.remove_assets({asset.iid for asset in self.project.assets})
        except ValueError as error:
            QMessageBox.warning(self, "Очистить список", str(error))
            return
        self._assets = {}
        self._thumbs = {}
        self._waves = {}
        self._thumb_pending = set()
        self._wave_pending = set()
        self._wave_failed = {}
        self._strip_pix = {}
        self._strip_bounds = {}
        self._strip_pending = set()
        self._sel_clip = None
        self.push_undo(before, "Очистить список")
