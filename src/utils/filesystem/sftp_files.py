"""Безопасные пути и атомарная передача файлов без зависимости от Qt."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import paramiko


class TransferCancelled(Exception):
    """Операция отменена пользователем."""


def describe_error(exc: Exception) -> str:
    """Сохранить тип, errno и понятную подсказку для ошибок файловой системы."""
    detail = str(exc).strip() or type(exc).__name__
    number = getattr(exc, "errno", None)
    hints = {
        errno.EACCES: "нет прав доступа",
        errno.ENOSPC: "нет места на диске",
        errno.EROFS: "файловая система только для чтения",
        errno.ENOENT: "путь не найден",
        errno.ENOTDIR: "ожидался каталог",
        errno.EEXIST: "объект уже существует",
    }
    hint = hints.get(number, "") if isinstance(number, int) else ""
    if number is None and isinstance(exc, OSError) and detail.lower() == "failure":
        hint = "SFTP-код 4: проверьте права, путь и свободное место на сервере"
    return f"{detail}{f' ({hint})' if hint else ''} [{type(exc).__name__}]"


def describe_ssh_failure(error: Exception) -> str:
    """Причина отказа подключения для пользователя, без деталей учётных данных."""
    if isinstance(error, paramiko.AuthenticationException):
        return "Отказ SSH-авторизации. Проверьте связку учётных данных хоста"
    if isinstance(error, paramiko.BadHostKeyException):
        return "Ключ сервера изменился. Проверьте known_hosts"
    if "No authentication methods" in str(error):
        return "Для хоста нет учётных данных. Укажите связку или SSH-ключ"
    if isinstance(error, paramiko.SSHException):
        return "SSH-подключение отклонено. Проверьте ключ сервера и доступность SSH"
    if isinstance(error, TimeoutError):
        return "Истекло время SSH-подключения"
    return "Не удалось подключиться по SSH. Проверьте доступность хоста"


def validate_name(name: str, *, local: bool = False) -> str:
    """Проверить одно имя, не допуская подмены пути или Windows ADS."""
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise ValueError(f"Недопустимое имя: {name!r}")
    if local and os.name == "nt":
        reserved_check = getattr(os.path, "isreserved", None)
        reserved = (
            bool(reserved_check(name))
            if reserved_check
            else PureWindowsPath(name).is_reserved()
        )
        if (
            any(c in name for c in '\\:<>"|?*')
            or any(ord(c) < 32 for c in name)
            or name.endswith((".", " "))
            or reserved
        ):
            raise ValueError(f"Имя не поддерживается в Windows: {name!r}")
    return name


def is_local_link(path: str | Path) -> bool:
    """Распознать ссылки и directory reparse points, включая junction."""
    try:
        attrs = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(attrs.st_mode) or bool(
        getattr(attrs, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def local_child(directory: str, name: str) -> str:
    """Построить путь непосредственного потомка без следования ссылкам."""
    validate_name(name, local=True)
    base = Path(directory).absolute()
    target = base / name
    if target.parent != base or is_local_link(target):
        raise ValueError(f"Небезопасный целевой путь: {target}")
    return str(target)


def remote_child(directory: str, name: str) -> str:
    """Построить удалённый путь из одного проверенного имени."""
    return str(PurePosixPath(directory) / validate_name(name))


def remote_stat(sftp: Any, path: str) -> Any:
    """Отличать отсутствие объекта от ошибки доступа или соединения."""
    try:
        return sftp.lstat(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise


def ensure_remote_dir(sftp: Any, path: str) -> None:
    """Создать каталог и проверить тип при любом ответе «уже существует»."""
    try:
        sftp.mkdir(path)
    except OSError:
        attrs = remote_stat(sftp, path)
        if attrs is None or not stat.S_ISDIR(attrs.st_mode or 0):
            raise


@dataclass
class OperationControl:
    """Отмена и общий deadline, включая ожидание сетевого ответа."""

    cancelled: Callable[[], bool] = lambda: False
    deadline: float | None = None

    def check(self) -> None:
        """Прервать операцию при отмене или истечении общего времени."""
        if self.cancelled():
            raise TransferCancelled("Операция отменена")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutError("Превышено время ожидания операции")

    @contextmanager
    def watch(self, channel: Any) -> Iterator[None]:
        """Закрывать только канал этой операции при отмене/deadline."""
        stopped = threading.Event()

        def monitor() -> None:
            while not stopped.wait(0.1):
                try:
                    self.check()
                except (TransferCancelled, TimeoutError):
                    try:
                        channel.close()
                    except (OSError, paramiko.SSHException):
                        pass
                    return

        watcher = threading.Thread(target=monitor, daemon=True, name="sftp-deadline")
        watcher.start()
        try:
            self.check()
            yield
            self.check()
        except Exception:
            self.check()
            raise
        finally:
            stopped.set()
            watcher.join(0.2)


def open_sftp_channel(
    transport: paramiko.Transport, control: OperationControl
) -> paramiko.SFTPClient:
    """Открыть отдельный канал с ограничением negotiation и без общего клиента."""
    control.check()
    timeout = (
        min(10.0, max(0.01, control.deadline - time.monotonic()))
        if control.deadline is not None
        else 10.0
    )
    channel = transport.open_session(timeout=timeout)
    channel.settimeout(10)
    try:
        negotiation = OperationControl(
            cancelled=control.cancelled,
            deadline=min(control.deadline or float("inf"), time.monotonic() + 10),
        )
        with negotiation.watch(channel):
            channel.invoke_subsystem("sftp")
            client = paramiko.SFTPClient(channel)
        return client
    except Exception:
        channel.close()
        raise


def atomic_download(
    sftp: Any,
    source: str,
    destination: str,
    callback: Callable[[int, int], None] | None = None,
    *,
    control: OperationControl | None = None,
    replace: bool = True,
) -> None:
    """Скачать во временный соседний файл, сохранив старый при ошибке."""
    control = control or OperationControl()
    control.check()
    target = Path(destination).absolute()
    if is_local_link(target):
        raise ValueError(f"Целевой файл является ссылкой: {target}")
    fd, temporary = tempfile.mkstemp(
        prefix=".dno-sftp-", suffix=".part", dir=target.parent
    )
    os.close(fd)
    try:

        def progress(current: int, total: int) -> None:
            control.check()
            if callback is not None:
                callback(current, total)

        sftp.get(
            source, temporary, callback=progress, max_concurrent_prefetch_requests=32
        )
        control.check()
        if is_local_link(target):
            raise ValueError(f"Целевой файл стал ссылкой: {target}")
        if replace:
            if target.exists():
                os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
            os.replace(temporary, target)
        elif os.name == "nt":
            # Windows rename не заменяет существующий путь.
            os.rename(temporary, target)
        else:
            # Ссылка создаётся эксклюзивно; существующий файл не заменяется.
            os.link(temporary, target)
    finally:
        _remove_local_temporary(temporary)


@contextmanager
def atomic_remote_writer(
    sftp: Any,
    destination: str,
    *,
    control: OperationControl | None = None,
    replace: bool = True,
) -> Iterator[str]:
    """Дать путь временного соседнего файла и опубликовать его после записи.

    При ошибке внутри блока исходный файл не меняется, временный удаляется.
    """
    control = control or OperationControl()
    control.check()
    parent = str(PurePosixPath(destination).parent)
    temporary = remote_child(parent, f".dno-sftp-{uuid.uuid4().hex}.part")
    previous = remote_stat(sftp, destination)
    if previous is not None and not stat.S_ISREG(previous.st_mode or 0):
        raise ValueError(f"Цель не является обычным файлом: {destination}")
    created = False
    try:
        with sftp.open(temporary, "wx"):
            created = True
        yield temporary
        control.check()
        if previous is not None and replace:
            if previous.st_mode is not None:
                sftp.chmod(temporary, stat.S_IMODE(previous.st_mode))
            try:
                sftp.posix_rename(temporary, destination)
            except OSError as exc:
                raise OSError(
                    "Сервер не выполнил атомарную замену. Исходный файл сохранён: "
                    f"{destination}: {exc}"
                ) from exc
        else:
            # Стандартный SFTP rename запрещает замену существующего объекта.
            sftp.rename(temporary, destination)
        created = False
    finally:
        if created:
            try:
                remove_remote_temporary(sftp, temporary)
            except (OSError, paramiko.SSHException):
                pass


def atomic_upload(
    sftp: Any,
    source: str,
    destination: str,
    callback: Callable[[int, int], None] | None = None,
    *,
    control: OperationControl | None = None,
    replace: bool = True,
) -> None:
    """Загрузить во временный файл; не удалять старый при отсутствии atomic rename."""
    control = control or OperationControl()

    def progress(current: int, total: int) -> None:
        control.check()
        if callback is not None:
            callback(current, total)

    with atomic_remote_writer(
        sftp, destination, control=control, replace=replace
    ) as temporary:
        sftp.put(source, temporary, callback=progress)


@dataclass(frozen=True)
class FileEntry:
    """Данные строки файловой панели без объектов интерфейса."""

    name: str
    mode: int | None
    size: int
    modified: float | None


def list_remote_dir(
    sftp: Any, path: str, control: OperationControl
) -> tuple[str, list[FileEntry]]:
    """Прочитать каталог целиком: read-ahead исчерпан до следующих запросов канала."""
    normalized = sftp.normalize(path or ".")
    entries: list[FileEntry] = []
    for entry in sftp.listdir_iter(normalized, read_aheads=16):
        control.check()
        validate_name(entry.filename)
        entries.append(
            FileEntry(entry.filename, entry.st_mode, entry.st_size or 0, entry.st_mtime)
        )
    return normalized, entries


def remove_remote_tree(
    sftp: Any,
    path: str,
    control: OperationControl,
    on_removed: Callable[[str], None] | None = None,
) -> None:
    """Удалить объект рекурсивно; lstat не даёт следовать по ссылкам на каталоги."""
    control.check()
    attrs = sftp.lstat(path)
    if stat.S_ISDIR(attrs.st_mode or 0):
        entries = list(sftp.listdir_iter(path, read_aheads=16))
        for entry in entries:
            remove_remote_tree(
                sftp, remote_child(path, entry.filename), control, on_removed
            )
        control.check()
        sftp.rmdir(path)
    else:
        sftp.remove(path)
    if on_removed is not None:
        on_removed(path)


def remove_remote_temporary(sftp: Any, path: str) -> None:
    """Убрать собственный временный файл, в том числе после отмены канала."""
    try:
        sftp.remove(path)
        return
    except FileNotFoundError:
        return
    except (OSError, paramiko.SSHException):
        transport = sftp.get_channel().get_transport()
        if transport is None or not transport.is_active():
            return
    cleanup = None
    try:
        control = OperationControl(deadline=time.monotonic() + 3)
        cleanup = open_sftp_channel(transport, control)
        with control.watch(cleanup.get_channel()):
            while True:
                control.check()
                try:
                    cleanup.remove(path)
                    break
                except FileNotFoundError:
                    break
                except OSError as exc:
                    # Сервер Windows может ещё закрывать handle старого канала.
                    if exc.errno not in (None, errno.EACCES, errno.EPERM, errno.EBUSY):
                        raise
                    time.sleep(0.05)
    except (OSError, paramiko.SSHException):
        # При потере всего транспорта удаление недоступно; конечный файл цел.
        pass
    finally:
        if cleanup is not None:
            cleanup.close()


def atomic_local_copy(
    source: str, destination: str, control: OperationControl, *, replace: bool
) -> None:
    """Копировать локально с отменой и безопасной фиксацией результата."""
    target = Path(destination)
    if is_local_link(target):
        raise ValueError(f"Целевой файл является ссылкой: {target}")
    fd, temporary = tempfile.mkstemp(
        prefix=".dno-sftp-", suffix=".part", dir=target.parent
    )
    try:
        with os.fdopen(fd, "wb") as output, open(source, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                control.check()
                output.write(chunk)
        control.check()
        shutil.copystat(source, temporary, follow_symlinks=False)
        if is_local_link(target):
            raise ValueError(f"Целевой файл стал ссылкой: {target}")
        if replace:
            os.replace(temporary, target)
        elif os.name == "nt":
            os.rename(temporary, target)
        else:
            os.link(temporary, target)
    finally:
        _remove_local_temporary(temporary)


def _remove_local_temporary(path: str) -> None:
    """Убрать собственный временный файл, включая readonly-копию Windows."""
    try:
        Path(path).unlink(missing_ok=True)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        Path(path).unlink(missing_ok=True)
