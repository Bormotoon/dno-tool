"""Скачать пак SVG-иконок для пользовательских панелей в assets/custom/mono.

Источники (на выбор флагом --source):
- lucide (default, ISC): https://github.com/lucide-icons/lucide
- material (Apache-2.0): https://github.com/google/material-design-icons (subset)

Запуск: .venv/Scripts/python scripts/fetch_custom_icons.py [--source lucide]

Точечно: --only <имя> докачивает один файл и дописывает его
в mono.zip хирургически (остальные записи не трогаются).
"""

import argparse
import io
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).parent.parent
DEST = ROOT / "assets" / "custom" / "mono"
MONO_ZIP = ROOT / "assets" / "custom" / "mono.zip"

SOURCES = {
    "lucide": "https://github.com/lucide-icons/lucide/archive/refs/heads/main.zip",
    "material": "https://github.com/google/material-design-icons/archive/refs/heads/master.zip",
}


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "dno-tool-icon-fetcher"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _clean_svg(text: str) -> str:
    # Qt SVG не рендерит <text> и фильтры - выкидываем такие файлы позже
    return text


def _wanted(only: set | None, base: str) -> bool:
    """Подходит ли файл под фильтр --only (с/без .svg)."""
    if not only:
        return True
    return base in only or base.removesuffix(".svg") in only


def fetch_lucide(data: bytes, dest: Path, only: set | None = None) -> int:
    z = zipfile.ZipFile(io.BytesIO(data))
    # lucide-main/icons/*.svg
    names = [n for n in z.namelist() if "/icons/" in n and n.endswith(".svg")]
    count = 0
    for n in names:
        base = n.rsplit("/", 1)[-1]
        if not _wanted(only, base):
            continue
        raw = z.read(n).decode("utf-8", errors="replace")
        if "<text" in raw or "<filter" in raw:
            continue
        (dest / base).write_text(_clean_svg(raw), encoding="utf-8")
        count += 1
    return count


def fetch_material(
    data: bytes, dest: Path, only: set | None = None, limit: int = 2000
) -> int:
    z = zipfile.ZipFile(io.BytesIO(data))
    # material: src/<name>/materialiconsoutlined/24px.svg - берем подмножество
    count = 0
    for n in z.namelist():
        if not n.endswith("materialiconsoutlined/24px.svg"):
            continue
        m = re.search(r"src/([^/]+)/materialicons", n)
        if not m:
            continue
        base = m.group(1) + ".svg"
        if not _wanted(only, base):
            continue
        raw = z.read(n).decode("utf-8", errors="replace")
        if "<text" in raw or "<filter" in raw:
            continue
        (dest / base).write_text(_clean_svg(raw), encoding="utf-8")
        count += 1
        if count >= (10**9 if only else limit):
            break
    return count


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=list(SOURCES), default="lucide")
    ap.add_argument("--no-pack", action="store_true", help="Не паковать в mono.zip")
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        help="Только указанные файлы (с/без .svg), можно несколько",
    )
    args = ap.parse_args()
    only = set(args.only) or None
    DEST.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {args.source} ...")
    data = _download(SOURCES[args.source])
    if args.source == "lucide":
        n = fetch_lucide(data, DEST, only)
    else:
        n = fetch_material(data, DEST, only)
    print(f"Saved {n} icons to {DEST}")
    if not args.no_pack:
        from pack_asset_zip import pack_dir

        if only and MONO_ZIP.is_file():
            # Точечно: дописать только скачанные файлы в существующий пак.
            import os

            from icon_toolkit import bake_svg, write_zip_surgical

            updates = {}
            for name in only:
                base = name if name.endswith(".svg") else name + ".svg"
                staged = DEST / base
                if not staged.is_file():
                    print(f"В источнике нет файла: {base}")
                    sys.exit(1)
                raw = staged.read_text(encoding="utf-8")
                text = _clean_svg(raw).removesuffix("\r\n").removesuffix("\n")
                updates[base] = bake_svg(text)
            write_zip_surgical(str(MONO_ZIP), updates, label="mono.zip")
        else:
            pack_dir(str(DEST), str(MONO_ZIP))


if __name__ == "__main__":
    sys.exit(main())
