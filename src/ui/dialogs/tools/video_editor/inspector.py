"""Инспектор выделенного объекта и контроллер атомарных правок."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.domain.video.editing import VIDEO_KEYS
from src.domain.video.models import Clip, Track
from src.logger import logger

from .notice_bar import ERROR_STYLE
from .wheel_guard import guard_wheel

if TYPE_CHECKING:
    from .dialog import VideoEditorDialog


VIDEO_FIELDS = (
    ("opacity", "Непрозрачность", 0, 1),
    ("scale", "Масштаб", 0.01, 4),
    ("position_x", "Смещение по X", -4, 4),
    ("position_y", "Смещение по Y", -4, 4),
    ("crop_left", "Кадрирование слева", 0, 0.99),
    ("crop_right", "Кадрирование справа", 0, 0.99),
    ("crop_top", "Кадрирование сверху", 0, 0.99),
    ("crop_bottom", "Кадрирование снизу", 0, 0.99),
    ("video_fade_in", "Появление, с", 0, 1e9),
    ("video_fade_out", "Исчезновение, с", 0, 1e9),
)
AUDIO_FIELDS = (
    ("volume", "Громкость", 0, 2),
    ("fade_in", "Нарастание, с", 0, 1e9),
    ("fade_out", "Затухание, с", 0, 1e9),
    ("normalization_gain", "Нормализация", 0, 100),
)
PERCENT_KEYS = {
    "opacity",
    "scale",
    "position_x",
    "position_y",
    "crop_left",
    "crop_right",
    "crop_top",
    "crop_bottom",
    "volume",
}
# Ползунок смещения покрывает обычные значения, большие вводятся в поле.
SLIDER_LIMITS = {"position_x": (-1, 1), "position_y": (-1, 1)}
FIELD_HINTS = {
    "scale": "100% — клип занимает весь холст. Масштаб меняется от центра клипа.",
    "position_x": "Смещение центра клипа от центра холста, в процентах ширины холста.",
    "position_y": "Смещение центра клипа от центра холста, в процентах высоты холста.",
}


class PercentField(QWidget):
    """Ползунок и поле в процентах; модель хранит те же значения долями."""

    committed = Signal()

    def __init__(
        self,
        label: str,
        low: float,
        high: float,
        slider_low: float,
        slider_high: float,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(round(slider_low * 100), round(slider_high * 100))
        self.slider.setSingleStep(1)
        self.slider.setPageStep(10)
        self.slider.setMinimumWidth(40)
        self.slider.setAccessibleName(label)
        self.spin = QDoubleSpinBox(self)
        self.spin.setDecimals(1)
        self.spin.setRange(low * 100, high * 100)
        self.spin.setSingleStep(1)
        self.spin.setSuffix(" %")
        self.spin.setKeyboardTracking(False)
        self.spin.setFixedWidth(84)
        self.spin.setAccessibleName(label)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.spin)
        self.spin.valueChanged.connect(self._spin_changed)
        self.spin.editingFinished.connect(self.committed)
        self.slider.valueChanged.connect(self._slider_changed)
        self.slider.sliderReleased.connect(self.committed)

    def _spin_changed(self, value: float) -> None:
        with QSignalBlocker(self.slider):
            self.slider.setValue(round(value))

    def _slider_changed(self, value: int) -> None:
        with QSignalBlocker(self.spin):
            self.spin.setValue(value)
        # Перетаскивание завершается отпусканием; клавиши и щелчок — сразу.
        if not self.slider.isSliderDown():
            self.committed.emit()

    @staticmethod
    def snap(value: float) -> float:
        """Значение модели с точностью, которую показывает поле."""
        return round(round(value * 100, 1) / 100, 6)

    def value(self) -> float:
        return round(self.spin.value() / 100, 6)

    def setValue(self, value: float) -> None:  # noqa: N802
        with QSignalBlocker(self.spin), QSignalBlocker(self.slider):
            self.spin.setValue(value * 100)
            self.slider.setValue(round(value * 100))

    def has_pending_input(self, value: float) -> bool:
        """Пользователь тянет ползунок или ввёл текст, ещё не попавший в модель."""
        return (
            self.slider.isSliderDown()
            or self.spin.cleanText() != self.spin.textFromValue(value * 100)
        )


class InspectorWidget(QWidget):
    """Поля отправляют намерения; обновление из модели не порождает правок."""

    changed = Signal(str, str, object)
    reset_requested = Signal(str)
    normalize_requested = Signal()
    rotate_requested = Signal(int)

    def __init__(self, titles: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(196)
        self._identity = ("", "")
        self._generation = 0
        self._pending_section: str | None = None
        self._updating = False
        self.fields: dict[str, QWidget] = {}
        self.sections: dict[str, QFrame] = {}
        self._values: dict[str, Any] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs)
        properties = QWidget(self.tabs)
        properties_layout = QVBoxLayout(properties)
        properties_layout.setContentsMargins(6, 6, 6, 6)
        properties_layout.setSpacing(4)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setAccessibleName("Сообщение инспектора")
        self.message.hide()
        properties_layout.addWidget(self.message)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(4)
        self.scroll_area.setWidget(self.body)
        properties_layout.addWidget(self.scroll_area, 1)
        self.scroll_area.verticalScrollBar().rangeChanged.connect(self._reveal_section)
        self.scroll_area.verticalScrollBar().sliderPressed.connect(self._cancel_reveal)
        self.tabs.addTab(properties, "Инспектор")
        self.tabs.addTab(titles, "Наложения")
        self._empty()

    def _empty(self) -> None:
        hint = QLabel("Выберите клип, титр или заголовок дорожки на таймлайне.")
        hint.setWordWrap(True)
        self.body_layout.addWidget(hint)
        self.body_layout.addStretch()

    def _clear(self) -> None:
        self._pending_section = None
        self._generation += 1
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.fields.clear()
        self.sections.clear()

    def _section(self, key: str, title: str, *, reset: bool = True) -> QFormLayout:
        group = QFrame()
        group.setFrameShape(QFrame.Shape.StyledPanel)
        group.setFrameShadow(QFrame.Shadow.Sunken)
        group.setAccessibleName(title)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 4, 8, 6)
        layout.setSpacing(4)
        layout.addWidget(QLabel(title))
        form_host = QWidget(group)
        form = QFormLayout(form_host)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(4)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addWidget(form_host)
        self.sections[key] = group
        self.body_layout.addWidget(group)
        if reset:
            button = QPushButton("Сбросить раздел")
            button.clicked.connect(lambda: self.reset_requested.emit(key))
            form.addRow(button)
        return form

    def set_message(self, text: str = "", *, error: bool = False) -> None:
        self.message.setStyleSheet(ERROR_STYLE if error else "")
        self.message.setText(text)
        self.message.setVisible(bool(text))

    def _emit(self, key: str, value: Any, generation: int) -> None:
        if not self._updating and generation == self._generation:
            field = self.fields.get(key)
            previous = self._values.get(key)
            if isinstance(previous, (float, int)) and not isinstance(previous, bool):
                if isinstance(field, QDoubleSpinBox):
                    previous = round(previous, field.decimals())
                elif isinstance(field, PercentField):
                    previous = field.snap(previous)
            if previous == value:
                return
            self.changed.emit(*self._identity, {key: value})

    def _number(
        self, form: QFormLayout, key: str, label: str, low: float, high: float
    ) -> None:
        if key in PERCENT_KEYS:
            field = PercentField(label, low, high, *SLIDER_LIMITS.get(key, (low, high)))
            field.setToolTip(FIELD_HINTS.get(key, ""))
            generation = self._generation
            field.committed.connect(
                lambda: self._emit(key, field.value(), generation)
            )
            self.fields[key] = field
            form.addRow(label, field)
            return
        field = QDoubleSpinBox()
        field.setDecimals(6)
        field.setRange(low, high)
        field.setSingleStep(0.04 if key != "volume" else 0.05)
        field.setKeyboardTracking(False)
        field.setMinimumWidth(80)
        field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        field.setAccessibleName(label)
        generation = self._generation
        field.editingFinished.connect(
            lambda: self._emit(key, field.value(), generation)
        )
        self.fields[key] = field
        form.addRow(label, field)

    def _check(self, form: QFormLayout, key: str, label: str) -> None:
        field = QCheckBox(label)
        generation = self._generation
        field.toggled.connect(lambda value: self._emit(key, value, generation))
        self.fields[key] = field
        form.addRow(field)

    def _combo(
        self, form: QFormLayout, key: str, label: str, entries: list[tuple[str, Any]]
    ) -> None:
        field = QComboBox()
        field.setMinimumWidth(80)
        field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        for text, value in entries:
            field.addItem(text, value)
        generation = self._generation
        field.activated.connect(
            lambda: self._emit(key, field.currentData(), generation)
        )
        field.setAccessibleName(label)
        self.fields[key] = field
        form.addRow(label, field)

    def display(
        self,
        kind: str,
        iid: str,
        title: str,
        values: dict[str, Any],
        *,
        video: bool = False,
        audio: bool = False,
        locked: bool = False,
        tracks: list[Track] | None = None,
    ) -> None:
        identity = (kind, iid)
        rebuild = identity != self._identity or (
            kind == "clip"
            and (
                video != ("video" in self.sections)
                or audio != ("audio" in self.sections)
            )
        )
        self.tabs.setTabToolTip(0, title)
        self.setAccessibleName(title)
        if rebuild:
            self._clear()
            self._identity = identity
            self.set_message()
            if kind == "clip":
                form = self._section("bounds", "Границы и скорость", reset=False)
                for key, label in (
                    ("timeline_start", "Начало на таймлайне, с"),
                    ("src_in", "Вход исходника, с"),
                    ("src_out", "Выход исходника, с"),
                    ("duration", "Длительность, с"),
                ):
                    self._number(form, key, label, 0, 1e9)
                self._combo(
                    form, "speed", "Скорость", [("0,5×", 0.5), ("1×", 1), ("2×", 2)]
                )
                if video:
                    form = self._section("video", "Видео")
                    self._combo(
                        form,
                        "rotation",
                        "Поворот",
                        [(f"{a}°", a) for a in (0, 90, 180, 270)],
                    )
                    turns = QHBoxLayout()
                    for text, tip, angle in (
                        ("↺ 90°", "Повернуть против часовой стрелки (Shift+R)", -90),
                        ("↻ 90°", "Повернуть по часовой стрелке (R)", 90),
                    ):
                        button = QPushButton(text)
                        button.setToolTip(tip)
                        button.setAccessibleName(tip)
                        button.clicked.connect(
                            lambda _=False, a=angle: self.rotate_requested.emit(a)
                        )
                        turns.addWidget(button)
                    form.addRow(turns)
                    self._check(form, "flip_h", "Отразить по горизонтали")
                    self.fields["flip_h"].setToolTip("Клавиша H")
                    self._check(form, "flip_v", "Отразить по вертикали")
                    self.fields["flip_v"].setToolTip("Клавиша V")
                    for row in VIDEO_FIELDS:
                        self._number(form, *row)
                if audio:
                    form = self._section("audio", "Звук")
                    for row in AUDIO_FIELDS:
                        self._number(form, *row)
                    self._check(form, "mute", "Выключить звук")
                    button = QPushButton("Нормализовать…")
                    button.setToolTip("Измерить звук и нормализовать пик до −1 dBFS")
                    button.clicked.connect(self.normalize_requested)
                    form.addRow(button)
            elif kind == "track":
                form = self._section("track", "Дорожка", reset=False)
                name = QLineEdit()
                generation = self._generation
                name.editingFinished.connect(
                    lambda: self._emit("name", name.text(), generation)
                )
                self.fields["name"] = name
                form.addRow("Имя", name)
                self._check(form, "locked", "Заблокировать")
                self._check(form, "hidden", "Скрыть")
                if audio:
                    self._check(form, "mute", "Выключить звук")
                    self._check(form, "solo", "Соло")
                    self._combo(
                        form,
                        "duck_source_iid",
                        "Приглушать по дорожке",
                        [("Не приглушать", "")]
                        + [
                            (tr.name, tr.iid)
                            for tr in tracks or []
                            if tr.kind == "audio" and tr.iid != iid
                        ],
                    )
                    for row in (
                        ("duck_threshold", "Порог", 0.000001, 1),
                        ("duck_ratio", "Соотношение", 1, 100),
                        ("duck_attack", "Атака, мс", 0.01, 9000),
                        ("duck_release", "Возврат, мс", 0.01, 9000),
                    ):
                        self._number(form, *row)
            elif kind == "text":
                hint = QLabel(
                    "Настройки выбранного наложения доступны во вкладке «Наложения»."
                )
                hint.setWordWrap(True)
                self.body_layout.addWidget(hint)
                button = QPushButton("Редактировать наложение")
                button.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
                self.body_layout.addWidget(button)
            else:
                self._empty()
            if kind:
                self.body_layout.addStretch()
            guard_wheel(self.body)
        self._updating = True
        try:
            for key, field in self.fields.items():
                field.setEnabled(not locked or (kind == "track" and key == "locked"))
                # Фоновое обновление не уничтожает ещё не завершённый ввод.
                if self._has_pending_input(key, field):
                    continue
                with QSignalBlocker(field):
                    value = values.get(key)
                    if isinstance(field, (QDoubleSpinBox, PercentField)):
                        field.setValue(value)
                    elif isinstance(field, QCheckBox):
                        field.setChecked(value)
                    elif isinstance(field, QComboBox):
                        field.setCurrentIndex(field.findData(value))
                    elif isinstance(field, QLineEdit):
                        field.setText(value)
            for group in self.sections.values():
                for button in group.findChildren(QPushButton):
                    button.setEnabled(not locked)
            self._values = dict(values)
            if locked:
                self.set_message(
                    "Дорожка заблокирована. Настройки доступны для просмотра."
                )
            elif self.message.text().startswith("Дорожка заблокирована"):
                self.set_message()
        finally:
            self._updating = False

    def _has_pending_input(self, key: str, field: QWidget) -> bool:
        """Фокус сам по себе не мешает: поле обязано показать отмену и повтор."""
        focus = QApplication.focusWidget()
        if field is not focus and not (focus and field.isAncestorOf(focus)):
            return False
        shown = self._values.get(key)
        if isinstance(field, PercentField):
            return shown is not None and field.has_pending_input(shown)
        if isinstance(field, QDoubleSpinBox):
            return shown is not None and field.cleanText() != field.textFromValue(shown)
        if isinstance(field, QLineEdit):
            return field.text() != shown
        if isinstance(field, QComboBox):
            return field.view().isVisible()
        return False

    def finish(self) -> None:
        for key, field in list(self.fields.items()):
            if not field.isEnabled():
                continue
            if isinstance(field, PercentField):
                field.spin.interpretText()
                previous = self._values.get(key)
                if previous is not None and field.value() != field.snap(previous):
                    field.committed.emit()
            elif isinstance(field, QDoubleSpinBox):
                field.interpretText()
                if field.value() != round(
                    self._values.get(key, field.value()), field.decimals()
                ):
                    field.editingFinished.emit()
            elif isinstance(field, QLineEdit):
                if field.text() != self._values.get(key):
                    field.editingFinished.emit()

    def show_section(self, key: str) -> None:
        self.tabs.setCurrentIndex(0)
        group = self.sections.get(key)
        if group:
            self._pending_section = key
            group.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            group.setFocus(Qt.FocusReason.OtherFocusReason)
            generation = self._generation

            def reveal() -> None:
                if generation == self._generation:
                    for section in self.sections.values():
                        section.layout().activate()
                    self.body_layout.activate()
                    self.scroll_area.verticalScrollBar().setValue(group.y())

            QTimer.singleShot(0, reveal)

    def _cancel_reveal(self) -> None:
        self._pending_section = None

    def _reveal_section(self) -> None:
        group = self.sections.get(self._pending_section or "")
        if group:
            self.scroll_area.verticalScrollBar().setValue(group.y())


class InspectorController(QObject):
    """Связывает намерения инспектора с доменом и общей историей."""

    def __init__(self, editor: VideoEditorDialog) -> None:
        super().__init__(editor)
        self.editor = editor
        self.widget = editor.inspector
        self.widget.changed.connect(self.change)
        self.widget.reset_requested.connect(self.reset)
        self.widget.normalize_requested.connect(editor.normalize_clip_audio)
        self.widget.rotate_requested.connect(self.rotate)
        self._changing = False

    def refresh(self) -> None:
        e = self.editor
        found = e._selected_clip_obj()
        if found:
            clip, track = found
            pair = [
                (tr, c)
                for tr in e.project.tracks
                for c in tr.clips
                if c.iid == clip.iid or (clip.link_iid and c.link_iid == clip.link_iid)
            ]
            audio = next((c for tr, c in pair if tr.kind == "audio"), None)
            picture = next((c for tr, c in pair if tr.kind == "video"), None)
            values = clip.to_dict()
            if picture is not None:
                values.update({key: getattr(picture, key) for key in VIDEO_KEYS})
            values["duration"] = clip.duration
            if audio:
                values.update(
                    {
                        key: getattr(audio, key)
                        for key in ("fade_in", "fade_out", "normalization_gain")
                    }
                )
            asset = e.project.asset_by_iid(clip.asset_iid)
            self.widget.display(
                "clip",
                clip.iid,
                f"{'Видео' if track.kind == 'video' else 'Звук'} · {asset.name if asset else 'Клип'}\nДорожка: {track.name}",
                values,
                video=picture is not None,
                audio=audio is not None,
                locked=any(tr.locked for tr, _ in pair),
            )
        elif e._sel_text:
            text = next(
                (t for t in e.project.text_overlays if t.iid == e._sel_text), None
            )
            if text:
                track = next(
                    (
                        tr
                        for tr in e.project.tracks
                        if any(c.iid == text.clip_iid for c in tr.clips)
                    ),
                    None,
                )
                self.widget.display(
                    "text",
                    text.iid,
                    f"{'Титр' if text.kind == 'text' else 'Наложение'} · {text.label[:60]}\nДорожка: {track.name if track else 'Титры'}",
                    {},
                    locked=bool(track and track.locked),
                )
                return
            e._sel_text = None
            self.refresh()
        elif e._sel_track:
            track = next(
                (tr for tr in e.project.tracks if tr.iid == e._sel_track), None
            )
            if track:
                self.widget.display(
                    "track",
                    track.iid,
                    f"Дорожка · {track.name}",
                    track.to_dict(),
                    audio=track.kind == "audio",
                    locked=track.locked,
                    tracks=e.project.tracks,
                )
                return
            e._sel_track = None
            self.refresh()
        else:
            self.widget.display("", "", "Инспектор", {})

    def change(self, kind: str, iid: str, values: dict[str, Any]) -> None:
        if self._changing or (kind, iid) != self.widget._identity:
            return
        self._changing = True
        e = self.editor
        try:
            e._commit_text_edit()
            before = e._take_snapshot()
            if "video_duration" not in values and any(
                key in values for key in ("video_fade_in", "video_fade_out")
            ):
                values = dict(
                    values,
                    video_offset=0,
                    video_duration=e._selected_clip_obj()[0].duration,
                )
            if "audio_duration" not in values and any(
                key in values for key in ("fade_in", "fade_out")
            ):
                values = dict(
                    values,
                    audio_offset=0,
                    audio_duration=e._selected_clip_obj()[0].duration,
                )
            if e.edits.properties(kind, iid, values):
                self.widget.set_message()
                e.push_undo(before, "Свойства объекта")
        except ValueError as error:
            logger.info(f"Видеоредактор: правка отклонена: {error}")
            self.widget.set_message(str(error), error=True)
            e.notice.show_error(str(error))
        finally:
            self._changing = False
            self.refresh()

    def picture_clip(self) -> Clip | None:
        """Видеочасть выделенного клипа, даже если выделена его звуковая половина."""
        e = self.editor
        found = e._selected_clip_obj()
        if found is None:
            return None
        clip, track = found
        if track.kind == "video":
            return clip
        return next(
            (
                other
                for tr in e.project.video_tracks()
                for other in tr.clips
                if clip.link_iid and other.link_iid == clip.link_iid
            ),
            None,
        )

    def rotate(self, degrees: int) -> None:
        clip = self.picture_clip()
        if clip is not None:
            self.change(
                *self.widget._identity, {"rotation": (clip.rotation + degrees) % 360}
            )

    def flip(self, key: str) -> None:
        clip = self.picture_clip()
        if clip is not None:
            self.change(*self.widget._identity, {key: not getattr(clip, key)})

    def move_and_scale(self, x: float, y: float, scale: float) -> None:
        if self.picture_clip() is not None:
            self.change(
                *self.widget._identity,
                {"position_x": x, "position_y": y, "scale": scale},
            )
            # При отказе рамка возвращается к значениям модели.
            self.editor._sync_transform_box()

    def reset(self, section: str) -> None:
        self.widget.finish()
        default = Clip()
        keys = (
            [row[0] for row in VIDEO_FIELDS]
            + ["rotation", "flip_h", "flip_v", "video_offset", "video_duration"]
            if section == "video"
            else (
                [row[0] for row in AUDIO_FIELDS]
                + ["mute", "audio_offset", "audio_duration"]
                if section == "audio"
                else []
            )
        )
        if keys and self.widget._identity[0] == "clip":
            self.change(
                *self.widget._identity, {key: getattr(default, key) for key in keys}
            )

    def open(self, section: str) -> None:
        self.editor._show_text_action.setChecked(True)
        self.refresh()
        if section == "text":
            self.widget.tabs.setCurrentIndex(1)
        else:
            self.widget.show_section(section)
