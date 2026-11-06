#!/usr/bin/env python3
"""Генератор тем иконок line/solid/badge: полный и точечный.

Глифы и цвета - пакет scripts/icons/ (модуль на группу, реестр в __init__):
точечное изменение/добавление иконки = правка одного модуля, этот скрипт
трогать не нужно.

Запуск:
  python scripts/gen_icon_themes.py
      полная регенерация 3 тем из flat.zip (как раньше)
  python scripts/gen_icon_themes.py --icon folder --theme line
      точечно: только folder.svg в line.zip (остальные записи байт-в-байт)
  python scripts/gen_icon_themes.py --icon menu_new --add
      новая иконка (painter уже добавлен в icons/): дописать во все темы;
      следующий полный прогон подхватит ее из zip сам
  python scripts/gen_icon_themes.py --list
      все имена иконок реестра
  python scripts/gen_icon_themes.py --icon folder --check
      только рендер + XML-валидация, без записи
"""

import argparse
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from icons import COLORS, PAINTERS, all_names  # noqa: E402
from icons._base import thumb_frame, thumb_underlay  # noqa: E402
from icon_toolkit import (  # noqa: E402
    bake_svg,
    read_zip_entries,
    render,
    validate_svg,
    write_zip_surgical,
)

ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets",
    "icons",
)
THEMES = [
    ("line", "Line"),
    ("solid", "Solid"),
    ("badge", "Badge"),
]
THEME_IDS = [t for t, _ in THEMES]

# Свойства theme.json: геометрия рамки миниатюр Veyon.
THUMB_HOLE = [34.0, 34.0, 60.0, 33.75]
THUMB_VIEWBOX = [0.0, 0.0, 128.0, 128.0]
THEME_THUMBNAIL = {
    "line": {"underlay": None},
    "solid": {"underlay": None},
    "badge": {"underlay": "thumb_underlay.svg"},
}


def theme_names():
    """Имена иконок темы flat (без расширения): из flat.zip, fallback - папка."""
    import zipfile

    zip_path = os.path.join(ROOT, "flat.zip")
    if os.path.isfile(zip_path):
        with zipfile.ZipFile(zip_path, "r") as zf:
            return sorted(
                n[:-4] for n in zf.namelist() if n.endswith(".svg") and "/" not in n
            )
    flat_dir = os.path.join(ROOT, "flat")
    return sorted(f[:-4] for f in os.listdir(flat_dir) if f.endswith(".svg"))


def icon_bytes(theme_id: str, name: str) -> bytes:
    """Готовые байты записи name.svg для темы (с бейдж-исключениями)."""
    if name == "ve_stop" and theme_id not in THEME_IDS:
        # Старые темы хранят свои подложки в SVG; наследуем оформление
        # соседней кнопки, чтобы не терять рамки, градиенты и блики.
        entries = read_zip_entries(os.path.join(ROOT, theme_id + ".zip"))
        template = entries["ve_play.svg"].decode("utf-8")
        previous = PAINTERS["ve_play"]("solid", COLORS["ve_play"])
        if template.count(previous) != 1:
            raise ValueError(f"Не найден единственный глиф ve_play в теме {theme_id}")
        svg = template.replace(previous, PAINTERS[name]("solid", COLORS[name]))
    elif name == "thumb_template" and theme_id == "badge":
        # Оверлей badge - рамка без круглой подложки (иначе вуалит скриншот).
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" '
            f'viewBox="0 0 128 128">{thumb_frame("badge", "")}</svg>'
        )
    else:
        svg = render(theme_id, name, PAINTERS[name], COLORS.get(name, "#64748B"))
    return bake_svg(svg)


def theme_json_bytes(theme_id: str) -> bytes:
    """Байты theme.json темы (как раньше: json.dump + перевод строки)."""
    theme_json = {
        "thumbnail": {
            "viewBox": THUMB_VIEWBOX,
            "hole": THUMB_HOLE,
            "overlay": "thumb_template.svg",
            "underlay": THEME_THUMBNAIL[theme_id]["underlay"],
        }
    }
    return bake_svg(json.dumps(theme_json, indent=2, ensure_ascii=False))


def underlay_bytes(theme_id: str):
    """Байты подложки миниатюр (только темы с underlay). None - нет."""
    underlay_name = THEME_THUMBNAIL[theme_id]["underlay"]
    if not underlay_name:
        return None
    return bake_svg(
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" '
        f'viewBox="0 0 128 128">{thumb_underlay(theme_id, "")}</svg>'
    )


def full_regen(names: list) -> None:
    """Полная регенерация тем (staging + перепаковка, как раньше)."""
    from pack_asset_zip import pack_dir

    for theme_id, _label in THEMES:
        d = tempfile.mkdtemp(prefix=f"icons_{theme_id}_")
        try:
            for n in names:
                if n == "thumb_template" and theme_id == "badge":
                    continue  # оверлей badge пишется ниже без подложки
                data = icon_bytes(theme_id, n)
                validate_svg(data, f"{theme_id}/{n}")
                with open(os.path.join(d, n + ".svg"), "wb") as f:
                    f.write(data)
            print(f"{theme_id}: {len(names)} icons written")
            if theme_id == "badge":
                data = icon_bytes(theme_id, "thumb_template")
                validate_svg(data, f"{theme_id}/thumb_template")
                with open(os.path.join(d, "thumb_template.svg"), "wb") as f:
                    f.write(data)
            underlay_name = THEME_THUMBNAIL[theme_id]["underlay"]
            if underlay_name:
                data = underlay_bytes(theme_id)
                validate_svg(data, f"{theme_id}/{underlay_name}")
                with open(os.path.join(d, underlay_name), "wb") as f:
                    f.write(data)
            with open(os.path.join(d, "theme.json"), "wb") as f:
                f.write(theme_json_bytes(theme_id))
            print(f"{theme_id}: theme.json written")
            pack_dir(d, os.path.join(ROOT, theme_id + ".zip"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


def surgical_update(theme_ids: list, names: list, check_only: bool = False) -> int:
    """Точечно обновить записи в zip тем. Вернуть число изменений."""
    total = 0
    for theme_id in theme_ids:
        zip_path = os.path.join(ROOT, theme_id + ".zip")
        if not os.path.isfile(zip_path):
            print(f"SKIP {theme_id}: нет архива {zip_path}")
            continue
        updates = {}
        for n in names:
            data = icon_bytes(theme_id, n)
            validate_svg(data, f"{theme_id}/{n}")
            updates[n + ".svg"] = data
        if check_only:
            print(f"{theme_id}: проверено {len(updates)} (без записи)")
            continue
        old = read_zip_entries(zip_path)
        new_here = [k for k, v in updates.items() if k not in old]
        total += write_zip_surgical(zip_path, updates, label=f"{theme_id}.zip")
        for k in new_here:
            print(f"{theme_id}.zip: добавлена {k}")
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="Генератор тем иконок line/solid/badge")
    ap.add_argument(
        "--icon",
        action="append",
        default=[],
        help="Имя иконки (без .svg), можно несколько",
    )
    ap.add_argument(
        "--theme",
        action="append",
        default=[],
        choices=THEME_IDS,
        help="Только указанные темы (по умолчанию все 3)",
    )
    ap.add_argument(
        "--add", action="store_true", help="Новая иконка: имени еще нет в flat.zip"
    )
    ap.add_argument(
        "--list", action="store_true", help="Показать все имена иконок реестра и выйти"
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="Только рендер + валидация, без записи в zip",
    )
    args = ap.parse_args()

    if args.list:
        for n in all_names():
            print(n)
        print(f"--- всего: {len(all_names())} ---")
        return

    archive_names = theme_names()
    if args.icon:
        unknown = [n for n in args.icon if n not in PAINTERS]
        if unknown:
            print("Нет painters для: " + ", ".join(unknown))
            print("Добавить глиф в scripts/icons/<группа>.py, затем повторить.")
            sys.exit(1)
        if not args.add:
            outside = [n for n in args.icon if n not in archive_names]
            if outside:
                print("Иконок нет в flat.zip: " + ", ".join(outside))
                print("Если это новые - повторите с --add.")
                sys.exit(1)
        names = list(args.icon)
        theme_ids = args.theme or THEME_IDS
        surgical_update(theme_ids, names, check_only=args.check)
        return

    # Без фильтров - полная регенерация (как раньше).
    if args.theme or args.add or args.check:
        print("Фильтры --theme/--add/--check требуют --icon.")
        sys.exit(2)
    missing = [n for n in archive_names if n not in PAINTERS]
    if missing:
        print("MISSING painters for: " + ", ".join(missing))
        sys.exit(1)
    full_regen(archive_names)


if __name__ == "__main__":
    main()
