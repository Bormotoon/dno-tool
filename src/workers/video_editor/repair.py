"""Ремонт в отдельную копию без изменения оригинала и чужих бэкапов."""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
from pathlib import Path

from src.domain.video.file_output import AtomicOutput
from src.services.media import get_media_service, OperationContext


class ContainerRepair:
    @staticmethod
    def run(path: str, cancel: threading.Event | None = None) -> tuple[bool, str]:
        from .ffmpeg_utils import probe_media

        control = OperationContext.with_timeout(1800, cancel)
        source = Path(path).absolute()
        try:
            control.check()
            version = source.stat()
            try:
                before = probe_media(str(source), cancelled=control.cancelled)
            except InterruptedError:
                raise
            except Exception:
                before = {}
            target = source.with_name(
                f"{source.stem}_repaired_{uuid.uuid4().hex[:12]}{source.suffix}"
            )
            with tempfile.TemporaryDirectory(
                prefix=".dno-repair-", dir=source.parent
            ) as job:
                staging = Path(job) / f"repaired{source.suffix}"
                get_media_service().remux(
                    [(str(source), 0.0, None)],
                    str(staging),
                    control,
                    all_streams=True,
                    repair=True,
                )
                get_media_service().verify(str(staging), control)
                after = probe_media(str(staging), cancelled=control.cancelled)
                if (
                    not after
                    or after.get("duration", 0) <= 0
                    or not (after.get("has_video") or after.get("has_audio"))
                ):
                    raise ValueError(
                        "Результат ремонта не содержит доступных медиапотоков."
                    )
                if before:
                    for kind in ("video", "audio", "subtitle", "data", "attachment"):
                        if before.get("stream_counts", {}).get(kind, 0) != after.get(
                            "stream_counts", {}
                        ).get(kind, 0):
                            raise ValueError(
                                "При ремонте изменился состав потоков; копия не опубликована."
                            )
                    if before.get("duration", 0) > 0 and after["duration"] < before[
                        "duration"
                    ] - max(0.1, before["duration"] * 0.01):
                        raise ValueError("После ремонта потеряна часть длительности.")
                current = source.stat()
                if (version.st_size, version.st_mtime_ns, version.st_ino) != (
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ino,
                ):
                    raise ValueError(
                        "Исходник изменился или ещё записывается; ремонт отменён."
                    )
                with staging.open("r+b") as ready:
                    os.fsync(ready.fileno())
                control.check()
                AtomicOutput.publish(staging, target, overwrite=False)
            return True, str(target)
        except InterruptedError:
            return False, "отменено"
        except Exception as error:
            return False, str(error)
