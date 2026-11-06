"""Вариант сборки со служебным мастер-ключом нод или без него.

Вариант задаёт переменная окружения ``DNO_MASTER_KEY``: путь к файлу открытой
части ключа — файл кладётся в корень сборки под именем ``master-key.pub``
(программа ищет его там); пусто или ``0`` — сборка без ключа. Сборочные
скрипты задают переменную параметром ``-MasterKey <файл>``.

Используется spec-файлами PyInstaller (``datas``) и из командной строки:

    python packaging/master_key.py validate
    python packaging/master_key.py check <exe|каталог>

``validate`` до сборки проверяет файл, если вариант с ключом; ``check``
проверяет, что в готовом артефакте лежит именно этот ключ, а в сборке без
ключа его нет.
"""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import sys
from pathlib import Path

ENV = "DNO_MASTER_KEY"
# Имя файла в сборке: его ищет src/utils/security/node_master.py.
FILE_NAME = "master-key.pub"
SUFFIX = "-master"
_FILE_LIMIT = 4096


class MasterKeyError(SystemExit):
    """Ошибка варианта сборки: понятное сообщение и код 1."""

    def __init__(self, message: str) -> None:
        super().__init__(f"Ошибка: {message}")


def requested() -> Path | None:
    """Файл открытой части для сборки с ключом или None (сборка без ключа)."""
    value = os.environ.get(ENV, "").strip()
    if value in ("", "0"):
        return None
    return Path(value).expanduser().resolve()


def validate(path: Path) -> bytes:
    """Проверить файл открытой части (одна строка base64 с 32 байтами), вернуть его байты."""
    if not path.is_file():
        raise MasterKeyError(f"нет файла открытой части мастер-ключа: {path}")
    try:
        data = path.read_bytes()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise MasterKeyError(f"файл {path} не читается") from None
    lines = [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    try:
        raw = base64.b64decode(lines[0], validate=True) if len(lines) == 1 else b""
    except ValueError:
        raw = b""
    if len(data) > _FILE_LIMIT or len(raw) != 32:
        raise MasterKeyError(
            f"в {path} должна быть одна строка base64 открытого ключа Ed25519"
        )
    return data


def datas(work_dir: str | os.PathLike) -> list[tuple[str, str]]:
    """Элементы ``datas`` PyInstaller для выбранного варианта.

    Файл с другим именем копируется в ``work_dir/master-key`` под именем
    ``master-key.pub``: PyInstaller сохраняет имя исходного файла.
    """
    source = requested()
    if source is None:
        print("dno-tool: сборка без служебного мастер-ключа")
        return []
    validate(source)
    if source.name != FILE_NAME:
        staging = Path(work_dir) / "master-key"
        staging.mkdir(parents=True, exist_ok=True)
        target = staging / FILE_NAME
        shutil.copyfile(source, target)
        source = target
    print(f"dno-tool: сборка со служебным мастер-ключом ({requested()})")
    return [(str(source), ".")]


def _bundled(artifact: Path) -> bytes | None:
    """Содержимое master-key.pub в готовом артефакте или None."""
    if artifact.is_dir():
        # Linux onedir: файл в lib/; также проверяется каталог рядом с exe.
        for sub in (".", "lib", "_internal"):
            path = artifact / sub / FILE_NAME
            if path.is_file():
                return path.read_bytes()
        return None
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(artifact))
    for name in reader.toc:
        if name.replace("\\", "/") == FILE_NAME:
            return reader.extract(name)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="проверить файл до сборки")
    check = sub.add_parser("check", help="проверить готовый артефакт")
    check.add_argument("artifact", type=Path)
    args = parser.parse_args(argv)
    source = requested()
    if args.command == "validate":
        if source is None:
            print("dno-tool: сборка без служебного мастер-ключа")
        else:
            validate(source)
            print(f"dno-tool: сборка со служебным мастер-ключом ({source})")
        return 0
    expected = validate(source) if source is not None else None
    bundled = _bundled(args.artifact)
    if bundled == expected:
        print(f"Мастер-ключ: {'есть, совпадает с ' + str(source) if expected else 'нет'} — как ожидалось")
        return 0
    if expected is None:
        problem = "есть открытая часть мастер-ключа, а сборка без ключа"
    elif bundled is None:
        problem = "нет открытой части мастер-ключа"
    else:
        problem = f"открытая часть мастер-ключа отличается от {source}"
    print(f"Ошибка: в {args.artifact} {problem}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
