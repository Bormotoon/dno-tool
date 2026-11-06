"""Строка настройки: назначение, поле и происхождение правила рядом."""

from __future__ import annotations

import re

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QPainter, QPaintEvent, QPalette, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.config.kde_schema import KdeSetting, editable_setting
from src.domain.kde import KdeSnapshot
from src.ui.dialogs.kde.controller import KdeController
from src.ui.dialogs.kde.fields import ColorField, FontField
from src.ui.dialogs.kde.settings_model import KdeSettingsModel


def text_label(text: str, *, bold: bool = False) -> QLabel:
    """Текст переносится и никогда не интерпретируется как HTML."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    font = label.font()
    font.setBold(bold)
    label.setFont(font)
    return label


class DescriptionLabel(QLabel):
    """Две завершённые строки с многоточием; полное описание доступно в деталях."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.full_text = text
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setToolTip(text)
        self.setAccessibleDescription(text)
        self._update_text()

    def _update_text(self) -> None:
        metrics = self.fontMetrics()
        width = max(20, self.contentsRect().width())
        words = self.full_text.split()
        first: list[str] = []
        while (
            words and metrics.horizontalAdvance(" ".join(first + [words[0]])) <= width
        ):
            first.append(words.pop(0))
        if not first and words:
            first.append(words.pop(0))
        lines = [
            metrics.elidedText(" ".join(first), Qt.TextElideMode.ElideRight, width)
        ]
        if words:
            lines.append(
                metrics.elidedText(" ".join(words), Qt.TextElideMode.ElideRight, width)
            )
        self.setText("\n".join(lines))
        self.setFixedHeight(metrics.lineSpacing() * 2 + 2)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_text()


class KdeSettingRow(QFrame):
    """Обновление статуса не пересоздаёт поле при наборе значения."""

    edited = Signal(object)
    revert_requested = Signal()

    def __init__(self, definition: KdeSetting, controller: KdeController) -> None:
        super().__init__()
        self.definition = definition
        self.controller = controller
        self.editable = False
        self._editor_snapshot: KdeSnapshot | None = None
        self.setObjectName("kdeSettingRow")
        self._last_value = controller.value(definition.key)
        self._compact: bool | None = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(18, 16, 18, 16)
        self._layout.setSpacing(8)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(20)
        self.grid.setVerticalSpacing(10)
        self.identity = QWidget()
        identity = QVBoxLayout(self.identity)
        identity.setContentsMargins(0, 0, 0, 0)
        identity.setSpacing(4)
        identity.addWidget(text_label(definition.title, bold=True))
        self.description = DescriptionLabel(definition.description)
        identity.addWidget(self.description)
        self.key_label = text_label(definition.key.name)
        self.key_label.setObjectName("kdeSecondary")
        if definition.title.startswith("Дополнительн"):
            identity.addWidget(self.key_label)
        else:
            self.key_label.setParent(self.identity)
            self.key_label.hide()
        self.identity.setToolTip(definition.key.label)
        self.editor_host = QWidget()
        self.editor_host.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.editor_layout = QVBoxLayout(self.editor_host)
        self.editor_layout.setContentsMargins(0, 0, 0, 0)
        self.status_host = QWidget()
        status = QVBoxLayout(self.status_host)
        status.setContentsMargins(0, 0, 0, 0)
        status.setSpacing(4)
        self.state_label = text_label("")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status.addWidget(self.state_label)
        self.details_button = QPushButton("Подробности")
        self.details_button.setFlat(True)
        self.details_button.setCheckable(True)
        self.details_button.setAccessibleName("Подробности: " + definition.title)
        status.addWidget(self.details_button)
        self._layout.addLayout(self.grid)
        self.details = QWidget()
        details = QVBoxLayout(self.details)
        details.setContentsMargins(0, 8, 0, 0)
        details.addWidget(text_label("Идентификатор: " + definition.key.label))
        details.addWidget(text_label(definition.description))
        self.source_label = text_label("")
        details.addWidget(self.source_label)
        details.addWidget(text_label("Применение: " + definition.apply_hint))
        details.addWidget(
            text_label(
                "Файл: " + definition.filename + " · Группа: " + definition.key.group
            )
        )
        self.revert_button = QPushButton("Вернуть исходное значение")
        self.revert_button.clicked.connect(self.revert_requested)
        details.addWidget(self.revert_button, 0, Qt.AlignmentFlag.AlignLeft)
        self._layout.addWidget(self.details)
        self.details.hide()
        self.details_button.toggled.connect(self.details.setVisible)
        self.error_label = text_label("")
        self._layout.addWidget(self.error_label)
        self.error_label.hide()
        self._build_editor()
        self.sync()
        self._arrange(False)

    def _emit(self, value: str | None) -> None:
        self._last_value = value
        self.edited.emit(value)

    def _build_editor(self) -> None:
        while self.editor_layout.count():
            item = self.editor_layout.takeAt(0)
            if item is not None and (widget := item.widget()) is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        definition = self.definition
        snapshot = self.controller.draft.snapshot if self.controller.draft else None
        self._editor_snapshot = snapshot
        self.editable = False
        local = self.controller.value(definition.key)
        self._last_value = local
        self.override: QCheckBox | None = None
        if snapshot is None:
            self.editor: QWidget = QLineEdit("Не прочитано")
            self.editor.setAccessibleName(definition.title)
            self.editor.setEnabled(False)
            self.editor_layout.addWidget(self.editor)
            return
        inherited, _, locked = snapshot.policy(definition.key)
        entry = snapshot.document.entries.get(definition.key)
        self.editable = bool(
            editable_setting(snapshot, definition.key) is not None
            and snapshot.document.writable
            and not locked
            and (entry is None or entry.editable)
        )
        if not self.editable:
            field = QLineEdit("Задано политикой" if locked else local or "Не задано")
            field.setReadOnly(True)
            self.editor = field
        elif definition.kind in ("bool", "restriction"):
            combo = QComboBox()
            for title, value in (
                ("Наследовать", None),
                ("Разрешить" if definition.kind == "restriction" else "Да", "true"),
                ("Запретить" if definition.kind == "restriction" else "Нет", "false"),
            ):
                combo.addItem(title, value)
            if local not in (None, "true", "false"):
                combo.addItem("Исходное: " + local, local)
            combo.setCurrentIndex(max(0, combo.findData(local)))
            combo.currentIndexChanged.connect(
                lambda index: self._emit(combo.itemData(index))
            )
            self.editor = combo
        else:
            self.override = QCheckBox("Задать в личном файле")
            self.override.setChecked(local is not None)
            self.override.setAccessibleName("Личное значение: " + definition.title)
            self.editor_layout.addWidget(self.override)
            initial = local if local is not None else inherited or ""
            if definition.kind == "color":
                color = ColorField(initial)
                color.value_changed.connect(self._emit)
                self.editor = color
            elif definition.kind == "font":
                font = FontField(
                    initial,
                    snapshot.resources.get("fonts", ()),
                    qt_major=5 if re.search(r"\b5\.", snapshot.version) else 6,
                )
                font.value_changed.connect(self._emit)
                self.editor = font
            else:
                choice = QComboBox()
                if definition.kind == "enum":
                    choice.setProperty("kdeEnum", True)
                    for title, stored in definition.choices:
                        choice.addItem(title, stored)
                    if choice.findData(initial) < 0:
                        choice.addItem(
                            "Исходное: " + initial if initial else "Выберите значение…",
                            initial if initial else None,
                        )
                    choice.setCurrentIndex(
                        choice.findData(initial if initial else None)
                    )
                    choice.currentIndexChanged.connect(
                        lambda index: self._emit(choice.itemData(index))
                    )
                else:
                    choice.setEditable(True)
                    choice.addItems(
                        list(snapshot.resources.get(definition.resource, ()))
                    )
                    choice.setCurrentText(initial)
                    choice.currentTextChanged.connect(self._emit)
                self.editor = choice
            self.override.toggled.connect(self._override_changed)
        self.editor.setAccessibleName(definition.title)
        self.editor_layout.addWidget(self.editor)

    def _override_changed(self, checked: bool) -> None:
        editor = self.editor
        value = (
            editor.get_value()
            if isinstance(editor, (ColorField, FontField))
            else (
                editor.currentData()
                if isinstance(editor, QComboBox) and editor.property("kdeEnum")
                else editor.currentText() if isinstance(editor, QComboBox) else ""
            )
        )
        self._emit(value if checked else None)

    def sync(self) -> None:
        """Обновить происхождение, ошибку и доступность без потери фокуса."""
        controller, key = self.controller, self.definition.key
        local = controller.value(key)
        snapshot = controller.draft.snapshot if controller.draft else None
        if local != self._last_value or snapshot is not self._editor_snapshot:
            self._build_editor()
        changed = bool(controller.draft and key in controller.draft.changes)
        if snapshot:
            value, source, locked = snapshot.policy(key)
            self.source_label.setText(
                "Прочитанное значение: "
                + KdeSettingsModel.format_value(self.definition, value)
                + "\nИсточник: "
                + source
            )
            status = (
                "Системная блокировка"
                if locked
                else (
                    "Изменено"
                    if changed
                    else (
                        "Личный файл"
                        if local is not None
                        else (
                            "Наследуется" if value is not None else "Значение не задано"
                        )
                    )
                )
            )
            if locked:
                status += "\n" + KdeSettingsModel.format_value(self.definition, value)
            elif not self.editable:
                status = "Только просмотр"
        else:
            status = "Значение не загружено"
            self.source_label.setText(
                "Выберите пользователя и загрузите профиль, чтобы прочитать личные значения."
            )
        invalid = controller.invalid.get(key)
        self.state_label.setText("Ошибка значения" if invalid else status)
        explanation = (
            "В личном файле пользователя значение не задано. KDE использует системные настройки "
            "или встроенные значения приложения. Фактическое значение здесь не определено."
            if not invalid and status == "Значение не задано"
            else ""
        )
        self.state_label.setToolTip(explanation)
        self.state_label.setAccessibleDescription(explanation)
        self.error_label.setText("Ошибка: " + invalid[1] if invalid else "")
        self.error_label.setVisible(bool(invalid))
        enabled = bool(snapshot and controller.ready and not controller.busy)
        self.editor.setEnabled(enabled and getattr(self, "editable", False))
        if self.override is not None:
            with QSignalBlocker(self.override):
                self.override.setChecked(local is not None)
            self.override.setEnabled(enabled)
        self.revert_button.setVisible(
            bool(snapshot and getattr(self, "editable", False))
        )
        self.revert_button.setEnabled(enabled and (changed or bool(invalid)))

    def _arrange(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        for widget in (self.identity, self.editor_host, self.status_host):
            self.grid.removeWidget(widget)
        for column in range(3):
            self.grid.setColumnStretch(column, 0)
        if compact:
            self.grid.addWidget(self.identity, 0, 0, 1, 2)
            self.grid.addWidget(self.editor_host, 1, 0, 1, 2)
            self.grid.addWidget(self.status_host, 2, 0, 1, 2)
            self.grid.setColumnStretch(0, 1)
        else:
            self.grid.addWidget(self.identity, 0, 0)
            self.grid.addWidget(self.editor_host, 0, 1)
            self.grid.addWidget(self.status_host, 0, 2)
            stretches = (
                (59, 21, 20)
                if self.definition.kind in ("bool", "restriction")
                else (50, 30, 20)
            )
            for column, stretch in enumerate(stretches):
                self.grid.setColumnStretch(column, stretch)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._arrange(self.width() < 660)

    def paintEvent(self, event: QPaintEvent) -> None:
        """Карточка берёт цвета действующей темы при каждой отрисовке."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(self.palette().brush(QPalette.ColorRole.Base))
        painter.setPen(self.palette().color(QPalette.ColorRole.Midlight))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)
