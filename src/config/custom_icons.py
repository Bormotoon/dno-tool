"""Пак иконок пользовательских панелей (zip + user dir).

Формат поля icon у кнопки: ``theme:<key>`` | ``pack:<basename>`` |
``color:<basename>`` | ``file:<путь>``. Пустое = иконка команды по умолчанию.

``pack:`` - монохромный пак (``assets/custom/mono.zip`` с fallback на
папку ``assets/custom/mono/``, затем ``~/.dno-tool/custom_icons/``).
``color:`` - цветной пак (``assets/custom/color.zip`` с fallback
на папку, генерируется scripts/gen_colored_custom_icons.py
из монохромного): акцентные цвета по смыслу, видны на любой теме
без подкраски. Windows-бандл везет zip внутри, Linux-сборка (/opt) -
рядом с бинарником во внешнем assets/.
"""

import os
from os.path import expanduser
from pathlib import Path

from src.config.settings import get_asset_path

# Каталог своих иконок пользователя (данные, НЕ переименовывать).
USER_ICONS_DIRNAME = "custom_icons"
# Логические id паков в icon_theme_loader (пути - custom/mono, custom/color).
MONO_PACK_ID = "mono"
COLOR_PACK_ID = "color"
MONO_PACK_DIRNAME = "custom/mono"
COLOR_PACK_DIRNAME = "custom/color"


def get_pack_dir() -> str:
    """Каталог бандла иконок (assets/custom/mono)."""
    return get_asset_path(MONO_PACK_DIRNAME)


def get_color_pack_dir() -> str:
    """Каталог цветного пака (assets/custom/color)."""
    return get_asset_path(COLOR_PACK_DIRNAME)


def get_user_icons_dir() -> str:
    """Каталог своих иконок пользователя (~/.dno-tool/custom_icons)."""
    d = os.path.join(expanduser("~"), ".dno-tool", USER_ICONS_DIRNAME)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _list_svg_files(directory: str) -> list:
    try:
        p = Path(directory)
        if not p.is_dir():
            return []
        return sorted(
            [
                f.name
                for f in p.iterdir()
                if f.is_file() and f.suffix.lower() in (".svg", ".png")
            ]
        )
    except Exception:
        return []


def list_pack_icons() -> list:
    """Имена файлов пака из бандла (zip -> папка)."""
    try:
        from src.config.icon_theme_loader import list_pack_names

        names = list_pack_names(MONO_PACK_ID)
        if names:
            return names
    except Exception:  # noqa: BLE001 - откат на папку
        pass
    return _list_svg_files(get_pack_dir())


def list_color_pack_icons() -> list:
    """Имена файлов цветного пака из бандла (zip -> папка)."""
    try:
        from src.config.icon_theme_loader import list_pack_names

        names = list_pack_names(COLOR_PACK_ID)
        if names:
            return names
    except Exception:  # noqa: BLE001
        pass
    return _list_svg_files(get_color_pack_dir())


def pack_icon_bytes(basename: str):
    """Байты иконки монохромного пака (zip -> папка -> user dir)."""
    base = os.path.basename(basename or "")
    if not base:
        return None
    try:
        from src.config.icon_theme_loader import get_pack_bytes

        data = get_pack_bytes(MONO_PACK_ID, base)
        if data:
            return data
    except Exception:  # noqa: BLE001
        pass
    for d in (get_pack_dir(), get_user_icons_dir()):
        p = os.path.join(d, base)
        try:
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    return f.read()
        except OSError:
            continue
    return None


def color_pack_icon_bytes(basename: str):
    """Байты иконки цветного пака (zip -> папка)."""
    base = os.path.basename(basename or "")
    if not base:
        return None
    try:
        from src.config.icon_theme_loader import get_pack_bytes

        data = get_pack_bytes(COLOR_PACK_ID, base)
        if data:
            return data
    except Exception:  # noqa: BLE001
        pass
    p = os.path.join(get_color_pack_dir(), base)
    try:
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
    except OSError:
        pass
    return None


def list_user_icons() -> list:
    """Имена файлов своих иконок пользователя."""
    return _list_svg_files(get_user_icons_dir())


def pack_icon_path(basename: str) -> str:
    """Абсолютный путь к иконке пака (бандл, затем user dir)."""
    base = os.path.basename(basename or "")
    if not base:
        return ""
    for d in (get_pack_dir(), get_user_icons_dir()):
        p = os.path.join(d, base)
        if os.path.isfile(p):
            return p
    return ""


def color_pack_icon_path(basename: str) -> str:
    """Абсолютный путь к иконке цветного пака."""
    base = os.path.basename(basename or "")
    if not base:
        return ""
    p = os.path.join(get_color_pack_dir(), base)
    if os.path.isfile(p):
        return p
    return ""


def resolve_icon_spec(spec: str) -> tuple:
    """Разобрать spec в (kind, value). kind: theme|pack|color|file|empty."""
    s = (spec or "").strip()
    if not s:
        return ("empty", "")
    if s.startswith("theme:"):
        return ("theme", s[len("theme:") :])
    if s.startswith("pack:"):
        return ("pack", s[len("pack:") :])
    if s.startswith("color:"):
        return ("color", s[len("color:") :])
    if s.startswith("file:"):
        return ("file", s[len("file:") :])
    # Legacy: голый basename пака или ключ темы
    if "/" in s or "\\" in s or s.lower().endswith((".svg", ".png")):
        return ("file", s)
    return ("theme", s)
