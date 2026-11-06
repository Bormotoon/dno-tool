#!/usr/bin/env python3
"""Общий упаковщик loose-папок ассетов в zip (темы иконок, custom-паки).

Пакует только файлы из корня папки (без подпапок, basename-записи),
затем верифицирует архив: переоткрывает и читает каждую запись.
Zip-slip закрыт на чтении (см. src/config/icon_theme_loader._safe_name).
"""

import os
import sys
import zipfile


def pack_dir(src_dir: str, zip_path: str) -> dict:
    """Упаковать файлы папки в zip. Вернуть статистику {files, zip_kb, raw_kb}."""
    files = sorted(
        f for f in os.listdir(src_dir) if os.path.isfile(os.path.join(src_dir, f))
    )
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in files:
            zf.write(os.path.join(src_dir, f), f)
    total = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            print(f"ERROR {zip_path} corrupt entry: {bad}")
            sys.exit(1)
        for n in zf.namelist():
            total += len(zf.read(n))
    size = os.path.getsize(zip_path)
    stat = {"files": len(files), "zip_kb": size // 1024, "raw_kb": total // 1024}
    print(
        f"{os.path.basename(zip_path)}: {stat['files']} files, "
        f"{stat['zip_kb']} KB zip / {stat['raw_kb']} KB raw"
    )
    return stat
