"""Сгенерировать цветной пак иконок для пользовательских панелей.

Берет Lucide-пак из assets/custom/mono (монохром, stroke=currentColor)
и раскрашивает иконки в акцентные цвета по смыслу имени: железо - синее,
сеть - голубое, успех - зеленое, предупреждения - янтарные, опасность -
красные, защита - фиолетовые. Остальные (без группы) в цветной пак не
входят - они остаются в монохромном паке с автоподкраской под тему.

Цвета - средней насыщенности: читаются и на темной, и на светлой теме.
В цветных файлах нет currentColor, поэтому тинт под палитру их не трогает.

Запуск: .venv/Scripts/python scripts/gen_colored_custom_icons.py
Результат: assets/custom/color.zip (+ папка-staging, выбор - спек color:<имя>)

Точечно: --only <имя> перекрашивает один файл и дописывает его
в color.zip хирургически (остальные записи не трогаются).
"""

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from icon_toolkit import read_zip_entries, write_zip_surgical  # noqa: E402

ROOT = Path(__file__).parent.parent
SRC = ROOT / "assets" / "custom" / "mono"
DEST = ROOT / "assets" / "custom" / "color"
MONO_ZIP = ROOT / "assets" / "custom" / "mono.zip"
COLOR_ZIP = ROOT / "assets" / "custom" / "color.zip"

# Порядок важен: первая совпавшая группа побеждает (матч по целым токенам).
GROUPS: list = [
    (
        "#E85D5D",
        [  # опасность / удаление / запреты
            "trash",
            "delete",
            "ban",
            "skull",
            "siren",
            "bomb",
            "flame",
            "octagon-x",
            "circle-x",
            "x-octagon",
            "shield-x",
            "alert-octagon",
            "power-off",
            "unplug",
            "zap-off",
        ],
    ),
    (
        "#E8A13D",
        [  # предупреждения / файлы / инструменты
            "alert",
            "triangle",
            "circle-alert",
            "bell",
            "clock",
            "alarm",
            "hourglass",
            "timer",
            "folder",
            "file",
            "archive",
            "inbox",
            "wrench",
            "hammer",
            "tool",
            "settings-2",
            "calendar",
        ],
    ),
    (
        "#3ECF7A",
        [  # успех / добавление / питание
            "check",
            "badge-check",
            "circle-check",
            "square-check",
            "plus",
            "circle-plus",
            "square-plus",
            "save",
            "power",
            "plug",
            "zap",
            "play",
            "thumbs-up",
        ],
    ),
    (
        "#5B9BFF",
        [  # железо / компьютеры
            "server",
            "database",
            "monitor",
            "computer",
            "laptop",
            "smartphone",
            "tablet",
            "cpu",
            "memory",
            "hard-drive",
            "printer",
            "camera",
            "keyboard",
            "mouse",
            "router",
            "projector",
        ],
    ),
    (
        "#43C6E8",
        [  # сеть / облако / обмен
            "network",
            "globe",
            "earth",
            "wifi",
            "bluetooth",
            "cloud",
            "download",
            "upload",
            "sync",
            "refresh",
            "rss",
            "radio",
            "satellite",
            "webhook",
            "link",
            "share",
            "send",
        ],
    ),
    (
        "#A583FF",
        [  # защита / доступ / особое
            "shield",
            "lock",
            "key",
            "settings",
            "cog",
            "star",
            "crown",
            "gem",
            "palette",
            "wand",
            "sparkles",
            "eye",
            "fingerprint",
        ],
    ),
]


def pick_color(name: str) -> str | None:
    """Цвет по смыслу имени. Матч - по целым токенам (split по '-'),
    чтобы 'star' не цепляло '*-start', а 'key' - 'keyboard' целиком.
    Составные ключи ('circle-check') - как непрерывная подстрока токенов.
    """
    tokens = name.lower().split("-")
    for color, keywords in GROUPS:
        for kw in keywords:
            parts = kw.split("-")
            for i in range(len(tokens) - len(parts) + 1):
                if tokens[i : i + len(parts)] == parts:
                    return color
    return None


def source_text(name: str) -> str | None:
    """Исходный монохромный SVG: staging-файл, иначе запись mono.zip."""
    base = name if name.endswith(".svg") else name + ".svg"
    staged = SRC / base
    try:
        if staged.is_file():
            return staged.read_text(encoding="utf-8")
    except OSError:
        pass
    if MONO_ZIP.is_file():
        entries = read_zip_entries(str(MONO_ZIP))
        if base in entries:
            return entries[base].decode("utf-8")
    return None


def recolor_text(raw: str, color: str) -> str | None:
    """Перекрасить SVG-текст в цвет. None - файл не входит в цветной пак."""
    if "<text" in raw or "<filter" in raw:
        return None
    if "currentColor" not in raw and "currentcolor" not in raw:
        return None
    return raw.replace("currentColor", color).replace("currentcolor", color)


def recolor_one(name: str) -> bytes | None:
    """Готовые байты цветного файла (с переводом строк как в паке)."""
    import os

    raw = source_text(name)
    if raw is None:
        print(f"Нет исходника: {name}")
        return None
    # Из zip байты идут как есть (\r\n) - канонизировать, иначе bake удвоит \r.
    raw = raw.replace("\r\n", "\n")
    base = name if name.endswith(".svg") else name + ".svg"
    color = pick_color(Path(base).stem)
    if color is None:
        print(f"{base}: без группы - в цветной пак не входит")
        return None
    colored = recolor_text(raw, color)
    if colored is None:
        print(f"{base}: пропущен (<text>/<filter>/без currentColor)")
        return None
    # Исходник уже кончается переводом строки - снять один, иначе bake удвоит.
    return (colored.removesuffix("\n") + "\n").replace("\n", os.linesep).encode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Цветной пак custom-иконок")
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        help="Только указанные файлы (без .svg можно), можно несколько",
    )
    ap.add_argument(
        "--list", action="store_true", help="Показать файлы цветного пака и выйти"
    )
    args = ap.parse_args()

    if args.list:
        if COLOR_ZIP.is_file():
            for n in sorted(read_zip_entries(str(COLOR_ZIP))):
                print(n)
        elif DEST.is_dir():
            for f in sorted(DEST.iterdir()):
                if f.is_file():
                    print(f.name)
        else:
            print("Цветной пак не найден")
            return 1
        return 0

    if args.only:
        if not COLOR_ZIP.is_file():
            print(f"Нет {COLOR_ZIP} - сначала полный прогон без --only")
            return 1
        updates = {}
        for name in args.only:
            data = recolor_one(name)
            if data is None:
                return 1
            try:
                ET.fromstring(data)
            except Exception as e:  # noqa: BLE001
                print(f"ERROR {name}: невалидный XML: {e}")
                return 1
            base = name if name.endswith(".svg") else name + ".svg"
            updates[base] = data
        write_zip_surgical(str(COLOR_ZIP), updates, label="color.zip")
        return 0

    if not SRC.is_dir():
        print(f"Нет исходного пака: {SRC}")
        return 1
    DEST.mkdir(parents=True, exist_ok=True)
    done = 0
    skipped = 0
    for src_file in sorted(SRC.iterdir()):
        if not src_file.is_file() or src_file.suffix.lower() != ".svg":
            continue
        color = pick_color(src_file.stem)
        if color is None:
            continue
        try:
            raw = src_file.read_text(encoding="utf-8")
        except OSError:
            skipped += 1
            continue
        colored = recolor_text(raw, color)
        if colored is None:
            skipped += 1
            continue
        (DEST / src_file.name).write_text(colored, encoding="utf-8")
        done += 1
    print(f"Цветной пак: {done} иконок в {DEST} (пропущено: {skipped})")
    sys.path.insert(0, str(Path(__file__).parent))
    from pack_asset_zip import pack_dir

    pack_dir(str(DEST), str(COLOR_ZIP))
    return 0


if __name__ == "__main__":
    sys.exit(main())
