"""Менеджер тем иконок (Qt-free обертка над src.config.settings).

Каждая тема - архив ``assets/icons/<id>.zip`` (исходник - папка
``assets/icons/<id>/`` с полным набором SVG, те же имена файлов),
список - ``assets/icons/themes.json``. Чтение - на лету без распаковки
(``src.config.icon_theme_loader``).
Применение темы обновляет словарь ``ICONS`` in-place через
``set_icon_theme``; виджеты, зарегистрированные через
``src.ui.icons.set_themed_icon``, перечитываются ``refresh_themed_icons``.
"""

from src.config import (
    ICON_THEME_FALLBACK,
    get_icon_theme,
    list_icon_themes,
    remap_icon_path,
    set_icon_theme,
)

__all__ = [
    "ICON_THEME_FALLBACK",
    "apply_icon_theme",
    "get_icon_theme",
    "list_icon_themes",
    "remap_icon_path",
]


def apply_icon_theme(theme_id: str) -> str:
    """Применить тему иконок, вернуть фактически установленный id.

    Пустой/неизвестный id откатывается на fallback. Перерисовкой
    виджетов занимается вызыватель (MainWindow.refresh_icons).
    """
    return set_icon_theme(theme_id or ICON_THEME_FALLBACK)
