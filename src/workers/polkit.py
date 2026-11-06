"""Фоновые операции редактора политик: SFTP-чтение и безопасная запись по SSH."""

import json
import stat
import threading
import time
import xml.etree.ElementTree as ET
from copy import deepcopy

import paramiko
from PySide6.QtCore import QObject, QThread, Signal

from src.domain.models.device import DeviceModel
from src.logger import logger
from src.utils.security.polkit_policy import MAX_POLICY_BYTES, POLKIT_ACTIONS_DIR, PolkitPolicy


class PolkitLoadThread(QThread):
    """Читает политики через одну SFTP-сессию без процесса cat на каждый файл."""

    result = Signal(dict, str)
    progress = Signal(int, str)

    def __init__(self, device: DeviceModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.device = device

    def abort(self) -> None:
        self.requestInterruption()

    @staticmethod
    def _open_sftp(client: paramiko.SSHClient) -> paramiko.SFTPClient:
        channel = client.get_transport().open_session(timeout=10)
        channel.settimeout(10)
        # invoke_subsystem ожидает ответ сервера без учёта Channel.settimeout.
        deadline = threading.Timer(10, channel.close)
        deadline.daemon = True
        deadline.start()
        try:
            channel.invoke_subsystem("sftp")
            return paramiko.SFTPClient(channel)
        except Exception:
            channel.close()
            raise
        finally:
            deadline.cancel()

    def run(self) -> None:
        from src.workers.command.ssh import SSHWorker

        client = None
        sftp = None
        try:
            if self.isInterruptionRequested():
                return
            self.progress.emit(5, "Подключение к хосту...")
            client = SSHWorker().get_client(self.device)
            if self.isInterruptionRequested():
                return
            self.progress.emit(15, "Получение списка .policy файлов...")
            sftp = self._open_sftp(client)
            files = sorted(
                (entry for entry in sftp.listdir_attr(POLKIT_ACTIONS_DIR)
                 if entry.filename.endswith(".policy")
                 and "/" not in entry.filename and "\\" not in entry.filename
                 and (stat.S_ISREG(entry.st_mode or 0) or stat.S_ISLNK(entry.st_mode or 0))),
                key=lambda entry: entry.filename,
            )
            policies = {}
            errors = []
            for index, entry in enumerate(files):
                if self.isInterruptionRequested():
                    return
                self.progress.emit(20 + 75 * index // len(files),
                                   f"Чтение {entry.filename} ({index + 1}/{len(files)})...")
                path = f"{POLKIT_ACTIONS_DIR}/{entry.filename}"
                try:
                    if (entry.st_size or 0) > MAX_POLICY_BYTES:
                        raise ValueError("Файл превышает 4 МиБ")
                    with sftp.open(path, "rb") as stream:
                        content = bytearray()
                        while len(content) <= MAX_POLICY_BYTES:
                            if self.isInterruptionRequested():
                                return
                            chunk = stream.read(min(65536, MAX_POLICY_BYTES + 1 - len(content)))
                            if not chunk:
                                break
                            content.extend(chunk)
                    if len(content) > MAX_POLICY_BYTES:
                        raise ValueError("Файл превышает 4 МиБ")
                    parsed = PolkitPolicy.parse(ET.fromstring(content))
                    for action_id, data in parsed.items():
                        if action_id in policies:
                            raise ValueError(f"Повторяющийся action: {action_id}")
                    for data in parsed.values():
                        data["_source_file"] = path
                    policies.update(parsed)
                except (OSError, ValueError, ET.ParseError) as error:
                    errors.append(f"{entry.filename}: {error}")
            if not self.isInterruptionRequested():
                self.progress.emit(100, "Готово")
                warning = ""
                if errors:
                    warning = f"Не загружено файлов: {len(errors)}\n" + "\n".join(errors[:20])
                self.result.emit(policies, warning)
        except Exception as error:
            if not self.isInterruptionRequested():
                logger.error(f"Ошибка загрузки Polkit: {error}")
                self.result.emit({}, str(error))
        finally:
            try:
                if sftp is not None:
                    sftp.close()
            finally:
                if client is not None:
                    client.close()


class PolkitSaveThread(QThread):
    """Сохраняет снимок одного действия; завершение записи нельзя прерывать из UI."""

    result = Signal(bool, str)

    def __init__(self, device: DeviceModel, action_id: str, data: dict,
                 expected: dict, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self.action_id = action_id
        self.data = deepcopy(data)
        self.expected = dict(expected)

    def run(self) -> None:
        from src.workers.command.ssh import SSHWorker

        client = None
        try:
            client = SSHWorker().get_client(self.device)
            success, message = self._save_policy_override(client)
        except Exception as error:
            logger.error(f"Ошибка сохранения Polkit: {error}")
            success, message = False, str(error)
        finally:
            if client is not None:
                client.close()
        self.result.emit(success, message)

    def _save_policy_override(self, client: paramiko.SSHClient) -> tuple[bool, str]:
        from src.workers.command.executor_base import get_credentials
        from src.workers.command.sudo_utils import should_use_sudo, wrap_sudo_cmd

        command = PolkitPolicy.update_command(
            self.data["_source_file"], self.action_id, self.data["defaults"], self.expected,
        )
        password = None
        if client.get_transport().get_username() != "root":
            if should_use_sudo(self.device):
                password = get_credentials(self.device, use_key=False).password
                # Без пароля разрешён только NOPASSWD: интерактивного ожидания нет.
                command = wrap_sudo_cmd(command, True) if password is not None else f"sudo -n -- {command}"
            else:
                command = f"sudo -n -- {command}"
        stdin, stdout, stderr = client.exec_command(command, timeout=30)
        channel = stdout.channel
        try:
            if password is not None:
                stdin.write(password + "\n")
                stdin.flush()
            channel.shutdown_write()
            output, errors = bytearray(), bytearray()
            deadline = time.monotonic() + 30
            # Читаем оба потока ДО recv_exit_status: большой stderr не блокирует SSH.
            while True:
                if time.monotonic() >= deadline:
                    return False, "Таймаут сохранения. Проверьте файл на хосте и обновите список"
                for ready, read, target in (
                    (channel.recv_ready, channel.recv, output),
                    (channel.recv_stderr_ready, channel.recv_stderr, errors),
                ):
                    if ready():
                        target.extend(read(65536))
                if len(output) + len(errors) > 1024 * 1024:
                    return False, "Слишком большой ответ удалённой команды"
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                time.sleep(0.01)
            status = channel.recv_exit_status()
            if status != 0:
                return False, errors.decode("utf-8", errors="replace").strip() or f"Код завершения: {status}"
            response = json.loads(output.decode("utf-8"))
            backup = response.get("backup", "")
            message = f"Политика {self.action_id} сохранена."
            if backup:
                message += f"\nРезервная копия: {backup}"
            return True, message
        finally:
            channel.close()
            stdin.close()
            stdout.close()
            stderr.close()
