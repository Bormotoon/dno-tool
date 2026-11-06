"""Модели видеопроекта: ассеты, клипы, дорожки, текст, проект.

Эффекты и переходы зарезервированы как пустые списки в Clip,
чтобы добавить их позже без переработки архитектуры.
"""

from __future__ import annotations

import math
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, NamedTuple

from .markers import MarkerRules, VideoMarker
from .masks import VideoMask

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm")
AUDIO_EXTS = (".mp3", ".wav", ".flac", ".ogg", ".aac", ".m4a")

CORNERS = ("TL", "TR", "BL", "BR")


class SourceSelection(NamedTuple):
    """Границы фрагмента и номера потоков среди потоков соответствующего типа."""

    start: float
    end: float
    video_stream: int
    audio_stream: int


def new_iid() -> str:
    """Новый стабильный идентификатор (как iid в DeviceModel)."""
    return uuid.uuid4().hex


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class MediaAsset:
    """Импортированный файл: только метаданные, байты не грузим."""

    iid: str = field(default_factory=new_iid)
    path: str = ""
    name: str = ""
    kind: str = "video"  # video | audio
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""
    has_video: bool = True
    has_audio: bool = False
    channels: int = 0  # аудиоканалы исходника (0 = неизвестно)
    channel_layout: str = ""  # stereo | mono | 5.1 | ...
    video_stream: int = 0  # номер среди видеопотоков
    audio_stream: int = 0  # номер среди аудиопотоков
    copy_signature: str = ""  # проверенные параметры выбранных потоков, без содержимого
    recording_started: float = 0.0  # достоверное время начала записи, Unix timestamp
    # ошибка анализа (не сохраняется в проект, только для списка UI)
    probe_error: str = ""
    # ошибка генерации волны (не сохраняется, только для списка UI)
    wave_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("probe_error", None)
        d.pop("wave_error", None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MediaAsset":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class TextOverlay:
    """Наложение в углу кадра; имя сохранено для совместимости проектов."""

    iid: str = field(default_factory=new_iid)
    text: str = ""
    corner: str = "BR"  # TL | TR | BL | BR
    fontsize: int = 24
    color: str = "white"
    font_family: str = ""  # семейство шрифта, пусто = системный по умолчанию
    margin_x: int = 20  # отступ от левого/правого края (по стороне угла), px кадра
    margin_y: int = 20  # отступ от верхнего/нижнего края (по стороне угла), px кадра
    enabled: bool = True
    back_color: str = "black"  # цвет подложки
    back_alpha: int = 50  # прозрачность подложки, % (0 = без подложки)
    box_pad: int = 6  # внутренний отступ подложки, px кадра (drawtext boxborderw)
    start: float = 0.0
    end: float = 0.0  # ноль означает до конца проекта
    fontfile: str = ""
    clip_iid: str = ""  # интервал относительно начала указанного клипа
    kind: str = "text"  # text | image | rectangle | ellipse | arrow
    image_path: str = ""
    width: int = 240
    height: int = 120
    opacity: int = 100
    stroke_width: int = 6
    filled: bool = False
    arrow_direction: str = "right"
    rotation: int = 0

    @property
    def has_content(self) -> bool:
        return (
            bool(self.text)
            if self.kind == "text"
            else (bool(self.image_path) if self.kind == "image" else True)
        )

    @property
    def label(self) -> str:
        if self.kind == "text":
            return self.text
        from pathlib import Path

        return {
            "image": Path(self.image_path).name or "Картинка",
            "rectangle": "Прямоугольник",
            "ellipse": "Эллипс",
            "arrow": "Стрелка",
        }.get(self.kind, "Наложение")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TextOverlay":
        d = dict(d)
        if "margin" in d and "margin_x" not in d and "margin_y" not in d:
            # проекты до раздельных X/Y: общий отступ - в обе оси
            try:
                m = max(0, min(300, int(d.pop("margin"))))
            except (TypeError, ValueError):
                m = 20
            d["margin_x"] = d["margin_y"] = m
        else:
            d.pop("margin", None)
        known = {f.name for f in cls.__dataclass_fields__.values()}
        obj = cls(**{k: v for k, v in d.items() if k in known})
        if not obj.iid:
            obj.iid = new_iid()
        if obj.corner not in CORNERS:
            obj.corner = "BR"
        try:
            obj.back_alpha = max(0, min(100, int(obj.back_alpha)))
        except (TypeError, ValueError):
            obj.back_alpha = 50
        if not obj.back_color:
            obj.back_color = "black"
        try:
            obj.box_pad = max(0, min(30, int(obj.box_pad)))
        except (TypeError, ValueError):
            obj.box_pad = 6
        for axis in ("margin_x", "margin_y"):
            try:
                setattr(obj, axis, max(0, min(16384, int(getattr(obj, axis)))))
            except (TypeError, ValueError):
                setattr(obj, axis, 20)
        return obj


@dataclass
class Clip:
    """Фрагмент ассета на таймлайне."""

    iid: str = field(default_factory=new_iid)
    asset_iid: str = ""
    timeline_start: float = 0.0
    src_in: float = 0.0
    src_out: float = 0.0
    volume: float = 1.0  # 0.0 - 2.0 (0-200%)
    mute: bool = False
    fade_in: float = 0.0
    fade_out: float = 0.0
    normalization_gain: float = 1.0
    audio_offset: float = 0.0
    audio_duration: float = 0.0
    opacity: float = 1.0
    scale: float = 1.0
    # Смещение центра слоя от центра холста в долях его ширины и высоты.
    position_x: float = 0.0
    position_y: float = 0.0
    rotation: int = 0
    # Отражение уже повёрнутого кадра, в координатах экрана.
    flip_h: bool = False
    flip_v: bool = False
    crop_left: float = 0.0
    crop_top: float = 0.0
    crop_right: float = 0.0
    crop_bottom: float = 0.0
    video_fade_in: float = 0.0
    video_fade_out: float = 0.0
    video_offset: float = 0.0
    video_duration: float = 0.0
    speed: float = 1.0
    # Связь V/A пары: одинаковый link_iid у видеоклипа на V и его звука
    # на A (один ассет, одни границы). Пусто = без пары.
    link_iid: str = ""
    # Резерв под будущее: эффекты, переходы, титры.
    effects: list[dict[str, Any]] = field(default_factory=list)
    transitions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.src_out - self.src_in) / self.speed

    @property
    def timeline_end(self) -> float:
        return self.timeline_start + self.duration

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Clip":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        obj = cls(**{k: v for k, v in d.items() if k in known})
        obj.volume = _clamp(float(obj.volume), 0.0, 2.0)
        return obj


@dataclass
class Track:
    """Дорожка таймлайна: video или audio."""

    iid: str = field(default_factory=new_iid)
    kind: str = "video"
    name: str = "V1"
    clips: list[Clip] = field(default_factory=list)
    hidden: bool = False
    locked: bool = False
    mute: bool = False
    solo: bool = False
    duck_source_iid: str = ""
    duck_threshold: float = 0.03
    duck_ratio: float = 8.0
    duck_attack: float = 20.0
    duck_release: float = 300.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "iid": self.iid,
            "kind": self.kind,
            "name": self.name,
            "clips": [c.to_dict() for c in self.clips],
            **{
                key: getattr(self, key)
                for key in (
                    "hidden",
                    "locked",
                    "mute",
                    "solo",
                    "duck_source_iid",
                    "duck_threshold",
                    "duck_ratio",
                    "duck_attack",
                    "duck_release",
                )
            },
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Track":
        return cls(
            iid=d.get("iid", new_iid()),
            kind=d.get("kind", "video"),
            name=d.get("name", "V1"),
            clips=[Clip.from_dict(c) for c in d.get("clips", [])],
            **{
                key: d[key]
                for key in (
                    "hidden",
                    "locked",
                    "mute",
                    "solo",
                    "duck_source_iid",
                    "duck_threshold",
                    "duck_ratio",
                    "duck_attack",
                    "duck_release",
                )
                if key in d
            },
        )

    def sorted_clips(self) -> list[Clip]:
        return sorted(self.clips, key=lambda c: c.timeline_start)

    def free_start(self, t: float, dur: float) -> float:
        """Первое свободное окно [t, t+dur) без наложений на дорожке."""
        t = max(0.0, t)
        dur = max(0.0, dur)
        ordered = sorted(self.clips, key=lambda c: c.timeline_start)
        for _ in range(len(ordered) + 1):
            for c in ordered:
                if t < c.timeline_end - 1e-9 and t + dur > c.timeline_start + 1e-9:
                    t = c.timeline_end
                    break
            else:
                break
        return t

    def ripple_after(self, t: float, delta: float) -> None:
        """Сдвинуть все клипы правее t на delta (для ripple-delete)."""
        for c in self.clips:
            if c.timeline_start >= t - 1e-9:
                c.timeline_start = max(0.0, c.timeline_start + delta)


@dataclass(frozen=True)
class ClipEdit:
    """Намерение переместить или обрезать клип, без зависимости от сцены."""

    clip_iid: str
    track_iid: str
    timeline_start: float
    src_in: float
    src_out: float


@dataclass
class ProjectCanvas:
    """Холст и рациональный FPS; нули означают автоматический выбор."""

    width: int = 0
    height: int = 0
    fps_num: int = 0
    fps_den: int = 1
    fit: str = "contain"


@dataclass
class Project:
    """Корень монтажа."""

    name: str = "Без названия"
    assets: list[MediaAsset] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    master_volume: float = 1.0
    audio_limiter: bool = False
    text_overlays: list[TextOverlay] = field(default_factory=list)
    iid: str = field(default_factory=new_iid)
    canvas: ProjectCanvas = field(default_factory=ProjectCanvas)
    markers: list[VideoMarker] = field(default_factory=list)
    masks: list[VideoMask] = field(default_factory=list)
    master_mute: bool = False

    @property
    def effective_master_volume(self) -> float:
        """Мастер-громкость с учётом выключения общего звука."""
        return 0.0 if self.master_mute else self.master_volume

    def __post_init__(self) -> None:
        if not self.tracks:
            self.tracks = [
                Track(kind="video", name="V1"),
                Track(kind="audio", name="A1"),
            ]

    def asset_by_iid(self, iid: str) -> MediaAsset | None:
        for a in self.assets:
            if a.iid == iid:
                return a
        return None

    def append_assets(
        self,
        asset_iids: list[str],
        source_ranges: dict[str, tuple[float, float]] | None = None,
    ) -> list[tuple[str, str]]:
        """Атомарно добавить упорядоченную пачку в конец общего монтажа."""
        assets = []
        for iid in dict.fromkeys(asset_iids):
            asset = self.asset_by_iid(iid)
            if asset is None or asset.kind not in ("video", "audio"):
                raise ValueError("Неизвестный источник или тип медиа.")
            if (
                isinstance(asset.duration, bool)
                or not isinstance(asset.duration, (int, float))
                or not math.isfinite(asset.duration)
                or asset.duration <= 0
            ):
                raise ValueError("Дождитесь завершения анализа всех выбранных файлов.")
            track = next(
                (item for item in self.tracks if item.kind == asset.kind), None
            )
            if track is None:
                raise ValueError("Нет дорожки для выбранного источника.")
            audio = (
                next((item for item in self.tracks if item.kind == "audio"), None)
                if asset.kind == "video" and asset.has_audio
                else None
            )
            if asset.kind == "video" and asset.has_audio and audio is None:
                raise ValueError("Нет аудиодорожки для связанного звука.")
            if track.locked or (audio is not None and audio.locked):
                raise ValueError("Дорожка заблокирована.")
            source_in, source_out = (source_ranges or {}).get(
                iid, (0.0, asset.duration)
            )
            if (
                any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in (source_in, source_out)
                )
                or not 0 <= source_in < source_out <= asset.duration
            ):
                raise ValueError(
                    "Диапазон исходника должен лежать внутри его длительности."
                )
            assets.append((asset, track, audio, source_in, source_out))
        start = self.duration()
        added = []
        for asset, track, audio, source_in, source_out in assets:
            link = new_iid() if audio else ""
            clip = Clip(
                asset_iid=asset.iid,
                timeline_start=start,
                src_in=source_in,
                src_out=source_out,
                link_iid=link,
            )
            track.clips.append(clip)
            if audio:
                audio.clips.append(
                    Clip(
                        asset_iid=asset.iid,
                        timeline_start=start,
                        src_in=source_in,
                        src_out=source_out,
                        link_iid=link,
                    )
                )
            added.append((clip.iid, track.iid))
            start += source_out - source_in
        return added

    def video_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "video"]

    def audio_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "audio"]

    def duration(self) -> float:
        end = 0.0
        for t in self.tracks:
            for c in t.clips:
                end = max(end, c.timeline_end)
        return end

    def update_properties(self, kind: str, iid: str, values: dict[str, Any]) -> bool:
        """Проверить изменённый объект до применения: посторонние не мешают правке."""
        from .project_file import ProjectFileStore

        before = self.to_dict()
        snapshot = self.to_dict()
        candidates = (
            snapshot["tracks"]
            if kind == "track"
            else (
                [clip for track in snapshot["tracks"] for clip in track["clips"]]
                if kind == "clip"
                else snapshot["text_overlays"] if kind == "text" else []
            )
        )
        target = next((item for item in candidates if item["iid"] == iid), None)
        if target is None or "iid" in values:
            raise ValueError(
                "Объект правки не найден или изменяется его идентификатор."
            )
        if kind == "clip" and any(
            track.locked and any(clip.iid == iid for clip in track.clips)
            for track in self.tracks
        ):
            raise ValueError("Дорожка заблокирована.")
        if all(target.get(key) == value for key, value in values.items()):
            return False
        target.update(values)
        ProjectFileStore.validate_changes(before, snapshot)
        original = next(
            item
            for item in (
                self.tracks
                if kind == "track"
                else (
                    [clip for track in self.tracks for clip in track.clips]
                    if kind == "clip"
                    else self.text_overlays
                )
            )
            if item.iid == iid
        )
        # from_dict приводит значения так же, как при чтении файла проекта.
        checked = (
            Track.from_dict(target)
            if kind == "track"
            else (
                Clip.from_dict(target)
                if kind == "clip"
                else TextOverlay.from_dict(target)
            )
        )
        for key in values:
            setattr(original, key, deepcopy(getattr(checked, key)))
        return True

    def add_track(self, kind: str, name: str) -> Track:
        """Верхние видеослои добавляются в конец списка дорожек."""
        if kind not in ("video", "audio") or not name.strip():
            raise ValueError("Укажите тип и название дорожки.")
        track = Track(kind=kind, name=name.strip())
        self.tracks.append(track)
        return track

    def set_clip_speed(self, iid: str, speed: float) -> bool:
        """Изменить скорость пары без сдвига посторонних клипов, с проверкой коллизий."""
        from .project_file import ProjectFileStore

        if isinstance(speed, bool) or speed not in (0.5, 1, 2):
            raise ValueError("Поддерживаются скорости 0.5×, 1× и 2×.")
        working = deepcopy(self)
        clip = next(
            (
                clip
                for track in working.tracks
                for clip in track.clips
                if clip.iid == iid
            ),
            None,
        )
        if clip is None:
            raise ValueError("Клип не найден.")
        changed = False
        for track in working.tracks:
            for other in track.clips:
                if other.iid == iid or (
                    clip.link_iid and other.link_iid == clip.link_iid
                ):
                    if track.locked:
                        raise ValueError("Дорожка заблокирована.")
                    factor = other.speed / speed
                    for key in (
                        "fade_in",
                        "fade_out",
                        "audio_offset",
                        "audio_duration",
                        "video_fade_in",
                        "video_fade_out",
                        "video_offset",
                        "video_duration",
                    ):
                        setattr(other, key, getattr(other, key) * factor)
                    changed = changed or other.speed != speed
                    other.speed = speed
        ProjectFileStore.validate_changes(self.to_dict(), working.to_dict())
        if changed:
            self.tracks = working.tracks
            self._keep_timed_items()
        return changed

    def remove_track(self, iid: str) -> bool:
        """Удаление дорожки сохраняет временные позиции остальных клипов."""
        track = next((item for item in self.tracks if item.iid == iid), None)
        if track is None:
            return False
        if track.locked or sum(item.kind == track.kind for item in self.tracks) <= 1:
            raise ValueError(
                "Нельзя удалить заблокированную или последнюю дорожку данного типа."
            )
        removed = {clip.iid for clip in track.clips}
        links = {clip.link_iid for clip in track.clips if clip.link_iid}
        if any(
            other.locked and any(clip.link_iid in links for clip in other.clips)
            for other in self.tracks
        ):
            raise ValueError("Связанная дорожка заблокирована.")
        for clip in track.clips:
            self.unlink_clip(clip.iid)
        self.tracks.remove(track)
        self.text_overlays = [
            text for text in self.text_overlays if text.clip_iid not in removed
        ]
        for other in self.tracks:
            if other.duck_source_iid == iid:
                other.duck_source_iid = ""
        self._keep_timed_items()
        return True

    def split_at(self, t: float, *, _clip_iids: set[str] | None = None) -> bool:
        """Разрезать все клипы под курсором, сохранив свойства и V/A-пары."""
        if (
            isinstance(t, bool)
            or not isinstance(t, (int, float))
            or not math.isfinite(t)
            or t < 0
        ):
            raise ValueError(
                "Позиция разреза должна быть конечным неотрицательным числом."
            )
        if any(
            track.locked
            and any(
                clip.timeline_start < t < clip.timeline_end
                and (_clip_iids is None or clip.iid in _clip_iids)
                for clip in track.clips
            )
            for track in self.tracks
        ):
            raise ValueError("Разрез затрагивает заблокированную дорожку.")
        changed = False
        right_links: dict[str, str] = {}
        for track in self.tracks:
            for clip in list(track.clips):
                if not clip.timeline_start < t < clip.timeline_end or (
                    _clip_iids is not None and clip.iid not in _clip_iids
                ):
                    continue
                mid = clip.src_in + (t - clip.timeline_start) * clip.speed
                right = deepcopy(clip)
                right.iid = new_iid()
                right.timeline_start = t
                right.src_in = mid
                if clip.video_fade_in or clip.video_fade_out:
                    clip.video_duration = clip.video_duration or clip.duration
                    right.video_duration = clip.video_duration
                    right.video_offset += t - clip.timeline_start
                for text in list(self.text_overlays):
                    if text.clip_iid != clip.iid:
                        continue
                    offset = t - clip.timeline_start
                    finish = text.end or clip.duration
                    if finish > offset and text.start < clip.duration:
                        duplicate = deepcopy(text)
                        duplicate.iid = new_iid()
                        duplicate.clip_iid = right.iid
                        duplicate.start = max(0, text.start - offset)
                        duplicate.end = finish - offset
                        self.text_overlays.append(duplicate)
                    if text.start >= offset:
                        self.text_overlays.remove(text)
                    else:
                        text.end = min(finish, offset)
                if clip.fade_in or clip.fade_out:
                    clip.audio_duration = clip.audio_duration or clip.duration
                    right.audio_duration = clip.audio_duration
                    right.audio_offset += t - clip.timeline_start
                if clip.link_iid:
                    right.link_iid = right_links.setdefault(clip.link_iid, new_iid())
                clip.src_out = mid
                track.clips.append(right)
                changed = True
        return changed

    def edit_range(self, start: float, end: float, *, keep: bool = False) -> bool:
        """Вырезать или оставить диапазон на всех дорожках одной транзакцией."""
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in (start, end)
            )
            or start < 0
            or end <= start
            or end > self.duration() + 1e-9
        ):
            raise ValueError(
                "Укажите диапазон внутри проекта: 0 ≤ In < Out ≤ длительность."
            )
        working = deepcopy(self)
        if any(track.locked and track.clips for track in self.tracks):
            raise ValueError("Правка диапазона затрагивает заблокированную дорожку.")
        working.split_at(start)
        working.split_at(end)
        for track in working.tracks:
            if keep:
                track.clips = [
                    clip
                    for clip in track.clips
                    if clip.timeline_start >= start - 1e-9
                    and clip.timeline_end <= end + 1e-9
                ]
                for clip in track.clips:
                    clip.timeline_start = max(0.0, clip.timeline_start - start)
            else:
                track.clips = [
                    clip
                    for clip in track.clips
                    if not (
                        clip.timeline_start >= start - 1e-9
                        and clip.timeline_end <= end + 1e-9
                    )
                ]
                track.ripple_after(end, -(end - start))
        if working.tracks == self.tracks:
            return False
        from .text_timing import TextTiming

        if keep:
            working.text_overlays = TextTiming.keep_time(
                working.text_overlays, start, end, self.duration()
            )
        else:
            working.text_overlays = TextTiming.remove_time(
                working.text_overlays, [(start, end)], self.duration()
            )
        self.tracks = working.tracks
        live = {clip.iid for track in self.tracks for clip in track.clips}
        self.text_overlays = [
            text
            for text in working.text_overlays
            if not text.clip_iid or text.clip_iid in live
        ]
        if keep:
            self.markers = MarkerRules.keep(self.markers, start, end)
            self.masks = MarkerRules.keep(self.masks, start, end)
        else:
            self.markers = MarkerRules.remove(self.markers, [(start, end)])
            self.masks = MarkerRules.remove(self.masks, [(start, end)])
        return True

    def unlink_clip(self, iid: str) -> bool:
        """Разорвать явную связь; refresh и split её не восстанавливают."""
        clip = next(
            (clip for track in self.tracks for clip in track.clips if clip.iid == iid),
            None,
        )
        if clip is None or not clip.link_iid:
            return False
        link = clip.link_iid
        if any(
            track.locked and any(other.link_iid == link for other in track.clips)
            for track in self.tracks
        ):
            raise ValueError(
                "Нельзя разделить видео и звук на заблокированной дорожке."
            )
        for track in self.tracks:
            for other in track.clips:
                if other.link_iid == link:
                    other.link_iid = ""
        return True

    def crossfade_audio(self, clip_iid: str, duration: float) -> bool:
        """Свести соседний звук без сдвига следующих клипов; нужны запасы исходников."""
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (float, int))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            raise ValueError(
                "Длительность перехода должна быть положительным конечным числом."
            )
        working = deepcopy(self)
        clips = [clip for track in working.audio_tracks() for clip in track.clips]
        left = next((clip for clip in clips if clip.iid == clip_iid), None)
        if left is None:
            raise ValueError("Выберите аудиоклип.")
        right = min(
            (
                clip
                for clip in clips
                if clip.timeline_start >= left.timeline_end - 1e-6
                and clip.iid != left.iid
            ),
            key=lambda clip: clip.timeline_start,
            default=None,
        )
        if right is None or abs(right.timeline_start - left.timeline_end) > 1e-6:
            raise ValueError(
                "Для перехода нужны два соседних звуковых клипа без промежутка."
            )
        affected = {left.iid, right.iid}
        links = {clip.link_iid for clip in (left, right) if clip.link_iid}
        if any(
            track.locked
            and any(
                clip.iid in affected or clip.link_iid in links for clip in track.clips
            )
            for track in working.tracks
        ):
            raise ValueError("Переход затрагивает заблокированную дорожку.")
        left_asset = working.asset_by_iid(left.asset_iid)
        right_asset = working.asset_by_iid(right.asset_iid)
        half = duration / 2
        if (
            left_asset is None
            or right_asset is None
            or left.src_out + half * left.speed > left_asset.duration + 1e-9
            or right.src_in < half * right.speed
            or duration > min(left.duration + half, right.duration + half)
        ):
            raise ValueError(
                "Недостаточно материала до/после границ исходника для перехода. Уменьшите длительность или подрежьте клипы."
            )
        working.unlink_clip(left.iid)
        working.unlink_clip(right.iid)
        left.src_out += half * left.speed
        right.src_in -= half * right.speed
        right.timeline_start -= half
        left.fade_out, right.fade_in = duration, duration
        for clip in (left, right):
            clip.audio_offset, clip.audio_duration = 0, clip.duration
        for track in working.audio_tracks():
            track.clips = [clip for clip in track.clips if clip.iid != right.iid]
        working.tracks.append(
            Track(
                kind="audio", name=f"A{len(working.audio_tracks()) + 1}", clips=[right]
            )
        )
        self.tracks = working.tracks
        return True

    def crossfade_video(self, iid: str, duration: float) -> bool:
        """Переход на отдельном верхнем слое без сдвига последующих клипов."""
        from .project_file import ProjectFileStore

        if (
            isinstance(duration, bool)
            or not isinstance(duration, (float, int))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            raise ValueError("Укажите положительную длительность перехода.")
        working = deepcopy(self)
        track = next(
            (
                track
                for track in working.video_tracks()
                if any(clip.iid == iid for clip in track.clips)
            ),
            None,
        )
        if track is None or track.locked:
            raise ValueError("Выберите клип на незаблокированной видеодорожке.")
        left = next(clip for clip in track.clips if clip.iid == iid)
        right = next(
            (
                clip
                for clip in track.sorted_clips()
                if abs(clip.timeline_start - left.timeline_end) < 1e-6
                and clip.iid != left.iid
            ),
            None,
        )
        if right is None:
            raise ValueError(
                "Нужен следующий видеоклип без промежутка на той же дорожке."
            )
        half = duration / 2
        asset = working.asset_by_iid(left.asset_iid)
        if (
            asset is None
            or left.src_out + half * left.speed > asset.duration + 1e-9
            or right.src_in < half * right.speed
            or duration > min(left.duration + half, right.duration + half)
        ):
            raise ValueError(
                "Недостаточно материала исходников для перехода. Подрежьте клипы или уменьшите длительность."
            )
        if any(
            clip.iid != iid
            and clip.iid != right.iid
            and clip.timeline_start < left.timeline_end + half
            and clip.timeline_end > left.timeline_end
            for clip in track.clips
        ):
            raise ValueError("Переход пересечёт посторонний клип.")
        working.unlink_clip(left.iid)
        working.unlink_clip(right.iid)
        left.src_out += half * left.speed
        right.src_in -= half * right.speed
        right.timeline_start -= half
        right.video_fade_in = duration
        right.video_offset = 0
        right.video_duration = right.duration
        track.clips.remove(right)
        index = working.tracks.index(track)
        working.tracks.insert(
            index + 1,
            Track(kind="video", name=f"{track.name} — переход", clips=[right]),
        )
        ProjectFileStore.validate_changes(self.to_dict(), working.to_dict())
        self.tracks = working.tracks
        return True

    def edit_clip(self, edit: ClipEdit) -> bool:
        """Проверить и атомарно применить геометрию клипа вместе с V/A-парой."""
        source = next(
            (tr for tr in self.tracks if any(c.iid == edit.clip_iid for c in tr.clips)),
            None,
        )
        target = next((tr for tr in self.tracks if tr.iid == edit.track_iid), None)
        if source is None or target is None or source.kind != target.kind:
            raise ValueError(
                "Клип и целевая дорожка должны существовать и иметь один тип."
            )
        if source.locked or target.locked:
            raise ValueError("Дорожка заблокирована.")
        clip = next(c for c in source.clips if c.iid == edit.clip_iid)
        values = (edit.timeline_start, edit.src_in, edit.src_out)
        if any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in values
        ):
            raise ValueError("Границы клипа должны быть конечными числами.")
        if edit.timeline_start < 0 or edit.src_in < 0 or edit.src_out <= edit.src_in:
            raise ValueError(
                "Начало клипа не может быть отрицательным, а длина — нулевой."
            )
        asset = self.asset_by_iid(clip.asset_iid)
        if (
            asset is None
            or not math.isfinite(asset.duration)
            or asset.duration <= 0
            or edit.src_out > asset.duration + 1e-9
        ):
            raise ValueError("Границы клипа выходят за длительность исходника.")
        mates = [
            (tr, other)
            for tr in self.tracks
            for other in tr.clips
            if clip.link_iid
            and other.iid != clip.iid
            and other.link_iid == clip.link_iid
        ]
        if len(mates) > 1:
            raise ValueError("Связь клипа содержит больше одной пары.")
        if mates and (
            mates[0][1].asset_iid != clip.asset_iid or mates[0][0].kind == source.kind
        ):
            raise ValueError(
                "Связанная пара должна содержать видео и звук одного исходника."
            )
        candidates = [(target, clip), *mates]
        if any(track.locked for track, _clip in candidates):
            raise ValueError("Связанная дорожка заблокирована.")
        end = edit.timeline_start + (edit.src_out - edit.src_in) / clip.speed
        if not math.isfinite(end):
            raise ValueError("Конец клипа выходит за допустимый диапазон времени.")
        for track, candidate in candidates:
            if any(
                other.iid != candidate.iid
                and edit.timeline_start < other.timeline_end - 1e-9
                and end > other.timeline_start + 1e-9
                for other in track.clips
            ):
                raise ValueError("Клип пересекает соседний клип на дорожке.")
        if target is source and all(
            abs(a - b) < 1e-9
            for a, b in zip(values, (clip.timeline_start, clip.src_in, clip.src_out))
        ):
            return False
        for _track, candidate in candidates:
            if candidate.src_in != edit.src_in or candidate.src_out != edit.src_out:
                candidate.audio_offset = 0.0
                candidate.audio_duration = (
                    edit.src_out - edit.src_in
                ) / candidate.speed
                candidate.fade_in = min(candidate.fade_in, candidate.audio_duration)
                candidate.fade_out = min(candidate.fade_out, candidate.audio_duration)
                candidate.video_offset = 0
                candidate.video_duration = candidate.audio_duration
                candidate.video_fade_in = min(
                    candidate.video_fade_in, candidate.video_duration
                )
                candidate.video_fade_out = min(
                    candidate.video_fade_out, candidate.video_duration
                )
            candidate.timeline_start = edit.timeline_start
            candidate.src_in = edit.src_in
            candidate.src_out = edit.src_out
        if target is not source:
            source.clips.remove(clip)
            target.clips.append(clip)
        self._keep_timed_items()
        return True

    def delete_clips(self, clip_iids: set[str], *, ripple: bool = False) -> bool:
        """Удалить клипы с парами; ripple вырезает объединение интервалов."""
        victims = [
            clip
            for track in self.tracks
            for clip in track.clips
            if clip.iid in clip_iids
        ]
        if not victims:
            return False
        links = {clip.link_iid for clip in victims if clip.link_iid}
        victims = [
            clip
            for track in self.tracks
            for clip in track.clips
            if clip.iid in clip_iids or (clip.link_iid and clip.link_iid in links)
        ]
        removed = {clip.iid for clip in victims}
        if any(
            track.locked
            and (ripple or any(clip.iid in removed for clip in track.clips))
            for track in self.tracks
        ):
            raise ValueError("Удаление затрагивает заблокированную дорожку.")
        intervals: list[tuple[float, float]] = []
        if ripple:
            for clip in sorted(victims, key=lambda clip: clip.timeline_start):
                start, end = clip.timeline_start, clip.timeline_end
                if intervals and start <= intervals[-1][1]:
                    intervals[-1] = (intervals[-1][0], max(end, intervals[-1][1]))
                else:
                    intervals.append((start, end))

        def mapped(time: float) -> float:
            return time - sum(
                max(0, min(time, end) - start)
                for start, end in intervals
                if time > start
            )

        tracks = deepcopy(self.tracks)
        bound_titles: dict[str, list[TextOverlay]] = {}
        titles = []
        for text in self.text_overlays:
            if text.clip_iid:
                bound_titles.setdefault(text.clip_iid, []).append(text)
            else:
                titles.append(deepcopy(text))
        pair_parts: dict[tuple[str, float, float], str] = {}
        if ripple:
            from .text_timing import TextTiming

            titles = TextTiming.remove_time(titles, intervals, self.duration())
        for track in tracks:
            remaining = []
            for clip in track.clips:
                if clip.iid in removed:
                    continue
                pieces = [(clip.timeline_start, clip.timeline_end)]
                for start, end in intervals:
                    next_pieces = []
                    for left, right in pieces:
                        if right <= start or left >= end:
                            next_pieces.append((left, right))
                        else:
                            if left < start:
                                next_pieces.append((left, start))
                            if right > end:
                                next_pieces.append((end, right))
                    pieces = next_pieces
                for index, (left, right) in enumerate(pieces):
                    part = deepcopy(clip)
                    part.iid = clip.iid if index == 0 else new_iid()
                    part.src_in = (
                        clip.src_in + (left - clip.timeline_start) * clip.speed
                    )
                    part.src_out = part.src_in + (right - left) * clip.speed
                    part.timeline_start = max(0.0, mapped(left))
                    if clip.fade_in or clip.fade_out:
                        part.audio_duration = clip.audio_duration or clip.duration
                        part.audio_offset += left - clip.timeline_start
                    if clip.video_fade_in or clip.video_fade_out:
                        part.video_duration = clip.video_duration or clip.duration
                        part.video_offset += left - clip.timeline_start
                    if clip.link_iid and len(pieces) > 1:
                        key = (clip.link_iid, left, right)
                        part.link_iid = pair_parts.setdefault(key, new_iid())
                    remaining.append(part)
                    for text in bound_titles.get(clip.iid, []):
                        offset = left - clip.timeline_start
                        text_start = max(text.start, offset)
                        text_end = min(
                            text.end or clip.duration, offset + part.duration
                        )
                        if text_end > text_start:
                            copy = deepcopy(text)
                            copy.iid = text.iid if index == 0 else new_iid()
                            copy.clip_iid = part.iid
                            copy.start, copy.end = (
                                text_start - offset,
                                text_end - offset,
                            )
                            titles.append(copy)
            track.clips = remaining
            ordered = track.sorted_clips()
            if any(
                right.timeline_start < left.timeline_end - 1e-9
                for left, right in zip(ordered, ordered[1:])
            ):
                raise ValueError("После удаления возникнет пересечение клипов.")
        self.tracks = tracks
        live = {clip.iid for track in tracks for clip in track.clips}
        self.text_overlays = [
            text for text in titles if not text.clip_iid or text.clip_iid in live
        ]
        if ripple:
            self.markers = MarkerRules.remove(self.markers, intervals)
            self.masks = MarkerRules.remove(self.masks, intervals)
        self._keep_timed_items()
        return True

    def _keep_timed_items(self) -> None:
        """Маркеры и маски не остаются за концом проекта после его сокращения."""
        duration = self.duration()
        self.markers = MarkerRules.keep(self.markers, 0, duration)
        self.masks = MarkerRules.keep(self.masks, 0, duration)

    def relink_clips(self) -> None:
        """Пересобрать связи V/A пар по совпадению границ.

        Самолечение после split/cut/undo: клипы одного ассета с равными
        стартом и src-границами на V и A получают общий link_iid.
        """
        for tr in self.tracks:
            for c in tr.clips:
                c.link_iid = ""
        vids = [t for t in self.tracks if t.kind == "video"]
        auds = [t for t in self.tracks if t.kind == "audio"]
        for vt in vids:
            for v in vt.clips:
                if v.link_iid:
                    continue
                for at in auds:
                    mate = None
                    for au in at.clips:
                        if au.link_iid:
                            continue
                        if (
                            au.asset_iid == v.asset_iid
                            and abs(au.timeline_start - v.timeline_start) < 1e-6
                            and abs(au.src_in - v.src_in) < 1e-6
                            and abs(au.src_out - v.src_out) < 1e-6
                        ):
                            mate = au
                            break
                    if mate is not None:
                        lid = new_iid()
                        v.link_iid = lid
                        mate.link_iid = lid
                        break

    def map_time(self, t: float) -> tuple[Clip, MediaAsset, float] | None:
        """timeline_time -> (clip, asset, source_time) для предпросмотра."""
        for track in self.video_tracks():
            for c in track.clips:
                if c.timeline_start <= t < c.timeline_end:
                    asset = self.asset_by_iid(c.asset_iid)
                    if asset is None:
                        continue
                    return (c, asset, c.src_in + (t - c.timeline_start) * c.speed)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "iid": self.iid,
            "name": self.name,
            "canvas": asdict(self.canvas),
            "assets": [a.to_dict() for a in self.assets],
            "tracks": [t.to_dict() for t in self.tracks],
            "master_volume": self.master_volume,
            "master_mute": self.master_mute,
            "audio_limiter": self.audio_limiter,
            "text_overlays": [o.to_dict() for o in self.text_overlays],
            "markers": [marker.to_dict() for marker in self.markers],
            "masks": [mask.to_dict() for mask in self.masks],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Project":
        p = cls(name=d.get("name", "Без названия"), iid=d.get("iid") or new_iid())
        p.canvas = ProjectCanvas(**d.get("canvas", {}))
        p.assets = [MediaAsset.from_dict(a) for a in d.get("assets", [])]
        p.tracks = [Track.from_dict(t) for t in d.get("tracks", [])]
        if not p.tracks:
            p.tracks = [Track(kind="video", name="V1"), Track(kind="audio", name="A1")]
        p.master_volume = _clamp(float(d.get("master_volume", 1.0)), 0.0, 2.0)
        p.master_mute = d.get("master_mute", False)
        p.audio_limiter = d.get("audio_limiter", False)
        p.markers = [VideoMarker(**item) for item in d.get("markers", [])]
        p.masks = [VideoMask(**item) for item in d.get("masks", [])]
        p.text_overlays = [
            TextOverlay.from_dict(o) for o in d.get("text_overlays", []) if o
        ]
        # миграция старого одиночного текста (до мульти-текстов)
        if not p.text_overlays:
            legacy = d.get("text_overlay")
            if isinstance(legacy, dict):
                p.text_overlays = [TextOverlay.from_dict(legacy)]
        return p

    def edit_state(self) -> dict[str, Any]:
        """Состояние монтажа без производных метаданных для dirty-state."""
        data = self.to_dict()
        keys = (
            "iid",
            "path",
            "name",
            "kind",
            "video_stream",
            "audio_stream",
            "recording_started",
        )
        data["assets"] = [{key: asset[key] for key in keys} for asset in data["assets"]]
        return data

    def copy_reasons(self) -> list[str]:
        """Правки, которые нельзя представить простой склейкой пакетов."""
        reasons = []
        if self.masks:
            reasons.append("Заданы маски областей кадра")
        if any(clip.speed != 1 for track in self.tracks for clip in track.clips):
            reasons.append("Изменена скорость клипа")
        if any(
            track.hidden or track.mute or track.solo or track.duck_source_iid
            for track in self.tracks
        ):
            reasons.append("Заданы видимость, микширование или ducking дорожек")
        if any(
            clip.opacity != 1
            or clip.scale != 1
            or clip.position_x
            or clip.position_y
            or clip.rotation
            or clip.flip_h
            or clip.flip_v
            or clip.crop_left
            or clip.crop_right
            or clip.crop_top
            or clip.crop_bottom
            or clip.video_fade_in
            or clip.video_fade_out
            for track in self.tracks
            for clip in track.clips
        ):
            reasons.append("Заданы преобразования или прозрачность видеоклипа")
        if (
            self.canvas.width
            or self.canvas.height
            or self.canvas.fps_num
            or self.canvas.fit != "contain"
        ):
            reasons.append("Заданы преобразования холста или FPS проекта")
        videos = sorted(
            (clip for track in self.video_tracks() for clip in track.clips),
            key=lambda clip: clip.timeline_start,
        )
        audios = [clip for track in self.audio_tracks() for clip in track.clips]
        if self.master_volume != 1:
            reasons.append("Изменена мастер-громкость")
        if self.master_mute:
            reasons.append("Выключен общий звук проекта")
        if self.audio_limiter:
            reasons.append("Включён ограничитель пиков звука")
        if any(
            clip.fade_in or clip.fade_out or clip.normalization_gain != 1
            for clip in audios
        ):
            reasons.append("Заданы затухания или нормализация звука")
        if any(
            overlay.enabled and overlay.has_content for overlay in self.text_overlays
        ):
            reasons.append("В проекте есть наложения")
        if any(clip.volume != 1 or clip.mute for clip in [*videos, *audios]):
            reasons.append("Изменены громкость или Mute клипа")
        cursor = 0.0
        matched: set[str] = set()
        for clip in videos:
            if abs(clip.timeline_start - cursor) > 1e-6:
                reasons.append("Есть пустоты или перекрытия на видеотаймлайне")
            cursor = clip.timeline_end
            asset = self.asset_by_iid(clip.asset_iid)
            if asset and (asset.video_stream or asset.audio_stream):
                reasons.append("Выбраны дополнительные медиапотоки")
            if asset is not None and asset.has_audio:
                mate = next(
                    (
                        audio
                        for audio in audios
                        if audio.iid not in matched
                        and audio.asset_iid == clip.asset_iid
                        and all(
                            abs(getattr(audio, key) - getattr(clip, key)) < 1e-6
                            for key in ("timeline_start", "src_in", "src_out")
                        )
                    ),
                    None,
                )
                if mate is None:
                    reasons.append("Звук видеоклипа отличается от исходного")
                else:
                    matched.add(mate.iid)
        if len(matched) != len(audios):
            reasons.append("Есть самостоятельные аудиоклипы")
        if abs(cursor - self.duration()) > 1e-6:
            reasons.append("Длительность проекта отличается от видеоряда")
        return list(dict.fromkeys(reasons))
