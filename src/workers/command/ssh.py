"""
SSH Worker - Исполнитель SSH команд.

Выполняет команды на удалённых хостах через SSH с поддержкой:
- Интерактивного вывода
- Таймаутов
- Прерывания выполнения
- Кодировки UTF-8/CP1251
"""

import codecs
import queue
import socket
import time
from typing import Optional, Tuple

import paramiko
from PySide6.QtCore import QObject

from src.config import (
    COMMAND_OUTPUT_HEAD_CHARS,
    COMMAND_OUTPUT_TAIL_CHARS,
    COMMAND_PROGRESS_INTERVAL_SEC,
    COMMAND_PROGRESS_MAX_CHARS,
    SSH_IDLE_POLL_MAX_SEC,
    SSH_IDLE_POLL_MIN_SEC,
    SSH_READ_SLICE_SEC,
    SSH_RECV_BUFFER_SIZE,
    config,
)
from src.domain.models.device import DeviceModel
from src.domain.utils import command_param_replacer
from src.logger import logger
from src.utils.text.output_stream import BoundedOutput, ProgressCoalescer

from .background import BackgroundLaunch
from .desktop_context import DesktopContext
from .executor_base import BaseCommandExecutor
from .locale_utils import build_locale_prefix
from .prompt_utils import PROMPT_SETTLE_SECONDS, PromptTail, matches_input_prompt
from .sudo_utils import (
    ensure_sudo_prefix,
    has_sudo,
    is_sudo_password_prompt,
    should_use_sudo,
    sudo_password_error,
)


class SSHWorker(BaseCommandExecutor):
    """
    Исполнитель SSH команд.

    Особенности:
    - Поддержка псевдо-терминала (PTY)
    - Обработка таймаутов
    - Автоматическая кодировка вывода
    - Прерывание выполнения
    - Интерактивный ввод через очередь (send_input)
    """

    def __init__(
        self,
        progress_callback: QObject | None = None,
        desktop_context: DesktopContext | None = None,
    ) -> None:
        super().__init__(progress_callback)
        self._input_queue: "queue.Queue[str]" = queue.Queue()
        self._waiting_for_input = False
        self.desktop_context = desktop_context
        self._background_launch: BackgroundLaunch | None = None
        self._coalescer: ProgressCoalescer | None = None
        self._encoding_warned = False
        self.abort_check = lambda: self.aborting
        # Связь оборвалась после отправки команды: результат неизвестен,
        # но команда до хоста дошла (выключение и перезагрузка рвут SSH сами).
        self.connection_lost_after_dispatch = False

    def emit_progress(self, device: DeviceModel, message: str) -> None:
        """Служебное сообщение: сначала отправить накопленный вывод, порядок важен."""
        self._flush_output()
        self._emit_direct(device, message)

    def _flush_output(self) -> None:
        if self._coalescer is not None:
            self._coalescer.flush()

    def _emit_direct(self, device: DeviceModel, message: str) -> None:
        """Отдавать в UI только пользовательский результат запуска."""
        if self._background_launch is not None:
            message = self._background_launch.filter_progress(message)
        if message:
            super().emit_progress(device, message)

    def _set_input_wait(self, device: DeviceModel, waiting: bool) -> None:
        """Сообщить только о переходе между выполнением и ожиданием ввода."""
        if waiting != self._waiting_for_input:
            if waiting:
                # Приглашение должно появиться раньше статуса ожидания ввода.
                self._flush_output()
            self._waiting_for_input = waiting
            self.emit_input_wait_changed(device, waiting)

    def send_input(self, text: str) -> None:
        """Поставить текст ввода в очередь (потокобезопасно, вызов из UI-потока).

        Текст будет отправлен в PTY активной команды в следующей итерации
        poll-цикла ``execute``. Если команда не выполняется, ввод останется
        в очереди и будет сброшен при следующем запуске ``execute``.
        """
        self._input_queue.put(text)

    def _drain_input(self, session: paramiko.Channel, device: DeviceModel) -> bool:
        """Отправить весь накопленный ввод из очереди в PTY-сессию.

        Вызывается в poll-цикле ``execute`` (в потоке worker'а) — единственный
        поток, владеющий ``session``, что делает ``session.sendall`` безопасным.
        """
        sent = False
        while not self._input_queue.empty():
            try:
                user_input = self._input_queue.get_nowait()
            except queue.Empty:
                break
            try:
                session.sendall(user_input + "\n")
                sent = True
                logger.info(f"[{device.host}] Отправлен пользовательский ввод")
            except Exception as e:
                logger.warning(f"[{device.host}] Ошибка отправки ввода: {e}")
        return sent

    def _decode_stream(
        self,
        decoder: codecs.IncrementalDecoder,
        raw_data: bytes,
        device: DeviceModel,
        final: bool = False,
    ) -> str:
        """Декодировать чанк вывода потоково (UTF-8, fallback CP1251).

        Инкрементальный декодер переживает разрыв многобайтового символа
        на границе чанков: незавершённый хвост буквы буферизуется внутри
        и склеивается со следующим чанком. CP1251 — только для
        по-настоящему не-UTF-8 байтов.
        """
        try:
            return decoder.decode(raw_data, final)
        except UnicodeDecodeError:
            if not self._encoding_warned:
                self._encoding_warned = True
                logger.warning(f"[{device.host}] Не UTF-8 вывод, используется CP1251")
            decoder.reset()
            return raw_data.decode("cp1251", errors="replace")

    def _drain_remaining(
        self,
        session: paramiko.Channel,
        device: DeviceModel,
        output: BoundedOutput,
        decoder: codecs.IncrementalDecoder,
    ) -> None:
        """Дочитать остаток вывода из канала после завершения команды.

        Гонка poll-цикла: быстрая команда с выводом больше SSH_RECV_BUFFER_SIZE
        завершается раньше, чем цикл успевает вычитать весь канал (по одному
        чанку за итерацию), — без дренирования хвост вывода терялся.
        """
        try:
            while session.recv_ready():
                raw_data = session.recv(SSH_RECV_BUFFER_SIZE)
                if not raw_data:
                    break
                data = self._decode_stream(decoder, raw_data, device)
                output.append(data)
                if self._coalescer is not None:
                    self._coalescer.push(data)
        except Exception as e:
            logger.debug(f"[{device.host}] Ошибка дочитывания остатка вывода: {e}")

    def execute(
        self,
        device: DeviceModel,
        command_text: str,
        timeout: int = config.app.ssh.command_timeout,
        background: bool = False,
    ) -> Tuple[str, bool]:
        """Выполнить шаг либо подтвердить передачу фонового процесса."""
        if type(background) is not bool:
            raise ValueError("background должен быть boolean")
        launch = BackgroundLaunch(command_text) if background else None
        self._background_launch = launch
        try:
            output, success = self._execute(
                device, launch.command if launch else command_text, timeout, launch
            )
        finally:
            # Накопленное должно пройти через фильтр подтверждения запуска.
            self._flush_output()
            self._coalescer = None
            self._background_launch = None
            if launch is not None:
                tail = launch.filter_progress("", final=True)
                if tail:
                    super().emit_progress(device, tail)
        if launch is not None:
            result = launch.finish(output, success)
            if "Статус запуска неизвестен" in result[0]:
                self.emit_progress(
                    device,
                    "\nСтатус запуска неизвестен; автоматический повтор не выполняется\n",
                )
            return result
        return output, success

    def _execute(
        self,
        device: DeviceModel,
        command_text: str,
        timeout: int = config.app.ssh.command_timeout,
        launch: BackgroundLaunch | None = None,
    ) -> Tuple[str, bool]:
        """
        Выполнение SSH команды на удалённом хосте.

        Args:
            device: Устройство для подключения
            command_text: Текст команды
            timeout: Таймаут выполнения в секундах

        Returns:
            (вывод, успех)
        """
        if self.abort_check():
            logger.info(f"[{device.host}] SSH выполнение прервано до подключения")
            return "Прервано пользователем", False

        output = BoundedOutput(COMMAND_OUTPUT_HEAD_CHARS, COMMAND_OUTPUT_TAIL_CHARS)
        result = None
        self.connection_lost_after_dispatch = False
        logger.info(f"[{device.host}] Начало выполнения SSH команды")

        # Создание подключения
        client = None
        session = None
        try:
            client = self.get_client(device, timeout)
            transport = client.get_transport()
            transport.set_keepalive(15)
            if self.desktop_context is not None:
                self.desktop_context.ensure(client, device, timeout, self.abort_check)
            session = self.get_session(client)
        except socket.timeout as e:
            error_msg = f"Таймаут подключения: не удалось подключиться к {device.host} за {config.app.ssh.connect_timeout} с"
            logger.info(f"[{device.host}] {error_msg}")
            self.emit_progress(device, error_msg)
            return error_msg, False
        except ConnectionRefusedError as e:
            error_msg = f"Отказ в подключении: хост {device.host} отклонил подключение"
            logger.info(f"[{device.host}] {error_msg}")
            self.emit_progress(device, error_msg)
            return error_msg, False
        except ConnectionError as e:
            error_msg = f"Ошибка подключения: {str(e)}"
            logger.info(f"[{device.host}] {error_msg}")
            self.emit_progress(device, error_msg)
            return error_msg, False
        except paramiko.AuthenticationException as e:
            error_msg = f"Ошибка аутентификации: {str(e)}"
            logger.error(f"[{device.host}] {error_msg}")
            self.emit_progress(device, error_msg)
            return error_msg, False
        except Exception as e:
            error_msg = str(e)
            err_lower = error_msg.lower()
            if any(
                kw in err_lower for kw in ("timed out", "timeout", "время ожидания")
            ):
                error_msg = f"Таймаут подключения: не удалось подключиться к {device.host} за {config.app.ssh.connect_timeout} с"
            elif any(kw in err_lower for kw in ("refused", "отклонено")):
                error_msg = (
                    f"Отказ в подключении: хост {device.host} отклонил подключение"
                )
            elif any(
                kw in err_lower for kw in ("unreachable", "no route", "недоступен")
            ):
                error_msg = f"Хост недоступен: {device.host}"
            logger.info(f"[{device.host}] {error_msg}")
            self.emit_progress(device, error_msg)
            return error_msg, False

        finally:
            if session is None and client is not None:
                client.close()

        start_time = time.time()
        timed_out = False
        overall_timeout = timeout or config.app.ssh.command_timeout

        logger.info(f"[{device.host}] Выполнение SSH-команды")

        # Весь скрипт получает одинаковые права, включая пайпы и перенаправления.
        from .executor_base import get_credentials as _get_creds

        _creds = _get_creds(device)
        _original_text = command_text
        if self.desktop_context is None:
            command_text = ensure_sudo_prefix(command_text, device, _creds.username)
        if command_text != _original_text:
            logger.info(f"[{device.host}] Авто-добавлен sudo")

        # Принудительная установка локали для кириллицы.
        # Локаль определяется через build_locale_prefix(device): глобальный дефолт
        # SSHConfig.locale перекрывается per-device через DeviceModel.locale.
        # Пустая локаль (None/"" на любом уровне) → префикс пуст → переменные не
        # экспортируются (устраняет предупреждение setlocale на системах без ru_RU.UTF-8).
        locale_prefix = build_locale_prefix(device)
        if self.abort_check():
            session.close()
            client.close()
            return "Прервано пользователем", False
        if self.desktop_context is None:
            command_text = f"{locale_prefix}{command_text}"
            try:
                if launch is not None:
                    launch.dispatched = True
                session.exec_command(command_text)
            except Exception as error:
                session.close()
                client.close()
                return f"Ошибка отправки SSH-команды: {error}", False
        else:
            try:
                if launch is not None:
                    launch.dispatched = True
                tail = self.desktop_context.launch(
                    session, device, timeout, self.abort_check, text=command_text
                )
                if tail:
                    output.append(tail)
                    self.emit_progress(device, tail)
            except (
                Exception
            ) as error:  # noqa: BLE001 - ошибка подготовки возвращается в результат задания
                if launch is not None and session.exit_status_ready():
                    # Явный отказ подготовки (setpriv/UID/cwd) предшествует
                    # запуску пользовательского shell; обрыв без rc остаётся неизвестным.
                    launch.completed = session.recv_exit_status() > 0
                session.close()
                client.close()
                return str(error), False

        # Сброс stale-ввода от предыдущей команды (worker переиспользуется)
        while not self._input_queue.empty():
            try:
                self._input_queue.get_nowait()
            except queue.Empty:
                break
        self._waiting_for_input = False
        prompt_tail = PromptTail()
        last_display_time: float | None = None

        # PTY-expect для sudo: если should_use_sudo(device) и команда содержит `sudo`,
        # перехватываем prompt sudo в выводе и автоматически отправляем пароль через
        # channel.send(). sudo читает пароль из /dev/tty (сам PTY), поэтому пайпы,
        # heredoc и sudo -u не конфликтуют с паролем. При NOPASSWD prompt не появляется
        # и пароль не отправляется (нет утечки в stdin команды).
        sudo_password = None
        sudo_password_sent = False
        automatic_sudo = (
            self.desktop_context is None
            and _creds.username != "root"
            and should_use_sudo(device)
            and has_sudo(command_text)
        )
        if automatic_sudo:
            sudo_password = _creds.password
            if sudo_password:
                logger.debug(f"[{device.host}] sudo-expect: ожидание prompt sudo")

        success = True

        # Потоковый UTF-8 декодер на время команды: переживает разрыв
        # многобайтового символа на границе чанков recv().
        decoder: codecs.IncrementalDecoder = codecs.getincrementaldecoder("utf-8")(
            errors="strict"
        )
        self._encoding_warned = False
        self._coalescer = ProgressCoalescer(
            lambda text: self._emit_direct(device, text),
            COMMAND_PROGRESS_INTERVAL_SEC,
            COMMAND_PROGRESS_MAX_CHARS,
        )
        idle_delay = SSH_IDLE_POLL_MIN_SEC

        try:
            while not self.abort_check():
                # Проверка живости соединения
                if not transport.is_active():
                    error_msg = "Потеря связи с хостом во время выполнения"
                    logger.error(f"[{device.host}] {error_msg}")
                    success = False
                    break

                # Проверка таймаута
                if time.time() - start_time > overall_timeout:
                    timed_out = True
                    error_msg = (
                        f"Команда не завершилась за {overall_timeout} с (таймаут)"
                    )
                    logger.error(f"[{device.host}] {error_msg}")
                    break

                # Чтение вывода: всё доступное, но не дольше среза. Удалённая
                # команда блокируется на записи, пока окно канала заполнено.
                got_data = False
                slice_end = time.monotonic() + SSH_READ_SLICE_SEC
                while session.recv_ready() and time.monotonic() < slice_end:
                    raw_data = session.recv(SSH_RECV_BUFFER_SIZE)
                    if not raw_data:
                        break
                    got_data = True
                    data = self._decode_stream(decoder, raw_data, device)
                    output.append(data)
                    self._coalescer.push(data)
                    if prompt_tail.feed(data):
                        last_display_time = time.monotonic()
                self._coalescer.flush_if_due()

                # Проверка завершения команды
                if session.exit_status_ready():
                    exit_status = session.recv_exit_status()
                    if launch is not None:
                        launch.completed = exit_status >= 0
                    success = exit_status == 0
                    # -1: канал закрыт без кода возврата, сессию убили на хосте.
                    self.connection_lost_after_dispatch = exit_status < 0
                    logger.info(
                        f"[{device.host}] Команда завершилась с кодом: {exit_status}"
                    )
                    # Добираем непрочитанный остаток вывода из канала, иначе
                    # хвост быстрой команды (>SSH_RECV_BUFFER_SIZE) теряется.
                    self._drain_remaining(session, device, output, decoder)
                    tail = self._decode_stream(decoder, b"", device, final=True)
                    if tail:
                        output.append(tail)
                        self.emit_progress(device, tail)
                    break

                # Завершение, отмена и обрыв имеют приоритет перед ожиданием ввода.
                if self.aborting or not transport.is_active():
                    break
                if session.closed:
                    raise ConnectionError("SSH-канал закрыт без результата команды")

                # Успешная отправка исключает старый запрос даже без нового вывода.
                if self._drain_input(session, device):
                    prompt_tail.clear()
                    last_display_time = None
                    self._set_input_wait(device, False)
                    idle_delay = SSH_IDLE_POLL_MIN_SEC

                prompt = matches_input_prompt(prompt_tail.line)
                if (
                    prompt
                    and automatic_sudo
                    and is_sudo_password_prompt(prompt_tail.line)
                ):
                    error = sudo_password_error(sudo_password, sudo_password_sent)
                    if error:
                        self.emit_progress(device, "\n" + error + "\n")
                        output.append("\n" + error + "\n")
                        success = False
                        break
                    assert sudo_password is not None
                    logger.info(f"[{device.host}] sudo-expect: отправка пароля")
                    session.sendall(sudo_password + "\n")
                    sudo_password_sent = True
                    prompt_tail.clear()
                    last_display_time = None
                    prompt = False

                if not prompt:
                    self._set_input_wait(device, False)
                elif (
                    last_display_time is not None
                    and time.monotonic() >= last_display_time + PROMPT_SETTLE_SECONDS
                    and not session.recv_ready()
                    and not session.exit_status_ready()
                    and not self.aborting
                    and transport.is_active()
                ):
                    self._set_input_wait(device, True)

                # Пока данные идут, паузы нет; при молчании опрос реже.
                if got_data:
                    idle_delay = SSH_IDLE_POLL_MIN_SEC
                else:
                    time.sleep(idle_delay)
                    idle_delay = min(idle_delay * 2, SSH_IDLE_POLL_MAX_SEC)

            # Обработка результатов
            if timed_out:
                session.close()
                error_msg = (
                    f"\nТаймаут выполнения команды\n"
                    f"Команда не завершилась за {overall_timeout} с (таймаут).\n"
                )
                logger.error(f"[{device.host}] {error_msg}")
                output.append(error_msg)
                self.emit_progress(device, error_msg)
                success = False
                result = (output.text(), False)

            elif self.abort_check():
                error_msg = "\nПрервано пользователем\n"
                logger.info(f"[{device.host}] {error_msg}")
                output.append(error_msg)
                self.emit_progress(device, error_msg)
                success = False
                result = (output.text(), False)

            elif not transport.is_active():
                self.connection_lost_after_dispatch = True
                error_msg = "Потеря связи с хостом во время выполнения"
                logger.error(f"[{device.host}] {error_msg}")
                output.append(error_msg)
                self.emit_progress(device, error_msg)
                success = False
                result = (output.text(), False)

        except socket.timeout as e:
            error_msg = f"Потеря связи с хостом {device.host} (таймаут)"
            logger.error(f"[{device.host}] {error_msg}")
            output.append(error_msg)
            self.emit_progress(device, error_msg)
            success = False
            result = (output.text(), False)

        except paramiko.AuthenticationException as e:
            error_msg = f"Ошибка аутентификации: {str(e)}"
            logger.error(f"[{device.host}] {error_msg}")
            output.append(error_msg)
            self.emit_progress(device, error_msg)
            success = False
            result = (output.text(), False)

        except paramiko.SSHException as e:
            err_str = str(e).lower()
            if any(
                kw in err_str
                for kw in ("session", "channel", "transport", "connection")
            ):
                self.connection_lost_after_dispatch = True
                error_msg = f"Потеря связи с хостом {device.host} ({str(e)})"
            else:
                error_msg = f"Ошибка SSH: {str(e)}"
            logger.error(f"[{device.host}] {error_msg}")
            output.append(error_msg)
            self.emit_progress(device, error_msg)
            success = False
            result = (output.text(), False)

        except (ConnectionError, OSError) as e:
            self.connection_lost_after_dispatch = True
            error_msg = f"Потеря связи с хостом {device.host} ({str(e)})"
            logger.error(f"[{device.host}] {error_msg}")
            output.append(error_msg)
            self.emit_progress(device, error_msg)
            success = False
            result = (output.text(), False)

        except Exception as e:
            error_msg = f"Ошибка выполнения команды: {str(e)}"
            logger.error(f"[{device.host}] {error_msg}")
            output.append(error_msg)
            self.emit_progress(device, error_msg)
            success = False
            result = (output.text(), False)

        finally:
            self._flush_output()
            self._set_input_wait(device, False)
            try:
                session.close()
                client.close()
                logger.info(f"[{device.host}] Соединение закрыто")
            except Exception as e:
                logger.warning(f"[{device.host}] Ошибка при закрытии соединения: {e}")

            if result is None:
                result = (output.text(), success)

        return result
