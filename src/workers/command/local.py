"""Исполнитель локальных команд.

Выполняет команды на локальном хосте через subprocess с поддержкой
потокового вывода stdout и stderr, таймаутов выполнения
и прерывания процесса пользователем."""

import locale
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Tuple

from src.config import (
    COMMAND_OUTPUT_HEAD_CHARS,
    COMMAND_OUTPUT_TAIL_CHARS,
    COMMAND_PROGRESS_INTERVAL_SEC,
    COMMAND_PROGRESS_MAX_CHARS,
    config,
)
from src.domain.models.device import DeviceModel
from src.domain.utils import command_param_replacer
from src.logger import logger
from src.utils.system.process_tree import terminate_process_tree
from src.utils.system.subprocess_env import build_clean_env
from src.utils.text.output_stream import BoundedOutput, ProgressCoalescer

from .executor_base import BaseCommandExecutor

# Потомок может унаследовать канал вывода и держать его после выхода команды.
LOCAL_PIPE_DRAIN_SEC = 1.0
# Очередь ограничена: быстрый вывод притормаживает процесс, а не растит память.
_OUTPUT_QUEUE_CHUNKS = 1024
_READ_SIZE = 16 * 1024
# Строка без перевода копится не больше этого, затем отдаётся как есть.
_MAX_PENDING_BYTES = 64 * 1024
_STDERR_LOG_CHARS = 2000


def _fallback_encoding() -> str:
    """Кодировка вывода, если он не UTF-8.

    Консольные программы Windows пишут в канал в OEM-кодировке (cp866 для
    русской системы), а не в ANSI (cp1251), которую выбрал бы text=True.
    """
    if sys.platform == "win32":
        return "oem"
    return locale.getpreferredencoding(False) or "utf-8"


def _decode(data: bytes, fallback: str) -> str:
    """UTF-8, если байты корректны, иначе кодировка системы; ошибок не бросает."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(fallback, errors="replace")


class _StreamDecoder:
    """Режет поток на завершённые строки и декодирует каждую порцию отдельно.

    Порции заканчиваются на ``\\n`` или ``\\r``, поэтому многобайтовый символ
    не разрывается, а ``\\r`` прогресс-баров виден сразу. Переводы строк
    приводятся к ``\\n``, как в текстовом режиме subprocess.
    """

    def __init__(self, fallback: str) -> None:
        self._fallback = fallback
        self._pending = b""

    def feed(self, data: bytes, final: bool = False) -> str:
        data = self._pending + data
        if final:
            cut = len(data)
        else:
            cut = max(data.rfind(b"\n"), data.rfind(b"\r")) + 1
            if cut and data[cut - 1 : cut] == b"\r":
                # Пара \r\n может прийти в разных порциях: \r ждёт следующую.
                cut -= 1
            if not cut:
                if len(data) < _MAX_PENDING_BYTES:
                    self._pending = data
                    return ""
                cut = len(data)
                # Не резать UTF-8-символ: отступить на начало последовательности.
                for back in range(1, 4):
                    if cut - back <= 0:
                        break
                    if data[cut - back] & 0xC0 != 0x80:
                        if data[cut - back] >= 0xC0:
                            cut -= back
                        break
        chunk, self._pending = data[:cut], data[cut:]
        if not chunk:
            return ""
        text = _decode(chunk, self._fallback)
        return text.replace("\r\n", "\n").replace("\r", "\n")


def _pump_stream(
    stream, kind: str, sink: queue.Queue[tuple[str, str | None]], stop: threading.Event
) -> None:
    """Передать текст потока в очередь; None означает конец потока.

    Поток закрывается здесь же: закрытие из другого потока на Windows
    блокируется, пока читатель находится в read.
    """

    def deliver(item: tuple[str, str | None]) -> bool:
        while not stop.is_set():
            try:
                sink.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    decoder = _StreamDecoder(_fallback_encoding())
    try:
        while True:
            data = stream.read(_READ_SIZE)
            if not data:
                break
            text = decoder.feed(data)
            if text and not deliver((kind, text)):
                return
        tail = decoder.feed(b"", final=True)
        if tail and not deliver((kind, tail)):
            return
    except (ValueError, OSError):
        pass  # Канал закрыт при завершении процесса.
    finally:
        deliver((kind, None))
        try:
            stream.close()
        except OSError:
            pass


class LocalWorker(BaseCommandExecutor):
    """
    Исполнитель локальных команд.

    Особенности:
    - Потоковый вывод stdout и stderr в порядке поступления, как в терминале
    - UTF-8 или кодировка консоли системы, без падения на чужих байтах
    - Таймаут и отмена действуют, даже если процесс молчит
    - Завершение всего дерева процессов
    """

    def execute(
        self,
        device: DeviceModel,
        command_text: str,
        timeout: int = config.app.ssh.command_timeout,
        *,
        params_replaced: bool = False,
    ) -> Tuple[str, bool]:
        """
        Выполнение локальной команды.

        Args:
            device: Устройство (используется для замены параметров)
            command_text: Текст команды
            timeout: Таймаут выполнения в секундах
            params_replaced: Текст уже подготовлен оркестратором.

        Returns:
            (вывод, успех)
        """
        if self.aborting:
            logger.info(f"[{device.host}] Выполнение локальной команды прервано")
            return "Прервано пользователем", False
        from src.utils.security.ban import BAN_MESSAGE, is_banned

        if is_banned():
            return BAN_MESSAGE, False

        cmd_success = False
        output = BoundedOutput(COMMAND_OUTPUT_HEAD_CHARS, COMMAND_OUTPUT_TAIL_CHARS)
        proc = None
        stop_pumps = threading.Event()

        try:
            # Замена параметров в команде
            if not params_replaced:
                command_text = command_param_replacer.replace_for_device(
                    command_text, device
                )
            # Текст не журналируется: после подстановки в нём могут быть пароли.
            logger.info(f"[{device.host}] Запуск локальной команды")

            # Запуск процесса. Отдельная сессия на POSIX даёт полное дерево;
            # на Windows консоль скрыта, иначе у GUI-приложения мелькает окно.
            proc = subprocess.Popen(
                command_text,
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=build_clean_env(),
                start_new_session=os.name == "posix",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            logger.debug(f"[{device.host}] Локальная команда запущена")

            stderr_tail, aborted = self._read_output(
                proc, device, timeout, output, stop_pumps
            )

            # Проверка кода завершения (returncode может быть None, если процесс не завершился)
            returncode = proc.poll()
            cmd_success = returncode == 0 and not aborted

            if cmd_success:
                logger.info(
                    f"[{device.host}] Локальная команда успешно завершена с кодом {returncode}"
                )
            else:
                logger.error(
                    f"[{device.host}] Локальная команда завершилась с кодом {returncode}"
                )
                if stderr_tail:
                    logger.error(f"[{device.host}] Ошибка вывода: {stderr_tail}")

        except Exception as e:
            error_msg = f"Ошибка выполнения локальной команды: {str(e)}"
            logger.error(f"[{device.host}] {error_msg}")
            cmd_success = False
            output.append(error_msg)
            self.emit_progress(device, error_msg)

        finally:
            stop_pumps.set()
            if proc is not None and proc.poll() is None:
                self._terminate(proc, device)
            logger.info(f"[{device.host}] Локальная команда завершена")

        return output.text(), cmd_success

    def _read_output(
        self,
        proc: subprocess.Popen[bytes],
        device: DeviceModel,
        timeout: int,
        output: BoundedOutput,
        stop_pumps: threading.Event,
    ) -> tuple[str, bool]:
        """Читать оба канала, соблюдая таймаут и отмену. Вернуть хвост stderr для журнала."""
        events: queue.Queue[tuple[str, str | None]] = queue.Queue(_OUTPUT_QUEUE_CHUNKS)
        for stream, kind in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            threading.Thread(
                target=_pump_stream,
                args=(stream, kind, events, stop_pumps),
                name=f"local-cmd-{kind}",
                daemon=True,
            ).start()

        coalescer = ProgressCoalescer(
            lambda text: self.emit_progress(device, text),
            COMMAND_PROGRESS_INTERVAL_SEC,
            COMMAND_PROGRESS_MAX_CHARS,
        )
        try:
            return self._pump_events(
                proc, device, timeout, output, events, coalescer
            )
        finally:
            coalescer.flush()

    def _pump_events(
        self,
        proc: subprocess.Popen[bytes],
        device: DeviceModel,
        timeout: int,
        output: BoundedOutput,
        events: queue.Queue[tuple[str, str | None]],
        coalescer: ProgressCoalescer,
    ) -> tuple[str, bool]:
        started = time.monotonic()
        stderr_tail = ""
        open_streams = 2
        exit_seen_at: float | None = None
        while open_streams or proc.poll() is None:
            coalescer.flush_if_due()
            if self.aborting:
                logger.info(f"[{device.host}] Локальная команда прервана пользователем")
                self._terminate(proc, device)
                output.append("\nПрервано пользователем\n")
                return stderr_tail, True
            if timeout and time.monotonic() - started >= timeout:
                self._terminate(proc, device)
                raise Exception(
                    f"Локальная команда превысила время ожидания {timeout} секунд"
                )
            try:
                kind, text = events.get(timeout=0.05)
            except queue.Empty:
                if proc.poll() is not None:
                    now = time.monotonic()
                    exit_seen_at = exit_seen_at if exit_seen_at is not None else now
                    if now - exit_seen_at >= LOCAL_PIPE_DRAIN_SEC:
                        break
                continue
            exit_seen_at = None
            if text is None:
                open_streams -= 1
                continue
            # stderr показывается вместе с stdout: так же выглядит вывод SSH через PTY.
            output.append(text)
            coalescer.push(text)
            if kind == "stderr":
                stderr_tail = (stderr_tail + text)[-_STDERR_LOG_CHARS:]
        return stderr_tail, False

    @staticmethod
    def _terminate(proc: subprocess.Popen[bytes], device: DeviceModel) -> None:
        try:
            terminate_process_tree(proc, kill_group=True)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            logger.warning(f"[{device.host}] Ошибка при завершении процесса: {error}")
