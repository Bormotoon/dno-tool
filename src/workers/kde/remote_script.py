"""Самодостаточный Python-протокол удалённых файловых операций KDE.

Скрипт передаётся по SSH и не устанавливается на хост. Исходный текст
хранится в модуле, поэтому доступен и внутри frozen-сборки.
"""

from __future__ import annotations

import shlex

REMOTE_SCRIPT = r"""
from __future__ import annotations

import base64
import datetime
import errno
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import uuid

MAX_FILE = 2 * 1024 * 1024
FILES = ("kdeglobals", "dolphinrc", "kwinrc", "konsolerc", "plasmarc", "krunnerrc")


class Failure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def run(arguments: list[str], timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=False)
        return result.stdout[:131072] if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def sessions() -> list[dict]:
    found = []
    for line in run(["loginctl", "list-sessions", "--no-legend", "--no-pager"]).splitlines()[:128]:
        parts = line.split()
        if not parts:
            continue
        details = run(["loginctl", "show-session", parts[0], "--no-pager", "-p", "Name", "-p", "User", "-p", "Type", "-p", "Desktop", "-p", "Class", "-p", "State", "-p", "Leader"])
        data = dict(item.split("=", 1) for item in details.splitlines() if "=" in item)
        if data.get("Type") in ("x11", "wayland") and data.get("Class") in ("user", "user-early"):
            found.append(data)
    return found


def profile(username: str, active: list[dict]) -> dict:
    try:
        user = pwd.getpwnam(username)
    except KeyError:
        raise Failure("profile", "Учётная запись больше не существует.")
    return {"username": user.pw_name, "uid": user.pw_uid, "gid": user.pw_gid, "home": user.pw_dir,
            "sessions": [s.get("Desktop", "KDE не определена") + " · " + s["Type"] + " · " + s.get("State", "") for s in active if s.get("User") == str(user.pw_uid)]}


def environment(user: dict, active: list[dict]) -> tuple[dict, str]:
    for session in active:
        if session.get("User") != str(user["uid"]):
            continue
        leader = session.get("Leader", "")
        if not leader.isdigit():
            continue
        try:
            path = "/proc/" + leader
            if os.stat(path).st_uid != user["uid"]:
                continue
            with open(path + "/environ", "rb") as stream:
                raw = stream.read(131072)
            values = dict(part.decode("utf-8", "replace").split("=", 1) for part in raw.split(b"\0") if b"=" in part)
            return values, "Окружение определено по графическому сеансу."
        except (OSError, ValueError):
            continue
    return {}, "Окружение сеанса недоступно: используются стандартные XDG-пути; переопределения окружения не проверены."


def open_dir(path: str, create: bool = False, uid: int = -1, gid: int = -1) -> int:
    if not os.path.isabs(path) or ".." in path.split("/"):
        raise Failure("path", "Ожидался абсолютный путь без переходов к родителю.")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = [part for part in path.split("/") if part]
        for index, part in enumerate(parts):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if not create or index != len(parts) - 1:
                    raise
                os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                if os.geteuid() == 0:
                    os.fchown(child, uid, gid)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_file(directory: int, filename: str) -> tuple[bytes, dict]:
    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return b"", {"exists": False, "fingerprint": "missing"}
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Failure("file_type", "Запись разрешена только для обычного файла без жёстких ссылок.")
        data = stream.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise Failure("size", "Файл превышает допустимые 2 МиБ.")
        attributes = {name: base64.b64encode(os.getxattr(stream.fileno(), name)).decode("ascii") for name in os.listxattr(stream.fileno())}
        return data, {"exists": True, "fingerprint": hashlib.sha256(data).hexdigest(), "uid": info.st_uid,
                      "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode), "device": info.st_dev, "inode": info.st_ino, "xattrs": attributes}


def sources(config_dirs: str, filename: str) -> list[dict]:
    result = []
    directories = [p for p in config_dirs.split(":") if os.path.isabs(p)]
    if not directories:
        directories = ["/etc/xdg"]
    candidates = []
    if filename == "kdeglobals":
        candidates.append("/etc/kde5rc")
    for directory in reversed(directories[:16]):
        if filename == "kdeglobals":
            candidates.append(os.path.join(directory, "system.kdeglobals"))
        candidates.append(os.path.join(directory, filename))
    for path in dict.fromkeys(candidates):
        descriptor = None
        try:
            descriptor = open_dir(os.path.dirname(path))
            raw, meta = read_file(descriptor, os.path.basename(path))
            result.append({"path": path, "raw": base64.b64encode(raw).decode("ascii"), "fingerprint": meta["fingerprint"], "error": ""})
        except FileNotFoundError:
            result.append({"path": path, "raw": "", "fingerprint": "missing", "error": ""})
        except (OSError, Failure) as error:
            result.append({"path": path, "raw": "", "fingerprint": "unreadable", "error": str(error)})
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return result


def resources(user: dict) -> dict:
    colors = set()
    icons = set()
    desktopthemes = set()
    for base in (os.path.join(user["home"], ".local/share"), "/usr/local/share", "/usr/share"):
        try:
            colors.update(os.path.splitext(name)[0] for name in os.listdir(os.path.join(base, "color-schemes")) if name.endswith(".colors"))
        except OSError:
            pass
        try:
            desktopthemes.update(name for name in os.listdir(os.path.join(base, "plasma/desktoptheme")) if os.path.isdir(os.path.join(base, "plasma/desktoptheme", name)))
        except OSError:
            pass
        try:
            icons.update(name for name in os.listdir(os.path.join(base, "icons")) if os.path.isfile(os.path.join(base, "icons", name, "index.theme")))
        except OSError:
            pass
    families = run(["fc-list", "--format=%{family}\n"])
    modules = set()
    for executable in ("kcmshell6", "kcmshell5"):
        listed = run(["env", "QT_QPA_PLATFORM=offscreen", executable, "--list"])
        for line in listed.splitlines():
            match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s+-\s+.+$", line)
            if match:
                modules.add(match.group(1))
        if modules:
            break
    return {"colors": sorted(colors)[:512], "icons": sorted(icons)[:512], "desktopthemes": sorted(desktopthemes)[:512], "fonts": sorted(set(families.splitlines()))[:2048], "modules": sorted(modules)[:512]}


def context(request: dict, active: list[dict]) -> tuple[dict, str, str, dict, str]:
    user = profile(str(request["username"]), active)
    expected = request.get("uid")
    if expected is not None and int(expected) != user["uid"]:
        raise Failure("profile", "UID пользователя изменился; перечитайте профиль.")
    filename = str(request.get("filename", "kdeglobals"))
    if filename not in FILES:
        raise Failure("path", "Файл не входит в список поддерживаемых конфигураций.")
    env, note = environment(user, active)
    config_dir = env.get("XDG_CONFIG_HOME") or os.path.join(user["home"], ".config")
    if not os.path.isabs(config_dir):
        raise Failure("path", "XDG_CONFIG_HOME не является абсолютным путём.")
    path = os.path.join(config_dir, filename)
    if request.get("path") and request["path"] != path:
        raise Failure("profile", "Путь профиля изменился; перечитайте файл.")
    return user, config_dir, filename, env, note


def snapshot(request: dict, active: list[dict], include_resources: bool = True) -> dict:
    user, directory, filename, env, note = context(request, active)
    descriptor = None
    try:
        descriptor = open_dir(directory)
        raw, meta = read_file(descriptor, filename)
    except FileNotFoundError:
        raw, meta = b"", {"exists": False, "fingerprint": "missing"}
    finally:
        if descriptor is not None:
            os.close(descriptor)
    policy = sources(env.get("XDG_CONFIG_DIRS", "/etc/xdg"), filename)
    if filename == "kdeglobals":
        own_system = os.path.join(directory, "system.kdeglobals")
        own_fd = None
        try:
            own_fd = open_dir(directory)
            data, metadata = read_file(own_fd, "system.kdeglobals")
            policy.append({"path": own_system, "raw": base64.b64encode(data).decode("ascii"), "fingerprint": metadata["fingerprint"], "error": ""})
        except FileNotFoundError:
            policy.append({"path": own_system, "raw": "", "fingerprint": "missing", "error": ""})
        except (OSError, Failure) as error:
            policy.append({"path": own_system, "raw": "", "fingerprint": "unreadable", "error": str(error)})
        finally:
            if own_fd is not None:
                os.close(own_fd)
    return {"profile": user, "path": os.path.join(directory, filename), "raw": base64.b64encode(raw).decode("ascii"),
            "meta": meta, "sources": policy, "environment_note": note,
            "resources": resources(user) if include_resources else {},
            "version": run(["plasmashell", "--version"]),
            "loaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def backup_dir(directory: int) -> int:
    try:
        os.mkdir(".dno-tool-kde-backups", 0o700, dir_fd=directory)
    except FileExistsError:
        pass
    descriptor = os.open(".dno-tool-kde-backups", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(descriptor)
        raise Failure("backup_permissions", "Каталог резервных копий принадлежит другому пользователю или имеет небезопасные права.")
    return descriptor


def backup_data(directory: int, backup_id: str, user: dict, path: str, identity: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", backup_id):
        raise Failure("backup", "Недопустимый идентификатор копии.")
    descriptor = os.open(backup_id + ".json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(MAX_FILE * 2)
    data = json.loads(raw)
    if data.get("uid") != user["uid"] or data.get("path") != path or data.get("identity") != identity:
        raise Failure("backup", "Копия относится к другому пользователю, хосту или файлу.")
    content = base64.b64decode(data["raw"], validate=True)
    if len(content) > MAX_FILE or (data["meta"]["exists"] and hashlib.sha256(content).hexdigest() != data["meta"]["fingerprint"]):
        raise Failure("backup", "Контрольная сумма копии не совпадает.")
    return data


def list_backups(request: dict, active: list[dict]) -> dict:
    user, directory, filename, env, note = context(request, active)
    parent = backups = None
    try:
        parent = open_dir(directory)
        try:
            backups = os.open(".dno-tool-kde-backups", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            return {"backups": []}
        result = []
        for name in os.listdir(backups)[:2048]:
            if not re.fullmatch(r"[0-9a-f]{32}\.json", name):
                continue
            try:
                data = backup_data(backups, name[:-5], user, os.path.join(directory, filename), str(request["identity"]))
                result.append({key: data[key] for key in ("id", "created_at", "path", "uid", "meta")})
            except (OSError, Failure, ValueError, KeyError):
                continue
        return {"backups": sorted(result, key=lambda item: item["created_at"], reverse=True)}
    except FileNotFoundError:
        return {"backups": []}
    finally:
        if backups is not None:
            os.close(backups)
        if parent is not None:
            os.close(parent)


def save(request: dict, active: list[dict]) -> dict:
    user, directory, filename, env, note = context(request, active)
    parent = open_dir(directory, True, user["uid"], user["gid"])
    backups = lock = None
    temporary = ".dno-kde-" + uuid.uuid4().hex
    committed = False
    try:
        backups = backup_dir(parent)
        lock = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=backups)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure("busy", "Другой редактор сохраняет настройки. Повторите позже.")
        current, meta = read_file(parent, filename)
        if meta["fingerprint"] != request["expected"]:
            raise Failure("conflict", "Файл на хосте изменён после загрузки. Сравните версии перед записью.")
        latest = snapshot(request, active, False)
        actual_policy = {source["path"]: source["fingerprint"] for source in latest["sources"]}
        if actual_policy != request["policies"]:
            raise Failure("conflict", "Источники системных настроек изменились. Перечитайте профиль.")
        desired_exists = True
        if request["operation"] == "restore":
            restored = backup_data(backups, str(request["backup_id"]), user, os.path.join(directory, filename), str(request["identity"]))
            desired = base64.b64decode(restored["raw"], validate=True)
            desired_exists = bool(restored["meta"]["exists"])
            desired_meta = restored["meta"]
        else:
            desired = base64.b64decode(request["raw"], validate=True)
            desired_meta = meta
        if len(desired) > MAX_FILE:
            raise Failure("size", "Файл превышает допустимые 2 МиБ.")
        desired.decode("utf-8-sig")
        same_metadata = not desired_exists or all(meta.get(key) == desired_meta.get(key) for key in ("uid", "gid", "mode", "xattrs"))
        if current == desired and meta["exists"] == desired_exists and same_metadata:
            return {"snapshot": latest, "backup_id": "", "unchanged": True}
        if meta["exists"] and os.geteuid() != 0 and (meta["uid"] != os.geteuid() or meta["gid"] not in (os.getegid(), *os.getgroups())):
            raise Failure("permission", "Для сохранения владельца файла необходимы права sudo.")
        backup_id = uuid.uuid4().hex
        record = {"id": backup_id, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "uid": user["uid"], "path": os.path.join(directory, filename), "identity": str(request["identity"]),
                  "raw": base64.b64encode(current).decode("ascii"), "meta": meta}
        descriptor = os.open(backup_id + ".json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=backups)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(record, ensure_ascii=True).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(backups)
        backup_data(backups, backup_id, user, os.path.join(directory, filename), str(request["identity"]))
        if desired_exists:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(desired)
                stream.flush()
                if os.geteuid() == 0:
                    os.fchown(stream.fileno(), desired_meta.get("uid", user["uid"]), desired_meta.get("gid", user["gid"]))
                os.fchmod(stream.fileno(), desired_meta.get("mode", 0o600))
                for name, value in desired_meta.get("xattrs", {}).items():
                    os.setxattr(stream.fileno(), name, base64.b64decode(value, validate=True))
                os.fsync(stream.fileno())
            written, unused_meta = read_file(parent, temporary)
            if written != desired:
                raise Failure("verify", "Проверка временного файла не пройдена.")
        again, check = read_file(parent, filename)
        if check != meta:
            raise Failure("conflict", "Файл изменён во время подготовки записи; исходник сохранён.")
        if desired_exists:
            os.replace(temporary, filename, src_dir_fd=parent, dst_dir_fd=parent)
        elif meta["exists"]:
            os.unlink(filename, dir_fd=parent)
        committed = True
        os.fsync(parent)
        verified, verified_meta = read_file(parent, filename)
        if verified != desired or verified_meta["exists"] != desired_exists:
            raise Failure("unknown", "Замена выполнена, но проверка результата не пройдена. Перечитайте файл; автоматический повтор запрещён.")
        if desired_exists and any(verified_meta[key] != desired_meta.get(key, {"uid": user["uid"], "gid": user["gid"], "mode": 0o600}[key]) for key in ("uid", "gid", "mode")):
            raise Failure("unknown", "Файл записан, но проверка владельца или прав не пройдена. Перечитайте профиль.")
        if desired_exists and verified_meta.get("xattrs", {}) != desired_meta.get("xattrs", {}):
            raise Failure("unknown", "Файл записан, но расширенные атрибуты отличаются. Перечитайте профиль.")
        result = snapshot(request, active, False)
        if result["meta"]["fingerprint"] != verified_meta["fingerprint"]:
            raise Failure("unknown", "Файл изменился сразу после записи; перечитайте и сравните версии.")
        history = list_backups(request, active)["backups"]
        for old in history[10:]:
            try:
                os.unlink(old["id"] + ".json", dir_fd=backups)
            except OSError:
                pass
        return {"snapshot": result, "backup_id": backup_id, "unchanged": False}
    except Failure:
        raise
    except BaseException as error:
        if committed:
            raise Failure("unknown", "Файл заменён, но подтверждение не получено. Перечитайте профиль перед повтором.") from error
        raise
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        if lock is not None:
            os.close(lock)
        if backups is not None:
            os.close(backups)
        os.close(parent)


def dispatch(request: dict) -> dict:
    active = sessions()
    operation = request["operation"]
    if operation == "discover":
        accounts = []
        minimum = 1000
        try:
            with open("/etc/login.defs", encoding="utf-8") as stream:
                match = re.search(r"^UID_MIN\s+(\d+)", stream.read(), re.MULTILINE)
                if match:
                    minimum = int(match.group(1))
        except OSError:
            pass
        for user in pwd.getpwall():
            if (user.pw_uid == 0 or minimum <= user.pw_uid < 65534) and not user.pw_shell.endswith(("nologin", "/false")):
                accounts.append(profile(user.pw_name, active))
        return {"profiles": sorted(accounts, key=lambda item: (item["uid"] == 0, not bool(item["sessions"]), item["username"])),
                "ssh_username": pwd.getpwuid(os.getuid()).pw_name, "version": run(["plasmashell", "--version"])}
    if operation == "load":
        return {"snapshot": snapshot(request, active)}
    if operation in ("save", "restore"):
        return save(request, active)
    if operation == "backups":
        return list_backups(request, active)
    if operation == "backup_content":
        user, directory, filename, env, note = context(request, active)
        parent = open_dir(directory)
        backups = None
        try:
            backups = os.open(".dno-tool-kde-backups", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            data = backup_data(backups, str(request["backup_id"]), user, os.path.join(directory, filename), str(request["identity"]))
            return {"raw": data["raw"], "exists": data["meta"]["exists"]}
        finally:
            if backups is not None:
                os.close(backups)
            os.close(parent)
    raise Failure("operation", "Неизвестная операция редактора KDE.")


def main() -> None:
    print("DNO_KDE_READY", flush=True)
    try:
        incoming = sys.stdin.buffer.read(MAX_FILE * 2)
        request = json.loads(incoming)
        data = dispatch(request)
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=True), flush=True)
    except Failure as error:
        print(json.dumps({"ok": False, "code": error.code, "message": str(error)}, ensure_ascii=True), flush=True)
    except PermissionError:
        print(json.dumps({"ok": False, "code": "permission", "message": "Недостаточно прав. Проверьте связку учётных данных и настройку sudo."}), flush=True)
    except OSError as error:
        message = "Ошибка доступа к файлу (код " + str(error.errno) + "). " + ("Ссылки и нештатные типы файлов не поддерживаются." if error.errno in (errno.ELOOP, errno.ENOTDIR) else str(error.strerror))
        print(json.dumps({"ok": False, "code": "filesystem", "message": message}), flush=True)
    except Exception:
        print(json.dumps({"ok": False, "code": "protocol", "message": "Не удалось обработать конфигурацию. Проверьте Python 3 и формат файла на хосте."}), flush=True)


if __name__ == "__main__":
    main()
"""


def build_remote_command(
    use_sudo: bool, prompt: str, script: str = REMOTE_SCRIPT
) -> str:
    """Найти совместимый Python, не полагаясь на конкретный алиас в PATH."""
    version_check = shlex.quote(
        "import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)"
    )
    body = (
        "for dno_python in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3.7 /usr/bin/python3 python; do "
        'if command -v "$dno_python" >/dev/null 2>&1 && '
        f'"$dno_python" -c {version_check} >/dev/null 2>&1; then '
        f'exec "$dno_python" -c {shlex.quote(script)}; fi; done; '
        "printf '%s\\n' 'DNO_KDE_PYTHON_MISSING: Python 3.7 or newer was not found' >&2; exit 127"
    )
    command = "sh -c " + shlex.quote(body)
    return (
        "sudo -S -p " + shlex.quote(prompt) + " -- " + command if use_sudo else command
    )
