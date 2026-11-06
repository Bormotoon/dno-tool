"""SSH-сервис редактора KDE с изолированными операциями и проверкой результата."""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import paramiko

from src.config.kde_schema import editable_setting
from src.domain.kde import KdeDraft, KdeProfile, KdeSnapshot, KdeSource, KdeTarget
from src.domain.models.device import DeviceModel
from src.services.base import BaseService
from src.workers.command.executor_base import create_ssh_client, get_credentials
from src.workers.command.sudo_utils import should_use_sudo
from src.workers.kde.remote_script import build_remote_command


class KdeOperationError(Exception):
    """Проверяемая ошибка операции; unknown запрещает слепой повтор записи."""

    def __init__(self, code: str, message: str, details: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class KdeConnection:
    """Снимок адреса и ссылки на связку; секреты разрешаются в рабочем потоке."""

    identity: tuple[str, str]
    host: str
    port: int | None
    credential_bundle_iid: str | None
    use_sudo: bool | None

    @classmethod
    def from_device(cls, device: DeviceModel) -> KdeConnection:
        node = getattr(device, "node_iid", None) or "local"
        if node != "local":
            raise ValueError(
                "Настройки KDE доступны только через прямое SSH-подключение."
            )
        return cls(
            (node, device.iid),
            device.host,
            device.port,
            device.credential_bundle_iid,
            device.use_sudo,
        )

    def device(self) -> DeviceModel:
        return DeviceModel(
            {
                "iid": self.identity[1],
                "host": self.host,
                "port": self.port,
                "credential_bundle_iid": self.credential_bundle_iid,
                "use_sudo": self.use_sudo,
            }
        )


class KdeOperationControl:
    """Отмена закрывает только ресурсы одной операции, не блокируя GUI."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._client: paramiko.SSHClient | None = None
        self._channel: paramiko.Channel | None = None

    def bind(
        self, client: paramiko.SSHClient, channel: paramiko.Channel | None = None
    ) -> None:
        with self._lock:
            self._client, self._channel = client, channel
        if self.cancelled.is_set():
            self.close()

    def cancel(self) -> None:
        self.cancelled.set()
        threading.Thread(
            target=self.close, name="kde-operation-cancel", daemon=True
        ).start()

    def close(self) -> None:
        with self._lock:
            channel, client = self._channel, self._client
            self._channel = self._client = None
        if channel is not None:
            with suppress(OSError, EOFError, paramiko.SSHException):
                channel.close()
        if client is not None:
            with suppress(OSError, EOFError, paramiko.SSHException):
                client.close()


class KdeService(BaseService):
    """Не хранит текущий профиль: каждый запрос несёт собственный target."""

    def request(
        self,
        connection: KdeConnection,
        request: dict[str, Any],
        control: KdeOperationControl,
        progress: Callable[[str], None],
    ) -> dict[str, Any]:
        """Выполнить один JSON-запрос, параллельно дренируя stdout и stderr."""
        device = connection.device()
        client = paramiko.SSHClient()
        control.bind(client)
        mutating = request["operation"] in ("save", "restore")
        sent = False
        deadline = time.monotonic() + 75.0
        try:
            self._check(control, deadline)
            progress("Подключение по SSH…")
            creds = get_credentials(device)
            client = create_ssh_client(device, client=client, connect_timeout=10.0)
            self._check(control, deadline)
            transport = client.get_transport()
            if transport is None:
                raise KdeOperationError("ssh", "SSH-соединение закрыто.")
            channel = transport.open_session(timeout=10.0)
            control.bind(client, channel)
            channel.settimeout(0.2)
            if request["operation"] == "discover":
                progress("Получение списка пользователей…")
                return self._discover_accounts(
                    channel, control, deadline, creds.username
                )
            elevated = (
                creds.username != "root"
                and request.get("username", creds.username) != creds.username
                and should_use_sudo(device)
            )
            prompt = "DNO_KDE_SUDO_PASSWORD:"
            command = build_remote_command(elevated, prompt)
            channel.exec_command(command)
            output = bytearray()
            errors = bytearray()
            password_sent = False
            progress("Проверка доступа…")
            while b"DNO_KDE_READY\n" not in output:
                self._check(control, deadline)
                self._drain(channel, output, errors)
                if prompt.encode() in errors:
                    if password_sent:
                        raise KdeOperationError(
                            "sudo", "Sudo не принял пароль выбранной связки."
                        )
                    if not creds.password:
                        raise KdeOperationError(
                            "sudo",
                            "Sudo требует пароль. Укажите его в связке учётных данных.",
                        )
                    self._send(
                        channel,
                        (creds.password + "\n").encode("utf-8"),
                        control,
                        deadline,
                    )
                    password_sent = True
                    errors.clear()
                if (
                    b"DNO_KDE_READY\n" not in output
                    and channel.exit_status_ready()
                    and not channel.recv_ready()
                ):
                    self._drain(channel, output, errors)
                    code = channel.recv_exit_status()
                    details = self._startup_details(
                        errors, code, creds.password, prompt
                    )
                    if b"DNO_KDE_PYTHON_MISSING" in errors:
                        raise KdeOperationError(
                            "python",
                            "На хосте не найден Python 3.7 или новее. Каталог доступен для просмотра; профиль пока не прочитан.",
                            details,
                        )
                    if elevated and "sudo" in details.casefold():
                        raise KdeOperationError(
                            "sudo",
                            "Sudo не разрешил запуск обработчика. Откройте подробности ошибки.",
                            details,
                        )
                    raise KdeOperationError(
                        "startup",
                        f"Обработчик не запустился (код {code}). Откройте подробности ошибки.",
                        details,
                    )
                control.cancelled.wait(0.03)
            payload = dict(request)
            payload["identity"] = ":".join(connection.identity)
            data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            if len(data) > 4 * 1024 * 1024:
                raise KdeOperationError("size", "Запрос превышает допустимый размер.")
            progress(
                "Сохранение и проверка файла…" if mutating else "Чтение данных хоста…"
            )
            sent = True
            self._send(channel, data, control, deadline)
            channel.shutdown_write()
            while True:
                self._check(control, deadline)
                self._drain(channel, output, errors)
                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    break
                control.cancelled.wait(0.03)
            code = channel.recv_exit_status()
            if code != 0:
                raise KdeOperationError(
                    "unknown" if mutating else "remote",
                    f"Удалённая операция завершилась с кодом {code}.",
                )
            body = bytes(output).split(b"DNO_KDE_READY\n", 1)[1]
            try:
                response = json.loads(body)
            except (ValueError, IndexError) as error:
                raise KdeOperationError(
                    "unknown" if mutating else "protocol",
                    "Хост вернул неполный ответ. Перечитайте профиль.",
                ) from error
            if not isinstance(response, dict) or not isinstance(
                response.get("ok"), bool
            ):
                raise KdeOperationError(
                    "protocol", "Хост вернул неподдерживаемый ответ."
                )
            if not response["ok"]:
                raise KdeOperationError(
                    str(response.get("code", "remote")),
                    str(response.get("message", "Ошибка удалённой операции.")),
                )
            result = response.get("data")
            if not isinstance(result, dict):
                raise KdeOperationError(
                    "protocol", "Хост вернул неподдерживаемые данные."
                )
            if request["operation"] == "discover":
                result["ssh_username"] = creds.username
            return result
        except KdeOperationError as error:
            if (
                sent
                and mutating
                and error.code in ("cancelled", "timeout", "size", "ssh", "protocol")
            ):
                raise KdeOperationError(
                    "unknown",
                    "Связь прервана во время записи. Результат неизвестен; перечитайте файл перед повтором.",
                ) from error
            raise
        except Exception as error:
            if sent and mutating:
                raise KdeOperationError(
                    "unknown",
                    "Подтверждение записи не получено. Результат неизвестен; перечитайте файл перед повтором.",
                ) from error
            if control.cancelled.is_set():
                raise KdeOperationError("cancelled", "Операция отменена.") from error
            raise KdeOperationError(
                "ssh",
                f"Не удалось выполнить SSH-операцию: {type(error).__name__}. Проверьте подключение и учётные данные.",
            ) from error
        finally:
            control.close()

    @staticmethod
    def _discover_accounts(
        channel: paramiko.Channel,
        control: KdeOperationControl,
        deadline: float,
        ssh_username: str,
    ) -> dict[str, Any]:
        """NSS-список не требует Python, sudo и пользовательских конфигов KDE."""
        command = (
            "dno_uid_min=$(awk '$1 == \"UID_MIN\" {print $2; exit}' /etc/login.defs 2>/dev/null); "
            "printf 'DNO_KDE_UID_MIN:%s\\n' \"${dno_uid_min:-1000}\"; "
            "printf 'DNO_KDE_PASSWD_BEGIN\\n'; "
            "if command -v getent >/dev/null 2>&1; then getent passwd; else cat /etc/passwd; fi; "
            "dno_status=$?; printf 'DNO_KDE_PASSWD_END\\n'; exit \"$dno_status\""
        )
        channel.exec_command(command)
        channel.shutdown_write()
        output, errors = bytearray(), bytearray()
        while True:
            KdeService._check(control, deadline)
            KdeService._drain(channel, output, errors)
            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                break
            control.cancelled.wait(0.03)
        code = channel.recv_exit_status()
        text = output.decode("utf-8", errors="replace")
        if (
            code != 0
            or "DNO_KDE_PASSWD_BEGIN\n" not in text
            or "DNO_KDE_PASSWD_END" not in text
        ):
            raise KdeOperationError(
                "accounts",
                "Не удалось прочитать список пользователей через NSS.",
                f"Код: {code}\n" + errors.decode("utf-8", errors="replace")[:4000],
            )
        match = re.search(r"DNO_KDE_UID_MIN:(\d+)", text)
        minimum = int(match.group(1)) if match else 1000
        content = text.split("DNO_KDE_PASSWD_BEGIN\n", 1)[1].split(
            "DNO_KDE_PASSWD_END", 1
        )[0]
        profiles: list[dict[str, Any]] = []
        for line in content.splitlines():
            fields = line.split(":")
            if len(fields) != 7 or not fields[2].isdigit() or not fields[3].isdigit():
                continue
            username, _, uid, gid, _, home, shell = fields
            if shell.endswith(("nologin", "/false")) or not home.startswith("/"):
                continue
            if (
                int(uid) != 0
                and not minimum <= int(uid) < 65534
                and username != ssh_username
            ):
                continue
            profiles.append(
                {
                    "username": username,
                    "uid": int(uid),
                    "gid": int(gid),
                    "home": home,
                    "sessions": [],
                }
            )
        profiles.sort(key=lambda profile: (profile["uid"] == 0, profile["username"]))
        return {"profiles": profiles, "ssh_username": ssh_username, "version": ""}

    @staticmethod
    def _startup_details(
        errors: bytearray, code: int, password: str | None, prompt: str
    ) -> str:
        text = errors.decode("utf-8", errors="replace")
        if password:
            text = text.replace(password, "***")
        text = (
            re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
            .replace(prompt, "")
            .replace("\x00", "")
        )
        return f"Этап: запуск удалённого обработчика\nКод завершения: {code}\n\n" + (
            text.strip()[-6000:] or "Хост не передал текст ошибки."
        )

    @staticmethod
    def _check(control: KdeOperationControl, deadline: float) -> None:
        if control.cancelled.is_set():
            raise KdeOperationError("cancelled", "Операция отменена.")
        if time.monotonic() >= deadline:
            raise KdeOperationError("timeout", "Истекло время ожидания операции.")

    @staticmethod
    def _drain(channel: paramiko.Channel, output: bytearray, errors: bytearray) -> None:
        for _ in range(16):
            if channel.recv_ready():
                output.extend(channel.recv(65536))
            if channel.recv_stderr_ready():
                errors.extend(channel.recv_stderr(65536))
            if len(output) > 24 * 1024 * 1024 or len(errors) > 65536:
                raise KdeOperationError(
                    "size", "Ответ хоста превысил допустимый размер."
                )
            if not channel.recv_ready() and not channel.recv_stderr_ready():
                break

    @classmethod
    def _send(
        cls,
        channel: paramiko.Channel,
        data: bytes,
        control: KdeOperationControl,
        deadline: float,
    ) -> None:
        offset = 0
        while offset < len(data):
            cls._check(control, deadline)
            if channel.send_ready():
                count = channel.send(data[offset : offset + 32768])
                if count <= 0:
                    raise KdeOperationError(
                        "ssh", "SSH-канал закрылся во время отправки."
                    )
                offset += count
            else:
                control.cancelled.wait(0.02)

    @staticmethod
    def target_request(snapshot: KdeSnapshot, operation: str) -> dict[str, Any]:
        return {
            "operation": operation,
            "username": snapshot.target.profile.username,
            "uid": snapshot.target.profile.uid,
            "path": snapshot.target.path,
            "filename": PurePosixPath(snapshot.target.path).name,
        }

    @classmethod
    def save_request(cls, connection: KdeConnection, draft: KdeDraft) -> dict[str, Any]:
        """Проверяет адресата, политики и diff независимо от состояния UI."""
        snapshot = draft.snapshot
        if (
            snapshot.target.identity != connection.identity
            or snapshot.target.host != connection.host
        ):
            raise KdeOperationError("target", "Снимок относится к другому хосту.")
        if not draft.dirty:
            raise KdeOperationError("unchanged", "Нет изменений для сохранения.")
        checked = KdeDraft(snapshot)
        for key, value in draft.changes.items():
            definition = editable_setting(snapshot, key)
            if definition is None:
                raise KdeOperationError(
                    "validation",
                    "Параметр не входит в подтверждённую схему редактирования.",
                )
            definition.validate(value)
            checked.set(key, value)
        result = cls.target_request(snapshot, "save")
        result.update(
            {
                "expected": snapshot.fingerprint,
                "raw": base64.b64encode(checked.render()).decode("ascii"),
                "policies": {
                    source.path: source.fingerprint for source in snapshot.sources
                },
            }
        )
        return result

    @staticmethod
    def decode_snapshot(
        connection: KdeConnection,
        data: dict[str, Any],
        previous: KdeSnapshot | None = None,
    ) -> KdeSnapshot:
        """Преобразует ответ только после проверки принадлежности профилю."""
        profile = KdeProfile.from_dict(data["profile"])
        target = KdeTarget(
            connection.identity, connection.host, profile, str(data["path"])
        )
        if previous and (
            target.identity != previous.target.identity
            or target.profile.uid != previous.target.profile.uid
            or target.path != previous.target.path
            or target.profile.username != previous.target.profile.username
        ):
            raise KdeOperationError(
                "target", "Ответ относится к другому профилю или пути."
            )
        resources = {
            str(key): tuple(map(str, values))
            for key, values in data.get("resources", {}).items()
        }
        if not resources and previous:
            resources = previous.resources
        raw = base64.b64decode(data["raw"], validate=True)
        meta = data["meta"]
        snapshot = KdeSnapshot(
            target,
            raw,
            bool(meta["exists"]),
            str(meta["fingerprint"]),
            tuple(
                KdeSource(
                    str(item["path"]),
                    base64.b64decode(item["raw"], validate=True),
                    str(item["fingerprint"]),
                    str(item.get("error", "")),
                )
                for item in data.get("sources", [])
            ),
            str(data.get("version", "")).strip(),
            str(data.get("environment_note", "")),
            resources,
            str(data.get("loaded_at", "")),
        )
        expected = snapshot.document.fingerprint if snapshot.exists else "missing"
        if snapshot.fingerprint != expected:
            raise KdeOperationError(
                "protocol", "Контрольная сумма ответа хоста не совпадает."
            )
        return snapshot
