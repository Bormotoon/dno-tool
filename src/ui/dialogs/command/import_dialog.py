"""Диалог импорта задач: выбор режима Заменить / Обновить с превью."""

from typing import Dict, Optional

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
)


class CommandImportDialog(QDialog):
    """Выбор режима импорта задач с превью merge-статистики.

    Args:
        parent: Родительский виджет.
        preview_stats: Статистика preview_merge (или None при ошибке).
        local_count: Число локальных задач до импорта.
    """

    MODE_MERGE = "merge"
    MODE_REPLACE = "replace"

    def __init__(
        self,
        parent=None,
        preview_stats: Optional[Dict] = None,
        local_count: int = 0,
    ) -> None:
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self._preview = preview_stats or {}
        self._local_count = local_count
        self._setup_ui()
        self.adjustSize()
        self.resize(self.width(), round(self.height() * 1.2))

    def _setup_ui(self) -> None:
        self.setWindowTitle("Импорт задач")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)

        info = QLabel(
            "Выберите режим импорта. Те же задачи находятся сами, "
            "новые задачи добавляются в совпавшую категорию."
        )
        info.setWordWrap(True)
        info.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.addWidget(info)

        self._merge_radio = QRadioButton("Обновить (рекомендуется)")
        self._merge_radio.setChecked(True)
        self._merge_radio.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._merge_radio.setToolTip(
            "Обновить найденные задачи на месте, добавить новые. "
            "Ничего не удаляет и не перемещает."
        )
        layout.addWidget(self._merge_radio)

        self._replace_radio = QRadioButton("Заменить все")
        self._replace_radio.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._replace_radio.setToolTip(
            "Полностью заменить текущие задачи содержимым файла."
        )
        layout.addWidget(self._replace_radio)

        group = QButtonGroup(self)
        group.addButton(self._merge_radio)
        group.addButton(self._replace_radio)

        self._preview_label = QLabel(self._build_preview_text())
        self._preview_label.setWordWrap(True)
        self._preview_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.addWidget(self._preview_label)

        self._warn_label = QLabel("")
        self._warn_label.setWordWrap(True)
        self._warn_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.addWidget(self._warn_label)

        self._merge_radio.toggled.connect(self._refresh_warning)
        self._refresh_warning()

        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Импортировать")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_preview_text(self) -> str:
        s = self._preview
        if not s:
            return f"Локальных задач: {self._local_count}. Превью недоступно."
        lines = [
            f"Локальных задач: {self._local_count}, "
            f"в файле: {s.get('total_imported', 0)}.",
            f"Будет обновлено: {s.get('updated', 0)}, добавлено: {s.get('added', 0)}.",
        ]
        extra = []
        if s.get("added_categories"):
            extra.append(f"новых разделов: {s['added_categories']}")
        if s.get("skipped_invalid"):
            extra.append(f"пропущено невалидных: {s['skipped_invalid']}")
        if s.get("duplicate_iids"):
            extra.append(f"повторов в файле: {s['duplicate_iids']}")
        if extra:
            lines.append("Дополнительно: " + ", ".join(extra) + ".")
        return "\n".join(lines)

    def _refresh_warning(self) -> None:
        if self._replace_radio.isChecked():
            self._warn_label.setText(
                "Внимание: режим Заменить удалит текущие задачи и локальные изменения."
            )
            self._warn_label.setVisible(True)
        else:
            self._warn_label.setText("")
            self._warn_label.setVisible(False)

    def selected_mode(self) -> str:
        """Вернуть выбранный режим: merge или replace."""
        if self._replace_radio.isChecked():
            return self.MODE_REPLACE
        return self.MODE_MERGE
