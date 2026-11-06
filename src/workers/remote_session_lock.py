"""Блокировка экрана и ввода на Linux-хосте без Veyon; передаётся по SSH.

Запускается от root. Управляющие режимы (``lock-input``, ``unlock-input``,
``lock-screen``, ``unlock-screen``) ставят или останавливают временные
службы systemd, сам захват держит демон этого же файла из ``/run/dno-tool``.
Ввод перехватывается на уровне ядра (EVIOCGRAB), поэтому одинаково
работает в X11, Wayland и на текстовой консоли. Виртуальные устройства
(uinput) не захватываются: через них вводят VNC-серверы и удалённое
управление продолжает работать. Совместим с Python 3.8 (МОС 12).
"""

import errno
import fcntl
import importlib
import json
import os
import pwd
import re
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
STATE_DIR = Path("/run/dno-tool")
SCRIPT = STATE_DIR / "session_lock.py"
SYS_INPUT = Path("/sys/class/input")
UNITS = {"input": "dno-input-lock", "screen": "dno-screen-lock"}
BINDINGS = ("PySide6", "PyQt6", "PyQt5", "PySide2")

# Ядро печатает capabilities словами размера long своей архитектуры.
LONG_BITS = struct.calcsize("l") * 8
# _IOW('E', 0x90, int): монопольный захват устройства evdev.
EVIOCGRAB = 0x40044590
EV_KEY, EV_REL, EV_ABS = 1, 2, 3
KEY_Q, KEY_A, KEY_Z = 16, 30, 44
BTN_LEFT, BTN_TOUCH = 0x110, 0x14A

# Частый просмотр сокращает окно между снятием одной блокировки и захватом
# устройства второй службой (пока держит первая, вторая получает EBUSY).
SCAN_INTERVAL = 0.25
SESSION_INTERVAL = 2.0
# Окно, проработавшее столько секунд, считается показанным.
OVERLAY_SETTLE_SEC = 1.5
# Доля высоты экрана, которую занимает замок.
LOCK_SCALE = 0.55
# Замок окна блокировки (QtSvg понимает SVG Tiny: градиенты без фильтров).
LOCK_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 300">
  <defs>
    <linearGradient id="steel" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#6f7882"/>
      <stop offset="0.22" stop-color="#e9edf1"/>
      <stop offset="0.42" stop-color="#ffffff"/>
      <stop offset="0.62" stop-color="#b9c1ca"/>
      <stop offset="1" stop-color="#5d6670"/>
    </linearGradient>
    <linearGradient id="brass" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffe9a8"/>
      <stop offset="0.18" stop-color="#ffd166"/>
      <stop offset="0.6" stop-color="#f0a52a"/>
      <stop offset="1" stop-color="#b8690c"/>
    </linearGradient>
    <linearGradient id="rim" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#fff6d6"/>
      <stop offset="1" stop-color="#8a4f08"/>
    </linearGradient>
    <linearGradient id="gloss" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.55"/>
      <stop offset="1" stop-color="#ffffff" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="hole" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#1c1204"/>
      <stop offset="1" stop-color="#4a2e08"/>
    </linearGradient>
    <radialGradient id="shadow" cx="0.5" cy="0.5" r="0.5">
      <stop offset="0" stop-color="#000000" stop-opacity="0.55"/>
      <stop offset="1" stop-color="#000000" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <ellipse cx="120" cy="286" rx="104" ry="12" fill="url(#shadow)"/>
  <path d="M60 150 V96 a60 60 0 0 1 120 0 V150" fill="none" stroke="#3d444c" stroke-width="30" stroke-linecap="round"/>
  <path d="M60 150 V96 a60 60 0 0 1 120 0 V150" fill="none" stroke="url(#steel)" stroke-width="24" stroke-linecap="round"/>
  <path d="M54 100 a66 66 0 0 1 40 -58" fill="none" stroke="#ffffff" stroke-opacity="0.6" stroke-width="4" stroke-linecap="round"/>
  <rect x="22" y="128" width="196" height="152" rx="28" fill="url(#rim)"/>
  <rect x="25" y="131" width="190" height="146" rx="25" fill="url(#brass)"/>
  <rect x="36" y="138" width="168" height="46" rx="20" fill="url(#gloss)"/>
  <path d="M120 175 A21 21 0 0 1 131 213.9 L137 250 Q138 256 132 256 H108 Q102 256 103 250 L109 213.9 A21 21 0 0 1 120 175 Z" fill="url(#hole)"/>
  <path d="M106 186 a16 16 0 0 1 14 -10" fill="none" stroke="#ffffff" stroke-opacity="0.25" stroke-width="3" stroke-linecap="round"/>
</svg>
"""

_SESSION_KEYS = (
    "DISPLAY",
    "XAUTHORITY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "LANG",
)


class LockError(Exception):
    """Ошибка, текст которой показывается оператору."""


def log(message):
    print("dno-lock: " + message, file=sys.stderr, flush=True)


def run(args, timeout=10, env=None, check=False):
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=timeout,
        env=env or {"PATH": PATH, "LC_ALL": "C"},
    )
    if check and result.returncode:
        raise LockError(
            "Команда {} завершилась с кодом {}".format(args[0], result.returncode)
        )
    return result


# ---------- Устройства ввода ----------


def read_bits(path):
    """Битовая маска capabilities: слова long, старшее первым."""
    value = 0
    for word in path.read_text().split():
        value = (value << LONG_BITS) | int(word, 16)
    return value


def wanted_device(event_dir):
    """Физическая клавиатура, мышь, тачпад или сенсорный экран."""
    device = event_dir / "device"
    parts = Path(os.path.realpath(str(device))).parts
    if any(parts[i : i + 2] == ("devices", "virtual") for i in range(len(parts))):
        return False
    caps = device / "capabilities"
    ev = read_bits(caps / "ev")
    keys = read_bits(caps / "key") if ev >> EV_KEY & 1 else 0

    def has(code):
        return bool(keys >> code & 1)

    if ev >> EV_KEY & 1 and has(KEY_A) and has(KEY_Z) and has(KEY_Q):
        return True
    if ev >> EV_REL & 1:
        return True
    return bool(ev >> EV_ABS & 1 and (has(BTN_TOUCH) or has(BTN_LEFT)))


def device_name(event_dir):
    try:
        return (event_dir / "device" / "name").read_text().strip()
    except OSError:
        return event_dir.name


class Grabber:
    """Держит захват устройств и подхватывает новые при каждом просмотре."""

    def __init__(self, sys_root=SYS_INPUT, dev_root=Path("/dev/input")):
        self.sys_root = sys_root
        self.dev_root = dev_root
        self.devices = {}
        # Отвергнутые устройства: имя -> inode каталога sysfs (новое подключение
        # с тем же именем получит другой inode и будет проверено заново).
        self.rejected = {}

    def scan(self):
        try:
            entries = sorted(self.sys_root.glob("event*"))
        except OSError:
            entries = []
        for entry in entries:
            if entry.name in self.devices:
                continue
            try:
                inode = entry.stat().st_ino
                if self.rejected.get(entry.name) == inode:
                    continue
                if not wanted_device(entry):
                    self.rejected[entry.name] = inode
                    continue
            except (OSError, ValueError):
                continue
            try:
                fd = os.open(
                    str(self.dev_root / entry.name),
                    os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC,
                )
            except OSError:
                continue
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 1)
            except OSError:
                # EBUSY: устройство держит другой демон блокировки; повтор позже.
                os.close(fd)
                continue
            self.devices[entry.name] = (fd, device_name(entry))

    def drain(self, timeout):
        """Читать и отбрасывать события; отключённые устройства забываются."""
        by_fd = {fd: name for name, (fd, _label) in self.devices.items()}
        if not by_fd:
            time.sleep(max(0.0, timeout))
            return
        try:
            ready, _w, _x = select.select(list(by_fd), [], [], max(0.0, timeout))
        except InterruptedError:
            return
        for fd in ready:
            try:
                while os.read(fd, 4096):
                    pass
            except BlockingIOError:
                continue
            except OSError:
                self._forget(by_fd[fd])

    def _forget(self, name):
        fd, _label = self.devices.pop(name)
        try:
            os.close(fd)
        except OSError:
            pass

    def release(self):
        for name in list(self.devices):
            fd = self.devices[name][0]
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 0)
            except OSError:
                pass
            self._forget(name)

    def names(self):
        return sorted(label for _fd, label in self.devices.values())


# ---------- Графические сеансы ----------


def session_properties(sid):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", sid):
        raise LockError("Некорректный идентификатор сеанса")
    out = run(["loginctl", "show-session", sid, "--no-pager"], check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def graphical(props):
    return (
        props.get("Remote") == "no"
        and props.get("Class") in ("user", "user-early")
        and props.get("Type") in ("x11", "wayland")
        and props.get("State") in ("active", "online")
    )


def graphical_sessions():
    out = run(["loginctl", "list-sessions", "--no-legend", "--no-pager"], check=True)
    sessions = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        try:
            props = session_properties(parts[0])
        except (LockError, subprocess.SubprocessError):
            continue
        if graphical(props) and props.get("User", "").isdigit():
            sessions.append(props)
    return sessions


def scope_environment(sid, uid):
    """Окружение процессов владельца внутри session-<id>.scope."""
    scope = "session-" + sid + ".scope"
    variants = {key: set() for key in _SESSION_KEYS}
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
        except OSError:
            continue
        env = dict(part.split(b"=", 1) for part in raw.split(b"\0") if b"=" in part)
        for key in _SESSION_KEYS:
            value = env.get(key.encode(), b"").decode("utf-8", "replace")
            if value:
                variants[key].add(value)
    return {key: sorted(values)[0] for key, values in variants.items() if values}


def user_manager_environment(account, runtime, bus):
    """Plasma 6 запускает оболочку через systemd --user: дисплей виден там."""
    setpriv = shutil.which("setpriv", path=PATH)
    if not setpriv:
        return {}
    args = [
        setpriv,
        "--reuid=" + str(account.pw_uid),
        "--regid=" + str(account.pw_gid),
        "--init-groups",
        "--",
        "systemctl",
        "--user",
        "show-environment",
    ]
    env = {
        "PATH": PATH,
        "LC_ALL": "C",
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": bus,
    }
    try:
        result = run(args, timeout=8, env=env)
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode:
        return {}
    found = {}
    for line in result.stdout.splitlines():
        key, _sep, value = line.partition("=")
        if key in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY") and value:
            found[key] = value
    return found


def owned_by(path, uid, kind):
    try:
        st = os.stat(path)
    except OSError:
        return False
    return st.st_uid == uid and kind(st.st_mode)


def session_environment(props):
    """Окружение окна блокировки для сеанса; ошибка — LockError."""
    uid = int(props["User"])
    account = pwd.getpwuid(uid)
    if account.pw_name != props.get("Name"):
        raise LockError("Не удалось подтвердить владельца сеанса")
    found = scope_environment(props["Id"], uid)
    runtime = found.get("XDG_RUNTIME_DIR") or "/run/user/{}".format(uid)
    if not owned_by(runtime, uid, stat.S_ISDIR):
        raise LockError("Не найден XDG_RUNTIME_DIR сеанса")
    bus = found.get("DBUS_SESSION_BUS_ADDRESS")
    if not bus:
        socket_path = os.path.join(runtime, "bus")
        bus = "unix:path=" + socket_path if os.path.exists(socket_path) else ""
    kind = props["Type"]
    need = "WAYLAND_DISPLAY" if kind == "wayland" else "DISPLAY"
    if not found.get(need) and bus:
        for key, value in user_manager_environment(account, runtime, bus).items():
            found.setdefault(key, value)
    if kind == "wayland" and not found.get("WAYLAND_DISPLAY"):
        if owned_by(os.path.join(runtime, "wayland-0"), uid, stat.S_ISSOCK):
            found["WAYLAND_DISPLAY"] = "wayland-0"
    if not found.get(need):
        raise LockError("Не найден {} сеанса {}".format(need, props["Id"]))
    env = {
        "PATH": PATH,
        "HOME": account.pw_dir,
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        "XDG_RUNTIME_DIR": runtime,
        "XDG_SESSION_TYPE": kind,
        "XDG_SESSION_ID": props["Id"],
        "LANG": found.get("LANG") or "C.UTF-8",
    }
    if bus:
        env["DBUS_SESSION_BUS_ADDRESS"] = bus
    for key in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY"):
        if found.get(key):
            env[key] = found[key]
    if kind == "wayland":
        platforms = ["wayland"]
        if env.get("DISPLAY"):
            # PyQt5 без модуля Wayland уйдёт в XWayland.
            platforms.append("xcb")
        env["QT_QPA_PLATFORM"] = ";".join(platforms)
    else:
        env["QT_QPA_PLATFORM"] = "xcb"
    return account, env


class Overlays:
    """Окно блокировки в каждом локальном графическом сеансе."""

    def __init__(self, binding):
        self.binding = binding
        self.items = {}
        self.sessions = []
        self.next_list = 0.0

    def sync(self):
        """Список сеансов обновляется раз в SESSION_INTERVAL, окна — каждый цикл."""
        now = time.monotonic()
        if now >= self.next_list:
            self.next_list = now + SESSION_INTERVAL
            try:
                self.sessions = graphical_sessions()
            except (LockError, OSError, subprocess.SubprocessError) as error:
                log("список сеансов недоступен: {}".format(error))
        alive = set()
        for props in self.sessions:
            sid = props["Id"]
            alive.add(sid)
            identity = (props.get("Leader"), props.get("TimestampMonotonic"))
            item = self.items.get(sid)
            if item is not None and item["identity"] != identity:
                self._stop(sid)
                item = None
            if item is None:
                item = {"identity": identity, "failures": 0, "next": 0.0}
                self.items[sid] = item
            item["user"] = props.get("Name", "")
            proc = item.get("proc")
            if proc is not None:
                if proc.poll() is None:
                    if item["state"] == "starting" and (
                        now - item["started"] >= OVERLAY_SETTLE_SEC
                    ):
                        item["state"] = "shown"
                        item["failures"] = 0
                    continue
                item["proc"] = None
                item["state"] = "failed"
                item["error"] = self._log_tail(sid) or "окно закрылось с кодом {}".format(
                    proc.returncode
                )
                item["failures"] += 1
                item["next"] = now + min(30.0, SESSION_INTERVAL * item["failures"])
                log("окно сеанса {}: {}".format(sid, item["error"]))
            if now < item["next"]:
                continue
            self._launch(sid, props, item, now)
        for sid in list(self.items):
            if sid not in alive:
                self._stop(sid)
                self.items.pop(sid, None)

    def _log_path(self, sid):
        return STATE_DIR / "overlay-{}.log".format(sid)

    def _log_tail(self, sid):
        try:
            data = self._log_path(sid).read_bytes()[-2000:]
        except OSError:
            return ""
        lines = [line.strip() for line in data.decode("utf-8", "replace").splitlines()]
        return " | ".join(line for line in lines if line)[-300:]

    def _launch(self, sid, props, item, now):
        try:
            account, env = session_environment(props)
            setpriv = shutil.which("setpriv", path=PATH)
            if not setpriv:
                raise LockError("На хосте нет setpriv (util-linux)")
            args = [
                setpriv,
                "--reuid=" + str(account.pw_uid),
                "--regid=" + str(account.pw_gid),
                "--init-groups",
                "--",
                sys.executable,
                "-I",
                str(SCRIPT),
                "overlay",
                self.binding,
            ]
            with open(str(self._log_path(sid)), "wb") as errors:
                item["proc"] = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                    env=env,
                    cwd="/",
                    close_fds=True,
                )
            item["state"] = "starting"
            item["started"] = now
            item.pop("error", None)
        except (LockError, OSError, KeyError, ValueError) as error:
            item["state"] = "failed"
            item["error"] = str(error)
            item["failures"] += 1
            item["next"] = now + min(30.0, SESSION_INTERVAL * item["failures"])
            log("окно сеанса {}: {}".format(sid, error))

    def _stop(self, sid):
        item = self.items.get(sid) or {}
        proc = item.get("proc")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        item["proc"] = None

    def stop_all(self):
        for sid in list(self.items):
            self._stop(sid)
            try:
                self._log_path(sid).unlink()
            except OSError:
                pass

    def snapshot(self):
        return [
            {
                "id": sid,
                "user": item.get("user", ""),
                "state": item.get("state", "starting"),
                "error": item.get("error", ""),
            }
            for sid, item in sorted(self.items.items())
        ]


# ---------- Демоны ----------


def status_path(kind):
    return STATE_DIR / (UNITS[kind] + ".json")


def write_status(kind, data):
    path = status_path(kind)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.chmod(str(temporary), 0o600)
    temporary.replace(path)


def daemon(kind, binding=None):
    stop = []

    def on_term(_signum, _frame):
        stop.append(True)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    os.umask(0o022)
    grabber = Grabber()
    overlays = Overlays(binding) if kind == "screen" else None
    last = None
    try:
        while not stop:
            grabber.scan()
            data = {"pid": os.getpid(), "kind": kind, "devices": grabber.names()}
            if overlays is not None:
                overlays.sync()
                data["sessions"] = overlays.snapshot()
            if data != last:
                write_status(kind, data)
                last = data
            grabber.drain(SCAN_INTERVAL)
    finally:
        if overlays is not None:
            overlays.stop_all()
        grabber.release()
        try:
            status_path(kind).unlink()
        except OSError:
            pass


# ---------- Окно блокировки (от имени пользователя) ----------


def overlay(binding):
    core = importlib.import_module(binding + ".QtCore")
    gui = importlib.import_module(binding + ".QtGui")
    widgets = importlib.import_module(binding + ".QtWidgets")
    svg = importlib.import_module(binding + ".QtSvg")
    Qt = core.Qt

    def member(owner, scope, name):
        # PyQt6 знает только вложенные перечисления, PyQt5 — только плоские имена.
        holder = getattr(owner, scope, None)
        if holder is not None and hasattr(holder, name):
            return getattr(holder, name)
        return getattr(owner, name)

    def enum(scope, name):
        return member(Qt, scope, name)

    app = widgets.QApplication([sys.argv[0]])
    renderer = svg.QSvgRenderer(core.QByteArray(LOCK_SVG.encode("utf-8")))
    if not renderer.isValid():
        raise LockError("Не удалось разобрать изображение замка")
    x11 = gui.QGuiApplication.platformName() == "xcb"
    flags = (
        enum("WindowType", "FramelessWindowHint")
        | enum("WindowType", "WindowStaysOnTopHint")
    )
    if x11:
        flags = flags | enum("WindowType", "X11BypassWindowManagerHint")

    class LockWindow(widgets.QWidget):
        def __init__(self, screen):
            super().__init__(None, flags)
            self.setCursor(gui.QCursor(enum("CursorShape", "BlankCursor")))
            self.setGeometry(screen.geometry())
            if x11:
                self.show()
            else:
                self.winId()
                self.windowHandle().setScreen(screen)
                self.showFullScreen()

        def paintEvent(self, _event):
            painter = gui.QPainter(self)
            painter.setRenderHint(member(gui.QPainter, "RenderHint", "Antialiasing"))
            rect = self.rect()
            center = core.QPointF(rect.center())
            background = gui.QRadialGradient(
                center, max(rect.width(), rect.height()) * 0.6
            )
            background.setColorAt(0.0, gui.QColor(30, 38, 50))
            background.setColorAt(1.0, gui.QColor(4, 6, 9))
            painter.fillRect(rect, gui.QBrush(background))
            size = renderer.defaultSize()
            height = rect.height() * LOCK_SCALE
            width = height * size.width() / max(1, size.height())
            if width > rect.width() * 0.8:
                width = rect.width() * 0.8
                height = width * size.height() / max(1, size.width())
            renderer.render(
                painter,
                core.QRectF(
                    center.x() - width / 2, center.y() - height / 2, width, height
                ),
            )
            painter.end()

        def closeEvent(self, event):
            event.ignore()

        def keyPressEvent(self, event):
            event.accept()

    windows = []

    def rebuild(*_args):
        while windows:
            window = windows.pop()
            window.hide()
            window.deleteLater()
        for screen in app.screens():
            windows.append(LockWindow(screen))

    rebuild()
    app.screenAdded.connect(rebuild)
    app.screenRemoved.connect(rebuild)
    timer = core.QTimer()
    timer.timeout.connect(lambda: [window.raise_() for window in windows])
    timer.start(1000)
    run_loop = getattr(app, "exec", None) or getattr(app, "exec_")
    return run_loop()


# ---------- Управляющие режимы ----------


def require_root():
    if os.geteuid() != 0:
        raise LockError("Нужны права root: включите sudo для хоста или войдите как root")


def unit_state(unit):
    result = run(
        ["systemctl", "show", unit + ".service", "-p", "ActiveState", "--value"]
    )
    return result.stdout.strip() or "inactive"


def journal_tail(unit):
    try:
        result = run(
            ["journalctl", "-u", unit + ".service", "-n", "6", "-o", "cat", "--no-pager"]
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return " | ".join(lines)[-400:]


def install(source):
    STATE_DIR.mkdir(mode=0o755, exist_ok=True)
    st = os.lstat(str(STATE_DIR))
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0:
        raise LockError("Каталог {} не принадлежит root".format(STATE_DIR))
    os.chmod(str(STATE_DIR), 0o755)
    try:
        if SCRIPT.read_bytes() == source:
            return
    except OSError:
        pass
    temporary = SCRIPT.with_name(SCRIPT.name + ".new")
    temporary.write_bytes(source)
    os.chmod(str(temporary), 0o644)
    temporary.replace(SCRIPT)


def find_binding():
    for name in BINDINGS:
        result = run(
            [sys.executable, "-I", "-c", "import {0}.QtWidgets, {0}.QtSvg".format(name)],
            timeout=20,
        )
        if result.returncode == 0:
            return name
    raise LockError(
        "На хосте нет Qt для Python с модулем QtSvg (PySide6, PyQt6 или PyQt5) "
        "для окна блокировки"
    )


def read_status(kind):
    try:
        return json.loads(status_path(kind).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def start_unit(kind, source, extra=()):
    unit = UNITS[kind]
    if not shutil.which("systemd-run", path=PATH):
        raise LockError("На хосте нет systemd-run")
    install(source)
    run(["systemctl", "reset-failed", unit + ".service"])
    try:
        status_path(kind).unlink()
    except OSError:
        pass
    args = [
        "systemd-run",
        "--unit=" + unit,
        "--collect",
        "--quiet",
        "--description=dno-tool: блокировка " + ("экрана" if kind == "screen" else "ввода"),
        "-p",
        "KillMode=control-group",
        "-p",
        "Restart=on-failure",
        "-p",
        "RestartSec=1",
        sys.executable,
        "-I",
        str(SCRIPT),
        "daemon-" + kind,
    ] + list(extra)
    result = run(args, timeout=20)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise LockError(
            "systemd-run не запустил службу: " + (detail[-1] if detail else "")
        )


def wait_status(kind, ready, deadline_sec):
    unit = UNITS[kind]
    deadline = time.monotonic() + deadline_sec
    data = None
    while time.monotonic() < deadline:
        data = read_status(kind)
        if data is not None and ready(data):
            return data
        if unit_state(unit) == "failed":
            raise LockError("Служба блокировки остановилась: " + journal_tail(unit))
        time.sleep(0.2)
    if data is None:
        raise LockError(
            "Служба блокировки не ответила: " + (journal_tail(unit) or "нет сведений")
        )
    return data


def stop_unit(kind):
    unit = UNITS[kind]
    was = unit_state(unit) in ("active", "activating", "reloading", "deactivating")
    run(["systemctl", "stop", unit + ".service"], timeout=20)
    run(["systemctl", "reset-failed", unit + ".service"])
    if unit_state(unit) in ("active", "activating"):
        raise LockError("Служба {} не остановилась".format(unit))
    return was


def devices_text(data):
    names = data.get("devices") or []
    if not names:
        if unit_state(UNITS["screen" if data.get("kind") == "input" else "input"]) == "active":
            return "устройства уже захвачены второй блокировкой"
        return "устройства ввода не найдены"
    return "устройств: {} ({})".format(len(names), ", ".join(names))


def lock_input(source):
    require_root()
    if unit_state(UNITS["input"]) != "active":
        start_unit("input", source)
    data = wait_status("input", lambda _d: True, 6)
    message = "Ввод заблокирован, " + devices_text(data)
    if unit_state(UNITS["screen"]) == "active":
        message += "; экран тоже заблокирован"
    print(message, flush=True)


def unlock_input(_source):
    require_root()
    was = stop_unit("input")
    if unit_state(UNITS["screen"]) == "active":
        print(
            "Блокировка ввода снята, но ввод остаётся заблокирован, "
            "пока заблокирован экран",
            flush=True,
        )
    else:
        print("Блокировка ввода снята" if was else "Ввод не был заблокирован", flush=True)


def _settled(data):
    return all(item["state"] != "starting" for item in data.get("sessions", []))


def lock_screen(source):
    require_root()
    if unit_state(UNITS["screen"]) != "active":
        binding = find_binding()
        if not shutil.which("setpriv", path=PATH):
            raise LockError("На хосте нет setpriv (util-linux)")
        start_unit("screen", source, [binding])
    data = wait_status(
        "screen", _settled, SESSION_INTERVAL * 2 + OVERLAY_SETTLE_SEC + 4
    )
    sessions = data.get("sessions", [])
    shown = [item for item in sessions if item["state"] == "shown"]
    failed = [item for item in sessions if item["state"] == "failed"]
    if sessions and not shown:
        stop_unit("screen")
        reason = failed[0]["error"] if failed else "окно не появилось"
        raise LockError("Окно блокировки не показано: " + reason)
    if not sessions:
        message = (
            "Графический сеанс не найден, заблокирован только ввод; "
            "окно появится после входа пользователя"
        )
    else:
        message = "Экран заблокирован (окон: {})".format(len(shown))
        if failed:
            message += "; не удалось в сеансе {}: {}".format(
                failed[0]["id"], failed[0]["error"]
            )
    print(message + ", " + devices_text(data), flush=True)


def unlock_screen(_source):
    require_root()
    was = stop_unit("screen")
    message = "Блокировка экрана снята" if was else "Экран не был заблокирован"
    if unit_state(UNITS["input"]) == "active":
        message += "; ввод остаётся заблокирован"
    print(message, flush=True)


CONTROL = {
    "lock-input": lock_input,
    "unlock-input": unlock_input,
    "lock-screen": lock_screen,
    "unlock-screen": unlock_screen,
}


def main(argv, source):
    mode = argv[1] if len(argv) > 1 else ""
    if mode in CONTROL:
        CONTROL[mode](source)
        return 0
    if mode == "daemon-input":
        daemon("input")
        return 0
    if mode == "daemon-screen":
        daemon("screen", argv[2])
        return 0
    if mode == "overlay":
        return overlay(argv[2])
    raise LockError("Неизвестный режим: " + mode)


if __name__ == "__main__":
    os.environ["PATH"] = PATH
    try:
        _source = globals().get("SOURCE")
        if _source is None:
            _source = Path(__file__).read_bytes()
        sys.exit(main(sys.argv, _source))
    except LockError as _error:
        log(str(_error))
        sys.exit(1)
    except (OSError, subprocess.SubprocessError) as _error:
        if getattr(_error, "errno", None) == errno.EACCES:
            log("Недостаточно прав: " + str(_error))
        else:
            log("Системная ошибка: " + str(_error))
        sys.exit(1)
