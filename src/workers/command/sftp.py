"""Исполнение SFTP-команд с проверкой направления и безопасной записью."""

from __future__ import annotations

import os
import posixpath
import re
import stat
import time

from src.config import config
from src.domain.models.device import DeviceModel
from src.domain.utils import command_param_replacer
from src.logger import logger
from src.utils.filesystem.sftp_files import (
    OperationControl,
    TransferCancelled,
    atomic_download,
    atomic_upload,
    is_local_link,
    local_child,
    remote_child,
    remote_stat,
)

from .desktop_context import DesktopContext
from .executor_base import BaseCommandExecutor, get_credentials
from .sftp_utils import open_sftp_with_diagnosis


def parse_sftp_command(text: str, username: str, host: str) -> tuple[bool, str, str]:
    """Разобрать ровно одно направление, не изменяя имён и префиксов путей."""
    parts = text.split("::")
    if len(parts) != 2 or not all(parts):
        raise ValueError("Неверный формат SFTP: требуется источник::назначение")
    source, destination = parts
    prefix = f"{username}@{host}:"
    source_remote = source.startswith(prefix)
    destination_remote = destination.startswith(prefix)
    if source_remote == destination_remote:
        raise ValueError(f"Ровно один путь должен начинаться с {prefix}")
    local = destination if source_remote else source
    if re.match(r"^[^/\\:]+@[^:]+:", local):
        raise ValueError("Путь указывает на другое SSH-подключение")
    if source_remote:
        source = source[len(prefix) :]
    else:
        destination = destination[len(prefix) :]
    if not source or not destination or "\0" in source or "\0" in destination:
        raise ValueError("Пути SFTP не должны быть пустыми или содержать NUL")
    return not source_remote, source, destination


def local_download_target(destination: str, remote_source: str) -> str:
    """Подготовить локальный путь скачивания.

    ``~`` раскрывается в домашний каталог оператора, каталог назначения
    (существующий или с завершающим разделителем) получает имя исходного
    файла, недостающие каталоги создаются.
    """
    target = os.path.expanduser(destination)
    if target.endswith(("/", "\\")) or os.path.isdir(target):
        name = posixpath.basename(remote_source)
        if not name:
            raise ValueError(f"Не удалось определить имя файла: {remote_source}")
        target = os.path.join(target, name)
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    return target


def local_tree_target(destination: str, remote_source: str) -> str:
    """Каталог, в который попадёт содержимое удалённого каталога.

    Как в rsync: источник с завершающим ``/`` копируется содержимым прямо в
    назначение, без него в существующий каталог (или путь с ``/`` на конце)
    добавляется каталог с исходным именем.
    """
    target = os.path.expanduser(destination)
    if not remote_source.endswith("/") and (
        target.endswith(("/", "\\")) or os.path.isdir(target)
    ):
        name = posixpath.basename(remote_source.rstrip("/"))
        if not name:
            raise ValueError(f"Не удалось определить имя каталога: {remote_source}")
        target = os.path.join(target, name)
    return target


def download_tree(
    sftp,
    source: str,
    target: str,
    control: OperationControl,
    on_file,
    problems: list[str],
) -> int:
    """Скачать каталог рекурсивно; вернуть число файлов.

    Ссылки и специальные файлы не обходятся, имена, недопустимые на этой ОС,
    пропускаются: всё пропущенное попадает в ``problems``, остальное копируется.
    Каждый файл пишется атомарно через временный соседний файл.
    """
    control.check()
    os.makedirs(target, exist_ok=True)
    if is_local_link(target):
        raise ValueError(f"Локальный каталог является ссылкой: {target}")
    # Список читается целиком до вложенных запросов: иначе теряются read-ahead ответы.
    entries = list(sftp.listdir_iter(source, read_aheads=16))
    copied = 0
    for entry in sorted(entries, key=lambda item: item.filename):
        control.check()
        remote = posixpath.join(source, entry.filename)
        try:
            remote_child(source, entry.filename)
            local = local_child(target, entry.filename)
        except ValueError as error:
            problems.append(f"{remote}: {error}")
            continue
        mode = entry.st_mode or 0
        if stat.S_ISDIR(mode):
            copied += download_tree(sftp, remote, local, control, on_file, problems)
        elif stat.S_ISREG(mode):
            on_file(remote)
            try:
                atomic_download(sftp, remote, local, control=control)
            except (PermissionError, FileNotFoundError) as error:
                # Нечитаемый или исчезнувший файл не обрывает сбор остальных.
                problems.append(f"{remote}: {error.strerror or error}")
                continue
            copied += 1
        else:
            problems.append(f"{remote}: пропущено (ссылка или специальный файл)")
    return copied


class SFTPWorker(BaseCommandExecutor):
    """Передача файла или каталога (только скачивание) с общим deadline."""

    def execute(
        self,
        device: DeviceModel,
        command_text: str,
        timeout: int = config.app.ssh.command_timeout,
        *,
        params_replaced: bool = False,
    ) -> tuple[str, bool]:
        """Выполнить команду и всегда вернуть текстовый итог, включая ошибку."""
        client = None
        sftp = None
        output: list[str] = []
        control = OperationControl(lambda: self.aborting, time.monotonic() + timeout)
        try:
            control.check()
            if not params_replaced:
                command_text = command_param_replacer.replace_for_device(
                    command_text, device
                )
            if "%active_user%" in command_text or "%active_home%" in command_text:
                client = self.get_client(device, timeout)
                control.check()
                identity = DesktopContext().active_identity(
                    client, device, max(0.1, (control.deadline or time.monotonic()) - time.monotonic()),
                    lambda: self.aborting,
                )
                command_text = command_param_replacer.replace_custom(
                    command_text,
                    {"active_user": identity["name"], "active_home": identity["home"]},
                )
            creds = get_credentials(device)
            upload, source, destination = parse_sftp_command(
                command_text, creds.username, device.host
            )
            if upload:
                source = os.path.expanduser(source)
            else:
                destination = os.path.expanduser(destination)
            local = source if upload else destination
            if is_local_link(local):
                raise ValueError(f"Локальный путь является ссылкой: {local}")
            if upload and not os.path.isfile(source):
                raise ValueError(f"Исходный путь не является обычным файлом: {source}")
            if client is None:
                client = self.get_client(device, timeout)
            control.check()
            sftp = open_sftp_with_diagnosis(client, host=device.host, control=control)
            last_emit = 0.0

            def progress(current: int, total: int) -> None:
                nonlocal last_emit
                control.check()
                now = time.monotonic()
                if now - last_emit >= 0.2:
                    last_emit = now
                    self.emit_progress(device, f"Передано: {current}/{total} байт")

            problems: list[str] = []
            with control.watch(sftp.get_channel()):
                directory = False
                if upload:
                    attrs = remote_stat(sftp, destination)
                    if attrs is not None and stat.S_ISDIR(attrs.st_mode or 0):
                        destination = remote_child(destination, os.path.basename(source))
                else:
                    attrs = sftp.lstat(source)
                    directory = stat.S_ISDIR(attrs.st_mode or 0)
                    if not directory and attrs.st_mode is not None and not stat.S_ISREG(
                        attrs.st_mode
                    ):
                        raise ValueError(
                            f"Удалённый путь не является обычным файлом или каталогом: {source}"
                        )
                    destination = (
                        local_tree_target(destination, source)
                        if directory
                        else local_download_target(destination, source)
                    )
                description = (
                    f"{'Загрузка' if upload else 'Скачивание'}: {source} → {destination}"
                )
                output.append(description)
                self.emit_progress(device, description)
                if upload:
                    atomic_upload(sftp, source, destination, progress, control=control)
                elif directory:
                    copied = download_tree(
                        sftp,
                        source.rstrip("/") or "/",
                        destination,
                        control,
                        lambda path: self.emit_progress(device, f"Скачивание: {path}"),
                        problems,
                    )
                    output.append(f"Скачано файлов: {copied}")
                else:
                    atomic_download(
                        sftp, source, destination, progress, control=control
                    )
            output.extend(problems)
            if problems:
                message = f"Передача SFTP завершена с пропусками: {len(problems)}"
                output.append(message)
                self.emit_progress(device, "\n".join([*problems, message]))
                return "\n".join(output), False
            message = "Передача SFTP успешно завершена"
            output.append(message)
            self.emit_progress(device, message)
            return "\n".join(output), True
        except Exception as exc:
            message = (
                "Передача отменена"
                if isinstance(exc, TransferCancelled) or self.aborting
                else f"Ошибка SFTP: {exc}"
            )
            logger.info(f"[{device.host}] {message}")
            output.append(message)
            self.emit_progress(device, message)
            return "\n".join(output), False
        finally:
            for resource in (sftp, client):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as exc:
                        logger.debug(f"Ошибка закрытия SFTP/SSH: {exc}")
