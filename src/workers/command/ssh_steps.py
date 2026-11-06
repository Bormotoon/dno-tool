"""Общее выполнение SSH-шагов с выбором прав исполнения."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from src.domain.models.command import Command
from src.domain.models.device import DeviceModel
from src.domain.utils import command_param_replacer

from .desktop_context import DesktopContext
from .executor_base import create_ssh_client
from .ssh import SSHWorker

if TYPE_CHECKING:
    from .shell_session import ShellSession

# Владелец рабочего стола для шагов, выполняемых не от его имени (например, от root).
ACTIVE_PLACEHOLDERS = ("%active_user%", "%active_home%")


def uses_active_identity(text: str) -> bool:
    """Нужен ли шагу владелец активного рабочего сеанса."""
    return any(placeholder in text for placeholder in ACTIVE_PLACEHOLDERS)


class SshStepRunner:
    """SSH-связка постоянна; каждый шаг выбирает только права исполнения."""

    def __init__(
        self,
        device: DeviceModel,
        worker: SSHWorker,
        *,
        session: bool,
        desktop_session: bool,
        start_timeout: int,
        abort_check: Callable[[], bool],
        progress_cb: Callable[[DeviceModel, str], None],
    ) -> None:
        self._device = device
        self._worker = worker
        self._session = session
        self._desktop_default = desktop_session
        self._start_timeout = start_timeout
        self._abort_check = abort_check
        self._progress_cb = progress_cb
        self._desktop_context: DesktopContext | None = None
        self._identity: dict[str, str] | None = None
        self._shell: ShellSession | None = None
        self._shell_desktop: bool | None = None

    def _active_identity(self) -> dict[str, str]:
        """Владелец рабочего сеанса, один на прогон задания.

        Если шаг рабочего стола уже подготовил сеанс, берётся его владелец:
        шаги от root и от пользователя относятся к одному и тому же человеку.
        """
        if self._identity is None:
            context = self._desktop_context
            if context is not None and context.token:
                data = json.loads(base64.b64decode(context.token))
                self._identity = {"name": data["name"], "home": data["home"]}
            else:
                client = create_ssh_client(self._device)
                try:
                    self._identity = DesktopContext().active_identity(
                        client, self._device, self._start_timeout, self._abort_check
                    )
                finally:
                    client.close()
        return self._identity

    def _with_identity(self, command: Command) -> Command:
        """Подставить %active_user% и %active_home% тем же механизмом, что и SFTP."""
        identity = self._active_identity()
        text = command_param_replacer.replace_custom(
            command.text,
            {"active_user": identity["name"], "active_home": identity["home"]},
        )
        return Command({**command.to_dict(), "text": text})

    def run(self, command: Command, timeout: int, *, tag: str = "") -> tuple[str, bool]:
        """Лениво открыть нужное окружение перед первым соответствующим шагом."""
        if self._abort_check():
            return "Прервано пользователем", False
        desktop = command.uses_desktop_session(self._desktop_default)
        if desktop and self._desktop_context is None:
            self._desktop_context = DesktopContext()
        if uses_active_identity(command.text):
            try:
                command = self._with_identity(command)
            except Exception as error:  # noqa: BLE001 - причина отказа нужна в результате шага
                return str(error), False
        context = self._desktop_context if desktop else None
        self._worker.desktop_context = context
        options = {"background": True} if command.background else {}
        if not self._session:
            return self._worker.execute(self._device, command.text, timeout, **options)
        if self._shell is not None and self._shell_desktop != desktop:
            self._shell.close()
            self._shell = None
        if self._shell is None:
            from .shell_session import ShellSession

            self._shell = ShellSession(
                self._device,
                progress_cb=self._progress_cb,
                abort_check=self._abort_check,
                desktop_context=context,
            )
            self._shell_desktop = desktop
            ok, error = self._shell.start(self._start_timeout)
            if not ok:
                return f"общий shell: {error}", False
        return self._shell.run(command.text, timeout, **({"tag": tag} if tag else {}), **options)

    def close(self) -> None:
        """Закрыть текущий shell при любом исходе задания."""
        try:
            if self._shell is not None:
                self._shell.close()
        finally:
            self._shell = None
            self._shell_desktop = None
            self._worker.desktop_context = None
