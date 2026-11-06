"""Базовый контракт страниц мастера (SetupPage).

Каждая страница - QWidget с sid. Shell вызывает on_enter при показе,
collect при уходе со страницы (Next/навигация/Finish), validate перед
уходом. Сохранение полного снимка выполняет ConfigService на Finish.
"""

from PySide6.QtWidgets import QLabel, QWidget


def hint_label(text: str) -> QLabel:
    """Серая многострочная подсказка вверху страницы (единый стиль)."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: gray;")
    return label


def clear_layout(layout) -> None:
    """Убрать все виджеты из layout без их удаления."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            layout.removeWidget(w)
            w.setParent(None)


class SetupPage(QWidget):
    """Базовая страница мастера."""

    sid: str = ""

    def __init__(self, parent=None):
        super().__init__(parent)

    # --- контракт, вызываемый shell ---

    def on_enter(self, ctx) -> None:
        """Вызывается при показе страницы (ленивая подгрузка)."""

    def collect(self, ctx) -> None:
        """Снапшот состояния виджетов в ctx. Без записи в config/services."""

    def validate(self, ctx) -> tuple:
        """Проверка перед уходом со страницы.

        Returns:
            (True, "") - можно идти дальше.
            (True, warn) - можно, но shell покажет предупреждение.
            (False, err) - блокировать переход.
        """
        return True, ""

    def summary_html(self, ctx) -> str:
        """Фрагмент сводки (строка html без обертки)."""
        return ""
