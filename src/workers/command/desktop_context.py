"""Контекст рабочего стола на один прогон задания, без нового PAM-входа."""

from __future__ import annotations

import base64
import json
import shlex
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import paramiko

from src.domain.models.device import DeviceModel

from .executor_base import get_credentials
from .locale_utils import get_locale
from .sudo_utils import should_use_sudo, sudo_password_error

# Код передаётся через SSH, поэтому не требует файлов и установки на хосте.
# Только белый список окружения; диагностические сообщения не содержат его значений.
REMOTE_HELPER = r"""
import base64, json, os, pwd, re, shutil, stat, subprocess, sys
from pathlib import Path

PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
KEYS = {"DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS", "XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP",
        "DESKTOP_SESSION", "KDE_SESSION_VERSION"}

def fail(message):
    raise RuntimeError(message)

def run(*args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, timeout=8, env={"PATH": PATH, "LC_ALL": "C"})
    if p.returncode:
        fail("Не удалось получить свойства сеанса через loginctl")
    return p.stdout

def properties(sid):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", sid):
        fail("Некорректный идентификатор сеанса")
    return dict(line.split("=", 1) for line in
                run("loginctl", "show-session", sid, "--no-pager").splitlines()
                if "=" in line)

def eligible(p):
    return (p.get("Active") == "yes" and p.get("Remote") == "no"
            and p.get("Class") in ("user", "user-early")
            and p.get("Type") in ("x11", "wayland")
            and p.get("State") in ("active", "online"))

def identity(p):
    return {k: p.get(k, "") for k in
            ("Id", "User", "Name", "Type", "Leader", "TimestampMonotonic")}

def check(ctx):
    p = properties(ctx["identity"]["Id"])
    if not eligible(p) or identity(p) != ctx["identity"]:
        fail("Выбранный рабочий сеанс завершён или изменился; задание остановлено")
    account = pwd.getpwuid(int(p["User"]))
    if (account.pw_name, account.pw_gid, account.pw_dir) != (
            ctx["name"], ctx["gid"], ctx["home"]):
        fail("Владелец выбранного сеанса изменился")

def prepare(identity_only=False):
    candidates = []
    for line in run("loginctl", "list-sessions", "--no-legend", "--no-pager").splitlines():
        if line.strip():
            p = properties(line.split()[0])
            if eligible(p):
                candidates.append(p)
    if len(candidates) != 1:
        fail("Нет активного локального рабочего сеанса" if not candidates else
             "Обнаружено несколько активных локальных рабочих сеансов")
    p = candidates[0]
    uid = int(p["User"])
    account = pwd.getpwuid(uid)
    if account.pw_name != p["Name"] or not os.path.isabs(account.pw_dir):
        fail("Не удалось подтвердить владельца сеанса")
    if identity_only:
        return dict(name=account.pw_name, home=account.pw_dir)
    scope = "session-" + p["Id"] + ".scope"
    variants = {k: set() for k in KEYS}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != uid:
                continue
            groups = (proc / "cgroup").read_text().splitlines()
            if not any(scope in line.split(":", 2)[-1].split("/") for line in groups):
                continue
            raw = (proc / "environ").read_bytes()
            env = dict(part.split(b"=", 1) for part in raw.split(b"\0") if b"=" in part)
            if env.get(b"XDG_SESSION_ID", p["Id"].encode()) != p["Id"].encode():
                continue
            for key in KEYS:
                value = env.get(key.encode(), b"").decode("utf-8", "strict")
                if value:
                    variants[key].add(value)
        except (OSError, UnicodeError):
            continue
    if any(len(values) > 1 for values in variants.values()):
        fail("Окружение выбранного рабочего сеанса неоднозначно")
    env = {k: next(iter(v)) for k, v in variants.items() if v}
    runtime = env.get("XDG_RUNTIME_DIR", "")
    if not os.path.isabs(runtime):
        fail("Не найден XDG_RUNTIME_DIR выбранного сеанса")
    st = os.stat(runtime)
    if st.st_uid != uid or not stat.S_ISDIR(st.st_mode):
        fail("Не подтверждён владелец каталога рабочего сеанса")
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        bus = Path(runtime) / "bus"
        if not bus.is_socket() or bus.stat().st_uid != uid:
            fail("Не найдена пользовательская шина D-Bus")
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(bus)
    # Plasma запускает приложения через пользовательский systemd вне session scope.
    # Версию KDE берём у менеджера того же подтверждённого владельца сеанса.
    if (not env.get("KDE_SESSION_VERSION")
            and (p.get("Desktop") in ("KDE", "Plasma")
                 or "KDE" in env.get("XDG_CURRENT_DESKTOP", "").split(":"))):
        args = ["/usr/bin/env", "XDG_RUNTIME_DIR=" + runtime,
                "DBUS_SESSION_BUS_ADDRESS=" + env["DBUS_SESSION_BUS_ADDRESS"],
                "systemctl", "--user", "show-environment"]
        if os.geteuid() != uid:
            args = ["runuser", "-u", account.pw_name, "--"] + args
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=8, env={"PATH": PATH, "LC_ALL": "C"})
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line in ("KDE_SESSION_VERSION=4", "KDE_SESSION_VERSION=5",
                            "KDE_SESSION_VERSION=6"):
                    env["KDE_SESSION_VERSION"] = line.split("=", 1)[1]
    if p["Type"] == "x11" and not env.get("DISPLAY"):
        fail("Не найден DISPLAY выбранного X11-сеанса")
    if p["Type"] == "wayland" and not env.get("WAYLAND_DISPLAY"):
        fail("Не найден WAYLAND_DISPLAY выбранного Wayland-сеанса")
    env.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name,
               PATH=PATH, XDG_SESSION_ID=p["Id"], XDG_SESSION_TYPE=p["Type"])
    ctx = dict(identity=identity(p), uid=uid, gid=account.pw_gid,
               name=account.pw_name, home=account.pw_dir, env=env)
    check(ctx)
    return ctx

def main():
    mode = sys.argv[1]
    if mode == "identity":
        print(json.dumps(prepare(identity_only=True), ensure_ascii=True), flush=True)
        return
    if mode == "prepare":
        print(json.dumps(prepare(), ensure_ascii=True), flush=True)
        return
    ctx = json.loads(base64.b64decode(sys.argv[2]))
    if mode == "check":
        check(ctx)
        return
    if mode == "launch":
        check(ctx)
        env = ctx["env"].copy()
        env.update(PS1="", PS2="", HISTFILE="/dev/null", TERM="dumb")
        if sys.argv[4]:
            env.update(LANG=sys.argv[4], LC_ALL=sys.argv[4], LC_CTYPE=sys.argv[4])
        args = [sys.executable, "-I", "-c", sys.argv[5],
                str(ctx["uid"]), ctx["home"], sys.argv[3], sys.argv[6]]
        if os.getuid() != ctx["uid"] or os.geteuid() != ctx["uid"]:
            if os.geteuid() != 0:
                fail("Недостаточно прав для переключения на владельца рабочего стола")
            executable = shutil.which("setpriv", path=PATH)
            if not executable:
                fail("На хосте отсутствует setpriv; установите util-linux")
            args = [executable, "--reuid=" + str(ctx["uid"]),
                    "--regid=" + str(ctx["gid"]), "--init-groups", "--"] + args
        os.execve(args[0], args, env)

try:
    main()
except Exception as error:
    print("Рабочий стол: " + str(error), file=sys.stderr, flush=True)
    sys.exit(1)
"""

_ENTER = r"""
import base64, os, sys
if os.getuid() != int(sys.argv[1]) or os.geteuid() != int(sys.argv[1]):
    sys.exit("Рабочий стол: UID исполнителя не подтверждён")
os.chdir(sys.argv[2])
print(sys.argv[3] + ":0", flush=True)
if sys.argv[4]:
    text = base64.b64decode(sys.argv[4]).decode("utf-8")
    os.execve("/bin/bash", ["bash", "--noprofile", "--norc", "-c", text], os.environ)
os.execve("/bin/bash", ["bash", "--noprofile", "--norc", "+H", "+o", "history", "-i"], os.environ)
"""


@dataclass
class DesktopContext:
    """Выбранный сеанс и окружение; экземпляр принадлежит одному прогону хоста."""

    token: str = field(default="", repr=False)

    def _command(self, mode: str, *args: str) -> str:
        """Собрать изолированный вызов helper без shell-подстановок."""
        return (
            "/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin /usr/bin/python3 -I -c "
            + " ".join(
                shlex.quote(arg) for arg in (REMOTE_HELPER, mode, self.token, *args)
            )
        )

    def _start(
        self, channel: paramiko.Channel, command: str, device: DeviceModel
    ) -> tuple[str, str | None]:
        """Запустить служебный канал, разрешая sudo только по настройкам хоста."""
        creds = get_credentials(device)
        prompt = ""
        if should_use_sudo(device) and creds.username != "root":
            prompt = "__DNO_DESKTOP_SUDO_" + uuid.uuid4().hex + ":"
            command = "sudo -p " + shlex.quote(prompt) + " -- " + command
        channel.exec_command("stty -echo && exec " + command)
        return prompt, creds.password

    @staticmethod
    def _read(
        channel: paramiko.Channel,
        timeout: float,
        abort_check: Callable[[], bool],
        prompt: str,
        password: str | None,
        marker: str = "",
    ) -> str:
        """Прочитать служебный ответ с отменой, deadline и отдельным sudo prompt."""
        deadline = time.monotonic() + timeout
        buf = b""
        sent = False
        try:
            while True:
                if abort_check():
                    raise RuntimeError("Прервано пользователем")
                if time.monotonic() >= deadline:
                    raise RuntimeError("Таймаут подготовки рабочего сеанса")
                if channel.recv_ready():
                    # После маркера вывод читает штатный потоковый декодер SSH.
                    chunk: bytes = channel.recv(1 if marker else 65536)
                    buf += chunk
                    if len(buf) > 262144:
                        raise RuntimeError(
                            "Слишком большой ответ подготовки рабочего сеанса"
                        )
                    if marker:
                        normalized = buf.replace(b"\r\n", b"\n")
                        needle = (marker + ":0\n").encode()
                        if needle in normalized:
                            return normalized.split(needle, 1)[1].decode(
                                "utf-8", "replace"
                            )
                    if prompt and buf.endswith(prompt.encode()):
                        error = sudo_password_error(password, sent)
                        if error:
                            raise RuntimeError(error)
                        assert password is not None
                        channel.sendall((password + "\n").encode())
                        sent = True
                        buf = b""
                elif channel.exit_status_ready():
                    rc = channel.recv_exit_status()
                    if rc or marker:
                        message = buf.decode("utf-8", "replace").strip()
                        if password:
                            message = message.replace(password, "[скрыто]")
                        raise RuntimeError(
                            message or "Не удалось подготовить рабочий сеанс"
                        )
                    return buf.decode("utf-8", "strict")
                elif channel.closed:
                    raise RuntimeError("SSH-канал подготовки рабочего сеанса закрыт")
                else:
                    time.sleep(0.05)
        except BaseException:
            channel.close()
            raise

    def active_identity(
        self,
        client: paramiko.SSHClient,
        device: DeviceModel,
        timeout: float,
        abort_check: Callable[[], bool],
    ) -> dict[str, str]:
        """Получить владельца активного сеанса без подготовки окружения."""
        if abort_check():
            raise RuntimeError("Прервано пользователем")
        transport = client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport недоступен")
        channel = transport.open_session(timeout=timeout)
        try:
            channel.get_pty(term="dumb")
            prompt, password = self._start(channel, self._command("identity"), device)
            return json.loads(
                self._read(channel, timeout, abort_check, prompt, password)
            )
        finally:
            channel.close()

    def ensure(
        self,
        client: paramiko.SSHClient,
        device: DeviceModel,
        timeout: float,
        abort_check: Callable[[], bool],
    ) -> None:
        """Подготовить контекст впервые либо проверить неизменность сеанса."""
        if abort_check():
            raise RuntimeError("Прервано пользователем")
        transport = client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport недоступен")
        channel = transport.open_session(timeout=timeout)
        try:
            channel.get_pty(term="dumb")
            mode = "check" if self.token else "prepare"
            prompt, password = self._start(channel, self._command(mode), device)
            result = self._read(channel, timeout, abort_check, prompt, password)
            if not self.token:
                data = json.loads(result)
                self.token = base64.b64encode(json.dumps(data).encode()).decode()
        finally:
            channel.close()

    def launch(
        self,
        channel: paramiko.Channel,
        device: DeviceModel,
        timeout: float,
        abort_check: Callable[[], bool],
        text: str | None = None,
    ) -> str:
        """Открыть shell и подтвердить UID до передачи управления пользовательскому тексту."""
        if abort_check():
            channel.close()
            raise RuntimeError("Прервано пользователем")
        marker = "__DNO_DESKTOP_READY_" + uuid.uuid4().hex + "__"
        encoded = base64.b64encode(text.encode()).decode() if text is not None else ""
        # Пустой независимый скрипт тоже должен завершиться, а не открыть shell.
        if text == "":
            encoded = base64.b64encode(b":").decode()
        command = self._command(
            "launch", marker, get_locale(device) or "", _ENTER, encoded
        )
        prompt, password = self._start(channel, command, device)
        return self._read(channel, timeout, abort_check, prompt, password, marker)
