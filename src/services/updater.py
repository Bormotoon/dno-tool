"""
Модуль обновления DNO Tool.

Проверяет наличие новых версий на GitHub Releases и загружает архив
с бинарником в каталог загрузок пользователя.
Для доступа к приватному репозиторию можно задать GITHUB_TOKEN_READ
в переменных окружения (необязательно для публичного репозитория).
"""

import os
import sys
import json
import shutil
import platform
import subprocess
import ssl
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional, Any

from src.utils.network.https import create_https_context

try:
    from src.logger import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

REPO = "xp9k/dno-tool"
COMMANDS_REPO = "xp9k/dno-tool-commands"
COMMANDS_DOWNLOAD_URL = f"https://github.com/{COMMANDS_REPO}"
GITHUB_API = "https://api.github.com"

FALLBACK_UPDATE_URL = "https://syno.xp9k.ru/d/s/17AbafYwPMRUXfKJca9f6VZDYdH7Nygg/P4VP6sAtFMohJRFQ-_1ZjLgQL_qCLa6E-obXgVQySHg0"

GITHUB_TOKEN_READ = os.environ.get("GITHUB_TOKEN_READ", "")

try:
    from src import __version__

    CURRENT_VERSION = __version__
except ImportError:
    CURRENT_VERSION = "0.0.0"


def is_installed_as_package() -> bool:
    """Определить, установлено ли приложение как системный пакет (RPM/dnf)."""
    if platform.system() != "Linux":
        return False
    try:
        exe_path = (
            os.readlink("/proc/self/exe")
            if os.path.islink("/proc/self/exe")
            else os.path.abspath(sys.argv[0])
        )
    except OSError:
        exe_path = os.path.abspath(sys.argv[0])
    try:
        result = subprocess.run(
            ["rpm", "-qf", exe_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _get_headers(token: Optional[str] = None) -> Dict[str, str]:
    """Сформировать заголовки HTTP-запроса к GitHub API."""
    headers = {
        "User-Agent": "dno-tool-updater",
        "Accept": "application/vnd.github.v3+json",
    }
    if token is None:
        token = GITHUB_TOKEN_READ
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def _fetch_json(url: str, token: Optional[str] = None) -> Any:
    """
    Выполнить GET-запрос и вернуть распарсенный JSON.

    Raises:
        PermissionError: Неверный токен (HTTP 401).
        FileNotFoundError: Релиз не найден (HTTP 404).
        ConnectionError: Сетевая ошибка.
    """
    import urllib.request
    import urllib.error

    headers = _get_headers(token)
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=15, context=create_https_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise PermissionError("Invalid GitHub token.")
        if e.code == 404:
            raise FileNotFoundError("Release not found.")
        raise
    except urllib.error.URLError as e:
        raise ConnectionError(f"Network error: {e.reason}")


def _check_fallback_available(
    url: str = FALLBACK_UPDATE_URL, timeout: int = 10
) -> bool:
    """
    Проверить доступность резервного источника обновлений (HEAD-запрос).

    Args:
        url: URL резервного источника (по умолчанию Synology Drive Sharing).
        timeout: Таймаут запроса в секундах.

    Returns:
        ``True`` если сервер ответил HTTP 200, иначе ``False``.
    """
    import urllib.request
    import urllib.error

    req = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "dno-tool-updater"}
    )
    try:
        with urllib.request.urlopen(
            req, timeout=timeout, context=create_https_context()
        ) as resp:
            return getattr(resp, "status", resp.getcode()) == 200
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        ConnectionError,
        OSError,
        ValueError,
    ):
        return False


def _download_file(url: str, dest: str, token: Optional[str] = None) -> None:
    """Скачать файл по URL и сохранить по пути ``dest``."""
    import urllib.request

    headers = _get_headers(token)
    headers["Accept"] = "application/octet-stream"
    req = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(req, timeout=120, context=create_https_context()) as resp:
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)

    from src.utils.filesystem.fs_utils import ensure_user_owned

    ensure_user_owned(dest)


def download_commands_json(token: Optional[str] = None) -> Dict[str, Any]:
    """
    Скачать ``commands.json`` из репозитория на GitHub.

    Returns:
        Словарь с ключами: ``success`` (bool), ``message`` (str),
        ``data`` (list | None), ``error`` (str | None).
    """
    try:
        url = f"{GITHUB_API}/repos/{COMMANDS_REPO}/contents/commands.json"
        headers = _get_headers(token)
        headers["Accept"] = "application/vnd.github.v3.raw"

        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=create_https_context()) as resp:
            raw = resp.read().decode("utf-8")

        data = json.loads(raw)

        return {
            "success": True,
            "message": "Команды загружены с сервера",
            "data": data,
            "error": None,
        }

    except (urllib.error.URLError, ssl.SSLError) as e:
        reason = getattr(e, "reason", e)
        certificate_error = isinstance(reason, ssl.SSLCertVerificationError) or (
            isinstance(reason, ssl.SSLError)
            and reason.reason == "CERTIFICATE_VERIFY_FAILED"
        )
        if certificate_error:
            return {
                "success": False,
                "message": (
                    "Не удалось подтвердить подлинность сервера загрузки (HTTPS).\n\n"
                    "Возможные причины: неверные дата и время на компьютере, "
                    "устаревшие корневые сертификаты, проверка HTTPS антивирусом "
                    "или прокси организации, либо проблема сертификата сервера.\n\n"
                    "Проверьте дату и время, обновите системные сертификаты. "
                    "В сети организации обратитесь к администратору для проверки "
                    "доверия к сертификату прокси.\n\n"
                    "Можно скачать commands.json через браузер со страницы загрузки "
                    "и выбрать «Загрузить из файла». Если браузер тоже сообщает "
                    "об ошибке сертификата, обратитесь к администратору."
                ),
                "data": None,
                "error": str(e),
                "error_code": "certificate_verify_failed",
                "manual_url": COMMANDS_DOWNLOAD_URL,
            }
        return {
            "success": False,
            "message": f"Ошибка загрузки команд: {e}",
            "data": None,
            "error": str(e),
        }
    except PermissionError as e:
        return {
            "success": False,
            "message": f"Ошибка авторизации: {e}",
            "data": None,
            "error": str(e),
        }
    except ConnectionError as e:
        return {
            "success": False,
            "message": f"Сетевая ошибка: {e}",
            "data": None,
            "error": str(e),
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Ошибка загрузки команд: {e}",
            "data": None,
            "error": str(e),
        }


def _is_version_tag(tag: str) -> bool:
    """Проверить, является ли тег версией (формат ``vX.Y.Z`` или ``X.Y.Z``)."""
    s = tag.lstrip("v")
    parts = s.split(".")
    return len(parts) >= 2 and all(p.isdigit() for p in parts)


def get_version_releases(token: Optional[str] = None) -> List[Dict[str, Any]]:
    """Получить список релизов, теги которых являются версиями."""
    releases = get_all_releases(token)
    return [r for r in releases if _is_version_tag(r.get("tag_name", ""))]


def get_latest_release(token: Optional[str] = None) -> Dict[str, Any]:
    """
    Получить последний версионируемый релиз.

    Raises:
        FileNotFoundError: Если версионируемых релизов не найдено.
    """
    url = f"{GITHUB_API}/repos/{REPO}/releases/latest"
    release = _fetch_json(url, token)
    tag = release.get("tag_name", "")
    if not _is_version_tag(tag):
        version_releases = get_version_releases(token)
        if not version_releases:
            raise FileNotFoundError("No versioned releases found.")
        version_releases.sort(
            key=lambda r: [int(x) for x in r["tag_name"].lstrip("v").split(".")]
        )
        release = version_releases[-1]
    return release


def get_all_releases(token: Optional[str] = None) -> List[Dict[str, Any]]:
    """Получить все релизы из GitHub API."""
    url = f"{GITHUB_API}/repos/{REPO}/releases"
    return _fetch_json(url, token)


def check_for_update(token: Optional[str] = None) -> Dict[str, Any]:
    """
    Проверить наличие обновления.

    Выполняет основную проверку через GitHub Releases репозитория приложения.
    Если GitHub недоступен (закрытый/удалённый репозиторий, сетевая ошибка),
    включается двойной fallback:

    1. Проверяется доступность резервного источника (Synology Drive Sharing).
       Если доступен — в поле ``fallback_url`` возвращается ссылка для ручной
       загрузки, поле ``error`` остаётся ``None``.
    2. Если резервный источник тоже недоступен — ``error`` остаётся ``None``,
       ``update_available`` остаётся ``False`` (молчание).

    Returns:
        Словарь с ключами: ``update_available`` (bool), ``current_version`` (str),
        ``latest_version`` (str), ``latest_tag`` (str), ``release_notes`` (str),
        ``fallback_url`` (str | None), ``error`` (str | None).
    """
    result = {
        "update_available": False,
        "current_version": CURRENT_VERSION,
        "latest_version": CURRENT_VERSION,
        "latest_tag": "",
        "release_notes": "",
        "fallback_url": None,
        "error": None,
    }

    try:
        release = get_latest_release(token)
        tag = release.get("tag_name", "")
        latest_version = tag.lstrip("v")
        result["latest_tag"] = tag
        result["latest_version"] = latest_version
        result["release_notes"] = release.get("body", "")

        current_parts = [int(x) for x in CURRENT_VERSION.split(".")]
        latest_parts = [int(x) for x in latest_version.split(".")]

        if latest_parts > current_parts:
            result["update_available"] = True

    except (PermissionError, FileNotFoundError, ConnectionError, Exception):
        # GitHub недоступен — тихий fallback на резервный источник.
        if _check_fallback_available():
            result["fallback_url"] = FALLBACK_UPDATE_URL
        # Если резервный источник тоже недоступен — молчим (error=None).

    return result


def _get_downloads_dir() -> Path:
    """Определить путь к каталогу загрузок пользователя."""
    if platform.system() == "Windows":
        downloads = Path(os.environ.get("USERPROFILE", "")) / "Downloads"
    else:
        xdg = os.environ.get("XDG_DOWNLOAD_DIR", "")
        if xdg and Path(xdg).is_dir():
            downloads = Path(xdg)
        else:
            downloads = Path.home() / "Downloads"
    if not downloads.is_dir():
        downloads.mkdir(parents=True, exist_ok=True)
        from src.utils.filesystem.fs_utils import ensure_user_owned

        ensure_user_owned(str(downloads))
    return downloads


def update(token: Optional[str] = None) -> Dict[str, Any]:
    """
    Скачать архив последнего релиза в каталог загрузок.

    Returns:
        Словарь с ключами: ``success`` (bool), ``message`` (str),
        ``download_dir`` (str), ``new_version`` (str), ``error`` (str | None).
    """
    system = platform.system()

    if system == "Linux":
        os_name = "mos"
    elif system == "Windows":
        os_name = "windows"
    else:
        return {
            "success": False,
            "message": f"Unsupported OS: {system}",
            "download_dir": "",
            "new_version": CURRENT_VERSION,
            "error": f"Unsupported OS: {system}",
        }

    try:
        release = get_latest_release(token)
        tag = release.get("tag_name", "")
        latest_version = tag.lstrip("v")
        archive_name = f"dno-tool-{latest_version}-{os_name}.zip"

        asset = None
        for a in release.get("assets", []):
            if a.get("name") == archive_name:
                asset = a
                break

        if not asset:
            return {
                "success": False,
                "message": f"Archive {archive_name} not found in release.",
                "download_dir": "",
                "new_version": latest_version,
                "error": f"Archive {archive_name} not found.",
            }

        download_url = asset.get("url") or asset.get("browser_download_url")
        dest_dir = _get_downloads_dir()
        dest_path = dest_dir / archive_name

        logger.info(f"Downloading {archive_name} to {dest_path}...")
        _download_file(download_url, str(dest_path), token)

        return {
            "success": True,
            "message": f"Archive saved to {dest_path}",
            "download_dir": str(dest_dir),
            "new_version": latest_version,
            "error": None,
        }

    except PermissionError as e:
        return {
            "success": False,
            "message": f"Auth error: {e}",
            "download_dir": "",
            "new_version": CURRENT_VERSION,
            "error": str(e),
        }
    except ConnectionError as e:
        return {
            "success": False,
            "message": f"Network error: {e}",
            "download_dir": "",
            "new_version": CURRENT_VERSION,
            "error": str(e),
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Download failed: {e}",
            "download_dir": "",
            "new_version": CURRENT_VERSION,
            "error": str(e),
        }


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="dno-tool updater")
    subparsers = parser.add_subparsers(dest="command")

    check_p = subparsers.add_parser("check", help="Check for updates")
    update_p = subparsers.add_parser("update", help="Install latest version")

    args = parser.parse_args()

    if args.command == "check":
        result = check_for_update()
        if result["error"]:
            print(f"Error: {result['error']}")
            sys.exit(1)
        if result["update_available"]:
            print(
                f"Update available: {result['current_version']} -> {result['latest_version']}"
            )
            if result["release_notes"]:
                print(f"\nRelease notes:\n{result['release_notes']}")
        else:
            print(f"You are up to date ({result['current_version']})")

    elif args.command == "update":
        result = update()
        print(result["message"])
        if not result["success"]:
            sys.exit(1)

    else:
        parser.print_help()
