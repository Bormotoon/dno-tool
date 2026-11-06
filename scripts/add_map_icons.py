#!/usr/bin/env python3
"""Иконки узлов карты сети (topo_*): добивка в zip всех 8 тем.

Глифы и акценты - scripts/icons/topo.py (PAINTERS/COLORS/TOPO_NAMES):
новая topo-иконка добавляется там, этот скрипт трогать не нужно.
Стиль: БЕЗ подложки (прозрачный фон), цветные семантические глифы.

Запуск:
  python scripts/add_map_icons.py
      все topo_* во все существующие zip (как раньше)
  python scripts/add_map_icons.py --icon topo_host --theme flat
      точечно: только topo_host.svg в flat.zip
  python scripts/add_map_icons.py --list / --check (как в gen_icon_themes.py)
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from icons import topo  # noqa: E402
from icon_toolkit import (  # noqa: E402
    THEME_IDS,
    THEME_VARIANT,
    bake_svg,
    validate_svg,
    write_zip_surgical,
)

ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets",
    "icons",
)


def topo_bytes(name: str, theme_id: str) -> bytes:
    """Готовые байты topo-иконки для темы."""
    return bake_svg(topo.topo_svg(name, THEME_VARIANT[theme_id]))


def update_theme(theme_id: str, names: list, check_only: bool = False) -> int:
    zip_path = os.path.join(ROOT, theme_id + ".zip")
    if not os.path.isfile(zip_path):
        print(f"SKIP {theme_id}: нет архива {zip_path}")
        return 0
    updates = {}
    for name in names:
        data = topo_bytes(name, theme_id)
        validate_svg(data, f"{theme_id}/{name}")
        updates[name + ".svg"] = data
        if name == "topo_camera":
            # Камера в инструментах, сканере и топологии имеет один рисунок.
            updates["camera.svg"] = data
    if check_only:
        print(f"{theme_id}: проверено {len(updates)} (без записи)")
        return 0
    return write_zip_surgical(zip_path, updates, label=f"{theme_id}.zip")


def main() -> None:
    ap = argparse.ArgumentParser(description="Добивка topo_* в zip тем")
    ap.add_argument(
        "--icon", action="append", default=[], help="Имя topo-иконки, можно несколько"
    )
    ap.add_argument(
        "--theme",
        action="append",
        default=[],
        choices=THEME_IDS,
        help="Только указанные темы (по умолчанию все)",
    )
    ap.add_argument(
        "--add", action="store_true", help="Новая иконка (допустить имя вне zip)"
    )
    ap.add_argument(
        "--list", action="store_true", help="Показать все topo-имена и выйти"
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="Только рендер + валидация, без записи в zip",
    )
    args = ap.parse_args()

    if args.list:
        for n in topo.TOPO_NAMES:
            print(n)
        print(f"--- всего: {len(topo.TOPO_NAMES)} ---")
        return

    names = args.icon or list(topo.TOPO_NAMES)
    unknown = [n for n in names if n not in topo.TOPO_NAMES]
    if unknown:
        print("Нет painters для: " + ", ".join(unknown))
        print("Добавить глиф в scripts/icons/topo.py, затем повторить.")
        sys.exit(1)
    theme_ids = args.theme or THEME_IDS
    if args.icon and not args.add:
        import zipfile

        for theme_id in theme_ids:
            zip_path = os.path.join(ROOT, theme_id + ".zip")
            if not os.path.isfile(zip_path):
                continue
            with zipfile.ZipFile(zip_path, "r") as zf:
                have = set(zf.namelist())
            outside = [n for n in names if n + ".svg" not in have]
            if outside:
                print(f"{theme_id}.zip: нет записей: " + ", ".join(outside))
                print("Если это новые - повторите с --add.")
                sys.exit(1)
    total = 0
    for theme_id in theme_ids:
        total += update_theme(theme_id, names, check_only=args.check)
    if not args.check:
        print(f"Итого изменений: {total}")


if __name__ == "__main__":
    main()
