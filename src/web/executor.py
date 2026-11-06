"""
Веб-исполнитель команд.

Переиспользует существующие SSHWorker/SFTPWorker/LocalWorker, но без
зависимости от Qt-сигналов. Прогресс передаётся через callback-функцию
для стриминга в WebSocket.
"""

import threading
from typing import Callable, Dict, List, Optional, Tuple

from src.config import COMMAND_OUTPUT_HEAD_CHARS, COMMAND_OUTPUT_TAIL_CHARS, config
from src.domain.models.device import DeviceModel
from src.domain.models.command import Command, CommandType
from src.domain.utils import command_param_replacer
from src.workers.command.ssh import SSHWorker
from src.workers.command.ssh_steps import SshStepRunner
from src.workers.command.sftp import SFTPWorker
from src.workers.command.local import LocalWorker
from src.logger import logger
from src.utils.text.output_stream import BoundedOutput


class ProgressRelay:
    """
    Эмуляция Qt-объекта с сигналом progress_update для BaseCommandExecutor.

    BaseCommandExecutor.emit_progress() вызывает
    `self._progress_callback.progress_update.emit(device, message)`.
    """

    def __init__(self, on_progress: Optional[Callable[[str], None]] = None):
        self._on_progress = on_progress
        self.progress_update = self._ProgressSignal(self)

    class _ProgressSignal:
        def __init__(self, relay: "ProgressRelay"):
            self._relay = relay

        def emit(self, device, message: str) -> None:
            if self._relay._on_progress:
                try:
                    self._relay._on_progress(message)
                except Exception as e:
                    logger.debug(f"WebExecutor: ошибка callback прогресса: {e}")


class WebCommandExecutor:
    """Синхронный исполнитель команд для веб-сервера (без QThread/Signal)."""

    def __init__(self) -> None:
        self._abort_event = threading.Event()

    def abort(self) -> None:
        self._abort_event.set()

    @property
    def is_aborting(self) -> bool:
        return self._abort_event.is_set()

    def execute(
        self,
        device: DeviceModel,
        commands: List[Command],
        timeout: int,
        params: Optional[Dict] = None,
        on_progress: Optional[Callable[[str], None]] = None,
        on_command_start: Optional[Callable[[Command, str], None]] = None,
        session: bool = False,
        desktop_session: bool = False,
    ) -> Tuple[str, bool]:
        """
        Выполнить список команд на устройстве.

        Args:
            device: Целевое устройство.
            commands: Список команд (Command).
            timeout: Таймаут выполнения.
            params: Пользовательские параметры для замены плейсхолдеров.
            on_progress: Callback стриминга вывода (часть вывода).
            on_command_start: Callback начала выполнения отдельной команды.

        Returns:
            (итоговый_вывод, успех)
        """
        self._abort_event.clear()

        relay = ProgressRelay(on_progress=on_progress)
        ssh_worker = SSHWorker(progress_callback=relay)
        ssh_worker.abort_check = lambda: self.is_aborting
        if type(desktop_session) is not bool:
            raise ValueError("desktop_session должен быть boolean")
        sftp_worker = SFTPWorker(progress_callback=relay)
        local_worker = LocalWorker(progress_callback=relay)

        # Связываем флаг прерывания
        ssh_worker.aborting = False
        sftp_worker.aborting = False
        local_worker.aborting = False

        ssh_steps = SshStepRunner(
            device, ssh_worker, session=session, desktop_session=desktop_session,
            start_timeout=timeout,
            abort_check=lambda: self.is_aborting,
            progress_cb=lambda d, m: on_progress(m + "\n") if on_progress else None,
        )
        output_all = BoundedOutput(
            COMMAND_OUTPUT_HEAD_CHARS, COMMAND_OUTPUT_TAIL_CHARS
        )
        success = True
        try:
            n_ssh = sum(1 for c in commands if c.commandType == CommandType.SSH)
            per_step = timeout or config.app.ssh.command_timeout
            overall = max(per_step, n_ssh * config.app.ssh.command_timeout)

            for cmd in commands:
                if self.is_aborting:
                    msg = "Прервано пользователем"
                    if output_all:
                        output_all.append("\n")
                    output_all.append(msg)
                    if on_progress:
                        on_progress(msg + "\n")
                    success = False
                    break

                try:
                    command_text = command_param_replacer.replace(
                        cmd.text, device, params or {}
                    )
                    processed_cmd = Command(
                        {**cmd.to_dict(), "text": command_text}
                    )

                    if on_command_start:
                        try:
                            on_command_start(cmd, command_text)
                        except Exception:
                            pass

                    # Распространение флага прерывания на workers
                    ssh_worker.aborting = self.is_aborting
                    sftp_worker.aborting = self.is_aborting
                    local_worker.aborting = self.is_aborting

                    if processed_cmd.commandType == CommandType.SSH:
                        out, ok = ssh_steps.run(
                            processed_cmd, overall if session else timeout
                        )
                    elif processed_cmd.commandType == CommandType.SFTP:
                        out, ok = sftp_worker.execute(
                            device, processed_cmd.text, timeout, params_replaced=True
                        )
                    elif processed_cmd.commandType == CommandType.LOCAL:
                        out, ok = local_worker.execute(
                            device, processed_cmd.text, timeout, params_replaced=True
                        )
                    else:
                        out = f"Неизвестный тип команды: {processed_cmd.commandType}"
                        ok = False
                except Exception as e:
                    out = f"Ошибка выполнения: {e}"
                    ok = False

                if out:
                    if output_all:
                        output_all.append("\n")
                    output_all.append(out)
                success = success and ok

                if not ok:
                    logger.info(
                        f"WebExecutor: остановка цепочки из-за ошибки на {device.host}"
                    )
                    break

            return output_all.text(), success
        finally:
            ssh_steps.close()
