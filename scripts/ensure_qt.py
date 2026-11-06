"""Проверить согласованность Qt перед сборкой и при необходимости обновить PySide6.

Ограничение версии берётся из requirements.txt. Проверка DLL выполняется
в отдельном процессе: обновление pip не должно менять загруженные библиотеки.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

QT_PACKAGES = ("PySide6", "shiboken6")
SPLIT_QT_PACKAGES = ("PySide6_Essentials", "PySide6_Addons")
PROBE = """
import json
import PySide6
from PySide6.QtCore import qVersion
from PySide6.QtNetwork import QTcpSocket
from PySide6.QtMultimedia import QMediaPlayer, QPlaybackOptions
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtQml import QQmlComponent
from PySide6.QtQuick import QSGRendererInterface
from PySide6.QtQuickWidgets import QQuickWidget
print(json.dumps({'pyside': PySide6.__version__, 'qt': qVersion()}))
"""


def release_tuple(value: str) -> tuple[int, ...]:
    """Принять только стабильную версию, дополняя отсутствующий patch нулём."""
    if re.fullmatch(r"\d+(?:\.\d+){1,3}", value) is None:
        raise ValueError("Требуется стабильная версия Qt")
    parts = tuple(int(part) for part in value.split("."))
    return parts + (0,) * max(0, 3 - len(parts))


def requirement(path: Path) -> tuple[str, tuple[int, ...]]:
    """Прочитать минимальную версию проекта без импорта PySide6."""
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"(PySide6>=([\d.]+))\s*(?:#.*)?", line)
        if match:
            return match[1], release_tuple(match[2])
    raise ValueError("В requirements.txt ожидается PySide6>=минимум")


def check(low: tuple[int, ...]) -> str | None:
    """Вернуть причину несовместимости пакетов или загружаемой библиотеки Qt."""
    versions = {}
    try:
        requirements = metadata.requires("PySide6") or []
    except metadata.PackageNotFoundError:
        return "Не установлен PySide6"
    # Сборка из исходников может создавать единый PySide6 wheel без split-пакетов.
    packages = list(QT_PACKAGES)
    for name in SPLIT_QT_PACKAGES:
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            normalized = name.lower().replace("_", "-")
            if any(re.split(r"[\s<>=!~;\[]", item, maxsplit=1)[0].lower().replace("_", "-") == normalized for item in requirements):
                return f"Не установлен обязательный {name}"
        else:
            packages.append(name)
    for name in packages:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            return f"Не установлен {name}"
    try:
        if not all(low <= release_tuple(value) for value in versions.values()):
            return "Версия Qt ниже требуемого минимума: " + str(versions)
    except ValueError as error:
        return str(error)
    if len(set(versions.values())) != 1:
        return "Версии PySide6, Addons, Essentials и shiboken6 не совпадают: " + str(versions)
    try:
        result = subprocess.run(
            [sys.executable, "-c", PROBE], capture_output=True, text=True,
            timeout=30, check=False,
        )
        if result.returncode:
            return "Не загружаются модули Qt Multimedia/Widgets/QML/Quick: " + result.stderr.strip()
        loaded = json.loads(result.stdout)
        if loaded["pyside"] != versions["PySide6"] or loaded["qt"] != versions["PySide6"]:
            return "Версия загруженной Qt не совпадает с установленной PySide6: " + str(loaded)
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as error:
        return "Не удалось проверить Qt: " + str(error)
    print(f"Qt {loaded['qt']}: {', '.join(packages)} и Multimedia согласованы.")
    return None


def explain_install_failure(low: tuple[int, ...]) -> None:
    """Объяснить ограничения официальных Linux wheels после ошибки pip."""
    if sys.platform != "linux" or low[:2] != (6, 12):
        return
    machine = platform.machine()
    libc, version = platform.libc_ver()
    print(
        f"Окружение: Python {platform.python_version()}, {machine}, "
        f"{libc or 'неизвестная libc'} {version}.",
        file=sys.stderr,
    )
    minimum = {"x86_64": "2.34", "aarch64": "2.39"}.get(machine)
    if minimum is None:
        print("Проверьте наличие wheels PySide6 6.12 для этой архитектуры.", file=sys.stderr)
        return
    print(f"Официальные wheels PySide6 6.12 для {machine} требуют glibc >= {minimum}.", file=sys.stderr)
    if libc == "glibc" and version and release_tuple(version) < release_tuple(minimum):
        print(
            "pip отбрасывает эти wheels из-за старой glibc. Для готовых пакетов "
            "нужен более новый Linux (можно контейнер). Полученный бинарник также "
            "потребует новую glibc: PyInstaller её не упаковывает. Для поддержки "
            "текущей ОС соберите Qt и PySide6 из исходников под её glibc.",
            file=sys.stderr,
        )
    else:
        print(
            "Проверьте доступность PyPI и актуальность pip; зеркало может не содержать 6.12. "
            "Список совместимых тегов: python -m pip debug --verbose.",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="Обновить Qt при неудачной проверке")
    parser.add_argument("--requirements", type=Path, default=Path(__file__).resolve().parents[1] / "requirements.txt")
    args = parser.parse_args()
    try:
        spec, low = requirement(args.requirements)
        error = check(low)
        if error and args.update:
            print(f"{error}\nОбновление {spec} в {sys.prefix}...", flush=True)
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", spec], check=True
            )
            error = check(low)
        if error:
            print(f"Ошибка: {error}. Требуется {spec}.", file=sys.stderr)
            return 1
    except subprocess.CalledProcessError as error:
        print(f"Проверка/обновление Qt не выполнены: {error}", file=sys.stderr)
        explain_install_failure(low)
        return 1
    except (OSError, ValueError) as error:
        print(f"Проверка/обновление Qt не выполнены: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
