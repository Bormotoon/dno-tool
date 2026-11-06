"""Изолированный Linux-супервизор удалённой записи; передаётся по SSH."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def write_json(path, data):
    """Публиковать состояние только целиком."""
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(path)


def process_identity(pid):
    """Проверить поколение PID и маркер среды перед отправкой сигнала."""
    try:
        proc = Path("/proc") / str(pid)
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        marker = (proc / "environ").read_bytes().split(b"\0")
        if ("DNO_RECORDING_SESSION=" + ROOT.name).encode() not in marker:
            return None
        return fields[19]
    except (OSError, IndexError):
        return None


def status():
    """Размер, завершение и хвост лога возвращаются одним снимком."""
    result_path = ROOT / "result.json"
    if result_path.exists():
        result = json.loads(result_path.read_text())
    else:
        identity_path = ROOT / "process.json"
        identity = (
            json.loads(identity_path.read_text()) if identity_path.exists() else {}
        )
        alive = bool(
            identity.get("start")
            and process_identity(identity["pid"]) == identity["start"]
        )
        result = {"state": "running" if alive else "unknown"}
    cfg = json.loads((ROOT / "config.json").read_text())
    output = cfg.get("output", "")
    try:
        result["size"] = Path(output).stat().st_size if output else 0
    except OSError:
        result["size"] = 0
    try:
        with (ROOT / "output.log").open("rb") as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - 32768))
            result["log"] = log.read().decode("utf-8", "replace").splitlines()[-100:]
    except OSError:
        result["log"] = []
    if result["state"] == "running" and not (ROOT / "ready").exists():
        if output:
            ready = any(
                line.startswith("frame=")
                and line[6:].strip().isdigit()
                and int(line[6:].strip()) > 0
                for line in result["log"]
            )
        else:
            ready = listening(identity["pid"], cfg["port"])
        if ready:
            (ROOT / "ready").touch(mode=0o600)
        else:
            result["state"] = "starting"
    return result


def listening(pid, port):
    """Порт должен принадлежать именно дочернему процессу этой сессии.

    Скрипт исполняется системным python3 хоста, поэтому только API Python 3.6:
    Path.readlink появился лишь в 3.9.
    """
    sockets = set()
    try:
        descriptors = list((Path("/proc") / str(pid) / "fd").iterdir())
    except OSError:
        return False
    for descriptor in descriptors:
        try:
            sockets.add(os.readlink(str(descriptor)))
        except OSError:
            # Дескриптор мог закрыться между перечислением и чтением.
            continue
    for table in ("tcp", "tcp6"):
        try:
            rows = (Path("/proc/net") / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            try:
                if (
                    int(fields[1].rsplit(":", 1)[1], 16) == port
                    and fields[3] == "0A"
                    and "socket:[" + fields[9] + "]" in sockets
                ):
                    return True
            except (IndexError, ValueError):
                continue
    return False


def safe_status():
    """Сбой чтения состояния не должен мешать запуску и остановке процесса."""
    try:
        return status()
    except Exception as error:  # noqa: BLE001 — ответ координатору обязателен
        return {
            "state": "unknown",
            "error": "Состояние не прочитано: " + repr(error),
            "log": [],
            "size": 0,
        }


def desktop_environment(display):
    """Найти авторизацию X11 до создания процесса захвата."""
    env = os.environ.copy()
    env["DISPLAY"] = display
    if not env.get("XAUTHORITY"):
        candidates = [Path.home() / ".Xauthority"]
        runtime = Path("/run/user") / str(os.getuid())
        candidates += list(runtime.glob("xauth_*")) + list(
            runtime.glob(".mutter-Xwaylandauth.*")
        )
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                values = dict(
                    v.split(b"=", 1)
                    for v in (proc / "environ").read_bytes().split(b"\0")
                    if b"=" in v
                )
                if (
                    values.get(b"DISPLAY", b"").decode(errors="replace").split(".")[0]
                    == display.split(".")[0]
                    and b"XAUTHORITY" in values
                ):
                    candidates.insert(0, Path(os.fsdecode(values[b"XAUTHORITY"])))
            except OSError:
                pass
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.R_OK):
                env["XAUTHORITY"] = str(candidate)
                break
    return env


def supervise():
    """Удерживать дочерний процесс и сохранять результат после финализации."""
    cfg = json.loads((ROOT / "config.json").read_text())
    try:
        env = (
            desktop_environment(cfg.get("display", ":0"))
            if cfg.get("capture_type") == "x11grab"
            else os.environ.copy()
        )
        env.update(cfg.get("env", {}))
        env["DNO_RECORDING_SESSION"] = ROOT.name
        if not shutil.which(cfg["argv"][0]):
            raise RuntimeError("Не найдена программа: " + cfg["argv"][0])
        output = cfg.get("output")
        if output:
            target = Path(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(target.parent).free < 64 * 1024 * 1024:
                raise RuntimeError("Недостаточно свободного места (требуется 64 МиБ)")
            # Эксклюзивное создание сохраняет существующую запись при совпадении имени.
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        with (ROOT / "output.log").open("wb") as log:
            child = subprocess.Popen(
                cfg["argv"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
            write_json(
                ROOT / "process.json",
                {"pid": child.pid, "start": process_identity(child.pid)},
            )
            code = child.wait()
        requested = (ROOT / "stop.requested").exists()
        valid = not output or (
            Path(output).is_file() and Path(output).stat().st_size > 0
        )
        ok = valid and (code == 0 or (requested and code in (255, -signal.SIGINT)))
        write_json(
            ROOT / "result.json",
            {"state": "completed" if ok else "error", "exit_code": code},
        )
    except Exception as error:  # noqa: BLE001 — результат обязателен при любой ошибке процесса
        write_json(ROOT / "result.json", {"state": "error", "error": str(error)})


def operate(action):
    """Запуск и остановка имеют ограниченное время подтверждения."""
    if action == "start":
        with (ROOT / "supervisor.log").open("ab") as log:
            subprocess.Popen(
                [sys.executable, __file__, str(ROOT), "supervise"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            snap = safe_status()
            if snap["state"] in ("completed", "error"):
                return snap
            if snap["state"] == "running":
                return snap
            time.sleep(0.1)
        return dict(
            safe_status(),
            state="unknown",
            error="Запуск не подтверждён; проверьте состояние или остановите сессию",
        )
    if action == "stop":
        snap = safe_status()
        if snap["state"] in ("completed", "error"):
            return snap
        identity_path = ROOT / "process.json"
        if not identity_path.exists():
            return dict(snap, state="unknown", error="Нет подтверждённого PID")
        identity = json.loads(identity_path.read_text())
        if (
            not identity.get("start")
            or process_identity(identity["pid"]) != identity["start"]
        ):
            return dict(
                snap, state="unknown", error="Принадлежность процесса не подтверждена"
            )
        (ROOT / "stop.requested").touch(mode=0o600)
        try:
            os.kill(identity["pid"], signal.SIGINT)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            snap = safe_status()
            if snap["state"] in ("completed", "error"):
                return snap
            time.sleep(0.1)
        return dict(safe_status(), state="unknown", error="Остановка не подтверждена")
    return safe_status()


if __name__ == "__main__":
    ROOT = Path(sys.argv[1])
    if (
        ROOT.is_symlink()
        or ROOT.stat().st_uid != os.getuid()
        or ROOT.stat().st_mode & 0o077
    ):
        raise RuntimeError("Небезопасный служебный каталог")
    if sys.argv[2] == "supervise":
        supervise()
    else:
        print(json.dumps(operate(sys.argv[2]), ensure_ascii=False), flush=True)
