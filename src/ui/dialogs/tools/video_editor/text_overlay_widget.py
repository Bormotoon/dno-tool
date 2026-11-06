"""Панель текстов в углах кадра: список блоков, у каждого своя урна."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.video.models import CORNERS, TextOverlay, new_iid
from src.ui.icons import set_themed_icon
from src.ui.widgets.device_list_toolbar import FLAT_TOOLBUTTON_QSS

from .graphic_overlay_widget import GRAPHIC_LABELS, GraphicOverlayBlock, choose_image
from .wheel_guard import guard_wheel

CORNER_LABELS = {
    "TL": "Левый верх",
    "TR": "Правый верх",
    "BL": "Левый низ",
    "BR": "Правый низ",
}

# подписи по-русски + код цвета для ffmpeg (data индекса)
COLOR_ITEMS = [
    ("Белый", "white"),
    ("Желтый", "yellow"),
    ("Черный", "black"),
    ("Красный", "red"),
    ("Голубой", "cyan"),
]

DEFAULT_FONT_LABEL = "По умолчанию"


def _system_font_families() -> list[str]:
    try:
        return sorted(set(QFontDatabase.families()))
    except Exception:
        return []


def _narrow(widget: QWidget) -> QWidget:
    """Поле использует всю доступную ширину узкой боковой панели."""
    widget.setMinimumWidth(0)
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    return widget


class _SliderSpin(QWidget):
    """Ползунок + спинбокс с одним значением: грубо - тянуть, точно - вбить."""

    valueChanged = Signal(int)

    def __init__(
        self, minv: int = 0, maxv: int = 300, val: int = 20, parent=None
    ) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(minv, maxv)
        self.spin = QSpinBox()
        self.spin.setRange(minv, maxv)
        self.spin.setFixedWidth(64)
        row.addWidget(self.slider, 1)
        row.addWidget(self.spin)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)
        self.setValue(val)

    def _from_slider(self, v: int) -> None:
        if self.spin.value() != v:
            self.spin.blockSignals(True)
            self.spin.setValue(v)
            self.spin.blockSignals(False)
        self.valueChanged.emit(v)

    def _from_spin(self, v: int) -> None:
        if self.slider.value() != v:
            self.slider.blockSignals(True)
            self.slider.setValue(v)
            self.slider.blockSignals(False)
        self.valueChanged.emit(v)

    def value(self) -> int:
        return self.slider.value()

    def setValue(self, v: int) -> None:
        self.slider.blockSignals(True)
        self.spin.blockSignals(True)
        try:
            self.slider.setValue(v)
            self.spin.setValue(v)
        finally:
            self.slider.blockSignals(False)
            self.spin.blockSignals(False)


class _OverlayBlock(QFrame):
    """Один текстовый блок: чекбокс + номер в заголовке, урна, поля под ними.

    QFrame вместо QGroupBox: тот резервирует место под встроенный
    тайтл и дизейблит детей при setCheckable(False) галочке.
    """

    edited = Signal()
    folded_changed = Signal(bool)
    duplicate_requested = Signal()

    def __init__(
        self, ov: TextOverlay | None = None, collapsed: bool = False, parent=None
    ) -> None:
        super().__init__(parent)
        ov = ov or TextOverlay(iid=new_iid())
        self._iid = ov.iid
        self._fontfile = ov.fontfile
        self._clip_iid = ov.clip_iid
        self._num = 1
        self._collapsed = False
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Sunken)
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 4, 8, 6)
        grid.setVerticalSpacing(4)
        grid.setHorizontalSpacing(4)

        title_row = QHBoxLayout()
        title_row.setSpacing(4)
        title_row.setContentsMargins(0, 0, 0, 0)
        self.chk = QCheckBox()
        # новый блок (текст пуст) тоже включен: галочка строго по enabled,
        # иначе добавленный текст "не отмечен, но активен"
        self.chk.setChecked(bool(ov.enabled))
        self.chk.setToolTip("Показывать текст")
        title_row.addWidget(self.chk, 0, Qt.AlignmentFlag.AlignVCenter)
        self.lbl_title = QLabel("Текст")
        self.lbl_title.setToolTip("Порядковый номер блока")
        title_row.addWidget(self.lbl_title)
        title_row.addStretch(1)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.btn_fold = QToolButton()
        self.btn_fold.setCheckable(True)
        self.btn_fold.setText("▲")
        self.btn_fold.setFixedSize(24, 24)
        self.btn_fold.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.btn_fold.setStyleSheet(FLAT_TOOLBUTTON_QSS)
        self.btn_fold.setToolTip("Свернуть блок")
        title_row.addWidget(self.btn_fold)
        self.btn_del = QToolButton()
        set_themed_icon(self.btn_del, "ve_delete")
        self.btn_del.setIconSize(QSize(16, 16))
        self.btn_del.setFixedSize(24, 24)
        self.btn_del.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.btn_del.setStyleSheet(FLAT_TOOLBUTTON_QSS)
        self.btn_del.setToolTip("Удалить этот текст")
        title_row.addWidget(self.btn_del)
        grid.addLayout(title_row, 0, 0, 1, 2)

        self.edit = QLineEdit(ov.text)
        self.edit.setPlaceholderText("Текст поверх видео")
        self.multiline = QPushButton("Многострочный текст…", self)
        self.multiline.clicked.connect(self._edit_multiline)
        self.start = QDoubleSpinBox(self)
        self.end = QDoubleSpinBox(self)
        for field, value, label in (
            (self.start, ov.start, "Начало титра, секунды"),
            (self.end, ov.end, "Конец титра, секунды"),
        ):
            field.setRange(0, 1e9)
            field.setDecimals(6)
            field.setValue(value)
            field.setAccessibleName(label)
            field.valueChanged.connect(self.edited)
        self.end.setSpecialValueText("до конца проекта")
        self.fontfile_button = QPushButton("Файл шрифта…", self)
        self.fontfile_button.setToolTip(
            self._fontfile
            or "Выбрать TTF/OTF. Тот же файл используется в предпросмотре и экспорте."
        )
        self.fontfile_button.clicked.connect(self._choose_fontfile)
        self.corner = QComboBox()
        self.corner.addItems([CORNER_LABELS[c] for c in CORNERS])
        for i, code in enumerate(CORNERS):
            self.corner.setItemData(i, code)
        idx = list(CORNERS).index(ov.corner) if ov.corner in CORNERS else 3
        self.corner.setCurrentIndex(idx)
        self.size = QSpinBox()
        self.size.setRange(8, 120)
        self.size.setValue(ov.fontsize)
        self.color = QComboBox()
        self.color.setEditable(True)
        for label, code in COLOR_ITEMS:
            self.color.addItem(label, code)
        # выставляем по коду цвета из модели; неизвестный (hex, имя) -
        # в поле редактирования, editable-комбо позволяет произвольный цвет
        ci = self.color.findData(ov.color)
        if ci >= 0:
            self.color.setCurrentIndex(ci)
        else:
            self.color.setEditText(ov.color or "white")
        self.fontbox = QComboBox()
        self.fontbox.setEditable(True)
        self.fontbox.setToolTip("Шрифт текста (пусто = системный по умолчанию)")
        # иначе минимум ширины - по самому длинному имени шрифта в системе,
        # и сужение строки (_narrow) на него не действует
        self.fontbox.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.fontbox.setMinimumContentsLength(12)
        self.fontbox.addItem(DEFAULT_FONT_LABEL, "")
        for fam in _system_font_families():
            self.fontbox.addItem(fam, fam)
        # значение модели - имя семейства; неизвестное - в поле ввода
        fi = self.fontbox.findData(ov.font_family) if ov.font_family else 0
        if fi is not None and fi >= 0:
            self.fontbox.setCurrentIndex(fi)
        elif ov.font_family:
            self.fontbox.setEditText(ov.font_family)
        self.pos_x = _SliderSpin(0, 16384, ov.margin_x)
        self.pos_x.setToolTip(
            "Отступ от левого края (углы TL/BL) или от правого (TR/BR), px кадра"
        )
        self.pos_y = _SliderSpin(0, 16384, ov.margin_y)
        self.pos_y.setToolTip(
            "Отступ от верхнего края (углы TL/TR) или от нижнего (BL/BR), px кадра"
        )
        self.rotation = _SliderSpin(-180, 180, ov.rotation)
        self.rotation.spin.setSuffix("°")
        self.rotation.setToolTip(
            "Поворот вокруг центра текста и подложки; положительный угол — по часовой стрелке"
        )
        self.rotation.slider.setAccessibleName("Поворот текста")
        self.rotation.spin.setAccessibleName("Поворот текста")
        self.rotation.valueChanged.connect(self.edited)
        self.back_color = QComboBox()
        self.back_color.setEditable(True)
        self.back_color.setToolTip("Цвет подложки под текстом")
        for label, code in COLOR_ITEMS:
            self.back_color.addItem(label, code)
        bi = self.back_color.findData(ov.back_color)
        if bi >= 0:
            self.back_color.setCurrentIndex(bi)
        else:
            self.back_color.setEditText(ov.back_color or "black")
        self.back_alpha = QSlider(Qt.Orientation.Horizontal)
        self.back_alpha.setRange(0, 100)
        try:
            start_alpha = max(0, min(100, int(ov.back_alpha)))
        except (TypeError, ValueError):
            start_alpha = 50
        self.back_alpha.setValue(start_alpha)
        self.back_alpha.setToolTip("Прозрачность подложки (0 = без подложки)")
        self.back_alpha_value = QLabel(f"{start_alpha} %")
        self.back_alpha_value.setMinimumWidth(44)
        self.back_alpha_value.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.back_alpha.valueChanged.connect(self._sync_alpha_label)
        alpha_row = QHBoxLayout()
        alpha_row.setContentsMargins(0, 0, 0, 0)
        alpha_row.addWidget(self.back_alpha, 1)
        alpha_row.addWidget(self.back_alpha_value)
        self.box_pad = QSpinBox()
        self.box_pad.setRange(0, 30)
        try:
            self.box_pad.setValue(max(0, min(30, int(getattr(ov, "box_pad", 6)))))
        except (TypeError, ValueError):
            self.box_pad.setValue(6)
        self.box_pad.setToolTip("Внутренний отступ подложки (как в итоговом видео)")

        # Поля - правые ~70% ширины (пустая треть слева), слайдер
        # прозрачности и ползунки позиции - на всю ширину (им тесно в 70%).
        # Форма в отдельном виджете-контейнере: сворачивание блока прячет
        # контейнер целиком (layout в layout виджета не дает - прятать нечего).
        self.form_host = QWidget()
        self.form_host.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout(self.form_host)
        form.setSpacing(4)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Текст:", _narrow(self.edit))
        form.addRow(self.multiline)
        form.addRow("Начало, с:", self.start)
        form.addRow("Конец, с:", self.end)
        form.addRow("Угол:", _narrow(self.corner))
        form.addRow("Позиция X:", self.pos_x)
        form.addRow("Позиция Y:", self.pos_y)
        form.addRow("Поворот:", self.rotation)
        form.addRow("Размер:", _narrow(self.size))
        form.addRow("Цвет:", _narrow(self.color))
        form.addRow("Шрифт:", _narrow(self.fontbox))
        form.addRow(self.fontfile_button)
        form.addRow("Подложка:", _narrow(self.back_color))
        form.addRow("Непрозрачность:", alpha_row)
        form.addRow("Внутр. отступ:", _narrow(self.box_pad))
        self.advanced = QToolButton(self)
        set_themed_icon(self.advanced, "menu_settings")
        self.advanced.setToolTip("Дополнительные параметры текста")
        self.advanced.setAccessibleName("Дополнительные параметры текста")
        self.advanced.setCheckable(True)
        self.advanced.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.advanced.setIconSize(QSize(24, 24))
        self.advanced.setFixedSize(32, 32)
        advanced_fields = (
            self.multiline,
            self.start,
            self.end,
            self.pos_x,
            self.pos_y,
            self.fontfile_button,
            self.box_pad,
        )

        def show_advanced(visible: bool) -> None:
            for field in advanced_fields:
                form.setRowVisible(field, visible)

        self.advanced.toggled.connect(show_advanced)
        show_advanced(False)
        form.addRow(self.advanced)
        grid.addWidget(self.form_host, 1, 0, 1, 2)

        self.chk.toggled.connect(self._on_toggled)
        self.btn_fold.toggled.connect(self._on_folded)
        # стартовое состояние полей: у загруженного enabled=False погашены,
        # у нового блока активны
        self._on_toggled(self.chk.isChecked())
        if collapsed:
            self.btn_fold.setChecked(True)

    def _context_menu(self, position: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction("Дублировать текст").triggered.connect(self.duplicate_requested)
        menu.exec(self.mapToGlobal(position))

    def _edit_multiline(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Текст титра")
        dialog.resize(480, 280)
        layout = QVBoxLayout(dialog)
        edit = QPlainTextEdit(self.edit.text(), dialog)
        layout.addWidget(edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            dialog,
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.edit.setText(edit.toPlainText())

    def _choose_fontfile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Файл шрифта", self._fontfile, "Шрифты (*.ttf *.otf *.ttc)"
        )
        if path:
            self._fontfile = path
            self.fontfile_button.setToolTip(path)
            self.edited.emit()

    @property
    def iid(self) -> str:
        return self._iid

    def set_number(self, n: int) -> None:
        """Порядковый номер в заголовке. Сам текст - только в поле ввода,
        в заголовок его не дублируем (свернутый блок показывает начало)."""
        self._num = max(1, n)
        self._refresh_title()

    def _refresh_title(self) -> None:
        base = f"Текст {self._num}"
        if self._collapsed:
            snippet = (self.edit.text().strip() or "")[:24]
            self.lbl_title.setText(f"{base}: {snippet}" if snippet else base)
        else:
            self.lbl_title.setText(base)

    def _on_folded(self, on: bool) -> None:
        self._collapsed = bool(on)
        self.form_host.setVisible(not on)
        self.btn_fold.setText("▼" if on else "▲")
        self.btn_fold.setToolTip("Развернуть блок" if on else "Свернуть блок")
        self._refresh_title()
        self.folded_changed.emit(self._collapsed)

    def _sync_alpha_label(self, v: int) -> None:
        self.back_alpha_value.setText(f"{v} %")

    def _on_toggled(self, on: bool) -> None:
        # поля гасим, урна остается активной
        for w in (
            self.edit,
            self.corner,
            self.pos_x,
            self.pos_y,
            self.size,
            self.rotation,
            self.color,
            self.fontbox,
            self.back_color,
            self.back_alpha,
            self.back_alpha_value,
            self.box_pad,
            self.multiline,
            self.start,
            self.end,
            self.fontfile_button,
        ):
            w.setEnabled(on)
        self.edited.emit()

    def get(self) -> TextOverlay:
        corner_code = self.corner.currentData() or self.corner.currentText()
        # data = код цвета для ffmpeg. Ручной ввод (hex/имя): editable-текст
        # комбо отличается от подписи выбранного пункта - берем его.
        label, _code = COLOR_ITEMS[
            self.color.currentIndex() if self.color.currentIndex() >= 0 else 0
        ]
        typed = self.color.currentText().strip()
        color = _code if typed == label else (typed or "white")
        font_typed = self.fontbox.currentText().strip()
        font_family = "" if font_typed in ("", DEFAULT_FONT_LABEL) else font_typed
        bi = self.back_color.currentIndex()
        blabel, _bcode = (
            COLOR_ITEMS[bi] if 0 <= bi < len(COLOR_ITEMS) else COLOR_ITEMS[0]
        )
        btyped = self.back_color.currentText().strip()
        back_color = _bcode if btyped == blabel else (btyped or "black")
        return TextOverlay(
            iid=self._iid,
            text=self.edit.text(),
            corner=corner_code,
            fontsize=self.size.value(),
            color=color,
            font_family=font_family,
            margin_x=self.pos_x.value(),
            margin_y=self.pos_y.value(),
            enabled=self.chk.isChecked(),
            back_color=back_color,
            back_alpha=self.back_alpha.value(),
            box_pad=self.box_pad.value(),
            start=self.start.value(),
            end=self.end.value(),
            fontfile=self._fontfile,
            clip_iid=self._clip_iid,
            rotation=self.rotation.value(),
        )


class TextOverlayWidget(QWidget):
    """Общая панель текста, картинок и фигур; снизу меню добавления."""

    changed = Signal()
    overlay_selected = Signal(str)
    gesture_started = Signal()
    gesture_finished = Signal()
    input_finished = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAccessibleName("Наложения")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._host = QWidget()
        self._host_lay = QVBoxLayout(self._host)
        self._host_lay.setContentsMargins(0, 0, 0, 0)
        self._host_lay.setSpacing(4)
        # stretch в конце: блоки жмутся по содержимому, не растягиваются
        self._host_lay.addStretch(1)
        self._scroll.setWidget(self._host)
        lay.addWidget(self._scroll, 1)

        self.lbl_empty = QLabel("Наложений нет. Добавьте блок ниже.")
        self.lbl_empty.setWordWrap(True)
        self.lbl_empty.setStyleSheet("color: gray;")
        self._host_lay.insertWidget(0, self.lbl_empty)

        self._blocks: list[_OverlayBlock | GraphicOverlayBlock] = []
        self._folded: set[str] = set()
        self._loading = False

        row = QHBoxLayout()
        self.btn_add = QToolButton()
        set_themed_icon(self.btn_add, "ve_add_overlay")
        self.btn_add.setIconSize(QSize(32, 32))
        self.btn_add.setFixedSize(32, 32)
        self.btn_add.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.btn_add.setStyleSheet(FLAT_TOOLBUTTON_QSS)
        self.btn_add.setToolTip("Добавить наложение")
        self.btn_add.setAccessibleName("Добавить наложение")
        menu = QMenu(self.btn_add)
        for kind, label in (("text", "Текст"), *GRAPHIC_LABELS.items()):
            menu.addAction(label).triggered.connect(
                lambda checked=False, value=kind: self._add_kind(value)
            )
        self.btn_add.setMenu(menu)
        self.btn_add.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        row.addStretch(1)
        row.addWidget(self.btn_add)
        lay.addLayout(row)

        self._sync_empty()

    # -- блоки --
    def _add_kind(self, kind: str) -> None:
        if kind == "image":
            overlay = choose_image(self)
            if overlay is None:
                return
        else:
            overlay = TextOverlay(kind=kind, corner="TL" if kind != "text" else "BR")
        self._add_block(overlay)
        self._emit_changed()

    def _duplicate(self, block: _OverlayBlock | GraphicOverlayBlock) -> None:
        overlay = block.get()
        overlay.iid = new_iid()
        self._add_block(overlay)
        self._emit_changed()

    def _add_block(self, ov: TextOverlay | None = None) -> None:
        self._loading = True
        try:
            collapsed = bool(ov and ov.iid in self._folded)
            block = (
                GraphicOverlayBlock(ov, collapsed=collapsed)
                if ov and ov.kind != "text"
                else _OverlayBlock(ov, collapsed=collapsed)
            )
            block.btn_del.clicked.connect(
                lambda _=False, b=block: self._remove_block(b)
            )
            block.edited.connect(self._emit_changed)
            block.duplicate_requested.connect(lambda b=block: self._duplicate(b))
            block.folded_changed.connect(
                lambda on, b=block: self._on_fold_toggled(b, on)
            )
            for w in (
                (block.edit, block.corner, block.size, block.color)
                if isinstance(block, _OverlayBlock)
                else ()
            ):
                sig = (
                    w.textChanged
                    if isinstance(w, QLineEdit)
                    else (
                        w.currentIndexChanged
                        if isinstance(w, QComboBox) and not w.isEditable()
                        else None
                    )
                )
                if sig is not None:
                    sig.connect(lambda *a: self._emit_changed())
            # color редактируемый: currentTextChanged ловит и ручной ввод
            if isinstance(block, _OverlayBlock):
                block.color.currentTextChanged.connect(lambda *a: self._emit_changed())
                block.fontbox.currentTextChanged.connect(
                    lambda *a: self._emit_changed()
                )
                block.back_color.currentTextChanged.connect(
                    lambda *a: self._emit_changed()
                )
                block.size.valueChanged.connect(lambda *a: self._emit_changed())
                block.pos_x.valueChanged.connect(lambda *a: self._emit_changed())
                block.pos_y.valueChanged.connect(lambda *a: self._emit_changed())
                block.back_alpha.valueChanged.connect(lambda *a: self._emit_changed())
                block.box_pad.valueChanged.connect(lambda *a: self._emit_changed())
            self._blocks.append(block)
            for slider in block.findChildren(QSlider):
                slider.sliderPressed.connect(self.gesture_started)
                slider.sliderReleased.connect(self.gesture_finished)
            for field in block.findChildren(QAbstractSpinBox):
                field.setKeyboardTracking(False)
                field.editingFinished.connect(self.input_finished)
            for child in [block, *block.findChildren(QWidget)]:
                child.installEventFilter(self)
            guard_wheel(block)
            # перед stretch: блоки сверху, пустота снизу
            self._host_lay.insertWidget(self._host_lay.count() - 1, block)
            self._renumber()
        finally:
            self._loading = False
        self._sync_empty()
        if not ov:
            # новый блок (кнопка/программно): известить диалог.
            # clicked(QBool) приходит сюда позиционно, поэтому not ov
            self._emit_changed()

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.Type.MouseButtonPress:
            block = next(
                (b for b in self._blocks if obj is b or b.isAncestorOf(obj)), None
            )
            if block:
                self.overlay_selected.emit(block.iid)
        return super().eventFilter(obj, event)

    def _on_fold_toggled(
        self, block: _OverlayBlock | GraphicOverlayBlock, on: bool
    ) -> None:
        # чисто UI-состояние: на проект и превью не влияет, но пережить
        # пересборку блоков (refresh) должно
        try:
            if on:
                self._folded.add(block.iid)
            else:
                self._folded.discard(block.iid)
        except RuntimeError:
            pass

    def _remove_block(self, block: _OverlayBlock | GraphicOverlayBlock) -> None:
        self._loading = True
        try:
            self._blocks.remove(block)
            self._folded.discard(block.iid)
            self._host_lay.removeWidget(block)
            block.deleteLater()
        finally:
            self._loading = False
        self._sync_empty()
        self._renumber()
        self._emit_changed()

    def _renumber(self) -> None:
        """Заголовки 'Текст 1..N' по порядку блоков."""
        for i, b in enumerate(self._blocks, 1):
            try:
                b.set_number(i)
            except RuntimeError:
                pass

    def _emit_changed(self) -> None:
        if not self._loading:
            self.changed.emit()

    def _sync_empty(self) -> None:
        self.lbl_empty.setVisible(not self._blocks)

    # -- API --
    def get_all(self) -> list[TextOverlay]:
        return [b.get() for b in self._blocks]

    def select_overlay(self, iid: str, *, focus: bool = False) -> None:
        """Показать настройки титра, выбранного на текстовой дорожке."""
        for block in self._blocks:
            if block.iid == iid:
                block.btn_fold.setChecked(False)
                self._host_lay.activate()
                self._scroll.ensureWidgetVisible(block.chk, 0, 0)
                if focus:
                    if isinstance(block, GraphicOverlayBlock):
                        block.focus_editor()
                    else:
                        block.edit.setFocus()
                        block.edit.selectAll()
                break

    def set_overlays(self, ovs: list[TextOverlay]) -> None:
        """Показать список; если он совпадает с текущим - не трогать блоки
        (иначе пересоздание рвет фокус ввода при каждом refresh)."""
        cur = [b.get() for b in self._blocks]
        if len(cur) == len(ovs) and all(a == b for a, b in zip(cur, ovs)):
            return
        self._loading = True
        try:
            for b in list(self._blocks):
                self._host_lay.removeWidget(b)
                b.deleteLater()
            self._blocks = []
            for ov in ovs:
                self._add_block(ov)
        finally:
            self._loading = False
        self._sync_empty()
