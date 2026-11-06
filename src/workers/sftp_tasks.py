"""Фоновые операции SFTP с независимыми каналами и снимками задач."""

from __future__ import annotations

import os
import shutil
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import paramiko
from PySide6.QtCore import QObject, QThread, Signal

from src.utils.filesystem.sftp_files import (
    FileEntry,
    OperationControl,
    TransferCancelled,
    atomic_download,
    atomic_local_copy,
    atomic_upload,
    describe_error,
    ensure_remote_dir,
    is_local_link,
    list_remote_dir,
    local_child,
    open_sftp_channel,
    remote_child,
    remote_stat,
    remove_remote_tree,
)


@dataclass(frozen=True)
class TransferItem:
    """Независимые от модели полные пути исходного и целевого объекта."""

    source: str
    destination: str
    is_dir: bool = False


@dataclass
class OperationResult:
    """Итог с явными частичными ошибками, пропусками и отменой."""

    completed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    failed: int = 0
    cancelled: bool = False

    def add_error(self, message: str) -> None:
        """Считать все ошибки, сохраняя ограниченную выборку сообщений."""
        self.failed += 1
        if len(self.errors) < 100:
            self.errors.append(message)

    @property
    def success(self) -> bool:
        """Успех означает отсутствие ошибок и отмены."""
        return not self.cancelled and not self.errors

    @property
    def message(self) -> str:
        """Сформировать краткий итог для окна и журнала."""
        state = "Отменено" if self.cancelled else "Завершено"
        if self.errors and not self.cancelled:
            state = "Завершено с ошибками"
        summary = (
            f"{state}. Выполнено: {self.completed}, пропущено: {self.skipped}, "
            f"ошибок: {self.failed}"
        )
        return summary + ("\n" + "\n".join(self.errors[:5]) if self.errors else "")


@dataclass
class ConflictRequest:
    """Ожидаемое решение GUI без обращения рабочего потока к виджетам."""

    source: str
    destination: str
    is_dir: bool
    ready: threading.Event = field(default_factory=threading.Event)
    decision: str = "cancel"
    apply_all: bool = False


class SftpTaskWorker(QThread):
    """Общий жизненный цикл: результат хранится до настоящего finished."""

    progress = Signal(int, str)
    log_message = Signal(str)
    conflict = Signal(object)

    def __init__(
        self,
        transport: paramiko.Transport | None,
        parent: QObject | None = None,
        *,
        remote: bool = True,
        timeout: float | None = None,
    ) -> None:
        super().__init__(parent)
        self.transport = transport
        self.remote = remote
        self.timeout = timeout
        self.result = OperationResult()
        self._cancel_event = threading.Event()
        self.control = OperationControl(self._cancel_event.is_set)
        self.sftp: Any = None
        self.description = "Подготовка..."
        self.badge = "Операция"
        self._decisions: dict[bool, str] = {}
        self._last_progress = 0.0
        self._last_log = 0.0

    def cancel(self) -> None:
        """Запросить остановку; сетевой watchdog закрывает собственный канал."""
        self._cancel_event.set()

    def run(self) -> None:
        """Выполнить задачу и закрыть принадлежащий ей канал в том же потоке."""
        if self.timeout is not None:
            self.control.deadline = time.monotonic() + self.timeout
        try:
            self.control.check()
            if self.remote:
                if self.transport is None or not self.transport.is_active():
                    raise ConnectionError("Нет SSH-соединения")
                self.sftp = open_sftp_channel(self.transport, self.control)
                with self.control.watch(self.sftp.get_channel()):
                    self.perform()
            else:
                self.perform()
            self.control.check()
            if not self.result.errors:
                self.progress.emit(100, self.result.message)
        except TransferCancelled:
            self.result.cancelled = True
        except Exception as exc:
            if self._cancel_event.is_set():
                self.result.cancelled = True
            else:
                self.result.add_error(describe_error(exc))
        finally:
            if self.sftp is not None:
                try:
                    self.sftp.close()
                except Exception:
                    pass
                self.sftp = None

    def perform(self) -> None:
        """Выполнить предметную операцию в рабочем потоке."""
        raise NotImplementedError

    def log_limited(self, message: str) -> None:
        """Не заполнять очередь GUI сообщениями для каждого мелкого файла."""
        now = time.monotonic()
        if now - self._last_log >= 0.15:
            self._last_log = now
            self.log_message.emit(message)

    def allow_replace(self, source: str, destination: str, is_dir: bool) -> bool:
        """Получить одно решение для любого способа запуска передачи."""
        decision = self._decisions.get(is_dir)
        if decision is None:
            request = ConflictRequest(source, destination, is_dir)
            self.conflict.emit(request)
            while not request.ready.wait(0.1):
                self.control.check()
            self.control.check()
            decision = request.decision
            if request.apply_all and decision != "cancel":
                self._decisions[is_dir] = decision
        if decision == "cancel":
            raise TransferCancelled()
        if decision == "skip":
            self.result.skipped += 1
            return False
        return True

    def file_progress(self, name: str) -> Callable[[int, int], None]:
        """Ограничить частоту сигналов независимо от количества файлов."""

        def callback(current: int, total: int) -> None:
            self.control.check()
            now = time.monotonic()
            if now - self._last_progress >= 0.15:
                self._last_progress = now
                self.progress.emit(
                    -1,
                    f"{name}: {current}/{total} байт; готово {self.result.completed}",
                )

        return callback


class TransferWorker(SftpTaskWorker):
    """Ленивое рекурсивное копирование без предварительного обхода дерева."""

    def __init__(
        self,
        transport: paramiko.Transport | None,
        items: list[TransferItem],
        is_upload: bool,
        parent: QObject | None = None,
        *,
        local: bool = False,
    ) -> None:
        super().__init__(transport, parent, remote=not local)
        self.items = tuple(items)
        self.local = local
        self.is_upload = is_upload
        self.badge = "Загрузка" if is_upload else "Скачивание"
        self.description = f"{self.badge}: {len(items)} объектов"

    def perform(self) -> None:
        """Передать объекты, сохранив ошибки отдельных объектов в результате."""
        self.progress.emit(-1, self.description)
        for item in self.items:
            self._copy_item(item)

    def _copy_item(self, item: TransferItem) -> None:
        self.control.check()
        try:
            if self.local:
                self._local_copy(item.source, item.destination)
            elif self.is_upload:
                self._upload(item.source, item.destination)
            else:
                self._download(item.source, item.destination)
        except (TransferCancelled, TimeoutError):
            raise
        except Exception as exc:
            self.control.check()
            message = f"{item.source} → {item.destination}: {describe_error(exc)}"
            self.result.add_error(message)
            self.log_limited(message)

    def _local_copy(self, source: str, destination: str) -> None:
        if is_local_link(source):
            self.result.skipped += 1
            self.log_limited(f"Пропущена ссылка: {source}")
            return
        directory = os.path.isdir(source)
        if not directory and not stat.S_ISREG(
            os.stat(source, follow_symlinks=False).st_mode
        ):
            raise ValueError(f"Не поддерживается тип исходного файла: {source}")
        if directory and Path(destination).resolve().is_relative_to(
            Path(source).resolve()
        ):
            raise ValueError("Нельзя скопировать каталог в самого себя")
        if is_local_link(destination):
            raise ValueError(f"Цель является ссылкой: {destination}")
        exists = os.path.lexists(destination)
        if exists:
            if os.path.isdir(destination) != directory:
                raise ValueError(f"Тип существующего объекта отличается: {destination}")
            if not self.allow_replace(source, destination, directory):
                return
        if directory:
            os.makedirs(destination, exist_ok=True)
            with os.scandir(source) as entries:
                for entry in entries:
                    self.control.check()
                    self._copy_item(
                        TransferItem(entry.path, local_child(destination, entry.name))
                    )
        else:
            atomic_local_copy(source, destination, self.control, replace=exists)
        self.result.completed += 1

    def _upload(self, source: str, destination: str) -> None:
        if is_local_link(source):
            self.result.skipped += 1
            self.log_limited(f"Пропущена ссылка: {source}")
            return
        source_stat = os.stat(source, follow_symlinks=False)
        directory = stat.S_ISDIR(source_stat.st_mode)
        existing = remote_stat(self.sftp, destination)
        if existing is not None:
            if bool(stat.S_ISDIR(existing.st_mode or 0)) != directory:
                raise ValueError(f"Тип существующего объекта отличается: {destination}")
            if stat.S_ISLNK(existing.st_mode or 0):
                raise ValueError(f"Целевой объект является ссылкой: {destination}")
            if not self.allow_replace(source, destination, directory):
                return
        if directory:
            ensure_remote_dir(self.sftp, destination)
            with os.scandir(source) as entries:
                for entry in entries:
                    self.control.check()
                    self._copy_item(
                        TransferItem(entry.path, remote_child(destination, entry.name))
                    )
            self.result.completed += 1
        elif stat.S_ISREG(source_stat.st_mode):
            atomic_upload(
                self.sftp,
                source,
                destination,
                self.file_progress(Path(source).name),
                control=self.control,
                replace=existing is not None,
            )
            self.result.completed += 1
        else:
            raise ValueError(f"Не поддерживается тип исходного файла: {source}")

    def _download(self, source: str, destination: str) -> None:
        attrs = self.sftp.lstat(source)
        mode = attrs.st_mode or 0
        if stat.S_ISLNK(mode):
            self.result.skipped += 1
            self.log_limited(f"Пропущена ссылка: {source}")
            return
        directory = stat.S_ISDIR(mode)
        if is_local_link(destination):
            raise ValueError(f"Целевой объект является ссылкой: {destination}")
        exists = os.path.lexists(destination)
        if exists:
            if os.path.isdir(destination) != directory:
                raise ValueError(f"Тип существующего объекта отличается: {destination}")
            if not self.allow_replace(source, destination, directory):
                return
        if directory:
            os.makedirs(destination, exist_ok=True)
            # До других запросов исчерпываем read-ahead ответы этого каталога.
            entries = list(self.sftp.listdir_iter(source, read_aheads=16))
            for entry in entries:
                self.control.check()
                self._copy_item(
                    TransferItem(
                        remote_child(source, entry.filename),
                        local_child(destination, entry.filename),
                    )
                )
            self.result.completed += 1
        else:
            if mode and not stat.S_ISREG(mode):
                raise ValueError(f"Не поддерживается тип исходного файла: {source}")
            atomic_download(
                self.sftp,
                source,
                destination,
                self.file_progress(Path(source).name),
                control=self.control,
                replace=exists,
            )
            self.result.completed += 1


class DeleteWorker(SftpTaskWorker):
    """Удаление без следования ссылкам и reparse points."""

    def __init__(
        self,
        transport: paramiko.Transport | None,
        paths: list[tuple[str, bool]],
        is_remote: bool,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(transport, parent, remote=is_remote)
        self.paths = tuple(paths)
        self.badge = "Удаление"
        self.description = f"Удаление {len(paths)} объектов"

    def perform(self) -> None:
        """Удалить объекты последовательно, учитывая ошибки каждого корня."""
        for path, _ in self.paths:
            self.control.check()
            try:
                if self.remote:
                    self._remote_delete(path)
                else:
                    self._local_delete(path)
            except (TransferCancelled, TimeoutError):
                raise
            except Exception as exc:
                self.control.check()
                self.result.add_error(f"{path}: {describe_error(exc)}")

    def _local_delete(self, path: str) -> None:
        self.control.check()
        if is_local_link(path):
            try:
                os.unlink(path)
            except IsADirectoryError:
                os.rmdir(path)
            except PermissionError:
                if os.path.isdir(path):
                    os.rmdir(path)
                else:
                    raise
        elif os.path.isdir(path):
            with os.scandir(path) as entries:
                for entry in entries:
                    self._local_delete(entry.path)
            self.control.check()
            os.rmdir(path)
        else:
            os.remove(path)
        self.result.completed += 1
        self.file_progress(path)(self.result.completed, 0)

    def _remote_delete(self, path: str) -> None:
        def removed(item: str) -> None:
            self.result.completed += 1
            self.file_progress(item)(self.result.completed, 0)

        remove_remote_tree(self.sftp, path, self.control, removed)


class DirectoryWorker(SftpTaskWorker):
    """Листинг и normalize в фоне с результатом до штатного finished."""

    def __init__(
        self,
        transport: paramiko.Transport | None,
        path: str,
        remote: bool,
        generation: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(transport, parent, remote=remote)
        self.path = path
        self.generation = generation
        self.navigation_mode = "refresh"
        self.entries: list[FileEntry] = []
        self.normalized_path = path

    def perform(self) -> None:
        """Считать каталог, не меняя отображаемый путь до успешного результата."""
        if self.remote:
            self.normalized_path, self.entries = list_remote_dir(
                self.sftp, self.path, self.control
            )
        else:
            self.normalized_path = os.path.abspath(self.path)
            with os.scandir(self.normalized_path) as entries:
                for entry in entries:
                    self.control.check()
                    attrs = entry.stat(follow_symlinks=False)
                    self.entries.append(
                        FileEntry(
                            entry.name,
                            attrs.st_mode,
                            attrs.st_size,
                            attrs.st_mtime,
                        )
                    )


class FileActionWorker(SftpTaskWorker):
    """Короткие файловые операции, выполняемые координатором вне GUI."""

    def __init__(
        self,
        transport: paramiko.Transport | None,
        remote: bool,
        operation: str,
        paths: list[tuple[str, str]],
        parent: QObject | None = None,
        *,
        mode: int = 0o644,
    ) -> None:
        super().__init__(transport, parent, remote=remote)
        self.operation = operation
        self.paths = tuple(paths)
        self.mode = mode
        self.badge = "Файлы"
        self.description = f"Файловая операция: {operation}"
        self.attributes: Any = None

    def perform(self) -> None:
        """Создать, переместить объект либо прочитать/изменить права."""
        for source, destination in self.paths:
            self.control.check()
            if self.operation == "mkdir":
                if self.remote:
                    self.sftp.mkdir(destination)
                else:
                    os.mkdir(destination)
            elif self.operation == "create":
                if self.remote:
                    with self.sftp.open(destination, "wx"):
                        pass
                else:
                    with open(destination, "x", encoding="utf-8"):
                        pass
            elif self.operation in {"rename", "move"}:
                if self.remote:
                    if remote_stat(self.sftp, destination) is not None:
                        raise FileExistsError(f"Уже существует: {destination}")
                    self.sftp.rename(source, destination)
                else:
                    if os.path.lexists(destination):
                        raise FileExistsError(f"Уже существует: {destination}")
                    shutil.move(source, destination)
            elif self.operation == "stat":
                self.attributes = (
                    self.sftp.stat(source) if self.remote else os.stat(source)
                )
            elif self.operation == "chmod":
                if self.remote:
                    self.sftp.chmod(source, self.mode)
                else:
                    os.chmod(source, self.mode)
            else:
                raise ValueError(f"Неизвестная файловая операция: {self.operation}")
            self.result.completed += 1


class ConnectionWorker(QThread):
    """Подключение с передачей владения только после настоящего finished."""

    def __init__(self, device: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self.ssh: paramiko.SSHClient | None = None
        self.sftp: paramiko.SFTPClient | None = None
        self.home = "/"
        self.error: str | None = None
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Отменить подключение без блокировки GUI."""
        self._cancel_event.set()

    def run(self) -> None:
        """Подключиться и закрыть неподтверждённые ресурсы при отмене/ошибке."""
        from src.workers.command.executor_base import create_ssh_client
        from src.workers.command.sftp_utils import open_sftp_with_diagnosis

        control = OperationControl(self._cancel_event.is_set)
        ssh = paramiko.SSHClient()
        try:
            with control.watch(ssh):
                create_ssh_client(self.device, client=ssh)
                sftp = open_sftp_with_diagnosis(
                    ssh, host=self.device.host, control=control
                )
                self.sftp = sftp
                with control.watch(sftp.get_channel()):
                    self.home = sftp.normalize(".")
            self.ssh = ssh
        except Exception as exc:
            self.error = str(exc)
            for resource in (self.sftp, ssh):
                if resource is not None:
                    try:
                        resource.close()
                    except (OSError, paramiko.SSHException):
                        pass
            self.sftp = None
