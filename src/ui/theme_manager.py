"""Менеджер тем оформления (QPalette).

Темы лежат по одной папке в ``assets/themes/<id>/`` - рядом ``theme.json``
палитры и запись в ``assets/themes/manifest.json``. Никакого QSS: тема -
это только Fusion + QPalette, поэтому перекрыть QSS темы стилями
компонентов или конфликтовать с ними нечем.

Формат ``theme.json`` - палитры qt-themes (MIT): primary/secondary,
red..blue, text/subtext*, overlay0..2, surface0..2, base/mantle/crust.
Логика раскладки по ролям - ``_update_palette``.

Тема ``system`` - нативные стиль и палитра, бывшие на старте приложения.
"""

import json

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

from src.config import get_asset_path
from src.logger import logger

SYSTEM_THEME_ID = "system"
FALLBACK_THEME_ID = "github-dark"
BASE_STYLE_NAME = "Fusion"

# Нативный стиль и палитра на момент первого вызова (обычно старт приложения).
# Возвращаем их при выборе системной темы, иначе после другой темы
# «системная» выглядела бы как Fusion с чужой палитрой.
_ORIG_STYLE_NAME: str = ""
_ORIG_PALETTE: QPalette = None


def _themes_dir() -> str:
    """Каталог с темами и манифестом (работает и во frozen-сборке)."""
    return get_asset_path("themes")


def list_themes() -> list:
    """Список тем из манифеста. При битом манифесте - только системная."""
    manifest_path = f"{_themes_dir()}/manifest.json"
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return data
    except (OSError, ValueError) as e:
        logger.error(f"Темы: не читается манифест {manifest_path}: {e}")
    return [
        {
            "id": SYSTEM_THEME_ID,
            "name": "Системная",
            "dark": None,
            "file": None,
            "palette_file": None,
        }
    ]


def get_theme(theme_id: str):
    """Запись темы по id или None."""
    for theme in list_themes():
        if theme.get("id") == theme_id:
            return theme
    return None


def read_theme_palette(theme_id: str):
    """Собрать QPalette темы. None для system или при ошибке."""
    theme = get_theme(theme_id)
    if theme is None or not theme.get("palette_file"):
        return None
    return _palette_from_file(theme["palette_file"])


def apply_theme(app: QApplication, theme_id: str) -> str:
    """Применить тему, вернуть фактически установленный id.

    system - вернуть нативные стиль и палитру, снять QSS. Остальные -
    Fusion + палитра из ``theme.json`` (QSS снимается: палитровых тем
    он не содержит). Пустой id считается битым конфигом и откатывается
    на fallback.
    При любой ошибке - откат на fallback, затем на system.
    """
    global _ORIG_STYLE_NAME, _ORIG_PALETTE
    if not _ORIG_STYLE_NAME:
        # QStyleFactory.create() принимает ключ ("windowsvista"), а не имя
        # класса ("QWindowsVistaStyle") - находим ключ текущего стиля.
        try:
            current = app.style().metaObject().className()
            for key in QStyleFactory.keys():
                style = QStyleFactory.create(key)
                if style is not None and style.metaObject().className() == current:
                    _ORIG_STYLE_NAME = key
                    break
        except Exception:
            pass
    if _ORIG_PALETTE is None:
        try:
            _ORIG_PALETTE = QPalette(app.palette())
        except Exception:
            _ORIG_PALETTE = None
    if not theme_id:
        # Пустая строка (битый конфиг) = дефолтная палитровая тема,
        # не «системная»: системную надо выбирать явно.
        return apply_theme(app, FALLBACK_THEME_ID)
    if theme_id == SYSTEM_THEME_ID:
        _restore_native(app)
        return SYSTEM_THEME_ID
    theme = get_theme(theme_id)
    if theme is None:
        logger.warning(f"Неизвестная тема {theme_id}, откат на {FALLBACK_THEME_ID}")
        return apply_theme(app, FALLBACK_THEME_ID)
    if BASE_STYLE_NAME in QStyleFactory.keys():
        try:
            app.setStyle(BASE_STYLE_NAME)
        except Exception as e:
            logger.warning(f"Не встал стиль {BASE_STYLE_NAME}: {e}")
    pal = read_theme_palette(theme_id)
    if pal is None and theme_id != FALLBACK_THEME_ID:
        logger.warning(f"Тема {theme_id} не прочиталась, откат на {FALLBACK_THEME_ID}")
        return apply_theme(app, FALLBACK_THEME_ID)
    if pal is None:
        _restore_native(app)
        return SYSTEM_THEME_ID
    try:
        app.setPalette(pal)
        app.setStyleSheet("")
    except Exception as e:
        logger.error(f"Тема {theme_id}: не применилась: {e}")
        _restore_native(app)
        return SYSTEM_THEME_ID
    return theme_id


def _restore_native(app: QApplication) -> None:
    """Вернуть нативные стиль и палитру (тема system)."""
    app.setStyleSheet("")
    if _ORIG_PALETTE is not None:
        try:
            app.setPalette(_ORIG_PALETTE)
        except Exception as e:
            logger.warning(f"Не вернулась нативная палитра: {e}")
    if _ORIG_STYLE_NAME:
        try:
            style = QStyleFactory.create(_ORIG_STYLE_NAME)
            if style is not None:
                app.setStyle(style)
            else:
                app.setStyle(_ORIG_STYLE_NAME)
        except Exception as e:
            logger.warning(f"Не вернулся нативный стиль {_ORIG_STYLE_NAME}: {e}")


def _palette_from_file(palette_file: str):
    """QPalette из JSON палитры (формат qt-themes, MIT). None при ошибке."""
    path = f"{_themes_dir()}/{palette_file}"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        colors = {key: QColor(value) for key, value in data.items()}
    except (OSError, ValueError) as e:
        logger.error(f"Палитра не читается {path}: {e}")
        return None
    required = (
        "primary",
        "secondary",
        "text",
        "base",
        "mantle",
        "crust",
        "surface0",
        "surface1",
        "surface2",
        "overlay0",
        "overlay1",
        "overlay2",
    )
    if any(key not in colors or not colors[key].isValid() for key in required):
        logger.error(f"В палитре {path} нет обязательных цветов")
        return None
    try:
        return _update_palette(QPalette(), colors)
    except Exception as e:
        logger.error(f"Палитра {path} не собралась: {e}")
        return None


def _update_palette(pal: QPalette, c: dict) -> QPalette:
    """Разложить цвета палитры по ролям (логика qt-themes update_palette)."""
    group = QPalette.ColorGroup
    role = QPalette.ColorRole
    highlighted = c["primary"]
    highlighted_text = c["mantle"] if highlighted.valueF() > 0.5 else c["text"]
    h, s, v, a = c["text"].getHsvF()
    bright_text = QColor.fromHsvF(h, s, 1 - v, a)
    dark_theme = c["text"].value() > c["base"].value()
    if dark_theme:
        pal.setColor(role.Base, c["mantle"])
        pal.setColor(role.AlternateBase, c["base"])
    else:
        pal.setColor(role.Base, c["crust"])
        pal.setColor(role.AlternateBase, c["mantle"])
    pal.setColor(role.Window, c["base"])
    pal.setColor(role.WindowText, c["text"])
    pal.setColor(role.PlaceholderText, c["overlay1"])
    pal.setColor(role.Text, c["text"])
    pal.setColor(role.Button, c["base"])
    pal.setColor(role.ButtonText, c["text"])
    pal.setColor(role.BrightText, bright_text)
    pal.setColor(role.ToolTipBase, c["mantle"])
    pal.setColor(role.ToolTipText, c["overlay2"])
    pal.setColor(role.Highlight, highlighted)
    pal.setColor(role.HighlightedText, highlighted_text)
    pal.setColor(role.Link, c["secondary"])
    pal.setColor(role.LinkVisited, c["secondary"])
    pal.setColor(role.Light, c["crust"])
    pal.setColor(role.Midlight, c["mantle"])
    pal.setColor(role.Mid, c["surface0"])
    pal.setColor(role.Dark, c["surface1"])
    pal.setColor(role.Shadow, c["overlay0"])
    pal.setColor(group.Inactive, role.Highlight, c["surface1"])
    pal.setColor(group.Inactive, role.Link, c["surface1"])
    pal.setColor(group.Inactive, role.LinkVisited, c["surface1"])
    pal.setColor(group.Disabled, role.WindowText, c["overlay1"])
    pal.setColor(group.Disabled, role.Base, c["base"])
    pal.setColor(group.Disabled, role.AlternateBase, c["base"])
    pal.setColor(group.Disabled, role.Text, c["overlay1"])
    pal.setColor(group.Disabled, role.PlaceholderText, c["overlay1"])
    pal.setColor(group.Disabled, role.Button, c["base"])
    pal.setColor(group.Disabled, role.ButtonText, c["overlay1"])
    pal.setColor(group.Disabled, role.BrightText, c["mantle"])
    pal.setColor(group.Disabled, role.Highlight, c["surface2"])
    pal.setColor(group.Disabled, role.HighlightedText, c["surface0"])
    pal.setColor(group.Disabled, role.Link, c["surface0"])
    pal.setColor(group.Disabled, role.LinkVisited, c["surface0"])
    try:
        pal.setColor(role.Accent, c["secondary"])
        pal.setColor(group.Inactive, role.Accent, c["surface1"])
        pal.setColor(group.Disabled, role.Accent, c["surface2"])
    except Exception:
        pass
    return pal
