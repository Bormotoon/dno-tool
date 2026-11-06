"""Выбор иконки кнопки: пак / тема / свой файл. С поиском и превью."""

import os
import shutil

from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QLabel,
    QPushButton,
    QComboBox,
    QDialogButtonBox,
    QFileDialog,
)

from src.config.custom_icons import (
    list_pack_icons,
    list_color_pack_icons,
    get_user_icons_dir,
    pack_icon_path,
)


class IconPickerDialog(QDialog):
    """Окно выбора иконки. Результат - spec строка."""

    def __init__(self, current_spec: str = "", parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Выбор иконки")
        self.resize(620, 520)
        self.result_spec = current_spec or ""

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.source = QComboBox(self)
        self.source.addItems(["Пак", "Пак цветной", "Тема", "Свои"])
        self.source.currentIndexChanged.connect(self._reload)
        top.addWidget(self.source)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Поиск...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        top.addWidget(self.search, 1)
        layout.addLayout(top)

        mid = QHBoxLayout()
        self.list = QListWidget(self)
        self.list.setIconSize(QSize(24, 24))
        self.list.itemSelectionChanged.connect(self._preview)
        self.list.itemDoubleClicked.connect(lambda *_: self.accept())
        mid.addWidget(self.list, 1)
        right = QVBoxLayout()
        self.preview = QLabel("?", self)
        self.preview.setFixedSize(96, 96)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("border: 1px solid palette(mid); font-size: 40px;")
        right.addWidget(self.preview)
        self.spec_label = QLabel("", self)
        self.spec_label.setWordWrap(True)
        self.spec_label.setMaximumWidth(180)
        right.addWidget(self.spec_label)
        self.file_btn = QPushButton("Свой файл...", self)
        self.file_btn.clicked.connect(self._pick_file)
        right.addWidget(self.file_btn)
        right.addStretch(1)
        mid.addLayout(right)
        layout.addLayout(mid, 1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._theme_keys: list = []
        try:
            from src.config import ICONS

            self._theme_keys = sorted(ICONS.keys())
        except Exception:
            pass
        self._pack_names = list_pack_icons()
        self._color_names = list_color_pack_icons()
        self._user_names: list = []
        self._reload()

    def _current_names(self) -> list:
        idx = self.source.currentIndex()
        if idx == 1:
            return list(self._color_names)
        if idx == 2:
            return list(self._theme_keys)
        if idx == 3:
            from src.config.custom_icons import list_user_icons

            self._user_names = list_user_icons()
            return list(self._user_names)
        return list(self._pack_names)

    def _spec_for(self, name: str) -> str:
        idx = self.source.currentIndex()
        if idx == 1:
            return f"color:{name}"
        if idx == 2:
            return f"theme:{name}"
        if idx == 3:
            return f"file:{os.path.join(get_user_icons_dir(), name)}"
        return f"pack:{name}"

    def _reload(self):
        self._apply_filter(self.search.text() if hasattr(self, "search") else "")

    def _apply_filter(self, text: str):
        from src.ui.widgets.custom_toolbar import custom_qicon

        q = (text or "").strip().lower()
        names = self._current_names()
        if q:
            names = [n for n in names if q in n.lower()]
        # Лимит для скорости: первые 400
        names = names[:400]
        self.list.clear()
        for n in names:
            item = QListWidgetItem(n)
            try:
                item.setIcon(custom_qicon(self._spec_for(n)))
            except Exception:
                pass
            item.setData(Qt.ItemDataRole.UserRole, self._spec_for(n))
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _preview(self):
        from src.ui.widgets.custom_toolbar import custom_qicon

        items = self.list.selectedItems()
        if not items:
            return
        spec = items[0].data(Qt.ItemDataRole.UserRole) or ""
        self.result_spec = spec
        self.spec_label.setText(spec)
        try:
            icon = custom_qicon(spec)
            pm = icon.pixmap(QSize(64, 64))
            if pm and not pm.isNull():
                self.preview.setPixmap(
                    pm.scaled(
                        QSize(64, 64),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                return
        except Exception:
            pass
        self.preview.setText("?")

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Своя иконка",
            "",
            "Иконки (*.svg *.png);;Все файлы (*)",
        )
        if not path:
            return
        # Копируем в user dir, чтобы путь не протух
        try:
            dest_dir = get_user_icons_dir()
            dest = os.path.join(dest_dir, os.path.basename(path))
            if os.path.abspath(path) != os.path.abspath(dest):
                shutil.copyfile(path, dest)
            self.result_spec = f"file:{dest}"
        except Exception:
            self.result_spec = f"file:{path}"
        self.accept()

    def accept(self):
        items = self.list.selectedItems()
        if items and not self.result_spec:
            self.result_spec = items[0].data(Qt.ItemDataRole.UserRole) or ""
        super().accept()
