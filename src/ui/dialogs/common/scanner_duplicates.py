"""Выбор повторно добавляемых хостов при импорте из сканера."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


class ScannerDuplicatesDialog(QDialog):
    """Показывает совпадения и возвращает индексы разрешённых копий."""

    def __init__(
        self,
        duplicates: list[tuple[int, str, str, str]],
        new_count: int,
        target_folder: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Сканирование — хосты уже существуют")
        self.resize(800, 460)
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        label = QLabel(
            f"Новых хостов: {new_count}. Уже существуют: {len(duplicates)}.\n"
            "Совпадения определяются по адресу и SSH-порту во всём локальном дереве.\n"
            "Отметьте хосты, которые нужно добавить повторно. "
            "Неотмеченные дубликаты будут пропущены."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        target = QLabel(f"Папка назначения: {target_folder}")
        target.setTextFormat(Qt.TextFormat.PlainText)
        target.setWordWrap(True)
        layout.addWidget(target)

        self._tree = QTreeWidget(self)
        self._tree.setHeaderLabels(["Добавить повторно", "Адрес : SSH-порт", "Уже находится в папках"])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        for index, name, address, locations in duplicates:
            item = QTreeWidgetItem([name, address, locations])
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            item.setToolTip(2, locations)
            self._tree.addTopLevelItem(item)
        self._tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._tree.setColumnWidth(0, 220)
        self._tree.setColumnWidth(1, 180)
        layout.addWidget(self._tree)

        actions = QHBoxLayout()
        select_all = QPushButton("Отметить все")
        select_all.clicked.connect(lambda: self._mark_all(Qt.CheckState.Checked))
        clear_all = QPushButton("Снять все")
        clear_all.clicked.connect(lambda: self._mark_all(Qt.CheckState.Unchecked))
        actions.addWidget(select_all)
        actions.addWidget(clear_all)
        actions.addStretch()
        layout.addLayout(actions)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена импорта")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)
        self._new_count = new_count
        self._tree.itemChanged.connect(self._update_count)
        self._update_count()

    def selected_indices(self) -> set[int]:
        """Вернуть индексы хостов, отмеченных для повторного добавления."""
        return {
            self._tree.topLevelItem(row).data(0, Qt.ItemDataRole.UserRole)
            for row in range(self._tree.topLevelItemCount())
            if self._tree.topLevelItem(row).checkState(0) == Qt.CheckState.Checked
        }

    def _mark_all(self, state: Qt.CheckState) -> None:
        self._tree.blockSignals(True)
        try:
            for row in range(self._tree.topLevelItemCount()):
                self._tree.topLevelItem(row).setCheckState(0, state)
        finally:
            self._tree.blockSignals(False)
        self._update_count()

    def _update_count(self) -> None:
        count = self._new_count + len(self.selected_indices())
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            f"Добавить · {count}" if count else "Пропустить все"
        )
