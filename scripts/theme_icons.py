#!/usr/bin/env python3
"""Свободное редактирование иконок в ZIP-темах и папках без генераторов.

Назначение и примеры команд: scripts/README.md.
"""

import argparse
import fnmatch
from pathlib import Path, PurePosixPath
import sys
import xml.etree.ElementTree as ET
import zipfile

from icon_toolkit import write_zip_surgical


DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "assets" / "icons"


def safe_name(name: str) -> str:
    """Проверить относительный путь, включая вложенные иконки."""
    name = name.replace("\\", "/")
    parts = name.split("/")
    if not name or any(p in ("", ".", "..") or ":" in p for p in parts):
        raise ValueError(f"Недопустимое имя записи: {name!r}")
    return name


class Theme:
    """Тема по произвольному пути либо имени относительно корня."""

    def __init__(self, value: str, root: Path):
        explicit = Path(value)
        candidates = (explicit, root / f"{value}.zip", root / value)
        self.path = next((p.resolve() for p in candidates if p.exists()), None)
        if self.path is None:
            raise ValueError(f"Тема не найдена: {value}")
        if not self.path.is_dir() and not zipfile.is_zipfile(self.path):
            raise ValueError(f"Ожидается ZIP или папка: {self.path}")

    def names(self) -> list[str]:
        """Все файлы, без ограничения реестром приложения или форматом."""
        if self.path.is_dir():
            return sorted(p.relative_to(self.path).as_posix()
                          for p in self.path.rglob("*") if p.is_file())
        with zipfile.ZipFile(self.path) as archive:
            names = [safe_name(i.filename) for i in archive.infolist() if not i.is_dir()]
            if len(names) != len(set(names)):
                raise ValueError(f"В архиве повторяются имена: {self.path}")
            return sorted(names)

    def select(self, patterns: list[str]) -> list[str]:
        """Выбрать точные имена, имена без расширения или маски."""
        names = self.names()
        result: set[str] = set()
        for pattern in patterns:
            matches = [n for n in names if n == pattern] or [
                n for n in names
                if fnmatch.fnmatchcase(n, pattern)
                or str(PurePosixPath(n).with_suffix("")) == pattern
            ]
            if not matches:
                raise ValueError(f"В {self.path.name} нет записей: {pattern}")
            result.update(matches)
        return sorted(result)

    def read(self, name: str) -> bytes:
        """Получить исходные байты без перекрашивания и fallback."""
        safe_name(name)
        if self.path.is_dir():
            return self.member_path(name).read_bytes()
        with zipfile.ZipFile(self.path) as archive:
            return archive.read(name)

    def member_path(self, name: str) -> Path:
        """Не позволять ссылкам папки выводить запись за пределы темы."""
        target = (self.path / safe_name(name)).resolve()
        if not target.is_relative_to(self.path):
            raise ValueError(f"Путь выходит за пределы темы: {name}")
        return target

    def write(self, updates: dict[str, bytes], add: bool = False) -> None:
        """Проверить весь набор до записи; ZIP заменяется атомарно."""
        existing = set(self.names())
        for name, data in updates.items():
            safe_name(name)
            if name not in existing and not add:
                raise ValueError(f"Нет записи {name}; для добавления укажите --add")
            if name.lower().endswith(".svg"):
                node = ET.fromstring(data)
                if node.tag not in ("svg", "{http://www.w3.org/2000/svg}svg"):
                    raise ValueError(f"Ожидается SVG: {name}")
            if self.path.is_dir():
                self.member_path(name)
        if self.path.is_dir():
            for name, data in updates.items():
                target = self.member_path(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            print(f"{self.path}: записей: {len(updates)}")
        else:
            write_zip_surgical(str(self.path), updates)


def main() -> int:
    """Команды не зависят от списка тем, painter-ов и размеров SVG."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help="Каталог тем (по умолчанию assets/icons)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("themes", help="Найти ZIP и папки в корне тем")
    listing = commands.add_parser("list", help="Показать имена записей")
    listing.add_argument("theme")
    listing.add_argument("patterns", nargs="*", default=["*"])
    extract = commands.add_parser("extract", help="Извлечь выбранные записи в папку")
    extract.add_argument("theme")
    extract.add_argument("patterns", nargs="+")
    extract.add_argument("--out", required=True, type=Path)
    extract.add_argument("--force", action="store_true", help="Перезаписать извлечённые файлы")
    replace = commands.add_parser("replace", help="Заменить запись любым готовым файлом")
    replace.add_argument("theme")
    replace.add_argument("name", help="Имя записи назначения, с расширением")
    replace.add_argument("file", type=Path)
    replace.add_argument("--add", action="store_true")
    copying = commands.add_parser("copy", help="Перенести записи между любыми темами")
    copying.add_argument("source")
    copying.add_argument("destination")
    copying.add_argument("patterns", nargs="+")
    copying.add_argument("--as", dest="rename", help="Новое имя для одной записи")
    copying.add_argument("--add", action="store_true")
    importing = commands.add_parser("import", help="Записать содержимое рабочей папки в тему")
    importing.add_argument("theme")
    importing.add_argument("directory", type=Path)
    importing.add_argument("--add", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "themes":
            for path in sorted(args.root.iterdir()):
                if path.is_dir() or path.suffix.lower() == ".zip":
                    print(path.name)
            return 0
        theme = Theme(args.source if args.command == "copy" else args.theme, args.root)
        if args.command == "list":
            print("\n".join(theme.select(args.patterns or ["*"])))
        elif args.command == "extract":
            names = theme.select(args.patterns)
            output = args.out.resolve()
            targets = [(name, (output / safe_name(name)).resolve()) for name in names]
            for name, target in targets:
                if not target.is_relative_to(output):
                    raise ValueError(f"Путь выходит за пределы --out: {name}")
                if target.exists() and not args.force:
                    raise ValueError(f"Файл существует: {target}; укажите --force")
            for name, target in targets:
                data = theme.read(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                print(target)
        elif args.command == "replace":
            theme.write({args.name: args.file.read_bytes()}, args.add)
        elif args.command == "copy":
            names = theme.select(args.patterns)
            if args.rename and len(names) != 1:
                raise ValueError("--as требует ровно одну исходную запись")
            updates = {args.rename or name: theme.read(name) for name in names}
            Theme(args.destination, args.root).write(updates, args.add)
        elif args.command == "import":
            if not args.directory.is_dir():
                raise ValueError(f"Нет рабочей папки: {args.directory}")
            source = Theme(str(args.directory), args.root)
            updates = {name: source.read(name) for name in source.names()}
            if not updates:
                raise ValueError("Рабочая папка пуста")
            theme.write(updates, args.add)
        return 0
    except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Ошибка: {error}\n")


if __name__ == "__main__":
    sys.exit(main())
