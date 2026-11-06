"""Архиватор SFTP с безопасными командами, отменой и отдельным каналом."""

from __future__ import annotations

import os
import shlex
import stat
import tarfile
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import IO

import paramiko
from PySide6.QtCore import QObject

from src.utils.filesystem.sftp_files import (
    OperationControl,
    atomic_local_copy,
    is_local_link,
    local_child,
    remote_child,
    remote_stat,
    remove_remote_temporary,
    validate_name,
)
from src.workers.sftp_tasks import SftpTaskWorker

TAR_FORMATS = (".tar.gz", ".tgz", ".tar", ".tar.bz2", ".tar.xz")
LOCAL_ARCHIVE_FORMATS = (*TAR_FORMATS, ".zip")
REMOTE_ARCHIVE_FORMATS = (*LOCAL_ARCHIVE_FORMATS, ".7z", ".rar")


def _archive_format(name: str) -> str:
    """Определить поддерживаемый формат по имени."""
    name = name.lower()
    if name.endswith(TAR_FORMATS):
        return "tar"
    return next((fmt for fmt in ("zip", "7z", "rar") if name.endswith("." + fmt)), "")


class _CheckedReader:
    """Проверять отмену внутри одного большого архивируемого файла."""

    def __init__(self, stream: IO[bytes], control: OperationControl) -> None:
        self.stream = stream
        self.control = control

    def read(self, size: int = -1) -> bytes:
        """Прочитать блок после проверки отмены."""
        self.control.check()
        return self.stream.read(size)


class SFTPArchiveWorker(SftpTaskWorker):
    """Локальные tar/zip и удалённые архиваторы с общим жизненным циклом."""

    def __init__(
        self,
        transport: paramiko.Transport | None,
        operation: str,
        path: str,
        archive_path: str,
        is_remote: bool,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(transport, parent, remote=is_remote, timeout=300)
        self.operation = operation
        self.path = path
        self.archive_path = archive_path
        self.is_remote = is_remote
        self.badge = "Архивация" if operation == "pack" else "Распаковка"
        self.description = f"{self.badge}: {path} → {archive_path}"

    def perform(self) -> None:
        """Проверить операцию и выполнить соответствующий backend."""
        self.control.check()
        self.progress.emit(-1, self.description)
        if self.operation == "pack":
            if not self.archive_path.lower().endswith((".tar.gz", ".tgz")):
                raise ValueError("Упаковка поддерживает .tar.gz и .tgz")
            self._pack_remote() if self.is_remote else self._pack_local()
        elif self.operation == "unpack":
            self._unpack_remote() if self.is_remote else self._unpack_local()
        else:
            raise ValueError(f"Неизвестная операция архива: {self.operation}")

    def _pack_local(self) -> None:
        target = Path(self.path).absolute()
        archive = Path(self.archive_path).absolute()
        if is_local_link(target) or is_local_link(archive):
            raise ValueError("Архивация через ссылки не поддерживается")
        if target.is_dir() and archive.is_relative_to(target):
            raise ValueError("Нельзя создавать архив внутри архивируемого каталога")
        exists = archive.exists()
        if exists and not self.allow_replace(str(target), str(archive), False):
            return
        with tempfile.TemporaryDirectory(
            prefix=".dno-pack-", dir=archive.parent
        ) as staging:
            temporary = Path(staging) / "archive.tar.gz"
            with tarfile.open(temporary, "w:gz") as output:
                self._add_local(output, target, target.name)
            atomic_local_copy(
                str(temporary), str(archive), self.control, replace=exists
            )
        self.result.completed += 1

    def _add_local(self, archive: tarfile.TarFile, source: Path, name: str) -> None:
        self.control.check()
        if is_local_link(source):
            self.result.skipped += 1
            return
        info = archive.gettarinfo(str(source), arcname=name)
        if info.isfile():
            with source.open("rb") as stream:
                archive.addfile(info, _CheckedReader(stream, self.control))
        elif info.isdir():
            archive.addfile(info)
            with os.scandir(source) as entries:
                for entry in entries:
                    self._add_local(
                        archive, Path(entry.path), str(PurePosixPath(name) / entry.name)
                    )
        else:
            raise ValueError(f"Неподдерживаемый тип файла: {source}")

    def _member_path(self, root: str, name: str) -> Path:
        member = PurePosixPath(name)
        if member.is_absolute() or ".." in member.parts:
            raise ValueError(f"Небезопасный путь в архиве: {name}")
        destination = root
        for part in member.parts:
            destination = local_child(destination, validate_name(part, local=True))
        return Path(destination)

    def _copy_stream(self, source: IO[bytes], destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as output:
            while chunk := source.read(1024 * 1024):
                self.control.check()
                output.write(chunk)

    def _unpack_local(self) -> None:
        fmt = _archive_format(self.path)
        if fmt not in {"tar", "zip"}:
            raise ValueError("Локально поддерживаются tar и zip")
        destination = Path(self.archive_path).absolute()
        if is_local_link(destination):
            raise ValueError("Каталог назначения является ссылкой")
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".dno-unpack-", dir=destination
        ) as staging:
            if fmt == "zip":
                with zipfile.ZipFile(self.path) as archive:
                    for entry in archive.infolist():
                        self.control.check()
                        target = self._member_path(staging, entry.filename)
                        if stat.S_ISLNK(entry.external_attr >> 16):
                            raise ValueError(
                                f"Ссылка в архиве не поддерживается: {entry.filename}"
                            )
                        if entry.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            with archive.open(entry) as stream:
                                self._copy_stream(stream, target)
            else:
                with tarfile.open(self.path, "r:*") as archive:
                    for member in archive:
                        self.control.check()
                        target = self._member_path(staging, member.name)
                        if member.isdir():
                            target.mkdir(parents=True, exist_ok=True)
                        elif member.isfile():
                            tar_stream = archive.extractfile(member)
                            if tar_stream is None:
                                raise ValueError(f"Не удалось прочитать {member.name}")
                            with tar_stream:
                                self._copy_stream(tar_stream, target)
                        else:
                            raise ValueError(
                                f"Ссылка или специальный файл в архиве: {member.name}"
                            )
            for staged in Path(staging).iterdir():
                self._install_local(
                    staged, Path(local_child(str(destination), staged.name))
                )

    def _install_local(self, source: Path, destination: Path) -> None:
        self.control.check()
        if is_local_link(destination):
            raise ValueError(f"Цель является ссылкой: {destination}")
        exists = destination.exists()
        if exists:
            if destination.is_dir() != source.is_dir():
                raise ValueError(f"Тип существующего объекта отличается: {destination}")
            if not self.allow_replace(str(source), str(destination), source.is_dir()):
                return
        if source.is_dir():
            destination.mkdir(exist_ok=True)
            for entry in source.iterdir():
                self._install_local(
                    entry, Path(local_child(str(destination), entry.name))
                )
        else:
            atomic_local_copy(
                str(source), str(destination), self.control, replace=exists
            )
        self.result.completed += 1

    def _pack_remote(self) -> None:
        target = PurePosixPath(self.path)
        archive = PurePosixPath(self.archive_path)
        attrs = self.sftp.lstat(str(target))
        if stat.S_ISDIR(attrs.st_mode or 0) and archive.is_relative_to(target):
            raise ValueError("Нельзя создавать архив внутри архивируемого каталога")
        existing = remote_stat(self.sftp, str(archive))
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode or 0):
                raise ValueError("Цель не является обычным файлом")
            if not self.allow_replace(str(target), str(archive), False):
                return
        temporary = remote_child(
            str(archive.parent), f".dno-pack-{uuid.uuid4().hex}.tar.gz"
        )
        try:
            with self.sftp.open(temporary, "wx"):
                pass
            command = shlex.join(
                ["tar", "czf", temporary, "-C", str(target.parent), "--", target.name]
            )
            self._exec_remote(command)
            self.control.check()
            if existing is not None:
                self.sftp.posix_rename(temporary, str(archive))
            else:
                self.sftp.rename(temporary, str(archive))
            self.result.completed += 1
        finally:
            try:
                remove_remote_temporary(self.sftp, temporary)
            except (OSError, paramiko.SSHException):
                pass

    def _unpack_remote(self) -> None:
        archive = str(PurePosixPath(self.path))
        destination = str(PurePosixPath(self.archive_path))
        fmt = _archive_format(archive)
        if not fmt:
            raise ValueError("Неподдерживаемый формат архива")
        archive = archive if archive.startswith("/") else "./" + archive
        destination = destination if destination.startswith("/") else "./" + destination
        commands = {
            "tar": [
                "tar",
                "xf",
                archive,
                "-C",
                destination,
                "--skip-old-files",
                "--no-overwrite-dir",
            ],
            "zip": ["unzip", "-P", "", "-n", archive, "-d", destination],
            "7z": ["7z", "x", archive, "-o" + destination, "-aos", "-y", "-p"],
            "rar": ["unrar", "x", "-p-", "-o-", archive, destination.rstrip("/") + "/"],
        }
        self.log_message.emit("Удалённая распаковка сохраняет существующие файлы")
        self._exec_remote(
            shlex.join(["mkdir", "-p", "--", destination])
            + " && "
            + shlex.join(commands[fmt])
        )
        self.result.completed += 1

    def _exec_remote(self, command: str, timeout: float = 300) -> str:
        if self.transport is None:
            raise ConnectionError("Нет SSH-соединения")
        channel = self.transport.open_session(timeout=10)
        channel.settimeout(1)
        control = OperationControl(
            self._cancel_event.is_set,
            min(self.control.deadline or float("inf"), time.monotonic() + timeout),
        )
        output = bytearray()
        errors = bytearray()
        try:
            with control.watch(channel):
                channel.exec_command(command)
                channel.shutdown_write()
                while True:
                    control.check()
                    if channel.recv_ready():
                        output.extend(channel.recv(32768))
                        del output[:-65536]
                    if channel.recv_stderr_ready():
                        errors.extend(channel.recv_stderr(32768))
                        del errors[:-65536]
                    if (
                        channel.exit_status_ready()
                        and not channel.recv_ready()
                        and not channel.recv_stderr_ready()
                    ):
                        break
                    self._cancel_event.wait(0.02)
                status = channel.recv_exit_status()
                if status != 0:
                    detail = (
                        (errors or output).decode("utf-8", errors="replace").strip()
                    )
                    raise RuntimeError(
                        f"Архиватор завершился с ошибкой ({status}): {detail}"
                    )
            return output.decode("utf-8", errors="replace")
        finally:
            channel.close()
