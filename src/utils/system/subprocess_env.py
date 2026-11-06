"""Построение чистого окружения для subprocess-вызовов системных программ.

PyInstaller frozen-бинарь устанавливает в ``os.environ`` утечки
(``LD_LIBRARY_PATH``, ``_MEIPASS``, ``PYTHONHOME``, ``QT_PLUGIN_PATH``, ...),
которые ломают внешние программы на платформе с отличающимся
glibc/libstdc++: загрузчик находит в ``LD_LIBRARY_PATH``
libpython/libstdc++ из сборочной системы и падает с
``GLIBCXX_3.4.x not found`` или ``GLIBC_2.x not found``.
Qt-переменные PySide6 заставляют внешние программы (со своим Qt)
грузить несовместимый плагин offscreen из PySide6-бандла.

Поэтому для системных команд (dnf, rpm, systemctl, veyon-cli) на Linux
строим чистую копию окружения без этих переменных.

На Windows не фильтруем: PyInstaller использует SetDllDirectory,
а не LD_LIBRARY_PATH, а очистка ломает поиск veyon-wcli.exe/DLL
(нужны ProgramFiles, PROGRAMDATA, SystemRoot, PATHEXT).

Дополнительно: приложение запускается через ``pkexec`` от ``root``,
но некоторые внешние программы (VLC) отказываются работать от ``root``.
``wrap_as_user`` оборачивает команду в ``runuser -u <user> --``,
определяя исходного пользователя через ``PKEXEC_UID``/``SUDO_UID``.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Dict


_ENV_LEAKS = (
    # ── PyInstaller / Python утечки ──
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "_MEIPASS",
    "_MEIPASS2",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONOPTIMIZE",
    # ── Qt-утечки PySide6/PyInstaller ──
    # Внешние программы с собственным Qt (veyon-cli) грузят несовместимые
    # плагины из PySide6-бандла → Could not load Qt platform plugin.
    "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
    "QT_QPA_PLATFORM",
    "QT_DEBUG_PLUGINS",
    "QT_LINUX_ACCESSIBILITY_ALWAYS_ON",
    "PYSIDE_DESIGNER_PLUGINS",
)


def build_clean_env(
    headless: bool = False,
) -> Optional[Dict[str, str]]:
    """Построить окружение без PyInstaller/PySide6-утечек.

    Args:
        headless: Если True, после очистки устанавливает
            ``QT_QPA_PLATFORM=offscreen`` — для headless-вызовов
            (veyon-cli без GUI). Программа грузит offscreen из своих
            системных плагинов, совместимых с её Qt.

    Returns:
        Очищенная копия ``os.environ`` на Linux, или ``None`` на Windows
        (где очистка не требуется и может навредить).
    """
    if sys.platform == "win32":
        return None

    env = dict(os.environ)
    for key in _ENV_LEAKS:
        env.pop(key, None)

    if headless:
        env["QT_QPA_PLATFORM"] = "offscreen"

    return env


def clean_env_kwargs() -> Dict[str, Dict[str, str]]:
    """Kwargs для subprocess с чистым окружением (или пустые на Windows).

    Сахар над ``build_clean_env``: на Linux возвращает
    ``{"env": <чистое окружение>}``, на Windows - ``{}`` (очистка там
    не требуется и ``build_clean_env`` возвращает ``None``).
    Предназначен для распаковки: ``subprocess.run(cmd, **clean_env_kwargs())``.
    """
    env = build_clean_env()
    return {"env": env} if env is not None else {}


def _original_username() -> Optional[str]:
    """Определить имя исходного пользователя от имени которого запущено приложение.

    Приложение работает от ``root`` (через pkexec/sudo), но ``PKEXEC_UID``
    или ``SUDO_UID`` содержат UID исходного пользователя.

    Returns:
        Имя пользователя, или ``None`` если приложение не от ``root``
        или переменные окружения отсутствуют (dev-режим, Windows).
    """
    if sys.platform == "win32":
        return None
    try:
        if os.geteuid() != 0:
            return None
    except AttributeError:
        return None
    uid_str = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    if not uid_str:
        return None
    try:
        uid = int(uid_str)
    except ValueError:
        return None
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        return None


def is_running_elevated() -> bool:
    """True если приложение запущено с правами администратора/root.

    - Linux/macOS: ``os.geteuid() == 0``
    - Windows: ``ctypes.windll.shell32.IsUserAnAdmin()``
    """
    if sys.platform == "win32":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def wrap_as_user(cmd: List[str]) -> List[str]:
    """Обернуть команду в ``runuser -u <user> --`` если приложение от root.

    Если приложение запущено не от ``root`` (dev-режим, Windows) или
    исходный пользователь не определён — возвращает ``cmd`` без изменений.

    Args:
        cmd: Команда для запуска (например ``['vlc', 'rtsp://...']``).

    Returns:
        Команда с префиксом ``runuser`` или исходная ``cmd``.
    """
    user = _original_username()
    if not user:
        return cmd
    # ``runuser`` в /sbin (или /usr/sbin на некоторых дистрибутивах).
    # В окружении pkexec /sbin может отсутствовать в PATH, поэтому
    # используем абсолютный путь.
    for candidate in ("/sbin/runuser", "/usr/sbin/runuser"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return [candidate, "-u", user, "--"] + cmd
    # Fallback: надеемся что runuser в PATH
    return ["runuser", "-u", user, "--"] + cmd


def _runuser_path() -> str:
    """Найти абсолютный путь к ``runuser`` (в /sbin или /usr/sbin)."""
    for candidate in ("/sbin/runuser", "/usr/sbin/runuser"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "runuser"


def safe_cwd_for_user() -> str:
    """Безопасный cwd для Popen-старта GUI-программ при pkexec-root.

    ``runuser -u <user> --`` не меняет cwd: процесс от имени пользователя
    наследует cwd root-процесса (из меню это обычно ``/root``), куда
    пользователю нет доступа. Konsole делает chdir в cwd до exec и падает
    с ``chdir: Permission denied`` + ``Невозможно запустить программу``.

    Returns:
        Путь к HOME целевого пользователя (через ``pwd``, не expanduser
        от root), либо ``/tmp``, либо ``/``. Не бросает исключений.
    """
    candidates: List[str] = []
    if sys.platform != "win32":
        try:
            import pwd

            uid_str = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
            if uid_str:
                try:
                    candidates.append(pwd.getpwuid(int(uid_str)).pw_dir)
                except (ValueError, KeyError):
                    pass
            user = _original_username()
            if user:
                try:
                    candidates.append(pwd.getpwnam(user).pw_dir)
                except KeyError:
                    pass
        except ImportError:
            pass
        try:
            candidates.append(os.path.expanduser("~"))
        except Exception:
            pass
        home_env = os.environ.get("HOME")
        if home_env:
            candidates.append(home_env)
        candidates.extend(["/tmp", "/"])
    else:
        try:
            candidates.append(os.getcwd())
        except Exception:
            pass
        try:
            candidates.append(os.path.expanduser("~"))
        except Exception:
            pass
    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            if os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK):
                return path
        except Exception:
            continue
    return "/tmp" if sys.platform != "win32" else os.path.expanduser("~")


def open_url_in_browser(url: str) -> bool:
    """Открыть URL в браузере по умолчанию, вернуть реальный успех.

    ``webbrowser.open`` на Linux от root (pkexec/sudo-запуск приложения)
    ненадёжен: PyInstaller экспортирует ``LD_LIBRARY_PATH`` во временный
    каталог - браузер/xdg-open подтягивает бандловскую libstdc++ и молча
    падает. Поэтому на elevated-Linux запуск идёт через ``runuser``
    (обёртка ``wrap_as_user``) с чистым окружением ``build_clean_env``:
    сначала ``xdg-open`` (стандартный хэндлер), fallback ``gio open``.

    Windows и не-elevated Linux - обычный ``webbrowser.open``.

    Args:
        url: Полный URL (например ``http://localhost:8765/vnc.html?...``).

    Returns:
        True при успехе запуска; False при ошибке (в ошибку не упасть,
        исключения глотаются - вызывающий код сам показывает сообщение).
    """
    if sys.platform == "win32":
        try:
            import webbrowser

            return webbrowser.open(url)
        except Exception:
            return False

    try:
        import webbrowser

        if not is_running_elevated():
            return bool(webbrowser.open(url))
    except Exception:
        pass

    # Elevated-Linux: от имени исходного пользователя с чистым окружением.
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    clean_env = build_clean_env()
    if clean_env is not None:
        kwargs["env"] = clean_env
    for opener in ("xdg-open", "gio"):
        try:
            subprocess.Popen(
                wrap_as_user(
                    [opener, "open", url] if opener == "gio" else [opener, url]
                ),
                **kwargs,
            )
            return True
        except Exception:
            continue
    # Последний шанс - штатный webbrowser (может и сработать).
    try:
        import webbrowser

        return bool(webbrowser.open(url))
    except Exception:
        return False


def run_elevated(cmd: List[str], timeout: float = 120.0) -> "tuple[bool, str]":
    """Выполнить команду с повышением прав (UAC на Windows / pkexec на Linux).

    Если приложение уже запущено от администратора/root — обычный
    ``subprocess.run`` без диалога авторизации.

    Windows (не elevated): ``ShellExecuteExW`` с ``lpVerb="runas"`` и
    ``SEE_MASK_NOCLOSEPROCESS`` — показывает системный диалог UAC и ждёт
    завершения процесса. Отмена UAC (GetLastError()==1223) не считается
    ошибкой запуска — возвращается ``False`` с пояснением.

    Linux (не root): ``pkexec`` — показывает polkit-диалог. Exit-коды
    pkexec: 126 = авторизация отклонена, 127 = агент не найден.

    Args:
        cmd: Команда с аргументами (``['veyon-cli', 'service', 'restart']``).
        timeout: Ожидание завершения процесса (актуально для Windows-пути).

    Returns:
        ``(ok, err)``: ok=True — команда выполнена (exit 0); err — текст
        ошибки для показа пользователю ('' при успехе).
    """
    if is_running_elevated():
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                ),
            )
        except FileNotFoundError:
            return False, f"Команда не найдена: {cmd[0]}"
        except Exception as e:
            return False, str(e)
        if result.returncode == 0:
            return True, ""
        err = (
            (result.stderr or b"").decode("utf-8", errors="replace").strip()
            if isinstance(result.stderr, bytes)
            else str(result.stderr or "").strip()
        )
        return False, err or f"Команда завершилась с кодом {result.returncode}"

    if sys.platform == "win32":
        return _run_elevated_windows(cmd, timeout)
    return _run_elevated_pkexec(cmd, timeout)


def _run_elevated_windows(cmd: List[str], timeout: float) -> "tuple[bool, str]":
    """Windows: UAC-элевация через ShellExecuteExW + ожидание завершения."""
    import ctypes
    from ctypes import wintypes

    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0
    WAIT_TIMEOUT_MS = 300
    ERROR_CANCELLED = 1223

    params = " ".join(f'"{a}"' if " " in a else a for a in cmd[1:])
    sei = _SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.hwnd = None
    sei.lpVerb = "runas"
    sei.lpFile = cmd[0]
    sei.lpParameters = params or None
    sei.lpDirectory = None
    sei.nShow = SW_HIDE

    try:
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
            rc = ctypes.GetLastError()
            if rc == ERROR_CANCELLED:
                return False, "Запрос UAC отклонён"
            return False, f"Не удалось запустить с правами администратора (код {rc})"
    except Exception as e:
        return False, str(e)

    # Ждём завершения процесса (пользователь мог не сразу подтвердить UAC)
    import time as _time

    deadline = _time.monotonic() + timeout
    INFINITE = 0xFFFFFFFF
    while _time.monotonic() < deadline:
        rc = ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, WAIT_TIMEOUT_MS)
        if rc == 0:  # WAIT_OBJECT_0 — процесс завершился
            # Читаем exit-код ДО CloseHandle: повышенный процесс не даёт
            # stdout/stderr, но exit-код доступен — реальный провал
            # (неверные аргументы, отказ доступа) не должен выглядеть успехом.
            exit_code = wintypes.DWORD(0)
            if ctypes.windll.kernel32.GetExitCodeProcess(
                sei.hProcess, ctypes.byref(exit_code)
            ):
                code = exit_code.value
                # DWORD → signed (veyon-cli возвращает -1 как 0xFFFFFFFF)
                if code >= 0x80000000:
                    code -= 0x100000000
                ctypes.windll.kernel32.CloseHandle(sei.hProcess)
                if code == 0:
                    return True, ""
                return (
                    False,
                    f"Команда завершилась с кодом {code} "
                    f"(вывод повышенного процесса недоступен)",
                )
            # Exit-код нечитаем (редко) — прежнее lenient-поведение.
            ctypes.windll.kernel32.CloseHandle(sei.hProcess)
            return True, ""
        if rc == 0x80:  # WAIT_ABANDONED — хендл потерял смысл
            ctypes.windll.kernel32.CloseHandle(sei.hProcess)
            return False, "Ожидание процесса прервано"
    ctypes.windll.kernel32.TerminateProcess(sei.hProcess, 1)
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return False, f"Превышено время ожидания ({int(timeout)} сек)"


def _run_elevated_pkexec(cmd: List[str], timeout: float) -> "tuple[bool, str]":
    """Linux: pkexec-элевация (polkit-диалог авторизации)."""
    full = ["pkexec"] + cmd
    try:
        result = subprocess.run(
            full,
            capture_output=True,
            timeout=timeout,
            env=build_clean_env(),
        )
    except FileNotFoundError:
        return False, "pkexec не найден в системе"
    except subprocess.TimeoutExpired:
        return False, f"Превышено время ожидания ({int(timeout)} сек)"
    except Exception as e:
        return False, str(e)

    rc = result.returncode
    if rc == 0:
        return True, ""
    err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    if rc == 126:
        return False, "Авторизация отклонена"
    if rc == 127:
        return False, "Не удалось запустить диалог авторизации (polkit-агент не найден)"
    return False, err or f"Команда завершилась с кодом {rc}"


def ensure_dir_as_user(path: Path) -> bool:
    """Создать каталог от имени исходного пользователя (не root).

    При запуске через pkexec от root, ``mkdir`` выполняется через
    ``runuser -u <user> -- mkdir -p``, чтобы каталог принадлежал
    исходному пользователю, который сможет работать с созданными файлами.

    В dev-режиме (не root) используется обычный ``os.makedirs``.

    Args:
        path: Каталог для создания (с родителями).

    Returns:
        True если каталог существует и доступен для записи.
    """
    try:
        path = Path(path)
    except TypeError:
        return False
    user = _original_username()
    if not user:
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path.is_dir()
        except Exception:
            return False
    try:
        subprocess.run(
            [_runuser_path(), "-u", user, "--", "mkdir", "-p", str(path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return path.is_dir()
    except Exception:
        return False
