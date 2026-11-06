"""Точечно добавить режимы просмотра камер во все восемь ZIP-тем."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from gen_icon_themes import icon_bytes
from icon_toolkit import THEME_IDS, bake_svg, validate_svg, write_zip_surgical
from icons.cameras import PAINTERS, legacy_svg

ROOT = Path(__file__).resolve().parents[1] / "assets" / "icons"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--theme", action="append", choices=THEME_IDS)
    parser.add_argument("--icon", action="append", choices=sorted(PAINTERS))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for theme in args.theme or THEME_IDS:
        path = ROOT / f"{theme}.zip"
        updates = {}
        with zipfile.ZipFile(path) as archive:
            for name in args.icon or sorted(PAINTERS):
                if theme in ("line", "solid", "badge"):
                    data = icon_bytes(theme, name)
                else:
                    state = "_on" if name.endswith("_on") else ""
                    template = archive.read(f"toolbar_thumbnails{state}.svg")
                    data = bake_svg(legacy_svg(name, template))
                validate_svg(data, f"{theme}/{name}")
                updates[f"{name}.svg"] = data
        if args.check:
            print(f"{theme}: проверено {len(updates)} иконок")
        else:
            write_zip_surgical(str(path), updates)


if __name__ == "__main__":
    main()
