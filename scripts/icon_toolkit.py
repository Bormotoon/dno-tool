#!/usr/bin/env python3
"""Общая библиотека генераторов иконок (Qt-free, только stdlib).

Примитивы, раньше продублированные в gen_icon_themes.py / add_map_icons.py:
- рендер одного SVG + XML-валидация (байты с переводом строк как раньше);
- чтение zip в память;
- хирургическая запись в zip (только указанные записи, остальные байт-в-байт);
- полная перепаковка темы из staging-папки.

Surgical-обновление сохраняет содержимое и атрибуты чужих записей.
Архив переписывается через соседний временный файл с проверкой до
атомарной замены; сжатые данные и порядок записей могут измениться.
"""

import os
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

COMPRESSLEVEL = 9

# Темы -> вариант глифа (soft один на 4 темы, paper свой, line/solid свои).
# Раньше THEME_VARIANT жил только в add_map_icons.py.
THEME_VARIANT = {
    "flat": "soft",
    "classic": "soft",
    "midnight": "soft",
    "neon": "soft",
    "paper": "paper",
    "line": "line",
    "solid": "solid",
    "badge": "solid",
}

THEME_IDS = ["flat", "classic", "midnight", "paper", "neon", "line", "solid", "badge"]

# Геометрия рамки миниатюр Veyon (дубль theme.json новых тем).
THUMB_HOLE = [34.0, 34.0, 60.0, 33.75]
THUMB_VIEWBOX = [0.0, 0.0, 128.0, 128.0]


def render(mode: str, name: str, painter, color: str) -> str:
    """Отрендерить иконку в SVG-текст (дословно render() из генератора).

    mode: line | solid | badge. Плейсхолдеры: "@@"/"@@sym@@" = чернила
    символа, "__SYM__" = акцент темы (виден на подложках badge).
    """
    from icons._base import doc

    ink = "#FFFFFF" if mode == "badge" else color
    inner = painter("badge" if mode == "badge" else mode, ink)
    sym = color if mode == "line" else "#FFFFFF"
    inner = inner.replace("@@sym@@", sym).replace("@@", sym)
    inner = inner.replace("__SYM__", color)
    tpl = doc(mode, color, name)
    return tpl.format(inner)


def bake_svg(text: str) -> bytes:
    """Готовые байты SVG-записи: текст + перевод строк как раньше.

    Полная регенерация писала файлы в текстовом режиме (``\\n`` ->
    ``os.linesep``), topo-скрипт делал ту же замену вручную. Здесь один
    канонический путь, чтобы surgical и full давали те же байты.
    """
    return (text + "\n").replace("\n", os.linesep).encode("utf-8")


def validate_svg(data: bytes, label: str) -> None:
    """XML-валидация SVG. Кидает SystemExit с понятной ошибкой."""
    try:
        ET.fromstring(data)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR {label}: невалидный XML: {e}")
        sys.exit(1)


def read_zip_entries(zip_path: str) -> dict:
    """Все записи архива {имя: байты} с проверкой целостности."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            print(f"ERROR {zip_path} corrupt entry: {bad}")
            sys.exit(1)
        return {n: zf.read(n) for n in zf.namelist()}


def write_zip_surgical(zip_path: str, updates: dict, label: str = "") -> int:
    """Хирургически обновить записи в zip.

    Args:
        zip_path: путь к архиву (должен существовать).
        updates: {имя записи: байты} - заменить/добавить; значение None -
            удалить запись.
        label: подпись для логов.

    Returns:
        Число измененных записей (замена с теми же байтами не считается).

    Остальные записи копируются дословно (те же сжатые данные невозможны
    при перезаписи - пересжимаются тем же методом/уровнем, содержимое
    идентично; порядок: старые по алфавиту, затем новые по алфавиту).
    Верификация нового архива - до атомарной замены оригинала.
    """
    tag = label or os.path.basename(zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            print(f"ERROR {zip_path} corrupt entry: {bad}")
            sys.exit(1)
        old = {n: (zf.getinfo(n), zf.read(n)) for n in zf.namelist()}
    changed = 0
    for name, data in updates.items():
        if data is None:
            if name in old:
                changed += 1
            continue
        if name not in old or old[name][1] != data:
            changed += 1
    if not changed:
        print(f"{tag}: без изменений")
        return 0
    fd, tmp_path = tempfile.mkstemp(
        prefix="icons_", suffix=".zip", dir=os.path.dirname(os.path.abspath(zip_path))
    )
    os.close(fd)
    try:
        with zipfile.ZipFile(
            tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=COMPRESSLEVEL
        ) as zf:
            # Комментарий архива не относится к заменяемым иконкам.
            with zipfile.ZipFile(zip_path, "r") as original:
                zf.comment = original.comment
            for fname in sorted(old):
                if fname in updates:
                    continue  # новая версия ниже, None = удалить
                # Исходный ZipInfo: дата/атрибуты нетронутых записей сохраняются.
                zf.writestr(old[fname][0], old[fname][1])
            for fname in sorted(updates):
                if updates[fname] is not None:
                    zf.writestr(fname, updates[fname])
        with zipfile.ZipFile(tmp_path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                print(f"ERROR {tag} corrupt entry: {bad}")
                sys.exit(1)
            for fname, data in updates.items():
                if data is None:
                    if fname in zf.namelist():
                        print(f"ERROR {tag}: {fname} не удалился")
                        sys.exit(1)
                elif zf.read(fname) != data:
                    print(f"ERROR {tag}: {fname} не записался")
                    sys.exit(1)
        os.replace(tmp_path, zip_path)
        print(f"{tag}: обновлено записей: {changed}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return changed


def pack_dir_staged(zip_path: str, src_dir: str, label: str = "") -> dict:
    """Полная упаковка staging-папки в zip (как pack_theme раньше).

    Возвращает статистику {files, zip_kb, raw_kb}. Верифицирует архив.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pack_asset_zip import pack_dir

    return pack_dir(src_dir, zip_path)
