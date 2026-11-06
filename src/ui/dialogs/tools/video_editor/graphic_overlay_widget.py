"""Поля картинки или фигуры в общей панели наложений."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QPoint, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.video.models import CORNERS, TextOverlay
from src.ui.icons import set_themed_icon
from src.ui.widgets.device_list_toolbar import FLAT_TOOLBUTTON_QSS
from src.utils.video_overlays import image_size

GRAPHIC_LABELS = {
    "image": "Картинка",
    "rectangle": "Прямоугольник",
    "ellipse": "Эллипс",
    "arrow": "Стрелка",
}


def choose_image(parent: QWidget, initial: str = "") -> TextOverlay | None:
    """Выбрать и проверить картинку до добавления блока в проект."""
    path, _ = QFileDialog.getOpenFileName(
        parent,
        "Добавить картинку",
        initial,
        "Картинки (*.png *.jpg *.jpeg *.webp *.bmp)",
    )
    if not path:
        return None
    try:
        size = image_size(path)
        size.scale(QSize(320, 240), Qt.AspectRatioMode.KeepAspectRatio)
        overlay = TextOverlay(
            kind="image",
            image_path=path,
            width=max(1, size.width()),
            height=max(1, size.height()),
            corner="TL",
        )
        return overlay
    except ValueError as error:
        QMessageBox.warning(parent, "Картинка", str(error))
        return None


class GraphicOverlayBlock(QFrame):
    """Редактор геометрии, внешнего вида и интервала одного наложения."""

    edited = Signal()
    folded_changed = Signal(bool)
    duplicate_requested = Signal()

    def __init__(
        self,
        overlay: TextOverlay,
        collapsed: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._overlay = replace(overlay)
        self._num = 1
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Sunken)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 6)
        layout.setSpacing(4)
        header = QHBoxLayout()
        self.chk = QCheckBox(self)
        self.chk.setChecked(overlay.enabled)
        self.chk.setToolTip("Показывать наложение")
        header.addWidget(self.chk)
        self.lbl_title = QLabel(self)
        self.lbl_title.setMinimumWidth(0)
        self.lbl_title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        header.addWidget(self.lbl_title, 1)
        self.btn_fold = QToolButton(self)
        self.btn_fold.setCheckable(True)
        self.btn_fold.setFixedSize(24, 24)
        self.btn_fold.setStyleSheet(FLAT_TOOLBUTTON_QSS)
        header.addWidget(self.btn_fold)
        self.btn_del = QToolButton(self)
        set_themed_icon(self.btn_del, "ve_delete")
        self.btn_del.setIconSize(QSize(16, 16))
        self.btn_del.setFixedSize(24, 24)
        self.btn_del.setStyleSheet(FLAT_TOOLBUTTON_QSS)
        self.btn_del.setToolTip("Удалить наложение")
        header.addWidget(self.btn_del)
        layout.addLayout(header)
        self.form_host = QWidget(self)
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setSpacing(4)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        layout.addWidget(self.form_host)
        self._integer_rows: dict[QSpinBox, QWidget] = {}
        self._integer_sliders: dict[QSpinBox, QSlider] = {}

        self.image_button = QPushButton(self)
        self.image_button.setMinimumWidth(0)
        self.image_button.clicked.connect(self._replace_image)
        if overlay.kind == "image":
            self.form.addRow("Файл:", self.image_button)
        self.image_button.setVisible(overlay.kind == "image")
        self._sync_image_label()
        self.width_field = self._integer("Ширина, px:", overlay.width, 1, 4096)
        self.height_field = self._integer("Высота, px:", overlay.height, 1, 4096)
        self.keep_ratio = QCheckBox("Сохранять пропорции", self)
        self.keep_ratio.setChecked(overlay.kind == "image")
        self.form.addRow(self.keep_ratio)
        self._ratio = overlay.width / overlay.height
        self.keep_ratio.toggled.connect(self._capture_ratio)
        self.width_field.valueChanged.connect(lambda value: self._resize(True, value))
        self.height_field.valueChanged.connect(lambda value: self._resize(False, value))
        self.corner = QComboBox(self)
        for code, label in zip(
            CORNERS, ("Левый верх", "Правый верх", "Левый низ", "Правый низ")
        ):
            self.corner.addItem(label, code)
        self.corner.setCurrentIndex(CORNERS.index(overlay.corner))
        self.form.addRow("Угол:", self.corner)
        self.corner.currentIndexChanged.connect(self.edited)
        self.pos_x = self._integer("Позиция X, px:", overlay.margin_x, 0, 16384)
        self.pos_y = self._integer("Позиция Y, px:", overlay.margin_y, 0, 16384)
        self.pos_x.setToolTip("Отступ от выбранного края кадра")
        self.pos_y.setToolTip("Отступ от выбранного края кадра")
        self.rotation = self._integer("Поворот:", overlay.rotation, -180, 180)
        self.rotation.setSuffix("°")
        self._integer_rows[self.rotation].setToolTip(
            "Вокруг центра элемента; положительный угол — по часовой стрелке"
        )
        self.opacity = self._integer("Непрозрачность, %:", overlay.opacity, 0, 100)
        self.color = QPushButton("Выбрать…", self)
        self.color.clicked.connect(self._choose_color)
        self.stroke = self._integer("Толщина, px:", overlay.stroke_width, 1, 100)
        self.filled = QCheckBox("Заливка", self)
        self.filled.setChecked(overlay.filled)
        self.filled.toggled.connect(self.edited)
        self.direction = QComboBox(self)
        for code, label in (
            ("right", "Вправо"),
            ("down", "Вниз"),
            ("left", "Влево"),
            ("up", "Вверх"),
        ):
            self.direction.addItem(label, code)
        self.direction.setCurrentIndex(self.direction.findData(overlay.arrow_direction))
        self.direction.currentIndexChanged.connect(self.edited)
        self.direction.setVisible(overlay.kind == "arrow")
        self.color.setVisible(overlay.kind != "image")
        self.filled.setVisible(overlay.kind in ("rectangle", "ellipse"))
        if overlay.kind != "image":
            self.form.addRow("Цвет:", self.color)
            self._sync_color()
            if overlay.kind != "arrow":
                self.form.addRow(self.filled)
            else:
                self.form.addRow("Направление:", self.direction)
        else:
            self.form.setRowVisible(self._integer_rows[self.stroke], False)
        self.start = self._time("Начало, с:", overlay.start)
        self.end = self._time("Конец, с:", overlay.end)
        self.end.setSpecialValueText("до конца проекта")
        self.btn_fold.toggled.connect(self._fold)
        self.btn_fold.setChecked(collapsed)
        self._fold(collapsed)
        self.chk.toggled.connect(self._toggle)
        self.form_host.setEnabled(overlay.enabled)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.set_number(1)

    def _integer(self, label: str, value: int, minimum: int, maximum: int) -> QSpinBox:
        row = QWidget(self.form_host)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        slider = QSlider(Qt.Orientation.Horizontal, row)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        slider.setMinimumWidth(32)
        slider.setAccessibleName(label)
        field = QSpinBox(row)
        field.setRange(minimum, maximum)
        field.setValue(value)
        field.setFixedWidth(64)
        field.setAccessibleName(label)
        layout.addWidget(slider, 1)
        layout.addWidget(field)
        slider.valueChanged.connect(field.setValue)
        field.valueChanged.connect(slider.setValue)
        field.valueChanged.connect(self.edited)
        self._integer_rows[field] = row
        self._integer_sliders[field] = slider
        self.form.addRow(label, row)
        return field

    def _set_integer(self, field: QSpinBox, value: int) -> None:
        """Обновить обе части поля без промежуточной правки проекта."""
        slider = self._integer_sliders[field]
        with QSignalBlocker(field), QSignalBlocker(slider):
            field.setValue(value)
            slider.setValue(field.value())

    def _time(self, label: str, value: float) -> QDoubleSpinBox:
        field = QDoubleSpinBox(self)
        field.setRange(0, 1e9)
        field.setDecimals(6)
        field.setValue(value)
        field.setAccessibleName(label)
        field.valueChanged.connect(self.edited)
        self.form.addRow(label, field)
        return field

    @property
    def iid(self) -> str:
        return self._overlay.iid

    def set_number(self, number: int) -> None:
        self._num = number
        self.lbl_title.setText(f"{GRAPHIC_LABELS[self._overlay.kind]} {number}")
        self.lbl_title.setToolTip(self.lbl_title.text())

    def _capture_ratio(self, enabled: bool) -> None:
        self._ratio = self.width_field.value() / self.height_field.value()

    def _resize(self, horizontal: bool, value: int) -> None:
        if not self.keep_ratio.isChecked():
            return
        other = self.height_field if horizontal else self.width_field
        wanted = round(value / self._ratio if horizontal else value * self._ratio)
        if wanted > other.maximum():
            wanted = other.maximum()
            current = self.width_field if horizontal else self.height_field
            self._set_integer(
                current,
                round(wanted * self._ratio if horizontal else wanted / self._ratio),
            )
        self._set_integer(other, max(1, wanted))
        self.edited.emit()

    def _toggle(self, enabled: bool) -> None:
        self.form_host.setEnabled(enabled)
        self.edited.emit()

    def _fold(self, collapsed: bool) -> None:
        self.form_host.setVisible(not collapsed)
        self.btn_fold.setText("▼" if collapsed else "▲")
        self.btn_fold.setToolTip("Развернуть блок" if collapsed else "Свернуть блок")
        self.folded_changed.emit(collapsed)

    def _context_menu(self, position: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction("Дублировать наложение").triggered.connect(
            self.duplicate_requested
        )
        menu.exec(self.mapToGlobal(position))

    def _sync_image_label(self) -> None:
        name = Path(self._overlay.image_path).name
        self.image_button.setText(
            (name[:18] + "…") if len(name) > 19 else (name or "Выбрать файл…")
        )
        self.image_button.setToolTip(self._overlay.image_path)
        self.image_button.setAccessibleName(Path(self._overlay.image_path).name)

    def _replace_image(self) -> None:
        selected = choose_image(self, self._overlay.image_path)
        if selected is not None:
            self._overlay.image_path = selected.image_path
            self._ratio = selected.width / selected.height
            self._set_integer(self.width_field, selected.width)
            self._set_integer(self.height_field, selected.height)
            self._sync_image_label()
            self.edited.emit()

    def _sync_color(self) -> None:
        self.color.setText(self._overlay.color)
        swatch = QPixmap(16, 16)
        swatch.fill(QColor(self._overlay.color))
        self.color.setIcon(QIcon(swatch))

    def _choose_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._overlay.color), self, "Цвет фигуры")
        if color.isValid():
            self._overlay.color = color.name()
            self._sync_color()
            self.edited.emit()

    def get(self) -> TextOverlay:
        return replace(
            self._overlay,
            enabled=self.chk.isChecked(),
            width=self.width_field.value(),
            height=self.height_field.value(),
            corner=self.corner.currentData(),
            margin_x=self.pos_x.value(),
            margin_y=self.pos_y.value(),
            opacity=self.opacity.value(),
            stroke_width=self.stroke.value(),
            filled=self.filled.isChecked(),
            start=self.start.value(),
            end=self.end.value(),
            arrow_direction=self.direction.currentData(),
            rotation=self.rotation.value(),
        )

    def focus_editor(self) -> None:
        self.width_field.setFocus()
        self.width_field.selectAll()
