"""Диалоги ввода и редактирования параметров команд (%param%): список и подстановка значений."""

import sys
from pathlib import Path
import re
from PySide6.QtWidgets import (
    QFileDialog,
    QComboBox,
    QApplication,
    QAbstractScrollArea,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QWidget,
    QDialogButtonBox,
    QFormLayout,
    QScrollArea,
    QListWidget,
    QListWidgetItem,
    QInputDialog,
    QMessageBox,
)
from PySide6.QtCore import Qt, Signal, QSize
from src.logger import logger
from src.ui.icons import _icon_from_key


class ParameterInputDialog(QDialog):
    """
    Диалоговое окно для ввода значений параметров, извлеченных из строки.

    Парсит строку вида "текст %param1:описание1% текст ... текст %param2:описание2% ..."
    и создает форму с полями ввода для каждого параметра.
    """

    def __init__(self, parameters: list, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Введите значения параметров")
        self.setMinimumWidth(400)  # Минимальная ширина окна
        self.setMaximumSize(600, 500)  # Предел роста, дальше - скролл внутри

        self.parameters = self._parse_parameters(
            parameters
        )  # Словарь {param_name: description}
        self.input_fields = {}  # Словарь для хранения QLineEdit {param_name: QLineEdit}
        self._ok_button = None  # Кнопка OK, доступна только при заполненных полях
        self.file_fields = set()
        self.result_data = None  # Словарь для хранения результата {param_name: value}

        # Основной макет
        main_layout = QVBoxLayout(self)

        # --- Область с прокруткой для формы ---
        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(
            True
        )  # Разрешить изменение размера виджета внутри
        scroll_area.setSizeAdjustPolicy(
            QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents
        )
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_widget = QWidget()  # Виджет-контейнер для формы
        form_layout = QFormLayout(
            scroll_widget
        )  # Макет формы для пар "метка: поле ввода"
        form_layout.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapAllRows
        )  # Перенос строк при необходимости

        if not self.parameters:
            # Если параметры не найдены
            form_layout.addRow(QLabel("В строке не найдено параметров для ввода."))
        else:
            # Создание полей ввода для каждого параметра
            # Порядок как в задаче: param2 раньше param10.
            sorted_params = sorted(
                self.parameters.items(), key=lambda item: self._param_number(item[0])
            )
            for param_name, description in sorted_params:
                is_file = str(description).startswith("file:")
                if is_file:
                    description = description[5:].strip() or "Файл"
                    self.file_fields.add(param_name)
                label = QLabel(f"{description}:")
                label.setWordWrap(True)
                line_edit = QLineEdit()
                if "пароль" in str(description).casefold():
                    line_edit.setEchoMode(QLineEdit.EchoMode.Password)
                line_edit.setPlaceholderText(f"Значение для {description}")
                line_edit.returnPressed.connect(self.accept)
                line_edit.textChanged.connect(self._update_ok_state)
                if is_file:
                    row = QHBoxLayout()
                    browse = QPushButton("…")
                    browse.setIcon(_icon_from_key("folder"))
                    browse.setToolTip("Выбрать файл…")
                    browse.setAccessibleName("Выбрать файл")
                    browse.setFixedWidth(48)
                    browse.setAutoDefault(False)
                    browse.clicked.connect(
                        lambda checked=False, field=line_edit: self._browse_file(field)
                    )
                    row.addWidget(line_edit, 1)
                    row.addWidget(browse)
                    form_layout.addRow(label, row)
                else:
                    form_layout.addRow(label, line_edit)
                self.input_fields[param_name] = (
                    line_edit  # Сохраняем ссылку на поле ввода
                )

        scroll_area.setWidget(
            scroll_widget
        )  # Устанавливаем виджет с формой в область прокрутки
        main_layout.addWidget(
            scroll_area
        )  # Добавляем область прокрутки в основной макет
        main_layout.addStretch(1)  # Лишнее место вниз, форма прижата к верху

        # --- Кнопки OK и Cancel ---
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)  # При нажатии OK вызываем accept
        button_box.rejected.connect(self.reject)  # При нажатии Cancel вызываем reject
        ok_button = button_box.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button = ok_button
        if ok_button is not None:
            ok_button.setDefault(True)
            ok_button.setAutoDefault(True)
            ok_button.setToolTip("Заполните все поля, чтобы запустить задачу")
        self._update_ok_state()

        main_layout.addWidget(button_box)

        # Устанавливаем фокус на первое поле ввода, если оно есть
        if self.input_fields:
            first_param_name = min(self.input_fields, key=self._param_number)
            self.input_fields[first_param_name].setFocus()

        self.adjustSize()  # Начальный размер под содержимое, дальше - min/max окна

    @staticmethod
    def _param_number(name: str) -> int:
        """Номер параметра из имени paramN для сортировки."""
        suffix = name.removeprefix("param")
        return int(suffix) if suffix.isdigit() else 0

    def _empty_fields(self) -> list[str]:
        """Имена незаполненных параметров; пробелы значением не считаются."""
        return [
            name
            for name, field in sorted(
                self.input_fields.items(), key=lambda item: self._param_number(item[0])
            )
            if not field.text().strip()
        ]

    def _update_ok_state(self) -> None:
        """Запуск доступен, только когда заполнены все поля."""
        if self._ok_button is not None:
            self._ok_button.setEnabled(not self._empty_fields())

    def _browse_file(self, field: QLineEdit) -> None:
        """Выбрать локальный файл для параметра задачи."""
        path, _ = QFileDialog.getOpenFileName(self, "Выбрать файл", field.text())
        if path:
            field.setText(path)

    def _parse_parameters(self, parameters: list):
        """
        Преобразует список параметров в словарь {param_name: description}.
        """
        parameters = {f"param{i + 1}": value for i, value in enumerate(parameters)}
        logger.debug(f"Найденные параметры: {parameters}")  # Отладочный вывод
        return parameters

    def accept(self):
        """
        Вызывается при нажатии кнопки OK.
        Собирает данные из полей ввода и сохраняет их.
        """
        empty = self._empty_fields()
        if empty:
            # Enter в поле не обходит недоступную кнопку OK.
            self.input_fields[empty[0]].setFocus()
            return
        for name in self.file_fields:
            field = self.input_fields[name]
            if not Path(field.text()).is_file():
                QMessageBox.warning(self, "Файл не найден", "Выберите существующий файл.")
                field.setFocus()
                return
        self.result_data = {}
        for param_name, line_edit in self.input_fields.items():
            self.result_data[param_name] = line_edit.text()
        logger.debug("Параметры команды собраны")
        super().accept()  # Закрывает диалог со статусом "принято"

    def get_data(self):
        """
        Возвращает собранные данные после закрытия диалога через OK.
        """
        return self.result_data

    def format_string(self):
        """
        Возвращает исходную строку с заменой %paramN:описание% на введённые значения.
        Если значение не введено, плейсхолдер остаётся.
        """
        result = self.input_string
        if self.result_data:
            # regex = r"%(\w+):[^%]+%" # Старая версия

            # Новое регулярное выражение:
            # %          - соответствует символу '%'
            # (\w+)      - захватывает имя параметра (группа 1)
            # (:[^%]+)?  - опционально (?) соответствует ':' и любым символам, кроме '%' (группа 2)
            # %          - соответствует символу '%'
            regex = r"%(\w+)(:[^%]+)?%"

            def repl(match):
                # Имя параметра всегда находится в первой захватывающей группе (\w+)
                param = match.group(1)
                # Ищем значение параметра в self.result_data
                # Если найдено, возвращаем значение.
                # Если не найдено, возвращаем исходное совпадение (т.е. плейсхолдер останется)
                return self.result_data.get(param, match.group(0))

            # Выполняем замену всех найденных совпадений
            result = re.sub(regex, repl, result)
        return result


class ParameterEditDialog(QDialog):
    """Название и тип параметра без ввода служебного синтаксиса."""

    def __init__(self, value: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Параметр задачи")
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        is_file = value.startswith("file:")
        self.name_edit = QLineEdit(value[5:] if is_file else value)
        self.name_edit.setPlaceholderText("Например: Файл для отправки")
        self.type_combo = QComboBox()
        self.type_combo.addItems(["Текст", "Файл"])
        self.type_combo.setCurrentIndex(1 if is_file else 0)
        form.addRow("Название:", self.name_edit)
        form.addRow("Тип:", self.type_combo)
        layout.addLayout(form)
        hint = QLabel("При запуске задачи тип «Файл» показывает поле пути и кнопку «Обзор…».")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.name_edit.textChanged.connect(
            lambda text: buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(text.strip()))
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self.name_edit.text().strip()))
        layout.addWidget(buttons)

    def value(self) -> str:
        """Вернуть параметр в совместимом формате хранения."""
        name = self.name_edit.text().strip()
        return f"file:{name}" if self.type_combo.currentIndex() == 1 else name


class ParamsInputDialog(QDialog):
    """Диалог для редактирования параметров команды"""

    result_ready = Signal(list)  # Signal emits list of parameter names

    def __init__(self, params=None, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Редактирование параметров")
        self.setMinimumSize(400, 300)
        self.params = params or []
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()

        # Parameters list
        params_label = QLabel("Параметры команды:")
        layout.addWidget(params_label)

        self.params_list = QListWidget()
        for value in self.params:
            item = QListWidgetItem()
            self._set_item_value(item, value)
            self.params_list.addItem(item)
        self.params_list.itemDoubleClicked.connect(self._edit_param)
        layout.addWidget(self.params_list)

        # Buttons for managing parameters
        buttons_layout = QHBoxLayout()

        self.add_btn = QPushButton("Добавить")
        self.add_btn.clicked.connect(self._add_param)
        buttons_layout.addWidget(self.add_btn)

        self.edit_btn = QPushButton("Изменить")
        self.edit_btn.clicked.connect(self._edit_param)
        buttons_layout.addWidget(self.edit_btn)

        self.remove_btn = QPushButton("Удалить")
        self.remove_btn.clicked.connect(self._remove_param)
        buttons_layout.addWidget(self.remove_btn)

        layout.addLayout(buttons_layout)

        # OK/Cancel buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self.setLayout(layout)

    def _add_param(self):
        """Добавить параметр с выбранным типом."""
        dialog = ParameterEditDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            item = QListWidgetItem()
            self._set_item_value(item, dialog.value())
            self.params_list.addItem(item)
            self.params_list.setCurrentItem(item)

    @staticmethod
    def _set_item_value(item: QListWidgetItem, value: str) -> None:
        """Отделить понятное название от формата хранения."""
        is_file = value.startswith("file:")
        name = value[5:] if is_file else value
        item.setText(f"{name} — {'Файл' if is_file else 'Текст'}")
        item.setData(Qt.ItemDataRole.UserRole, value)

    def _edit_param(self):
        """Изменить название или тип выбранного параметра."""
        current = self.params_list.currentItem()
        if not current:
            QMessageBox.warning(self, "Ошибка", "Выберите параметр для редактирования")
            return

        dialog = ParameterEditDialog(current.data(Qt.ItemDataRole.UserRole), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._set_item_value(current, dialog.value())

    def _remove_param(self):
        """Remove selected parameter"""
        current = self.params_list.currentItem()
        if not current:
            QMessageBox.warning(self, "Ошибка", "Выберите параметр для удаления")
            return

        reply = QMessageBox.question(
            self,
            "Подтверждение",
            "Вы уверены, что хотите удалить этот параметр?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.params_list.takeItem(self.params_list.row(current))

    def accept(self):
        """Handle dialog acceptance"""
        params = self.get_params()
        self.result_ready.emit(params)
        super().accept()

    def get_params(self) -> list:
        """Get current parameter list"""
        return [
            self.params_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.params_list.count())
        ]


# --- Пример использования ---
if __name__ == "__main__":
    app = QApplication(sys.argv)

    # Пример строки с параметрами
    input_str = (
        "Это строка %param1: Имя пользователя%, в которой есть что-то еще, "
        "%param2: Название города%, и даже %param3: Код страны%. "
        "Еще один параметр %param4: Очень длинное и подробное описание параметра номер четыре%."
    )

    dialog = ParameterInputDialog(input_str)
    result_code = dialog.exec()

    if result_code == QDialog.DialogCode.Accepted:
        output_data = dialog.get_data()
        print("\n--- Результат ---")
        print("Полученный словарь:")
        print(output_data)
        # Пример форматирования строки с заменой параметров
        formatted = dialog.format_string()
        print("\nСтрока с подстановкой значений:")
        print(formatted)
    else:
        print("\n--- Результат ---")
        print("Пользователь отменил ввод.")

    sys.exit(0)
