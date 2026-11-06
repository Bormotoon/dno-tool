"""
Command Worker - Оркестратор выполнения команд на устройствах.

Управляет выполнением команд на множестве устройств через пул потоков,
делегируя выполнение специализированным worker'ам:
- SSHWorker - SSH команды
- SFTPWorker - SFTP операции
- LocalWorker - Локальные команды

Архитектура:
- CommandWorker отвечает за оркестрацию и управление потоками
- CommandExecutor базовый класс для исполнителей
- Конкретные исполнители реализуют логику выполнения
"""

import threading
import uuid
from concurrent.futures import CancelledError, as_completed
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Signal

from src.architecture import EventBus, EventType
from src.config import COMMAND_OUTPUT_HEAD_CHARS, COMMAND_OUTPUT_TAIL_CHARS, config
from src.domain.models.command import Command, CommandType
from src.domain.models.device import DeviceModel
from src.domain.utils import command_param_replacer
from src.logger import logger
from src.utils.text.output_stream import BoundedOutput
from src.workers.application_pools import get_application_pool
from src.workers.task_metadata import operation_source

from ..base import BaseWorker
from .executor_base import BaseCommandExecutor
from .local import LocalWorker
from .sftp import SFTPWorker
from .ssh import SSHWorker
from .ssh_steps import SshStepRunner


class CommandWorker(BaseWorker):
    """
    Оркестратор выполнения команд на устройствах.

    Особенности:
    - Параллельное выполнение на множестве устройств
    - Поддержка прерывания выполнения
    - Интеграция с EventBus
    - Детальный прогресс выполнения
    """

    # object: Signal(str) копирует весь вывод дважды, 40 мс GUI на 16 Ми символов.
    result_ready = Signal(DeviceModel, bool, object)
    progress_update = Signal(DeviceModel, str)
    device_started = Signal(DeviceModel)
    input_requested = Signal(DeviceModel)
    input_wait_changed = Signal(DeviceModel, bool)

    started = Signal()
    finished = Signal()
    error = Signal(str)

    def __init__(
        self,
        devices: List[DeviceModel],
        commands: List[Command],
        timeout: int = config.app.ssh.command_timeout,
        params: Optional[Dict[str, Any]] = None,
        event_bus: Optional["EventBus"] = None,
        session: bool = False,
        source: str | None = None,
        desktop_session: bool = False,
    ):
        super().__init__()
        self.devices = devices
        self.commands = commands
        self.timeout = timeout
        self.params = params or {}
        self.source = source or operation_source.get() or "application"
        self._batch_id = uuid.uuid4().hex
        # Флаг задачи session (D2): вся задача в общем shell. Дефолт False -
        # без флага тракт побайтово без изменений (D7).
        self.session = bool(session)
        if type(desktop_session) is not bool:
            raise ValueError("desktop_session должен быть boolean")
        self.desktop_session = desktop_session

        self.event_bus = event_bus

        self._active_executors: set[BaseCommandExecutor] = set()
        self._active_ssh_workers: Dict[str, SSHWorker] = {}
        self._executor_lock = threading.Lock()
        self._futures = set()

    def execute(self):
        logger.info(
            f"CommandWorker: Запуск выполнения для {len(self.devices)} устройств"
        )

        with self._executor_lock:
            self._active_executors.clear()
            self._active_ssh_workers.clear()

        self.started.emit()
        if self.event_bus:
            self.event_bus.publish_typed(
                EventType.COMMAND_STARTED,
                source="CommandWorker",
                data={
                    "device_count": len(self.devices),
                    "device_iids": [d.iid for d in self.devices],
                    "command_count": len(self.commands),
                    "timeout": self.timeout,
                },
            )

        for device in self.devices:
            logger.info(
                f"Информация об устройстве - Хост: {device.host}, "
                f"Порт: {device.port or config.app.ssh.port}"
            )

        success_count = 0
        error_count = 0

        if self.is_aborting:
            logger.info("CommandWorker: Выполнение прервано до запуска")
            for device in self.devices:
                self.result_ready.emit(device, False, "Прервано пользователем")
                error_count += 1
            self.finished.emit()
            return

        with get_application_pool("commands").lease(source=self.source) as executor:
            future_to_device = {}
            for device in self.devices:
                if self.is_aborting:
                    logger.info(
                        "CommandWorker: Прерывание - пропуск оставшихся устройств"
                    )
                    break

                try:
                    future = executor.submit(
                        self._execute_on_device,
                        device,
                        self.commands,
                        self.timeout,
                        metadata={
                            "host": device.host,
                            "type": "command",
                            "device": device.name or device.host,
                            "batch_id": self._batch_id,
                        },
                    )
                except RuntimeError as error:
                    self.result_ready.emit(device, False, str(error))
                    error_count += 1
                    continue
                future_to_device[future] = device
                with self._executor_lock:
                    self._futures.add(future)
                if self.is_aborting:
                    future.cancel()

            for future in as_completed(future_to_device):
                # Future хранит весь вывод хоста: не держать его до конца пакета.
                device = future_to_device.pop(future)
                with self._executor_lock:
                    self._futures.discard(future)
                try:
                    final_output, success = future.result()

                    if success:
                        success_count += 1
                        error_msg = final_output
                    else:
                        error_count += 1
                        error_msg = (
                            final_output if final_output else "Неизвестная ошибка"
                        )

                    logger.info(
                        f"Выполнение команд для {device.host} завершено - Успешно: {success}"
                    )

                    self.result_ready.emit(device, success, error_msg)

                except CancelledError:
                    error_count += 1
                    self.result_ready.emit(device, False, "Прервано пользователем")

                except Exception as e:
                    error_count += 1
                    error_msg = f"Ошибка выполнения команд на {device.host}: {str(e)}"
                    logger.info(error_msg)
                    self.error.emit(error_msg)

                    self.result_ready.emit(device, False, error_msg)

                    if self.event_bus:
                        self.event_bus.publish_typed(
                            EventType.WORKER_ERROR,
                            source="CommandWorker",
                            data={"device_iid": device.iid, "error": str(e)},
                        )

        with self._executor_lock:
            self._active_executors.clear()
            self._futures.clear()

        logger.info("CommandWorker: Выполнение завершено")

        if self.event_bus:
            self.event_bus.publish_typed(
                EventType.COMMAND_FINISHED,
                source="CommandWorker",
                data={
                    "total_devices": len(self.devices),
                    "success_count": success_count,
                    "error_count": error_count,
                },
            )

        self.finished.emit()

    def _execute_on_device(
        self, device: DeviceModel, commands: List[Command], timeout: int
    ) -> tuple[str, bool]:
        if self.is_aborting:
            logger.info(f"[{device.host}] Выполнение прервано")
            return "Прервано пользователем", False
        from src.utils.security.ban import BAN_MESSAGE, is_banned

        if is_banned():
            return BAN_MESSAGE, False

        self.device_started.emit(device)

        ssh_worker = SSHWorker(progress_callback=self)
        ssh_worker.abort_check = lambda: self.is_aborting
        sftp_worker = SFTPWorker(progress_callback=self)
        local_worker = LocalWorker(progress_callback=self)

        with self._executor_lock:
            # abort() ставит флаг до захвата этой блокировки: либо он уже виден
            # здесь, либо abort() дождётся блокировки и увидит новых исполнителей.
            if self.is_aborting:
                for executor in (ssh_worker, sftp_worker, local_worker):
                    executor.abort()
            else:
                self._active_executors.update((ssh_worker, sftp_worker, local_worker))
                self._active_ssh_workers[device.iid] = ssh_worker

        ssh_steps = SshStepRunner(
            device, ssh_worker, session=self.session,
            desktop_session=self.desktop_session,
            start_timeout=timeout,
            abort_check=lambda: self.is_aborting,
            progress_cb=lambda d, m: self.progress_update.emit(d, m),
        )

        output = ""
        try:
            output_all = BoundedOutput(
                COMMAND_OUTPUT_HEAD_CHARS, COMMAND_OUTPUT_TAIL_CHARS
            )
            success = True

            # Таймаут session/fused-задачи (D10): max(task.timeout, N_ssh*per).
            n_ssh = sum(1 for c in commands if c.commandType == CommandType.SSH)
            per_step = timeout or config.app.ssh.command_timeout
            overall = max(per_step, n_ssh * config.app.ssh.command_timeout)

            for cmd in commands:
                output = ""
                cmd_success = True

                command_text = command_param_replacer.replace(
                    cmd.text, device, self.params
                )

                processed_cmd = Command({**cmd.to_dict(), "text": command_text})

                if self.event_bus:
                    self.event_bus.publish_typed(
                        EventType.COMMAND_STARTED,
                        source="CommandWorker",
                        data={
                            "device_iid": device.iid,
                            "command_type": cmd.commandType.value,
                            "command": cmd.text[:100],
                        },
                    )

                if cmd.commandType == CommandType.SSH:
                    output, cmd_success = ssh_steps.run(
                        processed_cmd, overall if self.session else timeout,
                        tag=cmd.text[:60],
                    )
                elif cmd.commandType == CommandType.SFTP:
                    output, cmd_success = sftp_worker.execute(
                        device, processed_cmd.text, timeout, params_replaced=True
                    )
                elif cmd.commandType == CommandType.LOCAL:
                    output, cmd_success = local_worker.execute(
                        device, processed_cmd.text, timeout, params_replaced=True
                    )
                else:
                    cmd_success = False
                    output = f"Неизвестный тип команды: {cmd.commandType}"

                if output:
                    if output_all:
                        output_all.append("\n")
                    output_all.append(output)
                success = success and cmd_success

                if not success:
                    logger.info(
                        f"[{device.host}] Остановка выполнения из-за ошибки в команде"
                    )
                    break

            return output_all.text(), success

        except Exception as e:
            error_msg = f"Ошибка: {str(e)}"
            logger.info(error_msg)
            self.progress_update.emit(device, error_msg)
            return error_msg, False

        finally:
            ssh_steps.close()
            with self._executor_lock:
                self._active_executors.difference_update(
                    (ssh_worker, sftp_worker, local_worker)
                )
                self._active_ssh_workers.pop(device.iid, None)

    def send_input(self, device_iid: str, text: str) -> bool:
        """Отправить ввод в активную SSH-сессию устройства.

        Args:
            device_iid: iid устройства (для поиска активного SSHWorker).
            text: Текст ввода (без перевода строки — добавляется автоматически).

        Returns:
            True если worker найден и ввод поставлен в очередь, иначе False
            (команда не выполняется, не SSH, или уже завершена).
        """
        with self._executor_lock:
            worker = self._active_ssh_workers.get(device_iid)
        if worker is None:
            return False
        worker.send_input(text)
        return True

    def abort(self):
        self._abort_event.set()
        with self._executor_lock:
            futures = tuple(self._futures)
            for executor in self._active_executors:
                try:
                    executor.abort()
                except Exception as e:
                    logger.debug(f"CommandWorker: Error aborting executor: {e}")
            self._active_executors.clear()
            self._active_ssh_workers.clear()
        for future in futures:
            future.cancel()

        if self.event_bus:
            self.event_bus.publish_typed(
                EventType.COMMAND_ABORTED,
                source="CommandWorker",
                data={"aborted": True},
            )

        logger.info(
            "CommandWorker: Флаг прерывания установлен и все executors прерваны"
        )
