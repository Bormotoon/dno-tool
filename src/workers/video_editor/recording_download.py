"""Отменяемая загрузка завершённой удалённой записи с проверкой результата."""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from src.domain.models import DeviceModel
from src.domain.video.file_output import AtomicOutput
from src.domain.video.output_lease import OutputLease

from .ffmpeg_utils import probe_media
from .process_control import ProcessControl


class RecordingDownloadWorker(QThread):
    progress = Signal(int)

    def __init__(
        self,
        device: DeviceModel,
        remote_path: str,
        target: str,
        overwrite: bool,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.device, self.remote_path, self.target = (
            copy.deepcopy(device),
            remote_path,
            Path(target).absolute(),
        )
        self.overwrite = overwrite
        self.cancelled = threading.Event()
        self.output = ""
        self.error = ""

    def run(self) -> None:
        import paramiko

        from src.workers.command.executor_base import create_ssh_client

        client = sftp = None
        control = ProcessControl(self.cancelled)
        done = threading.Event()
        deadline = time.monotonic() + 3600

        def interrupt() -> None:
            while not done.wait(0.05):
                if self.cancelled.is_set() or time.monotonic() >= deadline:
                    if client is not None:
                        client.close()
                    return

        watcher = threading.Thread(target=interrupt, daemon=True)
        watcher.start()
        try:
            control.check()
            client = paramiko.SSHClient()
            create_ssh_client(self.device, client=client, connect_timeout=5)
            control.check()
            sftp = client.open_sftp()
            sftp.get_channel().settimeout(5)
            before = sftp.stat(self.remote_path)
            if not before.st_size or before.st_size <= 0:
                raise ValueError("Удалённая запись пуста.")
            if (
                shutil.disk_usage(self.target.parent).free
                < before.st_size + 8 * 1048576
            ):
                raise ValueError("Недостаточно места для загрузки записи.")
            with (
                OutputLease(self.target),
                tempfile.TemporaryDirectory(
                    prefix=".dno-download-", dir=self.target.parent
                ) as job,
            ):
                staging = Path(job) / self.target.name
                with (
                    sftp.open(self.remote_path, "rb") as source,
                    staging.open("wb") as output,
                ):
                    size = 0
                    while True:
                        control.check()
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Истекло время загрузки записи")
                        chunk = source.read(256 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > before.st_size:
                            raise ValueError(
                                "Удалённая запись ещё изменяется. Дождитесь завершения."
                            )
                        output.write(chunk)
                        self.progress.emit(min(98, int(size / before.st_size * 98)))
                    output.flush()
                    os.fsync(output.fileno())
                after = sftp.stat(self.remote_path)
                if size != before.st_size or (before.st_size, before.st_mtime) != (
                    after.st_size,
                    after.st_mtime,
                ):
                    raise ValueError(
                        "Удалённый файл изменился при загрузке; результат не опубликован."
                    )
                metadata = probe_media(str(staging), cancelled=self.cancelled)
                if (
                    not metadata
                    or not (metadata.get("has_video") or metadata.get("has_audio"))
                    or float(metadata.get("duration", 0)) <= 0
                ):
                    raise ValueError(
                        "Загруженный файл не прошёл проверку медиа. Дождитесь завершения записи."
                    )
                control.check()
                AtomicOutput.publish(staging, self.target, self.overwrite)
                self.output = str(self.target)
                self.progress.emit(100)
        except Exception as error:  # noqa: BLE001 — ошибка публикуется после штатного завершения потока
            self.error = "Загрузка отменена" if self.cancelled.is_set() else str(error)
        finally:
            done.set()
            watcher.join(1)
            if sftp is not None:
                with suppress(Exception):
                    sftp.close()
            if client is not None:
                with suppress(Exception):
                    client.close()
