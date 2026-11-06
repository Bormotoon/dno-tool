"""Persistent shell только для задач с ``session: true``.

Один прогон на устройстве использует один канал, закрываемый владельцем в
``finally``. Команда и sentinel разбираются как brace group в том же shell:
переменные, функции и cwd сохраняются, sentinel не остается в stdin команды.
Интерактивный ввод не реализован; команда, читающая stdin/tty, может зависнуть
до таймаута. Незавершенный shell-синтаксис тоже не гарантирует sentinel.

При sudo для non-root старт заменяет login shell на persistent root Bash.
Готовность подтверждается маркером только после проверки uid и настройки
окружения. Пароль отправляется только по уникальному prompt текущего старта,
никогда вместе с командами инициализации. Парольный sudo требует приемочного
теста на реальном хосте; локальные тесты проверяют только механику протокола.
"""

from __future__ import annotations

import codecs
import logging
import re
import shlex
import time
import uuid
from collections.abc import Callable

import paramiko

from src.workers.command.background import BackgroundLaunch
from src.workers.command.desktop_context import DesktopContext
from src.workers.command.executor_base import get_credentials
from src.workers.command.locale_utils import get_locale
from src.workers.command.sudo_utils import should_use_sudo, sudo_password_error

logger = logging.getLogger(__name__)

_RECV_SIZE = 65536
_POLL_INTERVAL = 0.05
_ABORT_GRACE = 2.0


class ShellSession:
    """Один persistent shell на прогон задачи (per-device)."""

    def __init__(
        self,
        device,
        progress_cb: Callable[[object, str], None] | None = None,
        abort_check: Callable[[], bool] | None = None,
        desktop_context: DesktopContext | None = None,
    ) -> None:
        self._device = device
        self._progress_cb = progress_cb
        self._abort_check = abort_check or (lambda: False)
        self._client: paramiko.SSHClient | None = None
        self._chan: paramiko.Channel | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._pending = ""
        self._alive = False
        self._desktop_context = desktop_context

    def _emit(self, message: str) -> None:
        if self._progress_cb:
            try:
                self._progress_cb(self._device, message)
            except Exception:  # noqa: BLE001 - прогресс не должен ронять прогон
                logger.debug("Session progress callback failed")

    def start(self, timeout: float = 30) -> tuple[bool, str]:
        """Открыть shell. Возврат (ok, ошибка)."""
        from src.config import config

        self.close()
        device = self._device
        creds = None
        try:
            client = paramiko.SSHClient()
            self._client = client
            if config.app.ssh.strict_host_checking:
                client.load_system_host_keys()
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            creds = get_credentials(device)
            connect_timeout = config.app.ssh.connect_timeout
            client.connect(
                hostname=device.host,
                port=device.port or config.app.ssh.port or 22,
                username=creds.username,
                password=creds.password,
                pkey=creds.private_key,
                timeout=connect_timeout,
                banner_timeout=connect_timeout,
                auth_timeout=connect_timeout,
            )
            transport = client.get_transport()
            if transport is None:
                raise RuntimeError("SSH transport недоступен")
            transport.set_keepalive(15)
            if self._desktop_context is not None:
                self._desktop_context.ensure(client, device, timeout, self._abort_check)
            chan = transport.open_session()
            self._chan = chan
            chan.get_pty(term="dumb")
            if self._desktop_context is not None:
                self._pending = self._desktop_context.launch(
                    chan, device, timeout, self._abort_check
                )
                chan.settimeout(0.5)
                self._alive = True
                return True, ""
            chan.invoke_shell()
            chan.settimeout(0.5)

            marker = f"__DNO_READY_{uuid.uuid4().hex}__"
            prompt = ""
            init = (
                "export PS1='' PS2=''; unset PROMPT_COMMAND; export TERM=dumb; "
                "set +H; set +o history; export HISTFILE=/dev/null; "
            )
            loc = get_locale(device)
            if loc:
                loc = shlex.quote(loc)
                init += (
                    f"export LANG={loc}; export LC_ALL={loc}; export LC_CTYPE={loc}; "
                )
            ready = f"printf '%s:%s\\n' '{marker}' 0; "
            if should_use_sudo(device) and creds.username != "root":
                prompt = f"__DNO_SUDO_{uuid.uuid4().hex}__:"
                # Adjacent quoted fragments produce the prompt only at runtime.
                # Even a fragmented PTY echo can never contain its full token.
                prompt_arg = "'__DNO_SUDO_'" + shlex.quote(
                    prompt.removeprefix("__DNO_SUDO_")
                )
                script = (
                    f'if [ "$(id -u)" != 0 ]; then '
                    f"printf '%s:%s\\n' '{marker}' 1; exit 1; fi; "
                    + init
                    + ready
                    + "exec /bin/bash --noprofile --norc +H +o history -i"
                )
                # One parsed input only: no queued init/sentinel for sudo to read.
                command = (
                    "stty -echo; set +H; set +o history; export HISTFILE=/dev/null; "
                    "export PS1='' PS2=''; unset PROMPT_COMMAND; "
                    f"exec sudo -p {prompt_arg} -- "
                    f"/bin/bash --noprofile --norc -c {shlex.quote(script)}\n"
                )
            else:
                command = "stty -echo; " + init + ready + "\n"
            self._send(command)
            _, rc, error = self._read_until_marker(
                marker, timeout, sudo_prompt=prompt, sudo_password=creds.password
            )
            if error or rc != 0:
                self.close()
                return False, error or "sudo: root shell не подтвержден"
            if chan.closed or chan.exit_status_ready():
                self.close()
                return False, "shell завершен удаленно при старте"
        except Exception as exc:  # noqa: BLE001 - любая ошибка старта в текст
            self.close()
            error = str(exc)
            if creds and creds.password:
                error = error.replace(creds.password, "[скрыто]")
            return False, error
        self._alive = True
        return True, ""

    def run(
        self, text: str, timeout: float = 30, tag: str = "", background: bool = False
    ) -> tuple[str, bool]:
        """Фоновый шаг наследует cwd и экспортированные переменные shell."""
        if type(background) is not bool:
            raise ValueError("background должен быть boolean")
        launch = BackgroundLaunch(text) if background else None
        result = self._run(launch.command if launch else text, timeout, tag, launch)
        if launch is not None:
            finished = launch.finish(*result)
            if "Статус запуска неизвестен" in finished[0]:
                self._emit(
                    "Статус запуска неизвестен; автоматический повтор не выполняется"
                )
            return finished
        return result

    def _run(
        self, text: str, timeout: float, tag: str, launch: BackgroundLaunch | None
    ) -> tuple[str, bool]:
        """Выполнить текст в общем shell. Возврат (вывод, успех по rc)."""
        if not self._alive or self._chan is None:
            return "session не запущена", False
        marker = f"__DNO_END_{uuid.uuid4().hex}__"
        if tag:
            self._emit(f"# === {tag} ===")
        # Shell parses the whole group before execution, with no subshell.
        command = "{\n" + text.rstrip("\n") + "\n"
        # Separate arguments keep even expanded `set -x` source from matching.
        command += f"printf '%s:%s\\n' '{marker}' \"$?\"\n}}\n"
        try:
            if self._desktop_context is not None:
                self._desktop_context.ensure(
                    self._client, self._device, timeout, self._abort_check
                )
            if launch is not None:
                launch.dispatched = True
            self._send(command)
            out, rc, err = self._read_until_marker(marker, timeout)
            if launch is not None:
                launch.completed = not err
        except Exception as exc:  # noqa: BLE001 - broken send/read closes the session
            self.close()
            return f"Ошибка session: {exc}", False
        if err:
            return out + err, False
        self._emit(launch.finish(out, rc == 0)[0] if launch is not None else out)
        return out, rc == 0

    def close(self) -> None:
        """Закрыть канал и клиент (идемпотентно)."""
        self._alive = False
        try:
            if self._chan is not None:
                self._chan.close()
        except Exception:  # noqa: BLE001
            logger.debug("Could not close session channel")
        try:
            if self._client is not None:
                self._client.close()
        except Exception:  # noqa: BLE001
            logger.debug("Could not close session client")
        self._chan = None
        self._client = None
        self._pending = ""
        self._decoder.reset()

    def abort(self) -> None:
        """Аварийное прерывание: \\x03, дождаться sentinel до 2 с, закрыть."""
        try:
            if self._chan is not None:
                self._send("\x03")
                self._drain(_ABORT_GRACE)
        except Exception:  # noqa: BLE001
            logger.debug("Could not interrupt session channel")
        self.close()

    # --- внутреннее ---

    def _send(self, data: str) -> None:
        assert self._chan is not None
        self._chan.sendall(data.encode("utf-8"))

    def _recv_chunk(self) -> str:
        assert self._chan is not None
        if not self._chan.recv_ready():
            return ""
        try:
            raw = self._chan.recv(_RECV_SIZE)
        except TimeoutError:
            return ""
        if not raw:
            return ""
        try:
            return self._decoder.decode(raw)
        except UnicodeDecodeError:
            self._decoder.reset()
            return raw.decode("cp1251", errors="replace")

    def _drain(self, seconds: float) -> str:
        """Вычитать все доступное за время (баннер/эхо/хвосты)."""
        collected: list[str] = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            chunk = self._recv_chunk()
            if chunk:
                collected.append(chunk)
            else:
                if self._chan is None or self._chan.exit_status_ready():
                    break
                time.sleep(_POLL_INTERVAL)
        return "".join(collected)

    def _read_until_marker(
        self,
        marker: str,
        timeout: float,
        *,
        sudo_prompt: str = "",
        sudo_password: str | None = None,
    ) -> tuple[str, int, str]:
        """Читать полный marker:rc + LF/CRLF, оставляя хвост следующему чтению.

        Пароль разрешен только по уникальному ``sudo_prompt`` старта. При
        выполнении шагов shell уже elevated, вывод шага не запрашивает связку.
        """
        pattern = re.compile(re.escape(marker) + r":(0|[1-9][0-9]{0,2})\r?\n")
        buf = ""
        pending, self._pending = self._pending, ""
        deadline = time.monotonic() + timeout
        sent_password = False
        password_output_start = 0
        password = sudo_password
        while True:
            if self._abort_check():
                self.abort()
                return buf, -1, "Прервано пользователем"
            if time.monotonic() >= deadline:
                self.abort()
                return buf, -1, f"Таймаут session-команды ({timeout} с)"
            try:
                chunk = pending or self._recv_chunk()
                pending = ""
            except Exception as exc:  # noqa: BLE001 - close a broken channel
                self.close()
                return buf, -1, f"Ошибка чтения session: {exc}"
            if chunk:
                buf += chunk
                if (
                    sent_password
                    and password
                    and password in buf[password_output_start:]
                ):
                    self.close()
                    return (
                        buf.replace(password, "[скрыто]"),
                        -1,
                        "пароль sudo виден в выводе - остановлено",
                    )
                for match in pattern.finditer(buf):
                    rc = int(match.group(1))
                    if rc <= 255:
                        self._pending = buf[match.end() :]
                        return buf[: match.start()], rc, ""

                tail = buf.rsplit("\n", 1)[-1]
                prompted = bool(sudo_prompt) and tail.endswith(sudo_prompt)
                if prompted:
                    error = sudo_password_error(password, sent_password)
                    if error:
                        self.close()
                        return buf, -1, error
                    assert password is not None
                    password_output_start = len(buf)
                    self._send(password + "\n")
                    sent_password = True
            else:
                if (
                    self._chan is None
                    or self._chan.closed
                    or self._chan.exit_status_ready()
                ):
                    self.close()
                    return buf, -1, "shell завершен удаленно"
                time.sleep(_POLL_INTERVAL)
