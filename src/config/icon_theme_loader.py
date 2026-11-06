"""Лоадер тем иконок и custom-паков из zip-архивов (Qt-free).

Каждая тема - архив ``assets/icons/<id>.zip`` с тем же содержимым, что папка
``assets/icons/<id>/`` (SVG + ``theme.json``). Чтение - на лету, без
распаковки: ``get_icon_bytes`` отдает байты файла, Qt строит иконки из памяти
(``QPixmap.loadFromData`` / ``QSvgRenderer(QByteArray)``).

Порядок резолва (первый успех побеждает):
1. ``<theme>.zip`` (активная тема),
2. ``<fallback>.zip`` (дефолтная тема),
3. папка ``icons/<theme>/`` (dev-режим, zip еще не собран),
4. папка ``icons/<fallback>/``.

Custom-паки пользовательских панелей - архивы ``assets/custom/mono.zip``
и ``assets/custom/color.zip`` с тем же содержимым, что папки
``assets/custom/mono/`` и ``assets/custom/color/``. Читаются через
``get_pack_bytes`` / ``list_pack_names`` с fallback на папку (dev-режим).
Windows-бандл (frozen onefile) везет zip внутри; Linux-сборка (/opt)
кладет их рядом с бинарником во внешнем ``assets/`` (см. _asset_roots).

Битый/отсутствующий zip не роняет UI: лоадер молча откатывается на папку,
а при ее отсутствии возвращает None - вызыватель решает fallback сам.
Zip-slip закрыт: имена внутри архива сверяются с basename, записи с `/`,
``\\`` или `..` игнорируются.
"""

import json
import os
import threading
import zipfile

try:
    from src.logger import logger
except ImportError:  # pragma: no cover - ранний импорт/тесты без логгера
    import logging

    logger = logging.getLogger(__name__)

_FALLBACK_THEME = "flat"

# Кэш открытых архивов: theme_id -> ZipFile | None (None = нет/бит, не пыкаться).
_ZIPS: dict = {}
_ZIPS_LOCK = threading.RLock()

# Кэш прогрева темы: (theme_id, name) -> bytes. Заполняется warm_theme(),
# используется get_icon_bytes как fast-path без GIL-гонок по ZipFile.
_WARMED: dict = {}
_WARMED_LOCK = threading.RLock()

# Темы, для которых уже залогирована битность (не спамить в лог на каждую иконку).
_BROKEN_LOGGED: set = set()


def _asset_base():
    """Корень assets/ (frozen _MEIPASS или корень проекта).

    Для Linux-сборки в /opt внешние zip лежат рядом с бинарником
    (``<exe-dir>/assets``), а не в _MEIPASS - см. _asset_roots().
    Здесь возвращается первый существующий корень для совместимости.
    """
    for root in _asset_roots():
        return root
    import sys
    from pathlib import Path

    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "assets"
    return Path(__file__).parent.parent.parent / "assets"


def _external_assets_base():
    """Внешний assets/ рядом с frozen-бинарником (/opt/dno-tool/assets).

    Только для frozen: onefile распаковывается во временный _MEIPASS,
    а внешние zip лежат рядом с sys.executable. None - нет/не frozen.
    """
    import sys
    from pathlib import Path

    if not getattr(sys, "frozen", False):
        return None
    exe = Path(getattr(sys, "executable", ""))
    if not exe.name:
        return None
    for cand in (exe.parent / "assets", (exe.parent / ".." / "assets")):
        try:
            if cand.is_dir():
                return cand.resolve()
        except OSError:
            continue
    return None


def _asset_roots():
    """Все корни assets/ по приоритету: внешний -> _MEIPASS -> проект."""
    import sys
    from pathlib import Path

    roots = []
    ext = _external_assets_base()
    if ext is not None:
        roots.append(ext)
    if getattr(sys, "frozen", False):
        try:
            roots.append(Path(sys._MEIPASS) / "assets")
        except Exception:  # noqa: BLE001
            pass
    else:
        roots.append(Path(__file__).parent.parent.parent / "assets")
    return roots


def _safe_name(filename: str) -> str:
    """Безопасное имя записи: basename без путей. Пусто = недопустимо."""
    if not filename or not isinstance(filename, str):
        return ""
    if "/" in filename or "\\" in filename:
        return ""
    name = os.path.basename(filename)
    if not name or name in (".", "..") or name.startswith("."):
        return ""
    if ".." in name:
        return ""
    return name


def _open_zip(theme_id: str):
    """Открытый ZipFile темы (кэш). None - архив отсутствует или бит.

    Ищет ``icons/<id>.zip`` по всем корням (_asset_roots): внешний
    /opt-каталог побеждает _MEIPASS (Linux-сборка везет все zip наружу).
    """
    with _ZIPS_LOCK:
        if theme_id in _ZIPS:
            return _ZIPS[theme_id]
        for base in _asset_roots():
            path = str(base / "icons" / f"{theme_id}.zip")
            if not os.path.exists(path):
                continue
            try:
                zf = zipfile.ZipFile(path, "r")
                # Проверка целостности каталога (дешево, без чтения данных).
                bad = zf.testzip()
                if bad is not None:
                    raise zipfile.BadZipFile(f"corrupt entry: {bad}")
                _ZIPS[theme_id] = zf
                return zf
            except Exception as e:  # noqa: BLE001 - любой битняк = fallback на папку
                if theme_id not in _BROKEN_LOGGED:
                    _BROKEN_LOGGED.add(theme_id)
                    logger.error(f"Тема иконок {theme_id}: бит {path}: {e}")
                _ZIPS[theme_id] = None
                return None
        _ZIPS[theme_id] = None
        return None


def has_zip(theme_id: str) -> bool:
    """Есть ли целый архив темы."""
    return _open_zip(theme_id) is not None


def _read_from_zip(zf: zipfile.ZipFile, name: str):
    """Байты записи из открытого архива. None - нет записи."""
    try:
        if name not in zf.namelist():
            return None
        with zf.open(name, "r") as f:
            return f.read()
    except KeyError:
        return None
    except Exception as e:  # noqa: BLE001
        logger.error(f"Тема иконок: не читается запись {name}: {e}")
        return None


def get_icon_bytes(filename: str, theme_id: str = "", fallback: str = _FALLBACK_THEME):
    """Байты файла иконки с fallback-цепочкой zip->zip(fallback)->папка.

    Args:
        filename: имя файла (без путей, например "folder.svg").
        theme_id: тема (пусто = только fallback-цепочка по fallback).
        fallback: дефолтная тема для fallback.

    Returns:
        bytes или None (ничего не найдено).
    """
    name = _safe_name(filename)
    if not name:
        return None
    themes = ([theme_id] if theme_id and theme_id != fallback else []) + [fallback]
    with _WARMED_LOCK:
        for tid in themes:
            key = (tid, name)
            if key in _WARMED:
                return _WARMED[key]
    for tid in themes:
        zf = _open_zip(tid)
        if zf is not None:
            data = _read_from_zip(zf, name)
            if data:
                return data
    # Папки (dev-режим / zip еще не собран) - по всем корням.
    for base in _asset_roots():
        icons_base = base / "icons"
        for tid in themes:
            candidate = icons_base / tid / name
            try:
                if candidate.is_file():
                    return candidate.read_bytes()
            except OSError:
                continue
    return None


def get_theme_config(theme_id: str) -> dict:
    """Словарь theme.json темы (zip или папка). Битый/отсутствует = {}."""
    raw = get_icon_bytes("theme.json", theme_id, fallback=theme_id)
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError) as e:
        logger.error(f"Свойства темы иконок: не читается {theme_id}/theme.json: {e}")
        return {}


def warm_theme(theme_id: str, fallback: str = _FALLBACK_THEME) -> int:
    """Прогреть кэш темы: прочитать весь архив в память. Вернуть число файлов.

    Вызывать при старте (активная тема) и при переключении темы - первое
    открытие окон потом не дергает zip по каждой иконке.
    """
    zf = _open_zip(theme_id)
    if zf is None:
        return 0
    count = 0
    try:
        names = [n for n in zf.namelist() if _safe_name(n) and n == _safe_name(n)]
    except Exception:  # noqa: BLE001
        return 0
    with _WARMED_LOCK:
        for name in names:
            key = (theme_id, name)
            if key in _WARMED:
                count += 1
                continue
            data = _read_from_zip(zf, name)
            if data:
                _WARMED[key] = data
                count += 1
    # Fallback-тема тоже греется, чтобы fallback-чтения не ходили в zip.
    if theme_id != fallback and _open_zip(fallback) is not None:
        warm_theme(fallback, fallback)
    return count


def _open_asset_zip(relative: str, cache_key: str):
    """Открытый ZipFile произвольного ассета (кэш). None - нет/бит.

    Ищет по всем корням (_asset_roots): внешний /opt-каталог первый.
    """
    with _ZIPS_LOCK:
        if cache_key in _ZIPS:
            return _ZIPS[cache_key]
        for base in _asset_roots():
            path = str(base / relative)
            if not os.path.exists(path):
                continue
            try:
                zf = zipfile.ZipFile(path, "r")
                bad = zf.testzip()
                if bad is not None:
                    raise zipfile.BadZipFile(f"corrupt entry: {bad}")
                _ZIPS[cache_key] = zf
                return zf
            except Exception as e:  # noqa: BLE001 - любой битняк = fallback на папку
                if cache_key not in _BROKEN_LOGGED:
                    _BROKEN_LOGGED.add(cache_key)
                    logger.error(f"Ассет {relative}: бит {path}: {e}")
                _ZIPS[cache_key] = None
                return None
        _ZIPS[cache_key] = None
        return None


# Custom-паки пользовательских панелей: логический id -> zip/папка в assets/.
# mono - монохромный пак (спек pack:), color - цветной (спек color:).
_PACK_ZIPS = {
    "mono": "custom/mono.zip",
    "color": "custom/color.zip",
}
_PACK_DIRS = {
    "mono": "custom/mono",
    "color": "custom/color",
}
# Старые пути (до переезда в assets/custom/): только чтение-fallback,
# чтобы переходные сборки и распакованные архивы не теряли иконки.
_PACK_LEGACY_ZIPS = {
    "mono": "custom_icons.zip",
    "color": "custom_icons_color.zip",
}
_PACK_LEGACY_DIRS = {
    "mono": "custom_icons",
    "color": "custom_icons_color",
}
_PACK_IDS = tuple(_PACK_ZIPS)


def _pack_cache_key(pack_id: str) -> str:
    return f"pack:{pack_id}"


def has_pack_zip(pack_id: str) -> bool:
    """Есть ли целый архив custom-пака."""
    if pack_id not in _PACK_IDS:
        return False
    for rel in _pack_zip_candidates(pack_id):
        if _open_asset_zip(rel, _pack_cache_key(pack_id) + "|" + rel) is not None:
            return True
    return False


def _pack_zip_candidates(pack_id: str) -> list:
    """Относительные пути zip пака: новый -> legacy (переходный fallback)."""
    cands = []
    if pack_id in _PACK_ZIPS:
        cands.append(_PACK_ZIPS[pack_id])
    if pack_id in _PACK_LEGACY_ZIPS:
        cands.append(_PACK_LEGACY_ZIPS[pack_id])
    return cands


def _pack_dir_candidates(pack_id: str) -> list:
    """Относительные пути папок пака: новая -> legacy."""
    cands = []
    if pack_id in _PACK_DIRS:
        cands.append(_PACK_DIRS[pack_id])
    if pack_id in _PACK_LEGACY_DIRS:
        cands.append(_PACK_LEGACY_DIRS[pack_id])
    return cands


def list_pack_names(pack_id: str) -> list:
    """Имена файлов custom-пака (zip + fallback папка, без дублей)."""
    if pack_id not in _PACK_IDS:
        return []
    seen: set = set()
    out: list = []
    for rel in _pack_zip_candidates(pack_id):
        zf = _open_asset_zip(rel, _pack_cache_key(pack_id) + "|" + rel)
        if zf is None:
            continue
        try:
            for n in zf.namelist():
                safe = _safe_name(n)
                if safe and n == safe and safe not in seen:
                    seen.add(safe)
                    out.append(safe)
        except Exception:  # noqa: BLE001 - дальше fallback на папку
            pass
        break  # первый найденный zip побеждает
    for base in _asset_roots():
        for rel in _pack_dir_candidates(pack_id):
            d = base / rel
            try:
                if d.is_dir():
                    for f in sorted(d.iterdir()):
                        if f.is_file() and f.suffix.lower() in (".svg", ".png"):
                            if f.name not in seen:
                                seen.add(f.name)
                                out.append(f.name)
            except OSError:
                pass
    return sorted(out)


def get_pack_bytes(pack_id: str, filename: str):
    """Байты файла custom-пака: zip -> папка. None - не найден."""
    if pack_id not in _PACK_IDS:
        return None
    name = _safe_name(filename)
    if not name:
        return None
    with _WARMED_LOCK:
        key = (_pack_cache_key(pack_id), name)
        if key in _WARMED:
            return _WARMED[key]
    for rel in _pack_zip_candidates(pack_id):
        zf = _open_asset_zip(rel, _pack_cache_key(pack_id) + "|" + rel)
        if zf is not None:
            data = _read_from_zip(zf, name)
            if data:
                return data
            break  # zip найден, но записи нет - дальше только папки
    for base in _asset_roots():
        for rel in _pack_dir_candidates(pack_id):
            candidate = base / rel / name
            try:
                if candidate.is_file():
                    return candidate.read_bytes()
            except OSError:
                pass
    return None


def warm_packs(pack_ids=None) -> int:
    """Прогреть кэш custom-паков в память. Вернуть число файлов."""
    ids = list(pack_ids) if pack_ids else list(_PACK_IDS)
    total = 0
    for pid in ids:
        if pid not in _PACK_IDS:
            continue
        zf = None
        for rel in _pack_zip_candidates(pid):
            zf = _open_asset_zip(rel, _pack_cache_key(pid) + "|" + rel)
            if zf is not None:
                break
        if zf is None:
            continue
        try:
            names = [n for n in zf.namelist() if _safe_name(n) and n == _safe_name(n)]
        except Exception:  # noqa: BLE001
            continue
        with _WARMED_LOCK:
            for name in names:
                key = (_pack_cache_key(pid), name)
                if key in _WARMED:
                    total += 1
                    continue
                data = _read_from_zip(zf, name)
                if data:
                    _WARMED[key] = data
                    total += 1
    return total


def invalidate(theme_id: str = "") -> None:
    """Сбросить кэши (пересборка zip на лету в dev). Пусто = все темы и паки."""
    pack_prefix = _pack_cache_key(theme_id) + "|" if theme_id else ""
    keys = [theme_id, _pack_cache_key(theme_id)] if theme_id else []
    with _ZIPS_LOCK:
        if theme_id:
            # Паки кэшируются с суффиксом "|relpath" (новый + legacy zip).
            for k in [k for k in _ZIPS if k in keys or k.startswith(pack_prefix)]:
                zf = _ZIPS.pop(k, None)
                if zf is not None:
                    try:
                        zf.close()
                    except Exception:  # noqa: BLE001
                        pass
        else:
            for zf in _ZIPS.values():
                if zf is not None:
                    try:
                        zf.close()
                    except Exception:  # noqa: BLE001
                        pass
            _ZIPS.clear()
    with _WARMED_LOCK:
        if theme_id:
            for key in [k for k in _WARMED if k[0] in keys]:
                del _WARMED[key]
        else:
            _WARMED.clear()
    if theme_id:
        for k in keys:
            _BROKEN_LOGGED.discard(k)
    else:
        _BROKEN_LOGGED.clear()
