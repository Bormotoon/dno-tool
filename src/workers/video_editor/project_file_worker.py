"""Чтение и запись проекта вне GUI с удержанием QThread владельцем."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from src.domain.video.project_file import ProjectFileStore
from src.domain.video.models import Project
from .ffmpeg_utils import probe_media
from src.utils.filesystem.fs_utils import ensure_user_owned


class ProjectFileWorker(QThread):
    result = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        operation: str,
        path: str,
        snapshot: dict | None = None,
        parent: QObject | None = None,
        replacements: dict[str, str] | None = None,
        overwrite: bool = True,
    ) -> None:
        super().__init__(parent)
        self.operation = operation
        self.path = path
        self.snapshot = snapshot
        self.cancelled = threading.Event()
        self.replacements = replacements or {}
        self.overwrite = overwrite

    def run(self) -> None:
        try:
            if self.operation == "save":
                ProjectFileStore.save(
                    self.snapshot, self.path, self.cancelled, overwrite=self.overwrite
                )
                ensure_user_owned(self.path)
                snapshot = self.snapshot
                missing = []
            elif self.operation == "load":
                snapshot = ProjectFileStore.load(self.path)
                missing = [
                    asset["path"]
                    for asset in snapshot["assets"]
                    if not Path(asset["path"]).is_file()
                ]
            elif self.operation == "relink":
                project = Project.from_dict(self.snapshot)
                for iid, path in self.replacements.items():
                    if self.cancelled.is_set():
                        raise InterruptedError("Перепривязка отменена.")
                    asset = project.asset_by_iid(iid)
                    if asset is None:
                        raise ValueError("Исходник уже удалён из проекта.")
                    meta = probe_media(path, cancelled=self.cancelled)
                    if not meta or float(meta.get("duration", 0)) <= 0:
                        raise ValueError(f"Не удалось проверить исходник: {path}")
                    used = [
                        (track.kind, clip)
                        for track in project.tracks
                        for clip in track.clips
                        if clip.asset_iid == iid
                    ]
                    required = {kind for kind, _ in used} | {asset.kind}
                    if any(not meta.get(f"has_{kind}") for kind in required):
                        raise ValueError(
                            f"В новом исходнике отсутствует нужный видео- или аудиопоток: {path}"
                        )
                    if any(
                        getattr(asset, f"{kind}_stream")
                        >= len(meta.get(f"{kind}_streams", []))
                        for kind in required
                        if f"{kind}_streams" in meta
                    ):
                        raise ValueError(
                            "В новом исходнике отсутствует выбранный поток."
                        )
                    if any(clip.src_out > meta["duration"] + 1e-6 for _, clip in used):
                        raise ValueError(
                            f"Новый исходник короче используемого фрагмента: {path}"
                        )
                    asset.path = str(Path(path).absolute())
                    for key in (
                        "duration",
                        "width",
                        "height",
                        "fps",
                        "vcodec",
                        "acodec",
                        "has_video",
                        "has_audio",
                        "channels",
                        "channel_layout",
                        "copy_signature",
                    ):
                        if key in meta:
                            setattr(asset, key, meta[key])
                snapshot = ProjectFileStore.validate(project.to_dict()).to_dict()
                missing = []
            elif self.operation == "preflight":
                project = ProjectFileStore.validate(self.snapshot)
                used = {
                    clip.asset_iid for track in project.tracks for clip in track.clips
                }
                missing = [
                    asset.path
                    for asset in project.assets
                    if asset.iid in used and not Path(asset.path).is_file()
                ]
                snapshot = self.snapshot
            else:
                raise ValueError("Неизвестная операция с файлом проекта.")
            if self.operation in ("load", "preflight"):
                missing.extend(
                    overlay["image_path"]
                    for overlay in snapshot.get("text_overlays", [])
                    if overlay.get("kind") == "image"
                    and (self.operation == "load" or overlay.get("enabled", True))
                    and not Path(overlay["image_path"]).is_file()
                )
                missing = list(dict.fromkeys(missing))
            self.result.emit(
                {
                    "operation": self.operation,
                    "path": self.path,
                    "snapshot": snapshot,
                    "missing": missing,
                }
            )
        except Exception as exc:
            self.error.emit(str(exc))
