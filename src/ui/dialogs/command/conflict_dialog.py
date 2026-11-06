"""Диалог разрешения конфликтов импорта задач.

Показывается, когда импортированная задача совпадает с локальной,
но другое имя или другая категория. Решение общее на все конфликты текущего импорта.
"""

from typing import Dict, List, Optional

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
)


def _format_path(path) -> str:
    """Собрать читаемый путь категории."""
    parts = [str(p) for p in (path or ()) if p]
    return " / ".join(parts) if parts else "(корень)"


class CommandConflictDialog(QDialog):
    """Общее решение по конфликтам импорта.

    Args:
        parent: Родительский виджет.
        conflicts: Список конфликтов из find_conflicts.
        max_shown: Сколько примеров показать в списке.
    """

    KEEP_MINE = "mine"
    TAKE_THEIRS = "theirs"

    def __init__(
        self,
        parent=None,
        conflicts: Optional[List[Dict]] = None,
        max_shown: int = 10,
    ) -> None:
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self._conflicts = conflicts or []
        self._max_shown = max_shown
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("Конфликты импорта")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)

        count = len(self._conflicts)
        if count % 10 == 1 and count % 100 != 11:
            head = f"Найдено расхождение: {count}. Это та же задача, "
        elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
            head = f"Найдено расхождения: {count}. Это те же задачи, "
        else:
            head = f"Найдено расхождений: {count}. Это те же задачи, "
        info = QLabel(
            head + "но другое имя или другая категория. Выберите, что делать "
            "со всеми такими задачами. Содержимое команд обновится из файла в любом случае."
        )
        info.setWordWrap(True)
        info.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.addWidget(info)

        self._list = QListWidget()
        self._list.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        for c in self._conflicts[: self._max_shown]:
            local = f'{_format_path(c.get("local_path"))} → "{c.get("local_name")}"'
            imported = (
                f'{_format_path(c.get("imported_path"))} → "{c.get("imported_name")}"'
            )
            self._list.addItem(f"Моя: {local}\nИз файла: {imported}")
        if count > self._max_shown:
            self._list.addItem(f"... и еще {count - self._max_shown}")
        self._list.setMaximumHeight(220)
        layout.addWidget(self._list)

        self._mine_radio = QRadioButton("Оставить мои имена и места (рекомендуется)")
        self._mine_radio.setChecked(True)
        self._mine_radio.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._mine_radio.setToolTip(
            "Имена и позиции в дереве не меняются, остальное обновляется из файла."
        )
        layout.addWidget(self._mine_radio)

        self._theirs_radio = QRadioButton("Взять имена и места из файла")
        self._theirs_radio.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._theirs_radio.setToolTip(
            "Имена перезаписываются, задачи перемещаются в категории из файла."
        )
        layout.addWidget(self._theirs_radio)

        group = QButtonGroup(self)
        group.addButton(self._mine_radio)
        group.addButton(self._theirs_radio)

        self._apply_all = QCheckBox(
            "Применить ко всем конфликтам и больше не спрашивать"
        )
        self._apply_all.setChecked(True)
        self._apply_all.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._apply_all.setToolTip("Решение действует на все конфликты этого импорта.")
        layout.addWidget(self._apply_all)

        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Применить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def resolution(self) -> Dict[str, str]:
        """Вернуть решение {"name": ..., "parent": ...}."""
        if self._theirs_radio.isChecked():
            return {"name": self.TAKE_THEIRS, "parent": self.TAKE_THEIRS}
        return {"name": self.KEEP_MINE, "parent": self.KEEP_MINE}
