"""Редактор Bash-скриптов с подсветкой синтаксиса и вставкой параметров команд."""

import sys
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QVBoxLayout,
    QDialogButtonBox,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMenu,
    QMenuBar,
    QCheckBox,
)
from PySide6.QtGui import (
    QAction,
    QKeySequence,
    QShortcut,
)
from PySide6.QtCore import QSize, Qt
from src.ui.widgets.script_editor import ScriptEditor
from src.ui.icons import _icon_from_key, set_themed_icon


# Стандартные параметры CommandWorker
STANDARD_PARAMS = [
    ("%name%", "Имя устройства (псевдоним)"),
    ("%login%", "Логин пользователя (из устройства или конфига)"),
    ("%user%", "Логин пользователя (синоним %login%)"),
    ("%username%", "Логин пользователя (синоним %login%)"),
    ("%hostname%", "Хост устройства"),
    ("%host%", "Хост устройства (синоним %hostname%)"),
    ("%ip%", "IP-адрес устройства (синоним %host%)"),
    ("%port%", "Порт SSH устройства"),
    ("%password%", "Пароль пользователя"),
    ("%pass%", "Пароль пользователя (синоним %password%)"),
    ("%date%", "Текущая дата в формате YYYY.MM.DD"),
    ("%time%", "Текущее время в формате HHMMSS"),
    ("%timestamp%", "Временная метка в формате YYYY.MM.DD_HHMMSS"),
    ("%mac%", "MAC-адрес устройства"),
    ("%active_user%", "Владелец активного графического сеанса (SFTP и SSH-шаги)"),
    ("%active_home%", "Домашний каталог владельца активного сеанса (SFTP и SSH-шаги)"),
    ("%sudo%", "Повышение прав согласно настройкам SSH"),
]


class ParamsHelpDialog(QDialog):
    """
    Диалог справки по доступным параметрам команд.
    """

    def __init__(self, parent=None, custom_params=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Справка по параметрам")
        self.setMinimumSize(500, 400)

        layout = QVBoxLayout(self)

        # Описание
        desc_label = QLabel(
            "Параметры автоматически заменяются на значения при выполнении команды.\n"
            "Используйте %имя% для вставки значения."
        )
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        file_help = QLabel(
            "Для выбора файла перед запуском выберите тип «Файл» в редакторе параметра задачи "
            "и используйте %paramN% в пути источника SFTP. "
            "%paramN:filename% — имя файла без локального пути; "
            "%paramN:filename:base64% — это имя в Base64 для shell. "
            "Например: %param1%::%login%@%hostname%:%active_home%/document.pdf. "
            "Активный пользователь определяется на Linux; права передачи остаются у SSH-пользователя. "
            "В SSH-шаге без «Окружения рабочего стола» %active_user% и %active_home% позволяют "
            "работать с файлами этого пользователя от root."
        )
        file_help.setWordWrap(True)
        layout.addWidget(file_help)

        # Таблица параметров
        self.params_table = QTableWidget()
        self.params_table.setColumnCount(2)
        self.params_table.setHorizontalHeaderLabels(["Параметр", "Описание"])
        self.params_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.params_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.params_table.setColumnWidth(0, 120)
        self.params_table.verticalHeader().setVisible(False)
        self.params_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.params_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.params_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.params_table)

        # Заполняем стандартные параметры
        row = 0
        for param, description in STANDARD_PARAMS:
            self.params_table.insertRow(row)
            param_item = QTableWidgetItem(param)
            param_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.params_table.setItem(row, 0, param_item)
            self.params_table.setItem(row, 1, QTableWidgetItem(description))
            row += 1

        # Добавляем пользовательские параметры если есть
        if custom_params:
            for i, param in enumerate(custom_params, 1):
                self.params_table.insertRow(row)
                param_item = QTableWidgetItem(f"%param{i}%")
                param_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.params_table.setItem(row, 0, param_item)
                self.params_table.setItem(row, 1, QTableWidgetItem(param))
                row += 1

        # Кнопка закрытия
        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)

        self.setLayout(layout)


class BashEditorDialog(QDialog):
    """
    Диалоговое окно с многострочным редактором для Bash и кнопками OK/Cancel.
    """

    def __init__(
        self, parent=None, initial_text=None, custom_params=None, *,
        background: bool | None = None, desktop_session: bool | None = None,
    ):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Редактор Bash скрипта")
        self.setMinimumSize(700, 440)
        self.resize(960, 640)
        self.custom_params = custom_params or []

        layout = QVBoxLayout(self)

        self.script_editor = ScriptEditor(self)
        self.menu_bar = QMenuBar(self)
        self.menu_bar.setNativeMenuBar(False)
        layout.setMenuBar(self.menu_bar)
        self.script_editor.attach_menu_bar(self.menu_bar)
        self.editor = self.script_editor.editor
        self.editor.setPlaceholderText("Введите ваш bash скрипт здесь...")
        layout.addWidget(self.script_editor)
        self.desktopSessionCheckBox = QCheckBox("В окружении пользователя рабочего стола", self)
        set_themed_icon(self.desktopSessionCheckBox, "command_desktop_session")
        self.desktopSessionCheckBox.setIconSize(QSize(20, 20))
        self.desktopSessionCheckBox.setChecked(desktop_session is True)
        self.desktopSessionCheckBox.setVisible(desktop_session is not None)
        self.desktopSessionCheckBox.setToolTip(
            "Этот SSH-шаг выполняется от пользователя активного рабочего стола. "
            "SSH-подключение использует ту же настроенную учётную запись. "
            "При смене режима общий shell открывается заново."
        )
        layout.addWidget(self.desktopSessionCheckBox)
        self.backgroundCheckBox = QCheckBox("Запустить и не ждать", self)
        set_themed_icon(self.backgroundCheckBox, "command_background")
        self.backgroundCheckBox.setIconSize(QSize(20, 20))
        self.backgroundCheckBox.setChecked(background is True)
        self.backgroundCheckBox.setVisible(background is not None)
        self.backgroundCheckBox.setToolTip(
            "Linux: отдельный процесс переживает закрытие SSH. "
            "После подтверждения выводятся PID и журнал stdout/stderr. "
            "Ошибки приложения после запуска сохраняются в журнале. "
            "Отмена задания после подтверждения не останавливает процесс."
        )
        layout.addWidget(self.backgroundCheckBox)

        # Применяем подсветку синтаксиса
        self.highlighter = self.editor.highlighter

        if initial_text:
            self.editor.setPlainText(initial_text)

        # Панель с кнопками: "?" и "+" слева, OK/Cancel справа
        buttons_layout = QHBoxLayout()
        buttons_layout.setContentsMargins(0, 8, 0, 0)
        buttons_layout.setSpacing(8)

        self._show_params_menu()

        buttons_layout.addStretch()

        # Кнопки OK/Cancel справа
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setText("Сохранить")
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        self.save_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.save_shortcut.activated.connect(self.accept)
        buttons_layout.addWidget(self.button_box)

        layout.addLayout(buttons_layout)

        self.setLayout(layout)

    def _show_params_help(self):
        """Показать справку по параметрам"""
        dlg = ParamsHelpDialog(self, self.custom_params)
        dlg.exec()

    def _show_params_menu(self):
        """Показать меню для вставки параметров"""
        # if not self.custom_params:
        #     return

        # Создаем контекстное меню
        menu = self.menu_bar.addMenu("Параметры")

        # Добавляем стандартные параметры
        standard_section = QMenu("Стандартные параметры", self)
        standard_section.setIcon(_icon_from_key("menu_file"))
        menu.addMenu(standard_section)
        for param, description in STANDARD_PARAMS:
            action = QAction(f"{param} ({description})", self)
            action.triggered.connect(lambda checked, p=param: self._insert_param(p))
            standard_section.addAction(action)

        if self.custom_params:
            custom_section = QMenu("Параметры команды", self)
            custom_section.setIcon(_icon_from_key("command"))
            menu.addMenu(custom_section)
            for i, param in enumerate(self.custom_params, 1):
                param_name = f"%param{i}%"
                action = QAction(f"{param_name} — {param}", self)
                action.triggered.connect(
                    lambda checked, p=param_name: self._insert_param(p)
                )
                custom_section.addAction(action)

        menu.addSeparator()
        menu.addAction("Справка по параметрам…", self._show_params_help)

    def _insert_param(self, param: str):
        """Вставить параметр в позицию курсора"""
        cursor = self.editor.textCursor()
        cursor.insertText(param)
        self.editor.setFocus()

    def getText(self):
        return self.editor.toPlainText()

    def get_background(self) -> bool:
        """Прочитать выбранный режим SSH-шага."""
        return self.backgroundCheckBox.isChecked()

    def get_desktop_session(self) -> bool:
        """Прочитать выбор прав исполнения текущего SSH-шага."""
        return self.desktopSessionCheckBox.isChecked()

    def setText(self, text):
        self.editor.setPlainText(text)


# Пример использования
if __name__ == "__main__":
    app = QApplication(sys.argv)

    initial_script = """#!/bin/bash

# Это простой пример скрипта
NAME="Мир"
echo "Привет, $NAME!" # Вывод приветствия

# Цикл for
for i in {1..3}; do
  echo "Итерация $i"
  sleep 1 # Пауза на 1 секунду
done

# Условный оператор
if [ "$USER" = "root" ]; then
  echo "Выполняется от имени root."
else
  echo "Выполняется от имени $USER."
fi

# Проверка существования файла
FILE="/etc/passwd"
if [ -f "$FILE" ]; then
    echo "Файл $FILE существует."
else
    echo "Файл $FILE не найден."
fi

echo 'Это строка в одинарных кавычках, $NAME не заменится'
echo "Это строка в двойных кавычках, \\$NAME заменится: $NAME"

exit 0
"""

    dialog = BashEditorDialog(initial_text=initial_script)

    if dialog.exec():
        script_text = dialog.getText()
        print("--- Полученный скрипт: ---")
        print(script_text)
        print("--------------------------")
    else:
        print("Редактирование отменено.")

    sys.exit()
