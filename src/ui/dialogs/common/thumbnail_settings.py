"""Reusable, transport-independent screen thumbnail settings."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import (
    THUMBNAIL_RESOURCE_RANGES,
    THUMBNAIL_VNC_RANGES,
)
from src.services.configuration.config_service import ConfigService
from src.ui.widgets.thread_count_slider import ThreadCountSlider
from src.utils.text.text_utils import plural_ru

_MIB = 1024 * 1024
# Screen-area presets in pixels; tooltips show the physical sizes.
_SCREEN_PRESETS = (
    ("Full HD 1920 x 1080", 2_073_600),
    ("4K UHD 3840 x 2160", 8_294_400),
    ("2 x 4K (два монитора)", 16_588_800),
    ("8K UHD 7680 x 4320", 33_177_600),
)
_MANUAL_SCREEN_INDEX = len(_SCREEN_PRESETS)


def _int_range(
    ranges: Mapping[str, tuple[int | float, int | float]], key: str
) -> tuple[int, int]:
    """Return integral boundaries for resource controls."""
    minimum, maximum = ranges[key]
    return int(minimum), int(maximum)


def _memory_maximum_hint(maximum_bytes: int) -> str:
    """Describe the actual configured safety ceiling in familiar units."""
    return (
        f"Максимум: {maximum_bytes / 1024**3:g} ГБ "
        f"({maximum_bytes / _MIB:g} МБ). 1 МБ = 1024 x 1024 Б."
    )


def _get_config_service() -> ConfigService:
    from src.di import get_container

    return get_container().resolve(ConfigService)


def _wrapped_form(group: QGroupBox) -> QFormLayout:
    """Form layout whose long row labels wrap instead of widening rows."""
    form = QFormLayout(group)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    return form


class _MegabyteField:
    """Memory slider in MiB that preserves the exact original bytes.

    Opening the editor and saving without touching the field must not round
    the persisted byte value (for example 256 000 000 B is 244.140625 MiB).
    """

    def __init__(self, maximum_bytes: int, step_mb: int) -> None:
        maximum_mb = maximum_bytes / _MIB
        if step_mb == 8:
            bands = ((8, 64, 8), (80, 128, 16), (160, 512, 32), (640, maximum_mb, 128))
            small = (1.0, 2.0, 4.0)
            description = "8 МБ до 64 МБ; 16 МБ до 128 МБ; 32 МБ до 512 МБ; 128 МБ выше"
        else:
            bands = ((16, 512, 16), (576, 1024, 64), (1280, maximum_mb, 256))
            small = (1.0,)
            description = "16 МБ до 512 МБ; 64 МБ до 1024 МБ; 256 МБ выше"
        points = small + tuple(
            float(value)
            for start, end, step in bands
            for value in range(start, int(end) + 1, step)
        )
        self.control = _SliderField(
            1.0,
            maximum_mb,
            step_mb,
            decimals=3,
            suffix=" МБ",
            points=points,
            step_description=description,
        )
        self.control.setToolTip("1 МБ = 1024 x 1024 Б.")
        self._original = 0
        self._dirty = False
        self.control.valueChanged.connect(self._mark_dirty)

    def _mark_dirty(self) -> None:
        self._dirty = True

    def set_bytes(self, value: int) -> None:
        self._original = int(value)
        self.control.setValue(self._original / _MIB)
        self._dirty = False

    def bytes(self) -> int:
        if not self._dirty:
            return self._original
        return round(self.control.value() * _MIB)


class _SliderField(QWidget):
    """A quantized slider and read-only value, without rounding loaded settings."""

    valueChanged = Signal(object)

    def __init__(
        self,
        minimum: float,
        maximum: float,
        step: float,
        *,
        decimals: int = 0,
        suffix: str | tuple[str, str, str] = "",
        special: str = "",
        points: tuple[float, ...] = (),
        step_description: str = "",
    ) -> None:
        super().__init__()
        self._minimum = minimum
        self._maximum = maximum
        self._step = step
        self._decimals = decimals
        self._suffix = suffix
        self._special = special
        self._custom_points = points
        self._step_description = step_description
        self._value = minimum
        self._points: list[int | float] = []
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimumWidth(100)
        self.label = QLabel()
        self.label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.slider, stretch=1)
        layout.addWidget(self.label)
        self.setFocusProxy(self.slider)
        self._rebuild_points()
        self.slider.valueChanged.connect(self._slider_changed)

    def _format(self, value: float) -> str:
        if self._special and value == self._minimum:
            return self._special
        if self._decimals:
            number = self.locale().toString(float(value), "f", self._decimals)
            number = number.rstrip("0").rstrip(self.locale().decimalPoint())
        else:
            number = self.locale().toString(int(value))
        suffix = self._suffix
        if isinstance(suffix, tuple):
            suffix = " " + plural_ru(round(value, self._decimals), *suffix)
        return number + suffix

    def _rebuild_points(self) -> None:
        if self._custom_points:
            middle = [
                p for p in self._custom_points if self._minimum < p < self._maximum
            ]
        else:
            first = math.floor(self._minimum / self._step) + 1
            last = math.ceil(self._maximum / self._step)
            middle = [round(i * self._step, 6) for i in range(first, last)]
        self._points = sorted({self._minimum, *middle, self._maximum})
        with QSignalBlocker(self.slider):
            self.slider.setRange(0, len(self._points) - 1)
            self.slider.setSingleStep(1)
            self.slider.setPageStep(max(1, min(10, len(self._points) // 10)))
        widest = max(
            self.fontMetrics().horizontalAdvance(text)
            for text in (self._format(value) for value in self._points)
        )
        self.label.setMinimumWidth(widest + 4)
        self._sync()

    def _sync(self) -> None:
        nearest = min(
            range(len(self._points)), key=lambda i: abs(self._points[i] - self._value)
        )
        with QSignalBlocker(self.slider):
            self.slider.setValue(nearest)
        text = self._format(self._value)
        self.label.setText(text)
        self.slider.setAccessibleDescription(f"Текущее значение: {text}")

    def setValue(self, value: float) -> None:
        """Load the exact value; only user movement selects a quantized stop."""
        value = max(self._minimum, min(self._maximum, value))
        value = float(value) if self._decimals else int(value)
        changed = value != self._value
        self._value = value
        self._sync()
        if changed:
            self.valueChanged.emit(value)

    def value(self) -> int | float:
        return self._value

    def maximum(self) -> int | float:
        return self._maximum

    def setMaximum(self, maximum: float) -> None:
        self._maximum = max(self._minimum, maximum)
        self._rebuild_points()
        self.setValue(self._value)

    def _slider_changed(self, position: int) -> None:
        self.setValue(self._points[position])

    def setToolTip(self, text: str) -> None:
        step = self._step_description or self._format(self._step)
        text = f"{text}\nШаг ползунка: {step}.".strip()
        super().setToolTip(text)
        self.slider.setToolTip(text)
        self.label.setToolTip(text)


class _CollapsibleSection(QWidget):
    """Checkable QToolButton header toggling only group visibility."""

    def __init__(
        self, group: QGroupBox, parent: QWidget | None = None, expanded: bool = False
    ) -> None:
        super().__init__(parent)
        self._group = group
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._toggle = QToolButton()
        self._toggle.setCheckable(True)
        self._toggle.setChecked(expanded)
        self._toggle.setText(group.title())
        self._toggle.setAccessibleName(f"Секция {group.title()}")
        self._toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.toggled.connect(self._set_expanded)
        layout.addWidget(self._toggle)
        layout.addWidget(group)
        self._set_expanded(expanded)

    def _set_expanded(self, expanded: bool) -> None:
        # Collapse hides the fields only; values and enabled state remain.
        self._group.setVisible(expanded)
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )

    def set_expanded(self, expanded: bool) -> None:
        self._toggle.setChecked(expanded)

    def is_expanded(self) -> bool:
        return self._toggle.isChecked()


class ThumbnailConfigWidget(QWidget):
    """Edit a private snapshot; only save() changes the canonical configuration."""

    def __init__(
        self,
        parent: QWidget | None = None,
        config_service: ConfigService | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = config_service or _get_config_service()
        self._initial = self._service.get_thumbnail_config()
        self._slider_fields: dict[str, _SliderField] = {}
        resources = self._initial["resources"]
        vnc = self._initial["vnc"]
        layout = QVBoxLayout(self)
        info = QLabel(
            "Общие настройки миниатюр для приложения и веб-интерфейса. "
            "Они не меняют команды и удаленное управление. "
            "Источник и сжатие кадров хостов нод задаются на ноде-владельце."
        )
        info.setWordWrap(True)
        info.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(info)
        capture_group = QGroupBox("Источник и обновление")
        capture_form = _wrapped_form(capture_group)
        self._backend_combo = QComboBox()
        self._backend_combo.addItem("Veyon", "veyon")
        self._backend_combo.addItem("VNC", "vnc")
        self._backend_combo.setToolTip(
            "Откуда получать снимки экранов: через Veyon или напрямую через VNC.\n"
            "Выбирайте службу, которая настроена на удаленных компьютерах. "
            "Для отдельного компьютера источник можно выбрать в его свойствах.\n"
            "При ошибке программа не переключается на другой источник сама."
        )
        capture_form.addRow("Источник по умолчанию:", self._backend_combo)
        self._thread_count_field = ThreadCountSlider()
        self._thread_count_field.slider.setAccessibleName("Общие потоки миниатюр")
        capture_form.addRow("Общие потоки миниатюр:", self._thread_count_field)
        self._thread_count_field.setToolTip(
            "Общий лимит параллельного получения миниатюр для приложения, "
            "веб-интерфейса и нод. Не меняет качество и готовые кадры. "
            "Применяется после перезапуска программы."
        )
        restart_hint = QLabel(
            "Изменение числа потоков миниатюр применяется после перезапуска программы."
        )
        restart_hint.setWordWrap(True)
        restart_hint.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        capture_form.addRow(restart_hint)
        self._interval_field = self._slider_row(
            capture_form,
            "Интервал обновления:",
            "interval",
            1,
            60,
            step=1,
            suffix=("секунда", "секунды", "секунд"),
        )
        self._interval_field.setToolTip(
            "Как часто запрашивать новую миниатюру каждого компьютера.\n"
            "Меньший интервал дает более свежую картинку, но увеличивает нагрузку "
            "на сеть и процессор. Для большого списка компьютеров выбирайте "
            "больший интервал."
        )
        self._stagger_field = self._slider_row(
            capture_form,
            "Разброс старта:",
            "stagger",
            0.0,
            30.0,
            step=0.5,
            decimals=1,
            suffix=("секунда", "секунды", "секунд"),
            special="Выключен",
        )
        self._stagger_field.setToolTip(
            "Распределяет первые запросы по времени, чтобы все компьютеры "
            "не начинали передавать экраны одновременно.\n"
            "Полезно для большого списка: снижает начальную нагрузку, "
            "но первые картинки появляются не сразу. Ноль - начинать без задержки. "
            "Интервал последующих обновлений не меняется."
        )
        self._require_online_chk = QCheckBox("Получать кадры только от онлайн-хостов")
        self._require_online_chk.setToolTip(
            "Включено: получать миниатюры только от компьютеров, "
            "которые отвечают на проверку доступности SSH.\n"
            "Выключите, если VNC работает, а SSH отключен или заблокирован: "
            "программа все равно попробует получить экран. "
            "Обычный статус компьютера от этого не меняется."
        )
        capture_form.addRow(self._require_online_chk)
        layout.addWidget(capture_group)

        cache_group = QGroupBox("Общий кэш")
        cache_form = _wrapped_form(cache_group)
        self._cache_bytes_field = _MegabyteField(
            _int_range(THUMBNAIL_RESOURCE_RANGES, "cache_bytes")[1], step_mb=8
        )
        self._slider_fields["cache_memory"] = self._cache_bytes_field.control
        self._cache_bytes_field.control.slider.setAccessibleName("Бюджет общего кэша")
        self._cache_bytes_field.control.setToolTip(
            "Сколько оперативной памяти можно использовать для временных "
            "готовых миниатюр всех компьютеров вместе.\n"
            "Один кадр могут использовать приложение, браузер и ноды "
            "без повторного получения и подготовки картинки.\n"
            "Это не история: изображения не сохраняются на диск "
            "и не показываются после потери связи.\n"
            "Память не выделяется заранее. При заполнении убираются "
            "давно неиспользуемые картинки; маленький бюджет означает больше "
            "повторных запросов, а не худшее качество.\n"
            + _memory_maximum_hint(
                _int_range(THUMBNAIL_RESOURCE_RANGES, "cache_bytes")[1]
            )
        )
        cache_form.addRow("Бюджет памяти:", self._cache_bytes_field.control)
        layout.addWidget(cache_group)

        image_group = QGroupBox("Изображение")
        image_form = _wrapped_form(image_group)
        max_dimension = min(
            int(resources["max_thumbnail_dimension"]),
            _int_range(THUMBNAIL_RESOURCE_RANGES, "max_thumbnail_dimension")[1],
        )
        self._width_field = self._slider_row(
            image_form,
            "Ширина по умолчанию:",
            "width",
            1,
            max_dimension,
            step=16,
            suffix=" px",
        )
        self._width_field.setToolTip(
            "Базовая ширина миниатюры в пикселях и ширина карточки в браузере.\n"
            "Больше - крупнее и подробнее картинка, но больше расход сети и памяти. "
            "Размер карточек в самом приложении меняется на панели списка.\n"
            "Фактический размер получаемого кадра также зависит от множителя "
            "разрешения и максимальной стороны в разделе «Дополнительно»."
        )
        self._scale_field = self._slider_row(
            image_form, "Множитель разрешения:", "scale", 1, 4, step=1, suffix="x"
        )
        self._scale_field.setToolTip(
            "Насколько крупнее получать изображение относительно карточки. "
            "Например, для ширины 128 px множитель 2x запрашивает 256 px.\n"
            "Больший множитель делает текст четче, но увеличивает расход "
            "памяти и сети. Для экономии ресурсов выбирайте 1x или 2x."
        )
        self._format_combo = QComboBox()
        self._format_combo.addItem("PNG (без потерь)", "png")
        self._format_combo.addItem("JPEG", "jpeg")
        self._format_combo.setToolTip(
            "PNG сохраняет четкие буквы и линии без потери качества, "
            "но картинки могут занимать больше места.\n"
            "JPEG обычно уменьшает расход сети, но может размывать мелкий текст. "
            "Выбирайте его, если скорость передачи важнее идеальной четкости."
        )
        image_form.addRow("Формат кадра:", self._format_combo)
        self._quality_field = self._slider_row(
            image_form, "Качество JPEG:", "quality", 0, 100, step=5, suffix="%"
        )
        self._quality_field.setToolTip(
            "Качество картинки при выбранном JPEG. Больше - четче изображение, "
            "но больше передаваемых данных; меньше - меньше трафик и больше искажений.\n"
            "Начните с 70-85. Даже 100 не делает JPEG форматом без потерь. "
            "На картинки PNG эта настройка не влияет."
        )
        self._compression_field = self._slider_row(
            image_form, "Сжатие PNG:", "compression", 0, 9, step=1
        )
        self._compression_field.setToolTip(
            "Насколько сильно уменьшать размер файлов PNG перед передачей.\n"
            "Качество не меняется: буквы и детали сохраняются при любом значении. "
            "Большее сжатие экономит сеть, но сильнее нагружает процессор "
            "и может замедлять подготовку кадров. На JPEG не влияет."
        )
        self._image_section = _CollapsibleSection(image_group, expanded=False)
        layout.addWidget(self._image_section)

        vnc_group = QGroupBox("VNC - ресурсы и подключения")
        vnc_form = _wrapped_form(vnc_group)
        vnc_hint = QLabel(
            "Чтобы получить миниатюру через VNC, приложению нужен полный исходный экран "
            "каждого компьютера. Поэтому память зависит не только от размера "
            "карточки, но и от разрешения удаленных мониторов. "
            "Число подключений и доступная для них память задаются отдельно."
        )
        vnc_hint.setWordWrap(True)
        vnc_hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        vnc_form.addRow(vnc_hint)
        self._vnc_hint = vnc_hint
        self._vnc_sessions_field = self._slider_row(
            vnc_form,
            "Число сеансов:",
            "vnc_sessions",
            *_int_range(THUMBNAIL_VNC_RANGES, "max_sessions"),
            step=1,
            suffix=("сеанс", "сеанса", "сеансов"),
        )
        self._vnc_sessions_field.setToolTip(
            "Сколько подключений VNC можно держать готовыми для обновления экранов.\n"
            "Если компьютеров больше, часть подключений придется создавать заново, "
            "и миниатюры могут обновляться медленнее. "
            "Увеличивайте вместе с бюджетом памяти, если компьютеров много. "
            "Само это число не выделяет память заранее."
        )
        self._vnc_memory_field = _MegabyteField(
            _int_range(THUMBNAIL_VNC_RANGES, "memory_budget_bytes")[1], step_mb=16
        )
        self._slider_fields["vnc_memory"] = self._vnc_memory_field.control
        self._vnc_memory_field.control.slider.setAccessibleName("Бюджет памяти VNC")
        self._vnc_memory_field.control.setToolTip(
            "Память для получения экранов через VNC: здесь хранятся полные экраны "
            "всех компьютеров вместе, а не только маленькие миниатюры.\n"
            "Экран Full HD и его копия занимают примерно 16 МБ, экран 4K - 64 МБ "
            "на компьютер. Во время обновления дополнительно нужна рабочая память.\n"
            "При нехватке памяти подключения могут пересоздаваться, "
            "а обновления пропускаться. Увеличивайте для большого числа компьютеров "
            "или больших экранов, только если на этой машине достаточно свободной памяти.\n"
            "Это не расход всей программы, и память не выделяется заранее.\n"
            + _memory_maximum_hint(
                _int_range(THUMBNAIL_VNC_RANGES, "memory_budget_bytes")[1]
            )
        )
        vnc_form.addRow("Бюджет памяти:", self._vnc_memory_field.control)
        self._vnc_capture_timeout_field = self._slider_row(
            vnc_form,
            "Ожидание кадра:",
            "vnc_capture_timeout",
            *THUMBNAIL_VNC_RANGES["capture_timeout_sec"],
            step=0.5,
            decimals=1,
            suffix=("секунда", "секунды", "секунд"),
        )
        self._vnc_capture_timeout_field.setToolTip(
            "Сколько времени дать на подключение к компьютеру, "
            "получение экрана и подготовку одной миниатюры.\n"
            "Если кадр не удалось получить вовремя, вместо старой картинки "
            "показывается статусная иконка. Увеличьте время для медленных "
            "компьютеров или сети; слишком большое значение задерживает "
            "обработку недоступных компьютеров."
        )
        self._vnc_auth_backoff_field = self._slider_row(
            vnc_form,
            "Пауза после ошибки доступа:",
            "vnc_auth_backoff",
            *THUMBNAIL_VNC_RANGES["auth_backoff_sec"],
            step=5,
            decimals=1,
            suffix=("секунда", "секунды", "секунд"),
        )
        self._vnc_auth_backoff_field.setToolTip(
            "Сколько ждать перед повторной попыткой после ошибки доступа, "
            "например неверного пароля VNC.\n"
            "Пауза защищает удаленный компьютер от частых неудачных входов; "
            "при повторных ошибках она увеличивается автоматически. "
            "Чтобы восстановить доступ, проверьте пароль в настройках VNC."
        )
        veyon_limit_row = QWidget()
        veyon_limit_layout = QVBoxLayout(veyon_limit_row)
        veyon_limit_layout.setContentsMargins(0, 0, 0, 0)
        veyon_limit_note = QLabel(
            "Лимит соединений Veyon WebAPI остается в настройках Veyon: "
            "он используется также для управляющих функций и удаленного доступа."
        )
        veyon_limit_note.setWordWrap(True)
        veyon_limit_note.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        veyon_limit_layout.addWidget(veyon_limit_note)
        self._veyon_limit_btn = QPushButton("Открыть настройки Veyon - WebAPI")
        self._veyon_limit_btn.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._veyon_limit_btn.clicked.connect(self._open_veyon_webapi)
        veyon_limit_layout.addWidget(self._veyon_limit_btn)
        vnc_form.addRow(veyon_limit_row)
        self._vnc_section = _CollapsibleSection(vnc_group, expanded=False)
        self._vnc_group = vnc_group
        layout.addWidget(self._vnc_section)

        advanced_group = QGroupBox("Дополнительно")
        advanced_form = _wrapped_form(advanced_group)
        self._cache_entries_field = self._slider_row(
            advanced_form,
            "Записей общего кэша:",
            "cache_entries",
            *_int_range(THUMBNAIL_RESOURCE_RANGES, "cache_entries"),
            step=64,
            suffix=("запись", "записи", "записей"),
        )
        self._cache_entries_field.setToolTip(
            "Сколько готовых картинок можно временно оставить в памяти. "
            "У одного компьютера их может быть несколько: для разных размеров карточек.\n"
            "При достижении лимита старые неиспользуемые картинки удаляются. "
            "Ограничение действует вместе с бюджетом памяти: "
            "останавливает рост кэша по количеству, а не по размеру в МБ."
        )
        self._session_records_field = self._slider_row(
            advanced_form,
            "Записей реестра сеансов:",
            "session_records",
            *_int_range(THUMBNAIL_RESOURCE_RANGES, "max_session_records"),
            step=16,
            suffix=("запись", "записи", "записей"),
        )
        self._session_records_field.setToolTip(
            "Сколько разных подключений миниатюр приложение может учитывать "
            "одновременно для Veyon и VNC вместе.\n"
            "Если все места заняты, сбор для новых компьютеров откладывается. "
            "Увеличивайте, если большой список не помещается в этот лимит. "
            "Это не число рабочих потоков; обычно настройку менять не требуется."
        )
        self._session_records_field.valueChanged.connect(self._update_session_limit)
        self._vnc_variants_field = self._slider_row(
            advanced_form,
            "Вариантов на VNC-сеанс:",
            "vnc_variants",
            *_int_range(THUMBNAIL_VNC_RANGES, "max_image_variants"),
            step=1,
            suffix=("вариант", "варианта", "вариантов"),
        )
        self._vnc_variants_field.setToolTip(
            "Сколько версий одной миниатюры хранить для одного компьютера VNC, "
            "например для разных размеров карточек в приложении и браузере.\n"
            "Больше версий уменьшает повторную подготовку картинок, "
            "но занимает больше памяти. Это разные размеры одного экрана, "
            "а не история снимков."
        )
        self._vnc_poll_timeout_field = self._slider_row(
            advanced_form,
            "Ожидание обновления VNC:",
            "vnc_poll_timeout",
            *THUMBNAIL_VNC_RANGES["poll_timeout_sec"],
            step=0.05,
            decimals=3,
            suffix=("секунда", "секунды", "секунд"),
            points=(
                *(i / 20 for i in range(1, 21)),
                *(i / 2 for i in range(3, 21)),
                *(float(i) for i in range(15, 121, 5)),
            ),
            step_description="0,05 с до 1 с; 0,5 с до 10 с; 5 с выше",
        )
        self._vnc_poll_timeout_field.setToolTip(
            "Как долго ждать изменений экрана VNC, когда первый кадр уже получен.\n"
            "Неподвижный экран не считается ошибкой: при живом соединении "
            "сохраняется текущая картинка. Долгое ожидание может задерживать "
            "обработку других компьютеров. Не может быть больше времени "
            "из настройки «Ожидание кадра»."
        )
        self._vnc_capture_timeout_field.valueChanged.connect(self._update_poll_limit)
        self._max_dimension_field = self._slider_row(
            advanced_form,
            "Макс. сторона миниатюры:",
            "max_dimension",
            *_int_range(THUMBNAIL_RESOURCE_RANGES, "max_thumbnail_dimension"),
            step=64,
            suffix=" px",
        )
        self._max_dimension_field.setToolTip(
            "Наибольшая допустимая ширина или высота готовой миниатюры. "
            "Это не разрешение удаленного монитора.\n"
            "Ограничение защищает программу от слишком крупных картинок. "
            "Обычные запросы с большим множителем вписываются в этот размер; "
            "меньший предел экономит сеть и память, но снижает детализацию."
        )
        self._max_dimension_field.valueChanged.connect(self._width_field.setMaximum)
        self._screen_combo = QComboBox()
        for label, pixels in _SCREEN_PRESETS:
            self._screen_combo.addItem(label, pixels)
        self._screen_combo.addItem("Другая площадь (МП)", None)
        self._screen_combo.setToolTip(
            "Самый большой полный экран, который разрешено получать через VNC. "
            "Все мониторы одного компьютера считаются вместе.\n"
            "Full HD - около 2,1 МП, 4K - 8,3 МП, два монитора 4K - 16,6 МП. "
            "Если экран больше лимита, миниатюра не получится. "
            "Увеличение разрешает большие экраны, но требует больше памяти."
        )
        self._screen_combo.currentIndexChanged.connect(self._update_screen_controls)
        advanced_form.addRow("Площадь исходного экрана:", self._screen_combo)
        self._screen_mp_field = self._slider_row(
            advanced_form,
            "Площадь, МП:",
            "screen_mp",
            1.0,
            THUMBNAIL_VNC_RANGES["max_screen_pixels"][1] / 1_000_000,
            step=1,
            decimals=3,
            suffix=" МП",
        )
        self._screen_mp_field.setToolTip(
            "Свой предел суммарной площади всех мониторов одного компьютера. "
            "Доступен при выборе «Другая площадь». 1 МП - миллион пикселей.\n"
            "Например, для двух мониторов 4K нужно не меньше 16,6 МП. "
            "Это размер полного удаленного экрана, а не готовой миниатюры; "
            "большие экраны требуют большего бюджета памяти VNC."
        )
        self._advanced_section = _CollapsibleSection(advanced_group, expanded=False)
        self._advanced_group = advanced_group
        self._advanced_form = advanced_form
        layout.addWidget(self._advanced_section)

        display_group = QGroupBox("Отображение в списке")
        display_form = _wrapped_form(display_group)
        self._style_combo = QComboBox()
        self._style_combo.addItem("В рамке монитора", "monitor")
        self._style_combo.addItem("Одна картинка", "plain")
        self._style_combo.setToolTip(
            "Показывать экран в рамке монитора или без рамки. "
            "Меняется только внешний вид карточки: качество получаемых кадров "
            "и расход сети от этого не меняются. "
            "Кнопка рамки на панели списка переключает эту же настройку."
        )
        display_form.addRow("Стиль:", self._style_combo)
        display_info = QLabel(
            "Показ миниатюр и размер карточек переключаются на панели списка. "
            "Кнопка рамки на панели меняет этот же параметр стиля."
        )
        display_info.setWordWrap(True)
        display_info.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        display_form.addRow(display_info)
        layout.addWidget(display_group)
        layout.addStretch(1)
        for field, combo in (
            ("backend", self._backend_combo),
            ("format", self._format_combo),
            ("style", self._style_combo),
        ):
            combo.setCurrentIndex(max(0, combo.findData(self._initial[field])))
        for field, control in (
            ("thread_count", self._thread_count_field),
            ("interval_sec", self._interval_field),
            ("stagger_sec", self._stagger_field),
            ("width", self._width_field),
            ("quality", self._quality_field),
            ("compression", self._compression_field),
            ("scale", self._scale_field),
        ):
            control.setValue(self._initial[field])
        self._require_online_chk.setChecked(self._initial["require_online"])
        self._cache_bytes_field.set_bytes(resources["cache_bytes"])
        self._cache_entries_field.setValue(resources["cache_entries"])
        self._session_records_field.setValue(resources["max_session_records"])
        self._max_dimension_field.setValue(resources["max_thumbnail_dimension"])
        self._vnc_sessions_field.setValue(vnc["max_sessions"])
        self._vnc_memory_field.set_bytes(vnc["memory_budget_bytes"])
        self._vnc_capture_timeout_field.setValue(vnc["capture_timeout_sec"])
        self._vnc_poll_timeout_field.setValue(vnc["poll_timeout_sec"])
        self._vnc_auth_backoff_field.setValue(vnc["auth_backoff_sec"])
        self._vnc_variants_field.setValue(vnc["max_image_variants"])
        self._update_session_limit(self._session_records_field.value())
        self._update_poll_limit(self._vnc_capture_timeout_field.value())
        self._set_screen_pixels(vnc["max_screen_pixels"])
        self._format_combo.currentIndexChanged.connect(self._update_format_controls)
        self._backend_combo.currentIndexChanged.connect(self._update_backend_controls)
        self._update_format_controls()
        self._update_backend_controls()

    def _slider_row(
        self,
        form: QFormLayout,
        title: str,
        key: str,
        minimum: float,
        maximum: float,
        *,
        step: float,
        decimals: int = 0,
        suffix: str | tuple[str, str, str] = "",
        special: str = "",
        points: tuple[float, ...] = (),
        step_description: str = "",
    ) -> _SliderField:
        control = _SliderField(
            minimum,
            maximum,
            step,
            decimals=decimals,
            suffix=suffix,
            special=special,
            points=points,
            step_description=step_description,
        )
        control.slider.setAccessibleName(title.rstrip(":"))
        control.setToolTip("")
        self._slider_fields[key] = control
        form.addRow(title, control)
        return control

    def _update_session_limit(self, maximum: float) -> None:
        self._vnc_sessions_field.setMaximum(
            min(maximum, THUMBNAIL_VNC_RANGES["max_sessions"][1])
        )

    def _update_poll_limit(self, maximum: float) -> None:
        self._vnc_poll_timeout_field.setMaximum(
            min(maximum, THUMBNAIL_VNC_RANGES["poll_timeout_sec"][1])
        )

    def _update_backend_controls(self) -> None:
        """Gate VNC-only settings by the default thumbnail backend.

        Hidden widgets keep their values: switching sources must not reset
        the filled resource limits for devices overridden to VNC.
        """
        vnc = self._backend_combo.currentData() == "vnc"
        self._vnc_section.setVisible(vnc)
        for field in (
            self._vnc_variants_field,
            self._vnc_poll_timeout_field,
            self._screen_combo,
            self._screen_mp_field,
        ):
            self._advanced_form.setRowVisible(field, vnc)

    def _set_screen_pixels(self, pixels: int) -> None:
        index = self._screen_combo.findData(int(pixels))
        if index >= 0:
            self._screen_combo.setCurrentIndex(index)
        else:
            self._screen_combo.setCurrentIndex(_MANUAL_SCREEN_INDEX)
            self._screen_mp_field.setValue(int(pixels) / 1_000_000)
        self._update_screen_controls()

    def _update_screen_controls(self) -> None:
        manual = self._screen_combo.currentIndex() == _MANUAL_SCREEN_INDEX
        self._screen_mp_field.setEnabled(manual)

    def _screen_pixels(self) -> int:
        if self._screen_combo.currentIndex() == _MANUAL_SCREEN_INDEX:
            return round(self._screen_mp_field.value() * 1_000_000)
        pixels = self._screen_combo.currentData()
        return int(pixels) if pixels is not None else 16_000_000

    def _open_veyon_webapi(self) -> None:
        """Open the canonical Veyon WebAPI limit; no second storage here."""
        from src.ui.dialogs.common.veyon_settings import VeyonSettingsDialog

        dialog = VeyonSettingsDialog(self)
        dialog.show_webapi_section()
        dialog.exec()

    def _update_format_controls(self) -> None:
        jpeg = self._format_combo.currentData() == "jpeg"
        self._quality_field.setEnabled(jpeg)
        self._compression_field.setEnabled(not jpeg)

    def values(self) -> dict[str, Any]:
        return {
            "backend": self._backend_combo.currentData(),
            "thread_count": self._thread_count_field.value(),
            "interval_sec": self._interval_field.value(),
            "stagger_sec": self._stagger_field.value(),
            "width": self._width_field.value(),
            "format": self._format_combo.currentData(),
            "quality": self._quality_field.value(),
            "compression": self._compression_field.value(),
            "scale": self._scale_field.value(),
            "style": self._style_combo.currentData(),
            "require_online": self._require_online_chk.isChecked(),
            "resources": {
                "cache_bytes": self._cache_bytes_field.bytes(),
                "cache_entries": self._cache_entries_field.value(),
                "max_session_records": self._session_records_field.value(),
                "max_thumbnail_dimension": self._max_dimension_field.value(),
            },
            "vnc": {
                "max_sessions": self._vnc_sessions_field.value(),
                "memory_budget_bytes": self._vnc_memory_field.bytes(),
                "capture_timeout_sec": self._vnc_capture_timeout_field.value(),
                "poll_timeout_sec": self._vnc_poll_timeout_field.value(),
                "auth_backoff_sec": self._vnc_auth_backoff_field.value(),
                "max_image_variants": self._vnc_variants_field.value(),
                "max_screen_pixels": self._screen_pixels(),
            },
        }

    def save(self) -> bool:
        ok, message = self._service.save_thumbnail_config(self.values())
        if not ok:
            QMessageBox.warning(
                self, "Миниатюры", message or "Некорректные параметры миниатюр."
            )
        return ok


class ThumbnailSettingsDialog(QDialog):
    """Show thumbnail settings even when Veyon is not installed."""

    def __init__(
        self,
        parent: QWidget | None = None,
        config_service: ConfigService | None = None,
    ) -> None:
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Миниатюры экранов")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        # The editor grows with the resource groups: scroll it instead of
        # stretching the dialog, keeping the buttons pinned to the bottom.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.widget = ThumbnailConfigWidget(config_service=config_service)
        scroll.setWidget(self.widget)
        layout.addWidget(scroll, stretch=1)
        buttons = QDialogButtonBox()
        buttons.addButton("Сохранить", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        if self.widget.save():
            super().accept()


def thumbnail_settings_link(
    parent: QWidget | None = None,
    config_service: ConfigService | None = None,
) -> QWidget:
    """Link other settings windows to the single canonical thumbnail editor."""
    widget = QWidget(parent)
    widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    info = QLabel(
        "Источник, интервал, размер и сжатие - в едином разделе "
        "«Настройки - Миниатюры экранов»."
    )
    info.setWordWrap(True)
    info.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    layout.addWidget(info)
    button = QPushButton("Настройки миниатюр экранов")
    button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    button.clicked.connect(
        lambda: ThumbnailSettingsDialog(widget, config_service).exec()
    )
    layout.addWidget(button)
    layout.addStretch(1)
    return widget
