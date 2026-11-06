"""Блокировка скомпрометированной копии программы (без Qt).

Ставится при очистке ноды (kill-switch) до удаления данных. Отметка
хранится в нескольких местах вне ``~/.dno-tool``, поэтому переживает
очистку, переустановку и восстановление профиля из копии:

- Windows: реестр ``HKCU\\Software\\dno-tool`` и ``%LOCALAPPDATA%``;
- POSIX: ``$XDG_STATE_HOME`` (``~/.local/state``) и ``$XDG_CONFIG_HOME``
  (``~/.config``).

Найденная хотя бы в одном месте отметка (даже повреждённая) означает
блокировку. Снять её можно только служебным мастер-ключом (``lift_ban``);
в сборке без открытой части ключа блокировка программой не снимается.
Заблокированная копия не управляет хостами и нодами: запрещены SSH
(команды, терминал, файлы, питание, сведения о хосте), локальные команды,
функции Veyon, подключение к другим нодам и выполнение команд по их
запросам.

Результат проверки кэшируется на время процесса: блокировка, поставленная
в этом процессе, действует сразу, даже если записать отметку не удалось.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from src.logger import logger

BAN_MESSAGE = (
    "Эта копия программы заблокирована после очистки ноды: выполнение команд "
    "и подключение к нодам запрещены. Снять блокировку можно только "
    "служебным ключом."
)

_REG_PATH = r"Software\dno-tool"
_REG_VALUE = "InstancePolicy"
_FILE_NAME = "instance-policy.json"

_lock = threading.Lock()
_banned: bool | None = None


class BannedError(RuntimeError):
    """Эта копия программы заблокирована."""

    def __init__(self) -> None:
        super().__init__(BAN_MESSAGE)


def _file_markers() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return [Path(local) / "dno-tool-policy" / _FILE_NAME]
    state = os.environ.get("XDG_STATE_HOME") or str(home / ".local" / "state")
    conf = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return [
        Path(state) / "dno-tool-policy" / _FILE_NAME,
        Path(conf) / "dno-tool-policy" / _FILE_NAME,
    ]


def _registry_marker_exists() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH) as key:
            winreg.QueryValueEx(key, _REG_VALUE)
            return True
    except OSError:
        return False


def _markers_exist() -> bool:
    if _registry_marker_exists():
        return True
    return any(path.exists() for path in _file_markers())


def is_banned() -> bool:
    """True, если копия программы заблокирована."""
    global _banned
    value = _banned
    if value is None:
        with _lock:
            if _banned is None:
                try:
                    _banned = _markers_exist()
                except Exception:
                    # Сбой чтения не должен молча снимать блокировку: этот вызов
                    # отказывает, следующий прочитает отметки заново.
                    logger.warning("Блокировка: не удалось прочитать отметки")
                    return True
                if _banned:
                    logger.warning("Блокировка: копия программы заблокирована")
            value = _banned
    return value


def ensure_not_banned() -> None:
    """Бросить BannedError, если копия программы заблокирована."""
    if is_banned():
        raise BannedError()


def ban_instance() -> bool:
    """Заблокировать копию сразу и записать отметки; True, если записана хотя бы одна."""
    global _banned
    with _lock:
        _banned = True
    payload = json.dumps(
        {"blocked": True, "since": datetime.now(timezone.utc).isoformat()}
    )
    written = 0
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_PATH) as key:
                winreg.SetValueEx(key, _REG_VALUE, 0, winreg.REG_SZ, payload)
            written += 1
        except OSError:
            logger.warning("Блокировка: не удалось записать отметку в реестр")
    for path in _file_markers():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
            written += 1
        except OSError:
            logger.warning("Блокировка: не удалось записать файл отметки")
    logger.warning("Блокировка: копия программы заблокирована")
    return written > 0


def lift_ban(master_key_text: str) -> tuple[bool, str]:
    """Снять блокировку, если передан настоящий служебный мастер-ключ."""
    from .node_master import load_master_key

    try:
        load_master_key(master_key_text)
    except ValueError as error:
        return False, str(error)
    return remove_ban()


def remove_ban() -> tuple[bool, str]:
    """Удалить отметки блокировки.

    Вызывать только после проверки служебного ключа: локально — через
    ``lift_ban``, на сервере нод — из сессии, открытой мастер-ключом.
    """
    global _banned
    failed = False
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _REG_PATH, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, _REG_VALUE)
        except FileNotFoundError:
            pass
        except OSError:
            failed = True
    for path in _file_markers():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            failed = True
    if failed or _markers_exist():
        return False, "Не удалось удалить отметку блокировки"
    with _lock:
        _banned = False
    logger.warning("Блокировка: снята служебным ключом")
    return True, "Блокировка снята"


_guard_installed = False


def install_ssh_guard() -> None:
    """Проверять блокировку перед любым SSH-подключением процесса.

    SSH открывается во многих местах (команды, терминал, SFTP, питание,
    сведения о хосте, мастер настройки), поэтому проверка ставится на
    ``paramiko.SSHClient.connect`` один раз.
    """
    global _guard_installed
    with _lock:
        if _guard_installed:
            return
        _guard_installed = True
    import paramiko

    original = paramiko.SSHClient.connect

    def connect(self, *args, **kwargs):
        ensure_not_banned()
        return original(self, *args, **kwargs)

    connect.__wrapped__ = original
    paramiko.SSHClient.connect = connect


def _reset_for_tests() -> None:
    """Сбросить кэш проверки (тест-хук)."""
    global _banned
    with _lock:
        _banned = None
