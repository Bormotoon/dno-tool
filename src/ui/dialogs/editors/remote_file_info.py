"""Диалог просмотра и редактирования прав доступа удалённого файла."""

import stat

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QLabel,
    QGridLayout,
    QCheckBox,
    QHBoxLayout,
    QPushButton,
    QLineEdit,
)
from PySide6.QtCore import Qt


BITS = [
    stat.S_IRUSR,
    stat.S_IWUSR,
    stat.S_IXUSR,
    stat.S_IRGRP,
    stat.S_IWGRP,
    stat.S_IXGRP,
    stat.S_IROTH,
    stat.S_IWOTH,
    stat.S_IXOTH,
]


class RemoteFileInfoDialog(QDialog):
    def __init__(self, path, mode, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle(f"Права: {path}")
        self._syncing = False
        layout = QVBoxLayout(self)

        octal_layout = QHBoxLayout()
        octal_label = QLabel("Числовое значение:")
        self.octal_edit = QLineEdit()
        self.octal_edit.setMaxLength(4)
        self.octal_edit.setFixedWidth(80)
        self.octal_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.octal_edit.setText(oct(mode)[-3:])
        self.octal_edit.textEdited.connect(self._on_octal_edited)
        octal_layout.addWidget(octal_label)
        octal_layout.addWidget(self.octal_edit)
        octal_layout.addStretch()
        layout.addLayout(octal_layout)

        grid = QGridLayout()
        self.checks = {}
        labels = ["Чтение", "Запись", "Исполнение"]
        roles = ["Пользователь", "Группа", "Остальные"]
        for i, role in enumerate(roles):
            grid.addWidget(QLabel(role), i + 1, 0)
        for j, label in enumerate(labels):
            grid.addWidget(QLabel(label), 0, j + 1)
        for i in range(3):
            for j in range(3):
                bit = BITS[i * 3 + j]
                cb = QCheckBox()
                cb.setChecked(bool(mode & bit))
                cb.stateChanged.connect(self._on_check_changed)
                self.checks[(i, j)] = cb
                grid.addWidget(cb, i + 1, j + 1)
        layout.addLayout(grid)

        btns = QHBoxLayout()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Отмена")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(ok_btn)
        btns.addWidget(cancel_btn)
        layout.addLayout(btns)

    def _update_display(self):
        mode = self._compute_mode()
        octal = oct(mode)[-3:]
        self._syncing = True
        self.octal_edit.setText(octal)
        self._syncing = False

    def _on_check_changed(self):
        if self._syncing:
            return
        self._update_display()

    def _on_octal_edited(self, text):
        if self._syncing:
            return
        try:
            clean = text.strip().lstrip("0o") or "0"
            mode = int(clean, 8)
            if mode > 0o777:
                return
            self._syncing = True
            for i in range(3):
                for j in range(3):
                    self.checks[(i, j)].setChecked(bool(mode & BITS[i * 3 + j]))
            self._syncing = False
        except ValueError:
            pass

    def _compute_mode(self):
        mode = 0
        for i in range(3):
            for j in range(3):
                if self.checks[(i, j)].isChecked():
                    mode |= BITS[i * 3 + j]
        return mode

    def get_mode(self):
        return self._compute_mode()
