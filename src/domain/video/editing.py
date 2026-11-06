"""Атомарные монтажные операции без зависимости от Qt и истории интерфейса."""

from __future__ import annotations

import math
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from .markers import VideoMarker
from .masks import VideoMask
from .models import Clip, ClipEdit, MediaAsset, Project, ProjectCanvas, TextOverlay
from .project_file import ProjectFileStore


# Свойства изображения клипа: хранятся только на видеочасти связанной пары.
VIDEO_KEYS = (
    "opacity",
    "scale",
    "position_x",
    "position_y",
    "rotation",
    "flip_h",
    "flip_v",
    "crop_left",
    "crop_top",
    "crop_right",
    "crop_bottom",
    "video_fade_in",
    "video_fade_out",
    "video_offset",
    "video_duration",
)


class ProjectCommands:
    """Проверяет намерение целиком и только затем изменяет живые объекты."""

    def __init__(self, project: Project) -> None:
        self.project = project

    def split_clip(self, iid: str, time: float) -> bool:
        """Разрезать только указанный клип и его пару, проверив блокировки заранее."""
        if type(time) not in (int, float) or not math.isfinite(time) or time < 0:
            raise ValueError(
                "Позиция разреза должна быть конечным неотрицательным числом."
            )
        selected = next(
            (c for tr in self.project.tracks for c in tr.clips if c.iid == iid), None
        )
        if (
            selected is None
            or not selected.timeline_start < time < selected.timeline_end
        ):
            return False
        targets = [
            (tr, c)
            for tr in self.project.tracks
            for c in tr.clips
            if c.iid == iid or (selected.link_iid and c.link_iid == selected.link_iid)
        ]
        if any(tr.locked for tr, _ in targets):
            raise ValueError("Дорожка связанного клипа заблокирована.")
        return self.project.split_at(time, _clip_iids={c.iid for _, c in targets})

    def properties(self, kind: str, iid: str, values: dict[str, Any]) -> bool:
        """Проверить совокупную правку инспектора до изменения живой модели."""
        working = deepcopy(self.project)
        self._apply_properties(working, kind, iid, values)
        ProjectFileStore.validate_changes(self.project.to_dict(), working.to_dict())
        if working.to_dict() == self.project.to_dict():
            return False
        self._apply_properties(self.project, kind, iid, values)
        return True

    @staticmethod
    def _apply_properties(
        project: Project, kind: str, iid: str, values: dict[str, Any]
    ) -> None:
        values = dict(values)
        if kind == "track":
            track = next((tr for tr in project.tracks if tr.iid == iid), None)
            if track is None:
                raise ValueError("Дорожка не найдена.")
            if track.locked and set(values) != {"locked"}:
                raise ValueError("Дорожка заблокирована.")
        if kind == "text":
            text = next((t for t in project.text_overlays if t.iid == iid), None)
            if text and any(
                tr.locked and any(c.iid == text.clip_iid for c in tr.clips)
                for tr in project.tracks
            ):
                raise ValueError("Дорожка титра заблокирована.")
        if kind != "clip":
            project.update_properties(kind, iid, values)
            return
        clip, track = next(
            ((c, tr) for tr in project.tracks for c in tr.clips if c.iid == iid),
            (None, None),
        )
        if clip is None:
            raise ValueError("Клип не найден.")
        pair = [
            (tr, c)
            for tr in project.tracks
            for c in tr.clips
            if c.iid == iid or (clip.link_iid and c.link_iid == clip.link_iid)
        ]
        if any(tr.locked for tr, _ in pair):
            raise ValueError("Дорожка связанного клипа заблокирована.")
        # Преобразования относятся к видео, даже если выделена звуковая половина пары.
        video = {key: values.pop(key) for key in VIDEO_KEYS if key in values}
        if video:
            target = next((c for tr, c in pair if tr.kind == "video"), None)
            if target is None:
                raise ValueError("У этого клипа нет видео.")
            project.update_properties("clip", target.iid, video)
        if "speed" in values:
            project.set_clip_speed(iid, values.pop("speed"))
            clip, track = next(
                (c, tr) for tr in project.tracks for c in tr.clips if c.iid == iid
            )
        bounds = {
            key: values.pop(key)
            for key in ("timeline_start", "src_in", "src_out", "duration")
            if key in values
        }
        if bounds:
            source_in = bounds.get("src_in", clip.src_in)
            source_out = bounds.get("src_out", clip.src_out)
            if "duration" in bounds:
                source_out = source_in + bounds["duration"] * clip.speed
            project.edit_clip(
                ClipEdit(
                    iid,
                    track.iid,
                    bounds.get("timeline_start", clip.timeline_start),
                    source_in,
                    source_out,
                )
            )
        audio = {
            key: values.pop(key)
            for key in (
                "volume",
                "mute",
                "fade_in",
                "fade_out",
                "normalization_gain",
                "audio_offset",
                "audio_duration",
            )
            if key in values
        }
        if audio:
            for tr in project.tracks:
                for other in tr.clips:
                    if other.iid == iid or (
                        clip.link_iid and other.link_iid == clip.link_iid
                    ):
                        project.update_properties("clip", other.iid, audio)
        if values:
            project.update_properties("clip", iid, values)

    @staticmethod
    def _volume(value: float) -> None:
        if (
            type(value) not in (float, int)
            or not math.isfinite(value)
            or not 0 <= value <= 2
        ):
            raise ValueError("Громкость должна находиться в диапазоне 0..2.")

    def clip_audio(
        self, iid: str, *, volume: float | None = None, mute: bool | None = None
    ) -> None:
        """Громкость и Mute относятся ко всей явно связанной паре."""
        if volume is not None:
            self._volume(volume)
        if mute is not None and type(mute) is not bool:
            raise ValueError("Mute должен быть логическим значением.")
        selected = next(
            (
                clip
                for track in self.project.tracks
                for clip in track.clips
                if clip.iid == iid
            ),
            None,
        )
        if selected is None:
            raise ValueError("Клип не найден.")
        targets = [
            (track, clip)
            for track in self.project.tracks
            for clip in track.clips
            if clip.iid == iid
            or (selected.link_iid and clip.link_iid == selected.link_iid)
        ]
        if any(track.locked for track, _ in targets):
            raise ValueError("Дорожка связанного клипа заблокирована.")
        for _, clip in targets:
            if volume is not None:
                clip.volume = volume
            if mute is not None:
                clip.mute = mute

    def master_audio(
        self,
        *,
        volume: float | None = None,
        limiter: bool | None = None,
        mute: bool | None = None,
    ) -> None:
        """Проверяет все параметры мастера до записи первого поля."""
        if volume is not None:
            self._volume(volume)
        if limiter is not None and type(limiter) is not bool:
            raise ValueError("Лимитер должен быть логическим значением.")
        if mute is not None and type(mute) is not bool:
            raise ValueError("Mute должен быть логическим значением.")
        if volume is not None:
            self.project.master_volume = volume
        if limiter is not None:
            self.project.audio_limiter = limiter
        if mute is not None:
            self.project.master_mute = mute

    def texts(self, values: Sequence[TextOverlay]) -> None:
        previous = {item.iid: item for item in self.project.text_overlays}
        incoming = {item.iid: item for item in values}
        for iid, item in previous.items():
            if incoming.get(iid) != item and any(
                tr.locked and any(c.iid == item.clip_iid for c in tr.clips)
                for tr in self.project.tracks
            ):
                raise ValueError("Дорожка титра заблокирована.")
        snapshot = self.project.to_dict()
        snapshot["text_overlays"] = [item.to_dict() for item in values]
        self.project.text_overlays = ProjectFileStore.validate_section(
            snapshot, "text_overlays"
        )

    def text_interval(self, iid: str, start: float, end: float) -> bool:
        """Изменить время титра; привязанный титр остаётся внутри своего клипа."""
        if (
            any(
                type(value) not in (int, float) or not math.isfinite(value)
                for value in (start, end)
            )
            or start < 0
            or end <= start
        ):
            raise ValueError("Для текста требуется интервал 0 ≤ начало < конец.")
        values = deepcopy(self.project.text_overlays)
        text = next((item for item in values if item.iid == iid), None)
        if text is None:
            raise ValueError("Текст не найден.")
        if text.clip_iid:
            clip = next(
                (
                    clip
                    for track in self.project.tracks
                    for clip in track.clips
                    if clip.iid == text.clip_iid
                ),
                None,
            )
            if (
                clip is None
                or start < clip.timeline_start - 1e-9
                or end > clip.timeline_end + 1e-9
            ):
                raise ValueError(
                    "Привязанный текст должен находиться внутри своего клипа."
                )
            start, end = max(0.0, start - clip.timeline_start), min(
                clip.duration, end - clip.timeline_start
            )
        if (text.start, text.end) == (start, end):
            return False
        text.start, text.end = start, end
        self.texts(values)
        return True

    def remove_component(self, iid: str, kind: str) -> bool:
        """Удалить только видео или звук, сохранив вторую часть и время монтажа."""
        if kind not in ("video", "audio"):
            raise ValueError("Выберите видео или звук.")
        working = deepcopy(self.project)
        selected = next(
            (
                clip
                for track in working.tracks
                for clip in track.clips
                if clip.iid == iid
            ),
            None,
        )
        if selected is None:
            return False
        target = next(
            (
                clip
                for track in working.tracks
                if track.kind == kind
                for clip in track.clips
                if clip.iid == iid
                or (selected.link_iid and clip.link_iid == selected.link_iid)
            ),
            None,
        )
        if target is None:
            return False
        working.unlink_clip(iid)
        working.delete_clips({target.iid}, ripple=False)
        self.project.tracks = working.tracks
        self.project.text_overlays = working.text_overlays
        self.project.markers = working.markers
        self.project.masks = working.masks
        return True

    def canvas(self, value: ProjectCanvas) -> None:
        snapshot = self.project.to_dict()
        from dataclasses import asdict

        snapshot["canvas"] = asdict(value)
        self.project.canvas = ProjectFileStore.validate_section(snapshot, "canvas")

    def markers(self, values: Sequence[VideoMarker]) -> None:
        snapshot = self.project.to_dict()
        snapshot["markers"] = [item.to_dict() for item in values]
        self.project.markers = ProjectFileStore.validate_section(snapshot, "markers")

    def masks(self, values: Sequence[VideoMask]) -> None:
        snapshot = self.project.to_dict()
        snapshot["masks"] = [item.to_dict() for item in values]
        self.project.masks = ProjectFileStore.validate_section(snapshot, "masks")

    def reorder_video(self, iids: Sequence[str]) -> None:
        tracks = {track.iid: track for track in self.project.video_tracks()}
        if len(iids) != len(tracks) or set(iids) != set(tracks):
            raise ValueError("Порядок должен включать каждый видеослой ровно один раз.")
        ordered = iter(tracks[iid] for iid in iids)
        self.project.tracks = [
            next(ordered) if track.kind == "video" else track
            for track in self.project.tracks
        ]

    def rename_asset(self, iid: str, name: str) -> None:
        asset = self.project.asset_by_iid(iid)
        if asset is None or not name.strip() or "\0" in name or len(name) > 1_048_576:
            raise ValueError("Некорректное название исходника.")
        asset.name = name.strip()

    def import_assets(self, values: Sequence[MediaAsset]) -> None:
        snapshot = self.project.to_dict()
        snapshot["assets"].extend(item.to_dict() for item in values)
        ProjectFileStore.validate_section(snapshot, "assets")
        self.project.assets.extend(deepcopy(list(values)))

    def remove_assets(self, iids: set[str]) -> None:
        """Удаляет источники с клипами, сохраняя проект при отказе блокировки."""
        clips = {
            clip.iid
            for track in self.project.tracks
            for clip in track.clips
            if clip.asset_iid in iids
        }
        self.project.delete_clips(clips)
        self.project.assets = [
            asset for asset in self.project.assets if asset.iid not in iids
        ]

    def insert_asset(self, iid: str, track_iid: str, position: float) -> Clip:
        """Находит свободное место сразу на обеих дорожках связанной пары."""
        asset = self.project.asset_by_iid(iid)
        track = next(
            (item for item in self.project.tracks if item.iid == track_iid), None
        )
        if asset is None or track is None or track.kind != asset.kind:
            raise ValueError("Источник не соответствует дорожке.")
        if (
            type(position) not in (float, int)
            or not math.isfinite(position)
            or position < 0
        ):
            raise ValueError("Недопустимое положение клипа.")
        if (
            type(asset.duration) not in (float, int)
            or not math.isfinite(asset.duration)
            or asset.duration <= 0
        ):
            raise ValueError("Длительность исходника не определена.")
        audio = (
            next(iter(self.project.audio_tracks()), None)
            if asset.kind == "video" and asset.has_audio
            else None
        )
        if asset.kind == "video" and asset.has_audio and audio is None:
            raise ValueError("Нет дорожки связанного звука.")
        if track.locked or (audio is not None and audio.locked):
            raise ValueError("Дорожка заблокирована.")
        for _ in range(len(track.clips) + (len(audio.clips) if audio else 0) + 2):
            candidate = track.free_start(position, asset.duration)
            if audio is not None:
                candidate = audio.free_start(candidate, asset.duration)
            if candidate == position:
                break
            position = candidate
        from .models import new_iid

        clip = Clip(
            asset_iid=iid,
            timeline_start=position,
            src_out=asset.duration,
            link_iid=new_iid() if audio else "",
        )
        track.clips.append(clip)
        if audio is not None:
            audio.clips.append(
                Clip(
                    asset_iid=iid,
                    timeline_start=position,
                    src_out=asset.duration,
                    link_iid=clip.link_iid,
                )
            )
        return clip

    def append_source(
        self,
        iid: str,
        start: float,
        end: float,
        video: int,
        audio: int,
        video_duration: float = 0,
    ) -> list[tuple[str, str]]:
        """Выбор потоков и выкладка фрагмента фиксируются одним действием."""
        asset = self.project.asset_by_iid(iid)
        if (
            asset is None
            or type(video) is not int
            or type(audio) is not int
            or min(video, audio) < 0
            or max(video, audio) > 1024
            or type(video_duration) not in (float, int)
            or not math.isfinite(video_duration)
            or video_duration < 0
        ):
            raise ValueError("Недопустимый выбор потоков.")
        if video_duration > 0 and (
            end > video_duration
            or any(
                clip.asset_iid == iid and clip.src_out > video_duration + 0.05
                for track in self.project.video_tracks()
                for clip in track.clips
            )
        ):
            raise ValueError("Выбранный видеопоток короче используемых фрагментов.")
        added = self.project.append_assets([iid], {iid: (start, end)})
        asset.video_stream, asset.audio_stream = video, audio
        return added
