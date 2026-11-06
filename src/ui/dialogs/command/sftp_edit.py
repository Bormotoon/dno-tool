import os
import sys
import re
from pathlib import Path
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QTextEdit,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QFileDialog,
    QMenu,
)
from PySide6.QtGui import (
    QSyntaxHighlighter,
    QTextCharFormat,
    QColor,
    QFont,
    QTextDocument,
    QIcon,
)
from PySide6.QtCore import QDir

from src.config import ICONS
from src.ui.icons import _icon_from_key, qicon_from_path  # zip-темы: иконки из памяти

SPLITTER = r"::"


class SFTPCommandEditorDialog(QDialog):
    def __init__(self, parent=None, initial_text="sftp  ", custom_params=None):
        super().__init__(parent)
        self.custom_params = custom_params or []
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Редактор команд SFTP")
        self.setMinimumSize(600, 240)
        self.resize(600, 240)

        self.init_ui()

        parts = initial_text.split(SPLITTER)
        if len(parts) != 2:
            self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return None

        parts = parts[0:]

        source_parts = parts[0].split(":", 1)
        dest_parts = parts[1].split(":", 1)

        if len(source_parts) == 2 and "@" in source_parts[0]:
            self.editor_from.setText(str(source_parts[-1]))
            self.cb_command_type.setCurrentIndex(1)
        else:
            self.editor_from.setText(":".join(source_parts))

        if len(dest_parts) == 2 and "@" in dest_parts[0]:
            self.editor_to.setText(str(dest_parts[-1]))
            self.cb_command_type.setCurrentIndex(0)
        else:
            self.editor_to.setText(":".join(dest_parts))

        self.command_changed()
        self._update_browse_buttons()

    def init_ui(self):

        layout = QVBoxLayout(self)

        self.label_command_type = QLabel("Направление:")
        layout.addWidget(self.label_command_type)

        self.cb_command_type = QComboBox(self)
        self.cb_command_type.addItems(
            ["С локального на удаленный", "С удаленного на локальный"]
        )
        layout.addWidget(self.cb_command_type)

        self.label_from = QLabel("Путь файла, который нужно скопировать:")
        layout.addWidget(self.label_from)

        from_row = QHBoxLayout()
        self.editor_from = QTextEdit(self)
        self.editor_from.setPlaceholderText("Путь файла, который нужно скопировать")
        font = QFont("Monospace")
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        self.editor_from.setFont(font)
        from_row.addWidget(self.editor_from)

        self.btn_browse_from = QPushButton(self)
        self.btn_browse_from.setIcon(_icon_from_key("folder"))
        self.btn_browse_from.setToolTip("Выбрать локальный файл")
        self.btn_browse_from.setFixedSize(32, 32)
        self.btn_browse_from.clicked.connect(
            lambda: self._browse_file(self.editor_from)
        )
        from_row.addWidget(self.btn_browse_from)
        from_row.addWidget(self._parameter_button(self.editor_from))
        layout.addLayout(from_row)

        self.label_to = QLabel("Путь файла, куда нужно скопировать:")
        layout.addWidget(self.label_to)

        to_row = QHBoxLayout()
        self.editor_to = QTextEdit(self)
        self.editor_to.setPlaceholderText("Путь файла, куда нужно скопировать")
        font = QFont("Monospace")
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        self.editor_to.setFont(font)
        to_row.addWidget(self.editor_to)

        self.btn_browse_to = QPushButton(self)
        self.btn_browse_to.setIcon(_icon_from_key("folder"))
        self.btn_browse_to.setToolTip("Выбрать локальный файл")
        self.btn_browse_to.setFixedSize(32, 32)
        self.btn_browse_to.clicked.connect(lambda: self._browse_file(self.editor_to))
        to_row.addWidget(self.btn_browse_to)
        to_row.addWidget(self._parameter_button(self.editor_to))
        layout.addLayout(to_row)

        upload_button = QPushButton("Файл задачи → домашняя папка пользователя…", self)
        upload_menu = QMenu(upload_button)
        for index, description in enumerate(self.custom_params, 1):
            if str(description).startswith("file:"):
                action = upload_menu.addAction(str(description)[5:].strip() or "Файл")
                action.triggered.connect(
                    lambda checked=False, number=index: self._set_home_upload(number)
                )
        upload_button.setMenu(upload_menu)
        upload_button.setEnabled(bool(upload_menu.actions()))
        upload_button.setToolTip(
            "Выберите файловый параметр задачи: оба пути будут заполнены автоматически. "
            "Добавьте параметр задачи и выберите для него тип «Файл»."
        )
        layout.addWidget(upload_button)

        hint = QLabel(
            "~ в локальном пути — домашняя папка оператора. С хоста можно скачать каталог "
            "целиком: путь с «/» на конце копирует содержимое прямо в папку назначения, "
            "без «/» — вместе с самим каталогом. Ссылки пропускаются."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self.setLayout(layout)

        self.cb_command_type.setCurrentIndex(0)
        self._update_browse_buttons()

        self.editor_from.textChanged.connect(self.command_changed)
        self.editor_to.textChanged.connect(self.command_changed)
        self.cb_command_type.currentIndexChanged.connect(self.command_changed)
        self.cb_command_type.currentIndexChanged.connect(self._update_browse_buttons)

    def _set_home_upload(self, number: int) -> None:
        """Настроить загрузку выбранного файла с сохранением имени."""
        self.cb_command_type.setCurrentIndex(0)
        self.editor_from.setPlainText(f"%param{number}%")
        self.editor_to.setPlainText(f"%active_home%/%param{number}:filename%")

    def _parameter_button(self, editor: QTextEdit) -> QPushButton:
        """Создать меню вставки параметров в выбранное поле пути."""
        button = QPushButton("Параметры", self)
        menu = QMenu(button)
        entries = [
            ("Домашняя папка активного пользователя", "%active_home%"),
            ("Имя активного пользователя", "%active_user%"),
            ("Имя хоста", "%hostname%"),
        ]
        for index, description in enumerate(self.custom_params, 1):
            entries.append((str(description), f"%param{index}%"))
            if str(description).startswith("file:"):
                entries.append((f"Имя файла: {str(description)[5:]}", f"%param{index}:filename%"))
        for label, token in entries:
            action = menu.addAction(label)
            action.triggered.connect(
                lambda checked=False, value=token: editor.insertPlainText(value)
            )
        button.setMenu(menu)
        return button

    def _browse_file(self, editor: QTextEdit):
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите файл", QDir.homePath(), "Все файлы (*)"
        )
        if path:
            try:
                app_dir = self._get_app_dir()
                rel_path = os.path.relpath(path, app_dir)
                if not rel_path.startswith(".."):
                    editor.setPlainText(rel_path)
                else:
                    editor.setPlainText(path)
            except ValueError:
                editor.setPlainText(path)

    def _get_app_dir(self) -> str:
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        project_root = str(Path(__file__).parent.parent.parent.parent)
        return project_root

    def _update_browse_buttons(self):
        idx = self.cb_command_type.currentIndex()
        self.btn_browse_from.setEnabled(idx == 0)
        self.btn_browse_to.setEnabled(idx == 1)

    def getText(self):
        command = ""
        if self.cb_command_type.currentIndex() == 0:
            command = f"{self.editor_from.toPlainText()}{SPLITTER}%login%@%hostname%:{self.editor_to.toPlainText()}"
        else:
            command = f"%login%@%hostname%:{self.editor_from.toPlainText()}{SPLITTER}{self.editor_to.toPlainText()}"
        return command

    def command_changed(self):
        if self.cb_command_type.currentIndex() == 0:
            command = f"sftp {self.editor_from.toPlainText()}{SPLITTER}%login%@%hostname%:{self.editor_to.toPlainText()}"
            self.editor_from.setToolTip(command)
            self.editor_to.setToolTip(command)
        else:
            command = f"sftp %login%@%hostname%:{self.editor_from.toPlainText()}{SPLITTER}{self.editor_to.toPlainText()}"
            self.editor_from.setToolTip(command)
            self.editor_to.setToolTip(command)

        if self.editor_from.toPlainText() and self.editor_to.toPlainText():
            self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        else:
            self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
