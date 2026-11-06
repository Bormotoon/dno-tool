"""Версионированный файл монтажа: проверка, миграция и атомарная запись."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
from typing import Any
from collections.abc import Callable

from .models import Clip, MediaAsset, Project, ProjectCanvas, TextOverlay, Track
from .file_output import AtomicOutput
from .markers import MarkerRules, VideoMarker
from .masks import VideoMask


class ProjectFileStore:
    """Qt-free хранилище; вызывается из рабочего потока."""

    # 5: позиция клипа отсчитывается от центра холста, появилось отражение.
    VERSION = 5
    _TIMING_KEYS = ("timeline_start", "src_in", "src_out", "speed")
    MAX_BYTES = 32 * 1024 * 1024

    @staticmethod
    def _object(value: Any, allowed: set[str], label: str) -> dict:
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError(f"Некорректные или неизвестные поля: {label}.")
        return value

    @staticmethod
    def _number(
        data: dict,
        key: str,
        default: float,
        minimum: float = 0,
        maximum: float = 1e15,
        integer: bool = False,
    ) -> None:
        value = data.get(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not minimum <= value <= maximum
            or (integer and not isinstance(value, int))
        ):
            raise ValueError(f"Недопустимое число: {key}.")

    @staticmethod
    def _strings(data: dict, keys: tuple[str, ...]) -> None:
        for key in keys:
            value = data.get(key, "")
            if not isinstance(value, str) or "\0" in value or len(value) > 1_048_576:
                raise ValueError(f"Некорректная строка: {key}.")

    @staticmethod
    def _booleans(data: dict, keys: tuple[str, ...]) -> None:
        if any(key in data and not isinstance(data[key], bool) for key in keys):
            raise ValueError("Логическое поле должно содержать true/false.")

    @staticmethod
    def _list(value: Any, label: str) -> list:
        if not isinstance(value, list) or len(value) > 100_000:
            raise ValueError(f"Некорректный или слишком большой список: {label}.")
        return value

    @staticmethod
    def _identity(item: dict, seen: set[str]) -> None:
        iid = item.get("iid")
        if (
            not isinstance(iid, str)
            or len(iid) != 32
            or any(c not in "0123456789abcdef" for c in iid)
            or iid in seen
        ):
            raise ValueError("Пустой или повторяющийся iid в проекте.")
        seen.add(iid)

    @classmethod
    def _check_canvas(cls, value: Any) -> dict:
        canvas = cls._object(value, {f.name for f in fields(ProjectCanvas)}, "холст")
        for key in ("width", "height", "fps_num", "fps_den"):
            cls._number(
                canvas,
                key,
                1 if key == "fps_den" else 0,
                minimum=1 if key == "fps_den" else 0,
                maximum=1_000_000,
                integer=True,
            )
        if canvas.get("fit", "contain") not in ("contain", "cover"):
            raise ValueError("Неизвестная политика холста.")
        return canvas

    @classmethod
    def _check_asset(cls, asset: Any, seen: set[str]) -> None:
        cls._object(
            asset,
            {f.name for f in fields(MediaAsset)} - {"probe_error", "wave_error"},
            "исходник",
        )
        cls._identity(asset, seen)
        cls._strings(
            asset,
            (
                "path",
                "name",
                "kind",
                "vcodec",
                "acodec",
                "channel_layout",
                "copy_signature",
            ),
        )
        if not asset.get("path") or asset.get("kind", "video") not in (
            "video",
            "audio",
        ):
            raise ValueError("У исходника должны быть путь и поддерживаемый тип.")
        cls._booleans(asset, ("has_video", "has_audio"))
        for key in ("duration", "fps"):
            cls._number(asset, key, 0)
        cls._number(asset, "recording_started", 0, maximum=253402300799)
        for key in ("width", "height", "channels", "video_stream", "audio_stream"):
            cls._number(asset, key, 0, integer=True)

    @classmethod
    def _check_track(cls, track: Any) -> None:
        cls._object(track, {f.name for f in fields(Track)}, "дорожка")
        cls._strings(track, ("name", "kind"))
        cls._strings(track, ("duck_source_iid",))
        cls._booleans(track, ("hidden", "locked", "mute", "solo"))
        for key, default, low, high in (
            ("duck_threshold", 0.03, 0.000976563, 1),
            ("duck_ratio", 8, 1, 20),
            ("duck_attack", 20, 0.01, 2000),
            ("duck_release", 300, 0.01, 9000),
        ):
            cls._number(track, key, default, low, high)
        if track.get("kind", "video") not in ("video", "audio"):
            raise ValueError("Неизвестный тип дорожки.")

    @classmethod
    def _check_clip(cls, clip: Any) -> None:
        cls._object(clip, {f.name for f in fields(Clip)}, "клип")
        cls._strings(clip, ("asset_iid", "link_iid"))
        cls._booleans(clip, ("mute", "flip_h", "flip_v"))
        for key in ("timeline_start", "src_in", "src_out"):
            cls._number(clip, key, 0)
        cls._number(clip, "volume", 1, maximum=2)
        cls._number(clip, "normalization_gain", 1, minimum=0, maximum=100)
        cls._number(clip, "audio_duration", 0)
        cls._number(clip, "audio_offset", 0)
        if isinstance(clip.get("speed", 1), bool) or clip.get("speed", 1) not in (
            0.5,
            1,
            2,
        ):
            raise ValueError("Поддерживаются скорости 0.5×, 1× и 2×.")
        cls._number(clip, "opacity", 1, 0, 1)
        cls._number(clip, "scale", 1, 0.01, 4)
        for key in ("position_x", "position_y"):
            cls._number(clip, key, 0, -4, 4)
        for key in ("crop_left", "crop_right", "crop_top", "crop_bottom"):
            cls._number(clip, key, 0, 0, 0.99)
        if (
            clip.get("crop_left", 0) + clip.get("crop_right", 0) >= 0.99
            or clip.get("crop_top", 0) + clip.get("crop_bottom", 0) >= 0.99
        ):
            raise ValueError("Обрезка должна оставлять видимую область изображения.")
        if clip.get("rotation", 0) not in (0, 90, 180, 270) or isinstance(
            clip.get("rotation", 0), bool
        ):
            raise ValueError("Поддерживаются повороты 0/90/180/270 градусов.")
        for key in ("video_offset", "video_duration"):
            cls._number(clip, key, 0)
        natural = (clip.get("src_out", 0) - clip.get("src_in", 0)) / clip.get(
            "speed", 1
        )
        for key in ("video_fade_in", "video_fade_out"):
            cls._number(clip, key, 0, maximum=clip.get("video_duration", 0) or natural)
        for key in ("fade_in", "fade_out"):
            cls._number(clip, key, 0, maximum=clip.get("audio_duration", 0) or natural)
        if clip.get("src_out", 0) <= clip.get("src_in", 0):
            raise ValueError("Клип должен иметь положительную длительность.")
        for key in ("effects", "transitions"):
            if any(
                not isinstance(item, dict)
                for item in cls._list(clip.get(key, []), key)
            ):
                raise ValueError("Эффекты и переходы должны быть объектами.")

    @classmethod
    def _check_overlay(cls, overlay: Any) -> None:
        cls._object(
            overlay, {f.name for f in fields(TextOverlay)} | {"margin"}, "текст"
        )
        cls._strings(
            overlay,
            (
                "text",
                "corner",
                "color",
                "font_family",
                "back_color",
                "fontfile",
                "clip_iid",
                "kind",
                "image_path",
                "arrow_direction",
            ),
        )
        cls._booleans(overlay, ("enabled", "filled"))
        if overlay.get("kind", "text") not in (
            "text",
            "image",
            "rectangle",
            "ellipse",
            "arrow",
        ):
            raise ValueError("Неизвестный тип наложения.")
        if overlay.get("kind") == "image" and not overlay.get("image_path"):
            raise ValueError("Укажите файл картинки.")
        if overlay.get("arrow_direction", "right") not in (
            "right",
            "down",
            "left",
            "up",
        ):
            raise ValueError("Неизвестное направление стрелки.")
        cls._number(overlay, "start", 0)
        cls._number(overlay, "end", 0)
        if overlay.get("end", 0) and overlay["end"] <= overlay.get("start", 0):
            raise ValueError("Конец титра должен быть позже начала.")
        if overlay.get("corner", "BR") not in ("TL", "TR", "BL", "BR"):
            raise ValueError("Неизвестный угол текста.")
        for key, default, low, high in (
            ("fontsize", 24, 1, 4096),
            ("margin", 20, 0, 300),
            ("margin_x", 20, 0, 16384),
            ("margin_y", 20, 0, 16384),
            ("back_alpha", 50, 0, 100),
            ("box_pad", 6, 0, 30),
            ("width", 240, 1, 4096),
            ("height", 120, 1, 4096),
            ("opacity", 100, 0, 100),
            ("stroke_width", 6, 1, 100),
            ("rotation", 0, -180, 180),
        ):
            cls._number(overlay, key, default, low, high, integer=True)

    @classmethod
    def _check_markers(cls, value: Any, duration: float, seen: set[str]) -> None:
        for marker in cls._list(value, "маркеры"):
            cls._object(marker, {item.name for item in fields(VideoMarker)}, "маркер")
            cls._identity(marker, seen)
            cls._strings(marker, ("name", "note", "color"))
            VideoMarker(**marker).validate(duration)

    @classmethod
    def _check_masks(cls, value: Any, duration: float, seen: set[str]) -> None:
        for mask in cls._list(value, "маски"):
            cls._object(mask, {item.name for item in fields(VideoMask)}, "маска")
            cls._identity(mask, seen)
            VideoMask(**mask).validate(duration)

    @staticmethod
    def _duration(data: dict) -> float:
        """Длительность снимка с учётом скорости клипов."""
        return max(
            (
                clip.get("timeline_start", 0)
                + (clip.get("src_out", 0) - clip.get("src_in", 0))
                / clip.get("speed", 1)
                for track in data.get("tracks", [])
                for clip in track.get("clips", [])
            ),
            default=0,
        )

    @classmethod
    def _check_duck_source(cls, track: dict, tracks: list[dict]) -> None:
        source_iid = track.get("duck_source_iid", "")
        if not source_iid:
            return
        source = next((item for item in tracks if item.get("iid") == source_iid), None)
        if (
            track.get("kind", "video") != "audio"
            or source is None
            or source.get("kind", "video") != "audio"
            or source is track
        ):
            raise ValueError("Для ducking укажите другую звуковую дорожку.")

    @staticmethod
    def _check_asset_bounds(clip: dict, assets: dict[str, dict]) -> None:
        asset = assets.get(clip.get("asset_iid"))
        if asset is None:
            raise ValueError("Клип ссылается на отсутствующий исходник.")
        duration = asset.get("duration", 0)
        if duration > 0 and clip.get("src_out", 0) > duration + 1e-6:
            raise ValueError(
                f"Клип выходит за длительность исходника «{asset.get('name', '')}». "
                "Подрежьте клип или перепривяжите файл."
            )

    @classmethod
    def _check_link_group(cls, group: list[tuple[dict, dict]]) -> None:
        if len(group) != 2 or {
            track.get("kind", "video") for track, _ in group
        } != {"video", "audio"}:
            raise ValueError("Повреждена связь V/A-пары.")
        left, right = [clip for _, clip in group]
        defaults = {"timeline_start": 0, "src_in": 0, "src_out": 0, "speed": 1}
        if left.get("asset_iid") != right.get("asset_iid") or any(
            abs(left.get(key, defaults[key]) - right.get(key, defaults[key])) > 1e-6
            for key in cls._TIMING_KEYS
        ):
            raise ValueError("Границы V/A-пары не совпадают.")

    @classmethod
    def validate(cls, snapshot: dict) -> Project:
        """Не исправлять молча повреждённые поля и ссылки при чтении файла."""
        data = cls._object(
            deepcopy(snapshot),
            {f.name for f in fields(Project)} | {"text_overlay"},
            "проект",
        )
        cls._strings(data, ("iid", "name"))
        cls._number(data, "master_volume", 1, maximum=2)
        cls._booleans(data, ("audio_limiter", "master_mute"))
        cls._check_canvas(data.get("canvas", {}))
        seen: set[str] = set()
        cls._identity(data, seen)
        for asset in cls._list(data.get("assets", []), "исходники"):
            cls._check_asset(asset, seen)
        for track in cls._list(data.get("tracks", []), "дорожки"):
            cls._check_track(track)
            cls._identity(track, seen)
            for clip in cls._list(track.get("clips", []), "клипы"):
                cls._check_clip(clip)
                cls._identity(clip, seen)
        overlays = data.get("text_overlays", [])
        if not overlays and data.get("text_overlay") is not None:
            overlays = [data["text_overlay"]]
        for overlay in cls._list(overlays, "тексты"):
            cls._check_overlay(overlay)
            cls._identity(overlay, seen)
        # allow_nan=False также проверяет зарезервированные вложенные поля.
        json.dumps(data, allow_nan=False)
        duration = cls._duration(data)
        cls._check_markers(data.get("markers", []), duration, seen)
        cls._check_masks(data.get("masks", []), duration, seen)
        project = Project.from_dict(data)
        all_clips = {clip.iid for track in project.tracks for clip in track.clips}
        for text in project.text_overlays:
            if text.clip_iid and text.clip_iid not in all_clips:
                raise ValueError("Титр ссылается на отсутствующий клип.")
        for track in data.get("tracks", []):
            cls._check_duck_source(track, data["tracks"])
        groups: dict[str, list[tuple[dict, dict]]] = {}
        assets = {asset["iid"]: asset for asset in data.get("assets", [])}
        for track_model in project.tracks:
            ordered = track_model.sorted_clips()
            for left, right in zip(ordered, ordered[1:]):
                if left.timeline_end > right.timeline_start + 1e-9:
                    raise ValueError("Пересекающиеся клипы на одной дорожке.")
        for track in data.get("tracks", []):
            for clip in track.get("clips", []):
                cls._check_asset_bounds(clip, assets)
                link = clip.get("link_iid", "")
                if link:
                    if (
                        len(link) != 32
                        or any(c not in "0123456789abcdef" for c in link)
                        or link in seen
                    ):
                        raise ValueError("Некорректный идентификатор связи клипов.")
                    groups.setdefault(link, []).append((track, clip))
        for group in groups.values():
            cls._check_link_group(group)
        return project

    @classmethod
    def validate_changes(cls, before: dict, after: dict) -> None:
        """Проверить только объекты, которые затронула правка.

        Посторонний маркер или клип, уже нарушающий правила, не должен запрещать
        поворот выделенного клипа. Полная проверка остаётся на сохранение и экспорт.
        """
        old_tracks = {track["iid"]: track for track in before["tracks"]}
        old_clips = {
            clip["iid"]: clip for track in before["tracks"] for clip in track["clips"]
        }
        old_texts = {item["iid"]: item for item in before["text_overlays"]}
        tracks = after["tracks"]
        assets = {asset["iid"]: asset for asset in after["assets"]}
        clip_iids = {clip["iid"] for track in tracks for clip in track["clips"]}
        links: dict[str, list[tuple[dict, dict]]] = {}
        for track in tracks:
            for clip in track["clips"]:
                if clip.get("link_iid"):
                    links.setdefault(clip["link_iid"], []).append((track, clip))
        for track in tracks:
            old = old_tracks.get(track["iid"])
            settings = {key: val for key, val in track.items() if key != "clips"}
            if old is None or settings != {
                key: val for key, val in old.items() if key != "clips"
            }:
                cls._check_track(track)
                cls._check_duck_source(track, tracks)
            moved: set[str] = set()
            for clip in track["clips"]:
                previous = old_clips.get(clip["iid"])
                if previous == clip:
                    continue
                cls._check_clip(clip)
                json.dumps(clip, allow_nan=False)
                if previous is None or any(
                    previous.get(key) != clip.get(key) for key in cls._TIMING_KEYS
                ):
                    moved.add(clip["iid"])
                    cls._check_asset_bounds(clip, assets)
                    if clip.get("link_iid"):
                        cls._check_link_group(links[clip["link_iid"]])
            if old is not None and {c["iid"] for c in old["clips"]} != {
                c["iid"] for c in track["clips"]
            }:
                moved.update(c["iid"] for c in track["clips"])
            if moved:
                ordered = Track.from_dict(track).sorted_clips()
                for left, right in zip(ordered, ordered[1:]):
                    if (
                        left.iid in moved or right.iid in moved
                    ) and left.timeline_end > right.timeline_start + 1e-9:
                        raise ValueError("Пересекающиеся клипы на одной дорожке.")
        for overlay in after["text_overlays"]:
            if old_texts.get(overlay["iid"]) != overlay:
                cls._check_overlay(overlay)
                if overlay.get("clip_iid") and overlay["clip_iid"] not in clip_iids:
                    raise ValueError("Титр ссылается на отсутствующий клип.")

    @classmethod
    def validate_section(cls, snapshot: dict, section: str) -> Any:
        """Проверить один раздел снимка и вернуть его разобранным."""
        data = deepcopy(snapshot)
        if section == "canvas":
            return ProjectCanvas(**cls._check_canvas(data.get("canvas", {})))
        seen = {data["iid"]}
        for key, items in (
            ("assets", data["assets"]),
            ("tracks", data["tracks"]),
            ("clips", [c for t in data["tracks"] for c in t["clips"]]),
            ("text_overlays", data["text_overlays"]),
            ("markers", data["markers"]),
            ("masks", data["masks"]),
        ):
            if key != section:
                seen.update(item["iid"] for item in items)
        if section == "assets":
            for asset in cls._list(data["assets"], "исходники"):
                cls._check_asset(asset, seen)
            return [MediaAsset.from_dict(asset) for asset in data["assets"]]
        duration = cls._duration(data)
        if section == "markers":
            cls._check_markers(data["markers"], duration, seen)
            return [VideoMarker(**item) for item in data["markers"]]
        if section == "masks":
            cls._check_masks(data["masks"], duration, seen)
            return [VideoMask(**item) for item in data["masks"]]
        if section == "text_overlays":
            clip_iids = {c["iid"] for t in data["tracks"] for c in t["clips"]}
            for overlay in cls._list(data["text_overlays"], "тексты"):
                cls._check_overlay(overlay)
                cls._identity(overlay, seen)
                if overlay.get("clip_iid") and overlay["clip_iid"] not in clip_iids:
                    raise ValueError("Титр ссылается на отсутствующий клип.")
            json.dumps(data["text_overlays"], allow_nan=False)
            return [TextOverlay.from_dict(item) for item in data["text_overlays"]]
        raise ValueError(f"Неизвестный раздел проекта: {section}.")

    @classmethod
    def _migrate(cls, data: Any, version: int) -> None:
        """Перевести старый снимок на текущие правила, сохранив вид монтажа.

        Повреждённые значения здесь не исправляются: их отклонит validate.
        """
        if version >= 5 or not isinstance(data, dict):
            return
        tracks = data.get("tracks", [])
        if not isinstance(tracks, list):
            return

        def number(value: Any) -> bool:
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )

        for track in tracks:
            clips = track.get("clips", []) if isinstance(track, dict) else []
            for clip in clips if isinstance(clips, list) else []:
                if not isinstance(clip, dict) or not number(clip.get("scale", 1)):
                    continue
                # Раньше позиция задавала левый верхний угол слоя, теперь центр.
                shift = (clip.get("scale", 1) - 1) / 2
                for key in ("position_x", "position_y"):
                    if shift and number(clip.get(key, 0)):
                        clip[key] = max(-4, min(4, clip.get(key, 0) + shift))
        # Прежняя проверка не учитывала скорость и пропускала маркеры за концом.
        try:
            markers = [VideoMarker(**item) for item in data.get("markers", [])]
            kept = MarkerRules.keep(markers, 0, cls._duration(data))
        except (AttributeError, TypeError, ValueError, ZeroDivisionError):
            return
        data["markers"] = [marker.to_dict() for marker in kept]

    @staticmethod
    def _pairs(pairs: list[tuple[str, Any]]) -> dict:
        data: dict = {}
        for key, value in pairs:
            if key in data:
                raise ValueError(f"Повторяющееся поле JSON: {key}.")
            data[key] = value
        return data

    @classmethod
    def load(cls, path: str) -> dict:
        source = Path(path).resolve()
        with source.open("rb") as file:
            raw = file.read(cls.MAX_BYTES + 1)
        if len(raw) > cls.MAX_BYTES:
            raise ValueError("Файл проекта превышает 32 МиБ.")
        envelope = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=cls._pairs)
        cls._object(envelope, {"schema_version", "project"}, "файл проекта")
        version = envelope.get("schema_version")
        if type(version) is not int or not 1 <= version <= cls.VERSION:
            raise ValueError(
                "Версия файла проекта не поддерживается; обновите DNO Tool."
            )
        data = envelope.get("project")
        cls._migrate(data, version)
        project = cls.validate(data)
        for asset in project.assets:
            asset.path = os.path.abspath(source.parent / asset.path)
        for overlay in project.text_overlays:
            if overlay.image_path:
                overlay.image_path = os.path.abspath(source.parent / overlay.image_path)
        return project.to_dict()

    @classmethod
    def save(
        cls,
        snapshot: dict,
        path: str,
        cancelled: threading.Event | None = None,
        admit: Callable[[int], None] | None = None,
        overwrite: bool = True,
    ) -> None:
        project = cls.validate(snapshot)
        target = Path(path).resolve()
        for asset in project.assets:
            source = Path(asset.path).resolve()
            if source == target or (
                target.exists() and source.exists() and os.path.samefile(source, target)
            ):
                raise ValueError("Файл проекта нельзя записать поверх исходника.")
            try:
                asset.path = os.path.relpath(source, target.parent)
            except ValueError:
                asset.path = str(source)
        for overlay in project.text_overlays:
            if not overlay.image_path:
                continue
            source = Path(overlay.image_path).resolve()
            if source == target or (
                target.exists() and source.exists() and os.path.samefile(source, target)
            ):
                raise ValueError("Файл проекта нельзя записать поверх картинки.")
            try:
                overlay.image_path = os.path.relpath(source, target.parent)
            except ValueError:
                overlay.image_path = str(source)
        raw = json.dumps(
            {"schema_version": cls.VERSION, "project": project.to_dict()},
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        if len(raw) > cls.MAX_BYTES:
            raise ValueError("Файл проекта превышает 32 МиБ.")
        if admit is not None:
            admit(len(raw))
        staging: str | None = None
        try:
            fd, staging = tempfile.mkstemp(
                prefix=".dno-project-", suffix=".tmp", dir=target.parent
            )
            with os.fdopen(fd, "wb") as file:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("Сохранение проекта отменено.")
            AtomicOutput.publish(staging, target, overwrite)
        finally:
            if staging is not None:
                Path(staging).unlink(missing_ok=True)
