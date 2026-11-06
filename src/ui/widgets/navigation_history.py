"""История навигации по путям с поддержкой back/forward."""

from typing import List, Optional


class NavigationHistory:
    """Стек просмотренных путей с индексом текущего.

    Поддерживает переход назад/вперёд как в браузере: при переходе в новый путь
    forward-стек обрезается.
    """

    def __init__(self, initial: Optional[str] = None):
        self._items: List[str] = []
        self._index: int = -1
        if initial is not None:
            self.navigate(initial)

    def navigate(self, path: str) -> None:
        """Перейти в новый путь, обрезая forward-стек."""
        if self._items and self._index >= 0 and self._items[self._index] == path:
            return
        if self._index < len(self._items) - 1:
            self._items = self._items[: self._index + 1]
        self._items.append(path)
        self._index = len(self._items) - 1

    def back(self) -> Optional[str]:
        """Вернуться на шаг назад, вернуть путь или None."""
        if not self.can_back():
            return None
        self._index -= 1
        return self._items[self._index]

    def forward(self) -> Optional[str]:
        """Перейти вперёд, вернуть путь или None."""
        if not self.can_forward():
            return None
        self._index += 1
        return self._items[self._index]

    def can_back(self) -> bool:
        return self._index > 0

    def can_forward(self) -> bool:
        return 0 <= self._index < len(self._items) - 1

    def current(self) -> Optional[str]:
        if 0 <= self._index < len(self._items):
            return self._items[self._index]
        return None
