"""Диалог настроек камер видеонаблюдения ЕЦХД.

Три вкладки:
- «Подключение»: логин/пароль/таймаут пинга.
- «Запись»: каталог вывода, плеер, шаблон пути/имени файла, превью.
- «Камеры»: иерархия Адрес → Кабинет → Камера (адрес RTSP) с правкой узлов.
"""

from __future__ import annotations

import datetime
import re as _re
from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4
from typing import Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.ui.icons import _icon_from_key
from src.config.settings import CameraConfig, normalize_camera_config
from src.domain.models.camera import CameraModel
from src.logger import logger
from src.services import CameraService
from src.workers.camera_workers import DEFAULT_PING_INTERVAL, CameraHardwareInfoWorker

PLAYER_OPTIONS = ["vlc", "mpv", "ffplay", "системный"]


class _LabeledSlider(QWidget):
    """Ползунок с подписью текущего значения; ``special`` — текст для нуля."""

    def __init__(
        self,
        low: int,
        high: int,
        value: int,
        suffix: str = "",
        special: str = "",
        step: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._suffix = suffix
        self._special = special
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(low, high)
        self._slider.setSingleStep(step)
        self._slider.setPageStep(step * 5)
        self._slider.setValue(value)
        self._label = QLabel()
        self._label.setMinimumWidth(96)
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self._slider, 1)
        layout.addWidget(self._label)
        self._slider.valueChanged.connect(self._refresh)
        self._refresh(value)

    def _refresh(self, value: int) -> None:
        self._label.setText(
            self._special if value == 0 and self._special else f"{value}{self._suffix}"
        )

    def value(self) -> int:
        return self._slider.value()

    def setValue(self, value: int) -> None:
        self._slider.setValue(value)

    def setToolTip(self, text: str) -> None:
        super().setToolTip(text)
        self._slider.setToolTip(text)
        self._label.setToolTip(text)

# Колонки дерева камер: Адрес | Камера | Адрес RTSP.
# Адрес и кабинет оба пишут в колонку ADDR (различаются вложенностью).
COL_ADDR, COL_CAM, COL_IP = range(3)

OUTPUT_HINT = (
    "Каталог записи. Пусто = домашняя папка. "
    "Доступны те же подстановки, что в шаблоне пути."
)
PATH_MASK_HINT = (
    "Шаблон пути и каталога. Единые подстановки:\n"
    "  %addr% — адрес, %cabinet% — кабинет, %camera% — имя камеры,\n"
    "  %year% — год, %month% — месяц, %day% — день,\n"
    "  %hour% — час, %min% — минуты, %sec% — секунды.\n"
    "Значения адреса, кабинета и камеры очищаются от недопустимых символов.\n"
    "Путь должен оставаться внутри каталога записи. Пусто = адрес/год/месяц/день/кабинет."
)
FILE_MASK_HINT = (
    "Шаблон имени файла. Те же подстановки, что в шаблоне пути,\n"
    "плюс формат даты/времени strftime. Например: %camera%_%Y-%m-%d_%H-%M-%S\n"
    "Расширение .mkv добавляется автоматически."
    " К имени добавляется уникальный суффикс для защиты от перезаписи."
    " Пусто = имя камеры и дата/время. Разделители каталогов в имени запрещены."
)

CAMERA_SETTING_HINTS = {
    "ping_workers": (
        "Сколько камер проверять на доступность одновременно. Большее значение "
        "ускоряет опрос большого списка, но увеличивает нагрузку на сеть."
    ),
    "network_timeout": (
        "Сколько секунд ждать сетевых операций RTSP при записи и проверке потока. "
        "Увеличьте при медленной сети; при большом значении сбой обнаруживается позже."
    ),
    "stale_timeout": (
        "Сколько секунд запись может оставаться без прогресса. По истечении этого "
        "времени зависшая запись останавливается и выполняется переподключение."
    ),
    "record_crf": (
        "Качество при перекодировании видео: чем меньше число, тем лучше качество "
        "и больше файл. 23 — обычное значение. Для записи «Из потока» не используется."
    ),
    "record_gop": (
        "Интервал между ключевыми кадрами, в секундах. Меньше — точнее перемотка "
        "и быстрее восстановление после обрыва, но файл больше."
    ),
    "record_max_fps": (
        "Потолок частоты кадров при перекодировании. Лишние кадры отбрасываются, "
        "что снижает нагрузку и размер файла. «Как в потоке» — без ограничения."
    ),
    "record_video_bitrate": (
        "Целевой битрейт видео, кбит/с. «По CRF» — качество задаётся ползунком CRF, "
        "размер файла зависит от сцены. Фиксированный битрейт даёт предсказуемый размер."
    ),
    "record_audio_bitrate": (
        "Битрейт звука для AAC и Opus, кбит/с. Для PCM и FLAC не используется."
    ),
    "reconnect_grace": (
        "Сколько секунд при обрыве потока переподключаться и продолжать запись "
        "в тот же файл. Если связь не вернулась за это время, файл закрывается "
        "и запись продолжается в новом. 0 отключает такое переподключение."
    ),
    "offline_failures": (
        "Сколько последовательных неудачных проверок доступности нужно для статуса "
        "«Оффлайн». Успешная проверка сбрасывает счётчик. Большее значение сглаживает "
        "краткие сбои, но задерживает смену статуса."
    ),
    "retry_max": (
        "Верхний предел паузы между повторными подключениями, в секундах. "
        "После временного сбоя пауза постепенно увеличивается до этого значения. "
        "Это не количество попыток."
    ),
    "max_simultaneous": (
        "Максимальное число одновременно запущенных записей всех камер. "
        "Камеры, ожидающие переподключения, тоже занимают место. "
        "При достижении лимита новую запись запустить нельзя."
    ),
    "segment_minutes": (
        "Через сколько минут закрыть текущий файл и продолжить запись в новом. "
        "0 отключает деление по времени. Если задан и размер части, "
        "новая часть начинается при достижении любого из двух лимитов."
    ),
    "segment_mb": (
        "При каком размере текущего файла начинать новую часть, в МиБ "
        "(1 МиБ = 1 048 576 байт). 0 отключает деление по размеру. "
        "Размер проверяется периодически, поэтому файл может немного превысить лимит."
    ),
}

# «Адрес 12» → 12 (простой формат: слово + пробел + число).
_NUMBERED_NAME_RE = _re.compile(r"(Адрес|Кабинет)\s+(\d+)")


class CameraSettingsDialog(QDialog):
    """Диалог настроек камер (модальный)."""

    def __init__(
        self, camera_service: CameraService, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки камер ЕЦХД")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(560, 420)
        self.setMinimumSize(520, 360)
        self.setSizeGripEnabled(True)

        self._svc = camera_service
        self._cfg = deepcopy(camera_service.get_settings())
        self._probe = None
        self._hardware_info: CameraHardwareInfoWorker | None = None
        self._pending_result = None
        self._extra_fields = {}
        self._collected_ids = {}

        self._suppress_item_changed = False
        self._setup_ui()
        self._load_data()
        self._hardware_info = CameraHardwareInfoWorker(self)
        self._hardware_info.result.connect(self._set_decoding_device_names)
        self._hardware_info.finished.connect(self._hardware_info_finished)
        self._hardware_info.start()

    @Slot(str, str)
    def _set_decoding_device_names(self, gpu: str, cpu: str) -> None:
        self._preview_decoding_combo.setItemText(
            self._preview_decoding_combo.findData("gpu"), f"Аппаратное ({gpu})"
        )
        self._preview_decoding_combo.setItemText(
            self._preview_decoding_combo.findData("cpu"), f"Программное ({cpu})"
        )
        self._record_encoder_combo.setItemText(
            self._record_encoder_combo.findData("cpu"), f"Процессор ({cpu})"
        )
        self._record_encoder_combo.setItemText(
            self._record_encoder_combo.findData("gpu"), f"Видеокарта ({gpu})"
        )

    @Slot()
    def _hardware_info_finished(self) -> None:
        thread, self._hardware_info = self._hardware_info, None
        if thread is not None:
            thread.deleteLater()
        if self._pending_result is not None and self._probe is None:
            result, self._pending_result = self._pending_result, None
            self.done(result)

    # ---------- UI ----------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        if self._svc.load_error:
            error_label = QLabel(self._svc.load_error)
            error_label.setWordWrap(True)
            layout.addWidget(error_label)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, 1)

        self._tabs.addTab(self._build_cameras_tab(), "Камеры")
        self._tabs.addTab(self._build_connection_tab(), "Подключение")
        self._tabs.addTab(self._build_view_tab(), "Просмотр")
        self._tabs.addTab(self._build_record_tab(), "Запись")
        self._tabs.addTab(self._build_encoding_tab(), "Кодирование")

        self._button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self._button_box.button(QDialogButtonBox.Ok).setText("Сохранить")
        self._button_box.button(QDialogButtonBox.Ok).setEnabled(
            not self._svc.load_error
        )
        self._button_box.button(QDialogButtonBox.Cancel).setText("Закрыть")
        self._button_box.accepted.connect(self._on_accept)
        self._button_box.rejected.connect(self.reject)
        layout.addWidget(self._button_box)

    def _build_connection_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)

        self._login_edit = QLineEdit()
        self._login_edit.setPlaceholderText("Логин RTSP (например: subaccount)")
        self._login_edit.setToolTip(
            "Имя пользователя для доступа к RTSP-потокам. Применяется ко всем камерам."
        )
        form.addRow("Логин:", self._login_edit)

        self._password_edit = QLineEdit()
        self._password_edit.setPlaceholderText("Пароль RTSP")
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_edit.setToolTip(
            "Пароль доступа к RTSP-потокам всех камер. Используется для просмотра, записи и проверки подключения."
        )
        form.addRow("Пароль:", self._password_edit)

        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(1, 10)
        self._timeout_spin.setSuffix(" сек")
        self._timeout_spin.setToolTip(
            "Сколько секунд ждать ответа TCP-порта RTSP (554) при проверке доступности. Статус «Онлайн» означает, что порт отвечает; правильность пароля и наличие видео проверяются отдельной кнопкой."
        )
        form.addRow("Таймаут пинга:", self._timeout_spin)

        self._ping_interval_spin = QSpinBox()
        self._ping_interval_spin.setRange(5, 300)
        self._ping_interval_spin.setSuffix(" сек")
        self._ping_interval_spin.setToolTip(
            "Через сколько секунд повторять проверку доступности камер. Меньший интервал быстрее обновляет статус, но создаёт больше сетевых запросов."
        )
        form.addRow("Интервал опроса:", self._ping_interval_spin)
        for key, label, low, high, suffix in (
            ("ping_workers", "Параллельных проверок:", 1, 32, ""),
            ("network_timeout", "Таймаут RTSP:", 1, 120, " сек"),
            ("stale_timeout", "Ожидание прогресса записи:", 5, 600, " сек"),
            ("reconnect_grace", "Переподключение в тот же файл:", 0, 3600, " сек"),
            ("offline_failures", "Ошибок до статуса «Оффлайн»:", 1, 10, ""),
            ("retry_max", "Макс. пауза переподключения:", 3, 600, " сек"),
        ):
            self._add_integer_field(form, key, label, low, high, suffix)
        self._test_button = QPushButton("Проверить выбранную камеру")
        self._test_button.setToolTip(
            "Сначала выберите камеру на вкладке «Камеры». Проверка использует текущие логин, пароль и таймаут RTSP без сохранения настроек. Поток открывается встроенными медиабиблиотеками."
        )
        self._test_button.clicked.connect(self._test_connection)
        form.addRow(self._test_button)
        self._test_result = QLabel()
        self._test_result.setWordWrap(True)
        form.addRow(self._test_result)

        hint = QLabel(
            "Учётные данные применяются ко всем камерам. "
            "Каждая камера идентифицируется как «Адрес::Кабинет::Камера»."
        )
        hint.setWordWrap(True)
        form.addRow(hint)
        return self._scroll(w)

    def _scroll(self, widget):
        """Сохранить доступ к полям на небольших экранах."""
        form = widget.layout()
        if isinstance(form, QFormLayout):
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setFieldGrowthPolicy(
                QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
            )
            for row in range(form.rowCount()):
                label_item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                if label_item is not None and isinstance(label_item.widget(), QLabel):
                    label_item.widget().setWordWrap(True)
                    field_item = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
                    if field_item is not None and field_item.widget() is not None:
                        field = field_item.widget()
                        hint = field.toolTip() or next(
                            (
                                child.toolTip()
                                for child in field.findChildren(QWidget)
                                if child.toolTip()
                            ),
                            "",
                        )
                        label_item.widget().setToolTip(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        return scroll

    def _add_slider_field(
        self, form, key, label, low, high, suffix="", special="", step=1
    ):
        slider = _LabeledSlider(
            low, high, int(getattr(self._cfg, key)), suffix, special, step
        )
        slider.setToolTip(CAMERA_SETTING_HINTS[key])
        self._extra_fields[key] = slider
        form.addRow(label, slider)

    def _add_integer_field(self, form, key, label, low, high, suffix=""):
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setSuffix(suffix)
        spin.setValue(getattr(self._cfg, key))
        spin.setToolTip(CAMERA_SETTING_HINTS[key])
        self._extra_fields[key] = spin
        form.addRow(label, spin)

    @staticmethod
    def _field_with_hint(field: QWidget, text: str) -> QWidget:
        """Держать пояснение рядом с полем, без отдельной строки формы."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(field)
        hint = QLabel(text)
        hint.setWordWrap(True)
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        layout.addWidget(hint)
        return container

    def _build_view_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self._preview_backend_combo = QComboBox()
        self._preview_backend_combo.addItem("Qt Multimedia", "qt")
        self._preview_backend_combo.addItem("PyAV", "ffmpeg")
        self._preview_backend_combo.setToolTip(
            "Если поток не открывается, попробуйте другой способ просмотра. "
            "Изменение применяется после сохранения. Запись и внешний плеер настраиваются отдельно."
        )
        form.addRow("Способ просмотра:", self._preview_backend_combo)

        self._preview_decoding_combo = QComboBox()
        self._preview_decoding_combo.addItem("Аппаратное (GPU)", "gpu")
        self._preview_decoding_combo.addItem("Программное (CPU)", "cpu")
        self._preview_decoding_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._preview_decoding_combo.setMinimumContentsLength(20)
        self._preview_decoding_combo.setToolTip(
            "Изменение декодирования применяется после перезапуска приложения. "
            "При недоступности GPU просмотр продолжится на CPU с уведомлением."
        )
        form.addRow("Декодирование:", self._field_with_hint(
            self._preview_decoding_combo, "Применяется после перезапуска приложения."
        ))

        self._preview_fps_combo = QComboBox()
        for fps in (1, 3, 12, 25):
            self._preview_fps_combo.addItem(f"{fps} кадр/с", fps)
        self._preview_fps_combo.setToolTip(
            "Применяется после сохранения без переподключения камеры. "
            "Если источник медленнее, дополнительные кадры не создаются. "
            "Запись сохраняет исходный поток."
        )
        form.addRow("Частота показа:", self._preview_fps_combo)

        self._preview_resolution_combo = QComboBox()
        for width, height in ((320, 180), (640, 360), (1280, 720), (1920, 1080)):
            self._preview_resolution_combo.addItem(f"{width}×{height}", f"{width}x{height}")
        self._preview_resolution_combo.setToolTip(
            "Максимальный размер кадра просмотра. Фактический размер выбирается отдельно "
            "для каждой плитки с учётом масштаба экрана и пропорций. Исходник не увеличивается."
        )
        form.addRow("Потолок размера:", self._preview_resolution_combo)

        self._preview_transport_combo = QComboBox()
        self._preview_transport_combo.addItem("TCP — надёжная передача", "tcp")
        self._preview_transport_combo.addItem("UDP — возможны потери пакетов", "udp")
        self._preview_transport_combo.setToolTip(
            "Транспорт RTSP для Qt Multimedia. TCP восстанавливает потерянные пакеты. "
            "Qt 6.12 и новее передаёт видео напрямую по выбранному протоколу. "
            "Встроенный просмотр PyAV всегда использует TCP."
        )
        self._preview_backend_combo.currentIndexChanged.connect(
            lambda: self._preview_transport_combo.setEnabled(
                self._preview_backend_combo.currentData() == "qt"
            )
        )
        form.addRow("Передача RTSP:", self._preview_transport_combo)

        self._player_combo = QComboBox()
        self._player_combo.setEditable(True)
        self._player_combo.addItems(PLAYER_OPTIONS)
        self._player_combo.setToolTip(
            "Внешний плеер для открытия RTSP-потока. «Системный» — открыть через ассоциацию ОС."
        )
        player_row = QHBoxLayout()
        player_row.addWidget(self._player_combo, 1)
        browse_player = QPushButton("Обзор…")
        browse_player.clicked.connect(self._browse_player)
        player_row.addWidget(browse_player)
        form.addRow("Плеер:", player_row)

        return self._scroll(w)

    def _build_encoding_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self._record_video_combo = QComboBox()
        self._record_video_combo.addItem("Из потока (без перекодирования)", "copy")
        self._record_video_combo.addItem("H.264 (перекодировать)", "h264")
        self._record_video_combo.addItem("H.265 / HEVC (перекодировать)", "h265")
        self._record_video_combo.setToolTip(
            "Как записывать видео. «Из потока» сохраняет кодек камеры как есть — "
            "минимальная нагрузка на процессор и без потерь качества. H.264 и H.265 "
            "перекодируют поток: файлы обычно меньше, но нужна вычислительная мощность."
        )
        form.addRow("Видео при записи:", self._record_video_combo)
        self._record_encoder_combo = QComboBox()
        self._record_encoder_combo.addItem("Процессор (программный)", "cpu")
        self._record_encoder_combo.addItem("Видеокарта (если доступна)", "gpu")
        self._record_encoder_combo.setToolTip(
            "Какой кодер использовать при перекодировании. Аппаратный кодер "
            "(NVENC, Quick Sync, AMF) разгружает процессор. Он не мешает "
            "аппаратному декодированию просмотра: у видеокарты это отдельные блоки. "
            "У видеокарт есть предел числа одновременных сессий кодирования; камеры "
            "сверх него автоматически пишутся программным кодером."
        )
        form.addRow("Кодер:", self._record_encoder_combo)
        self._record_preset_combo = QComboBox()
        self._record_preset_combo.addItem("Быстрый (меньше нагрузка)", "fast")
        self._record_preset_combo.addItem("Сбалансированный", "balanced")
        self._record_preset_combo.addItem("Качественный (больше нагрузка)", "quality")
        self._record_preset_combo.setToolTip(
            "Скорость кодера. Медленные режимы дают меньший файл при том же "
            "качестве, но сильнее нагружают процессор или видеокарту."
        )
        form.addRow("Режим кодера:", self._record_preset_combo)
        self._record_resolution_combo = QComboBox()
        self._record_resolution_combo.addItem("Как в потоке", "source")
        for label, value in (
            ("4K (3840×2160)", "3840x2160"),
            ("2K (2560×1440)", "2560x1440"),
            ("Full HD (1920×1080)", "1920x1080"),
            ("HD (1280×720)", "1280x720"),
            ("SD (854×480)", "854x480"),
            ("360p (640×360)", "640x360"),
        ):
            self._record_resolution_combo.addItem(label, value)
        self._record_resolution_combo.setToolTip(
            "Потолок размера кадра при перекодировании. Кадр вписывается в "
            "выбранный размер с сохранением пропорций и никогда не увеличивается. "
            "Меньшее разрешение снижает нагрузку и размер файла."
        )
        form.addRow("Разрешение:", self._record_resolution_combo)
        self._add_slider_field(form, "record_crf", "Качество (CRF):", 15, 40)
        self._add_slider_field(
            form,
            "record_video_bitrate",
            "Битрейт видео:",
            0,
            20000,
            " кбит/с",
            "По CRF",
            250,
        )
        self._add_slider_field(
            form, "record_max_fps", "Частота кадров:", 0, 60, " к/с", "Как в потоке"
        )
        self._add_slider_field(
            form, "record_gop", "Ключевой кадр каждые:", 1, 10, " с"
        )
        self._record_audio_combo = QComboBox()
        self._record_audio_combo.addItem("Из потока (G.711 → PCM)", "auto")
        self._record_audio_combo.addItem("AAC", "aac")
        self._record_audio_combo.addItem("Opus", "opus")
        self._record_audio_combo.addItem("FLAC (без потерь)", "flac")
        self._record_audio_combo.addItem("PCM без сжатия", "pcm")
        self._record_audio_combo.addItem("Без звука", "none")
        self._record_audio_combo.setToolTip(
            "Как записывать звук. «Из потока» копирует звук камеры, если его принимает "
            "контейнер MKV; G.711 автоматически преобразуется в PCM. AAC сжимает звук, "
            "PCM сохраняет без потерь, «Без звука» не записывает звуковую дорожку."
        )
        form.addRow("Звук при записи:", self._record_audio_combo)
        self._add_slider_field(
            form,
            "record_audio_bitrate",
            "Битрейт звука:",
            32,
            320,
            " кбит/с",
            step=16,
        )
        self._record_audio_rate_combo = QComboBox()
        self._record_audio_rate_combo.addItem("Как в потоке", 0)
        for rate in (16000, 24000, 32000, 44100, 48000):
            self._record_audio_rate_combo.addItem(f"{rate} Гц", rate)
        self._record_audio_rate_combo.setToolTip(
            "Частота дискретизации звука. Любое значение, кроме «Как в потоке», "
            "заставляет перекодировать звук."
        )
        form.addRow("Частота звука:", self._record_audio_rate_combo)
        self._record_audio_channels_combo = QComboBox()
        self._record_audio_channels_combo.addItem("Как в потоке", 0)
        self._record_audio_channels_combo.addItem("Моно", 1)
        self._record_audio_channels_combo.addItem("Стерео", 2)
        self._record_audio_channels_combo.setToolTip(
            "Число каналов звука. Любое значение, кроме «Как в потоке», "
            "заставляет перекодировать звук."
        )
        form.addRow("Каналы звука:", self._record_audio_channels_combo)

        def _sync_record_video() -> None:
            transcode = self._record_video_combo.currentData() != "copy"
            for widget in (
                self._record_encoder_combo,
                self._record_preset_combo,
                self._record_resolution_combo,
                self._extra_fields["record_crf"],
                self._extra_fields["record_video_bitrate"],
                self._extra_fields["record_max_fps"],
                self._extra_fields["record_gop"],
            ):
                widget.setEnabled(transcode)
            self._extra_fields["record_crf"].setEnabled(
                transcode and self._extra_fields["record_video_bitrate"].value() == 0
            )

        def _sync_record_audio() -> None:
            mode = self._record_audio_combo.currentData()
            self._extra_fields["record_audio_bitrate"].setEnabled(
                mode in ("aac", "opus")
            )
            enabled = mode != "none"
            self._record_audio_rate_combo.setEnabled(enabled)
            self._record_audio_channels_combo.setEnabled(enabled)

        self._record_video_combo.currentIndexChanged.connect(
            lambda _index: _sync_record_video()
        )
        self._extra_fields["record_video_bitrate"]._slider.valueChanged.connect(
            lambda _value: _sync_record_video()
        )
        self._record_audio_combo.currentIndexChanged.connect(
            lambda _index: _sync_record_audio()
        )
        self._sync_record_audio = _sync_record_audio
        self._sync_record_video = _sync_record_video
        return self._scroll(w)

    def _build_record_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        out_row = QHBoxLayout()
        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText("Пусто = домашняя папка (можно %токены%)")
        self._output_edit.setToolTip(OUTPUT_HINT)
        out_row.addWidget(self._output_edit, 1)
        self._btn_browse = QPushButton("Обзор…")
        self._btn_browse.clicked.connect(self._browse_output)
        out_row.addWidget(self._btn_browse)
        out_w = QWidget()
        out_w.setLayout(out_row)
        form.addRow("Каталог записи:", out_w)

        self._path_mask_edit = QLineEdit()
        self._path_mask_edit.setToolTip(PATH_MASK_HINT)
        self._path_mask_edit.setPlaceholderText("%addr%/%year%/%month%/%day%/%cabinet%")
        form.addRow("Шаблон пути:", self._path_mask_edit)

        self._file_mask_edit = QLineEdit()
        self._file_mask_edit.setToolTip(FILE_MASK_HINT)
        self._file_mask_edit.setPlaceholderText("%camera%_%Y_%m_%d__%H_%M_%S")
        form.addRow("Шаблон имени файла:", self._file_mask_edit)

        self._max_hours_spin = QDoubleSpinBox()
        self._max_hours_spin.setRange(0.0, 720.0)
        self._max_hours_spin.setDecimals(1)
        self._max_hours_spin.setSingleStep(0.5)
        self._max_hours_spin.setSuffix(" ч")
        self._max_hours_spin.setSpecialValueText("без лимита")
        self._max_hours_spin.setToolTip(
            "Общая длительность записи одной камеры с момента запуска, в часах, "
            "включая ожидание переподключений. Смена файла не сбрасывает отсчёт. "
            "После лимита запись останавливается. 0 = без ограничения длительности."
        )
        form.addRow("Макс. длительность записи:", self._max_hours_spin)

        self._min_free_spin = QDoubleSpinBox()
        self._min_free_spin.setRange(0.0, 100.0)
        self._min_free_spin.setSingleStep(0.5)
        self._min_free_spin.setSuffix(" ГиБ")
        self._min_free_spin.setToolTip(
            "Минимум свободного места на диске назначения перед началом записи, "
            "в ГиБ (1 ГиБ = 1 073 741 824 байт). Если места меньше, запись не начнётся. "
            "0 отключает эту проверку; аварийный порог настраивается отдельно."
        )
        form.addRow("Мин. свободно на диске:", self._min_free_spin)
        self._emergency_free_spin = QDoubleSpinBox()
        self._emergency_free_spin.setRange(0, 100000)
        self._emergency_free_spin.setDecimals(2)
        self._emergency_free_spin.setSuffix(" ГиБ")
        self._emergency_free_spin.setValue(self._cfg.emergency_free_gb)
        self._emergency_free_spin.setToolTip(
            "Остановить текущую запись, если свободное место на диске назначения "
            "опустится ниже этого значения в ГиБ. Проверяется во время записи, "
            "чтобы не заполнить диск. 0 отключает аварийную остановку."
        )
        form.addRow("Остановить, если свободно менее:", self._emergency_free_spin)

        self._min_free_spin.setRange(0, 100000)
        self._min_free_spin.setSuffix(" ГиБ")
        for key, label, low, high, suffix in (
            ("max_simultaneous", "Одновременных записей:", 1, 256, ""),
            (
                "segment_minutes",
                "Длительность части (0 = без лимита):",
                0,
                1440,
                " мин",
            ),
            ("segment_mb", "Размер части (0 = без лимита):", 0, 1048576, " МиБ"),
        ):
            self._add_integer_field(form, key, label, low, high, suffix)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        self._preview_label = QLabel("")
        self._preview_label.setWordWrap(True)
        self._preview_label.setMinimumWidth(0)
        self._preview_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._preview_label.setStyleSheet("padding-top: 6px;")
        form.addRow("Превью:", self._preview_label)

        self._output_edit.textChanged.connect(self._update_preview)
        self._path_mask_edit.textChanged.connect(self._update_preview)
        self._file_mask_edit.textChanged.connect(self._update_preview)
        return self._scroll(w)

    def _build_cameras_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._cam_tree = _CamerasTree()
        self._cam_tree.setColumnCount(3)
        self._cam_tree.setHeaderLabels(["Адрес", "Камера", "Адрес RTSP"])
        self._cam_tree.headerItem().setToolTip(
            COL_ADDR,
            "Группировка камер: адрес объекта, внутри него — кабинет. Это названия для организации списка и подстановки в путь записи.",
        )
        self._cam_tree.headerItem().setToolTip(
            COL_CAM,
            "Название камеры. Используется в списке, заголовке плеера и шаблонах файлов записи.",
        )
        self._cam_tree.headerItem().setToolTip(
            COL_IP,
            "IP или имя камеры, необязательные порт и путь. Например: "
            "192.0.2.1/onvif1 или camera.example:8554/onvif/profile1?channel=1. "
            "Можно указать rtsp:// или rtsps://. Без порта используется 554. "
            "Логин и пароль задаются отдельно на вкладке «Подключение». "
            "Путь /onvif зависит от модели камеры; автоматического поиска ONVIF нет.",
        )
        header = self._cam_tree.header()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(COL_ADDR, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_CAM, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_IP, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self._cam_tree.setColumnWidth(COL_ADDR, 260)
        self._cam_tree.setColumnWidth(COL_CAM, 140)
        self._cam_tree.setColumnWidth(COL_IP, 150)
        self._cam_tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self._cam_tree.setEditTriggers(
            QTreeWidget.EditTrigger.DoubleClicked
            | QTreeWidget.EditTrigger.EditKeyPressed
            | QTreeWidget.EditTrigger.AnyKeyPressed
        )
        self._cam_tree.itemChanged.connect(self._on_camera_item_changed)
        layout.addWidget(self._cam_tree, 1)

        btn_row = QHBoxLayout()
        self._btn_add_addr = QPushButton("Добавить адрес")
        self._btn_add_cab = QPushButton("Добавить кабинет")
        self._btn_add_cam = QPushButton("Добавить камеру")
        self._btn_scanner = QPushButton("Сканер…")
        self._btn_remove = QPushButton("Удалить")
        self._btn_add_addr.clicked.connect(self._add_address)
        self._btn_add_cab.clicked.connect(self._add_cabinet)
        self._btn_add_cam.clicked.connect(self._add_camera)
        self._btn_scanner.clicked.connect(self._open_scanner)
        self._btn_remove.clicked.connect(self._remove_selected)
        btn_row.addWidget(self._btn_add_addr)
        btn_row.addWidget(self._btn_add_cab)
        btn_row.addWidget(self._btn_add_cam)
        btn_row.addWidget(self._btn_scanner)
        btn_row.addStretch(1)
        btn_row.addWidget(self._btn_remove)
        layout.addLayout(btn_row)

        hint = QLabel(
            "Двойной клик по ячейке — редактировать.<br>"
            "Перетаскивайте узлы мышью для перемещения камер и кабинетов.<br>"
            "Адрес RTSP: IP[:порт][/путь], например 192.0.2.1/onvif1."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return w

    # ---------- Загрузка ----------

    def _load_data(self) -> None:
        self._login_edit.setText(self._cfg.login or "")
        self._password_edit.setText(self._cfg.password or "")
        self._timeout_spin.setValue(int(self._cfg.timeout or 2))
        self._ping_interval_spin.setValue(
            int(self._cfg.ping_interval or DEFAULT_PING_INTERVAL)
        )
        self._output_edit.setText(self._cfg.output or "")
        self._player_combo.setCurrentText(self._cfg.player or "vlc")
        self._preview_backend_combo.setCurrentIndex(
            self._preview_backend_combo.findData(self._cfg.preview_backend)
        )
        self._preview_transport_combo.setCurrentIndex(
            self._preview_transport_combo.findData(self._cfg.preview_transport)
        )
        self._preview_transport_combo.setEnabled(self._cfg.preview_backend == "qt")
        for combo, value in (
            (self._preview_decoding_combo, self._cfg.preview_decoding),
            (self._preview_fps_combo, self._cfg.preview_fps),
            (self._preview_resolution_combo, self._cfg.preview_resolution),
            (self._record_video_combo, self._cfg.record_video),
            (self._record_encoder_combo, self._cfg.record_encoder),
            (self._record_audio_combo, self._cfg.record_audio),
            (self._record_preset_combo, self._cfg.record_preset),
            (self._record_resolution_combo, self._cfg.record_resolution),
            (self._record_audio_rate_combo, self._cfg.record_audio_rate),
            (self._record_audio_channels_combo, self._cfg.record_audio_channels),
        ):
            combo.setCurrentIndex(combo.findData(value))
        self._sync_record_video()
        self._sync_record_audio()
        self._path_mask_edit.setText(self._cfg.path_mask or "")
        self._file_mask_edit.setText(self._cfg.file_mask or "")
        try:
            self._max_hours_spin.setValue(float(self._cfg.max_record_hours or 0.0))
        except (TypeError, ValueError):
            self._max_hours_spin.setValue(0.0)
        try:
            self._min_free_spin.setValue(float(self._cfg.min_free_gb or 0.0))
        except (TypeError, ValueError):
            self._min_free_spin.setValue(0.0)
        self._build_cameras_tree()
        self._update_preview()

    def _build_cameras_tree(self) -> None:
        self._suppress_item_changed = True
        self._cam_tree.clear()
        for address, cabinets in (self._cfg.cameras or {}).items():
            addr_item = QTreeWidgetItem(self._cam_tree)
            addr_item.setText(COL_ADDR, address)
            addr_item.setIcon(COL_ADDR, _icon_from_key("folder"))
            addr_item.setData(0, Qt.ItemDataRole.UserRole, ("addr", address))
            addr_item.setFlags(addr_item.flags() | Qt.ItemFlag.ItemIsEditable)
            for cabinet, cams in (cabinets or {}).items():
                cab_item = QTreeWidgetItem(addr_item)
                cab_item.setText(COL_ADDR, cabinet)
                cab_item.setIcon(COL_ADDR, _icon_from_key("menu_folder"))
                cab_item.setData(0, Qt.ItemDataRole.UserRole, ("cab", address, cabinet))
                cab_item.setFlags(cab_item.flags() | Qt.ItemFlag.ItemIsEditable)
                for camera_name, ip in (cams or {}).items():
                    cam_item = QTreeWidgetItem(cab_item)
                    cam_item.setText(COL_CAM, camera_name)
                    cam_item.setText(COL_IP, ip)
                    key = f"{address}::{cabinet}::{camera_name}"
                    cam_item.setData(
                        0,
                        Qt.ItemDataRole.UserRole + 1,
                        self._cfg.camera_ids.get(key) or uuid4().hex,
                    )
                    cam_item.setIcon(COL_CAM, _icon_from_key("camera"))
                    cam_item.setData(
                        0,
                        Qt.ItemDataRole.UserRole,
                        ("cam", address, cabinet, camera_name),
                    )
                    cam_item.setFlags(cam_item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._cam_tree.expandAll()
        self._suppress_item_changed = False

    # ---------- Превью ----------

    def _update_preview(self) -> None:
        now = datetime.datetime.now()
        cam = CameraModel(
            address="Адрес",
            cabinet="Кабинет",
            camera_name="Камера",
            ip_address="0.0.0.0",
            output_path=self._output_edit.text().strip(),
            path_mask=self._path_mask_edit.text().strip(),
            file_mask=self._file_mask_edit.text().strip(),
        )
        try:
            full = cam.record_dir(now) / f"{cam.file_stem(now)}_….mkv"
            self._preview_label.setText(str(full))
        except Exception:
            self._preview_label.setText("<ошибка шаблона>")

    # ---------- Browse ----------

    def _browse_output(self) -> None:
        cur = self._output_edit.text() or ""
        path = QFileDialog.getExistingDirectory(self, "Каталог для записи", cur)
        if path:
            self._output_edit.setText(path)

    def _browse_player(self):
        path, _ = QFileDialog.getOpenFileName(self, "Исполняемый файл плеера")
        if path:
            self._player_combo.setCurrentText(path)

    def _test_connection(self):
        """Проверить текущие поля подключения без сохранения настроек."""
        from src.workers.camera_workers import CameraProbeWorker

        item = self._cam_tree.currentItem()
        if item is None or _item_kind(item) != "cam":
            self._test_result.setText("Выберите камеру на вкладке «Камеры»")
            return
        if self._probe is not None:
            return
        host = item.text(COL_IP).strip()
        camera = CameraModel(
            "",
            "",
            item.text(COL_CAM),
            host,
            self._login_edit.text().strip(),
            self._password_edit.text(),
        )
        try:
            _ = camera.rtsp_url
        except ValueError as error:
            self._test_result.setText(str(error))
            return
        self._probe = CameraProbeWorker(
            camera, self._extra_fields["network_timeout"].value(), self
        )
        self._probe.result.connect(lambda ok, text: self._test_result.setText(text))
        self._probe.finished.connect(self._probe_finished)
        self._test_button.setEnabled(False)
        self._test_result.setText("Проверка получения кадра…")
        self._probe.start()

    def _probe_finished(self):
        thread = self._probe
        self._probe = None
        thread.deleteLater()
        self._test_button.setEnabled(True)
        if self._pending_result is not None:
            result, self._pending_result = self._pending_result, None
            self.done(result)

    def has_pending_workers(self):
        return self._probe is not None or self._hardware_info is not None

    def done(self, result):
        if self.has_pending_workers():
            self._pending_result = result
            if self._probe is not None:
                self._probe.requestInterruption()
            return
        super().done(result)

    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def accept(self):
        self.done(QDialog.DialogCode.Accepted)

    def closeEvent(self, event):
        if self.has_pending_workers():
            self.reject()
            event.ignore()
            return
        super().closeEvent(event)

    # ---------- Правка дерева камер ----------

    def _add_address(self) -> None:
        # Автоинкремент имени: адреса хранятся по имени-ключу, дубликат
        # перезаписал бы существующий адрес вместе с кабинетами.
        name = f"Адрес {self._next_address_index()}"
        self._suppress_item_changed = True
        item = QTreeWidgetItem(self._cam_tree)
        item.setText(COL_ADDR, name)
        item.setIcon(COL_ADDR, _icon_from_key("folder"))
        item.setData(0, Qt.ItemDataRole.UserRole, ("addr", name))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._cam_tree.expandItem(item)
        self._cam_tree.setCurrentItem(item)
        self._cam_tree.editItem(item, COL_ADDR)
        self._suppress_item_changed = False

    def _add_cabinet(self) -> None:
        parent = self._selected_address_item()
        if parent is None:
            QMessageBox.information(self, "Кабинет", "Сначала выберите адрес.")
            return
        # Автоинкремент имени: кабинеты хранятся по имени-ключу внутри адреса,
        # дубликат перезаписал бы существующий кабинет вместе с камерами.
        name = f"Кабинет {self._next_cabinet_index(parent)}"
        self._suppress_item_changed = True
        item = QTreeWidgetItem(parent)
        item.setText(COL_ADDR, name)
        item.setIcon(COL_ADDR, _icon_from_key("menu_folder"))
        item.setData(0, Qt.ItemDataRole.UserRole, ("cab", parent.text(COL_ADDR), name))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._cam_tree.expandItem(parent)
        self._cam_tree.setCurrentItem(item)
        self._cam_tree.editItem(item, COL_ADDR)
        self._suppress_item_changed = False

    def _add_camera(self) -> None:
        parent = self._selected_cabinet_item()
        if parent is None:
            QMessageBox.information(self, "Камера", "Сначала выберите кабинет.")
            return
        # Автоинкремент имени: камеры хранятся по имени-ключу, дубликат
        # («Камера 1» при уже существующей) перезаписал бы запись.
        name = f"Камера {self._next_camera_index(parent)}"
        addr_name = parent.parent().text(COL_ADDR) if parent.parent() else ""
        cab_name = parent.text(COL_ADDR)
        self._suppress_item_changed = True
        item = QTreeWidgetItem(parent)
        item.setText(COL_CAM, name)
        item.setText(COL_IP, "0.0.0.0")
        item.setIcon(COL_CAM, _icon_from_key("camera"))
        item.setData(
            0,
            Qt.ItemDataRole.UserRole,
            ("cam", addr_name, cab_name, name),
        )
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._cam_tree.expandItem(parent)
        self._cam_tree.setCurrentItem(item)
        self._cam_tree.editItem(item, COL_CAM)
        self._suppress_item_changed = False

    def _remove_selected(self) -> None:
        item = self._cam_tree.currentItem()
        if item is None:
            return
        parent = item.parent() or self._cam_tree.invisibleRootItem()
        parent.removeChild(item)

    # ---------- IP-сканер ----------

    def _open_scanner(self) -> None:
        try:
            from src.ui.dialogs.tools.ip_scanner import IPScannerDialog

            dlg = IPScannerDialog(self)
            dlg.exported_device.connect(self._on_scanner_import)
            dlg.exec()
        except Exception as e:
            logger.exception("camera_settings: scanner open failed: %s", e)
            QMessageBox.critical(self, "Сканер", f"Не удалось открыть сканер: {e}")

    def _on_scanner_import(self, devices, folder) -> None:
        """Добавить найденные сканером устройства как камеры в выбранный адрес/кабинет.

        Логика:
        - Если выбран кабинет — камеры попадают в него.
        - Иначе если выбран адрес — создаётся «Кабинет N» внутри него.
        - Иначе создаются «Адрес N» и «Кабинет N» внутри него.
        Имена камер — «Камера N» (с автоинкрементом внутри кабинета), IP = device.host.
        """
        if not devices:
            return

        addr_item, cab_item = self._ensure_scanner_target()
        if addr_item is None:
            return
        if cab_item is None:
            cab_item = self._create_cabinet(addr_item)

        self._suppress_item_changed = True
        try:
            base = self._next_camera_index(cab_item)
            for i, dev in enumerate(devices, start=base):
                cam_item = QTreeWidgetItem(cab_item)
                name = f"Камера {i}"
                cam_item.setText(COL_CAM, name)
                cam_item.setText(COL_IP, getattr(dev, "host", "") or "")
                cam_item.setIcon(COL_CAM, _icon_from_key("camera"))
                addr_name = addr_item.text(COL_ADDR)
                cab_name = cab_item.text(COL_ADDR)
                cam_item.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    ("cam", addr_name, cab_name, name),
                )
            self._cam_tree.expandItem(addr_item)
            self._cam_tree.expandItem(cab_item)
        finally:
            self._suppress_item_changed = False

    def _ensure_scanner_target(self):
        """Вернуть (addr_item, cab_item) для размещения камер из сканера.

        Если выбран кабинет — он же. Если выбран адрес — создаёт кабинет в нём.
        Если ничего не выбрано — создаёт «Адрес N» и кабинет в нём.
        """
        addr_item = self._selected_address_item()
        if addr_item is not None:
            cab_item = self._selected_cabinet_item()
            if cab_item is not None:
                return addr_item, cab_item
            return addr_item, None
        return self._create_address_with_cabinet(), None

    def _create_address_with_cabinet(self) -> QTreeWidgetItem:
        addr_name = f"Адрес {self._next_address_index()}"
        addr_item = QTreeWidgetItem(self._cam_tree)
        addr_item.setText(COL_ADDR, addr_name)
        addr_item.setIcon(COL_ADDR, _icon_from_key("folder"))
        addr_item.setData(0, Qt.ItemDataRole.UserRole, ("addr", addr_name))
        return addr_item

    def _create_cabinet(self, addr_item: QTreeWidgetItem) -> QTreeWidgetItem:
        cab_name = f"Кабинет {self._next_cabinet_index(addr_item)}"
        cab_item = QTreeWidgetItem(addr_item)
        cab_item.setText(COL_ADDR, cab_name)
        cab_item.setIcon(COL_ADDR, _icon_from_key("menu_folder"))
        addr_name = addr_item.text(COL_ADDR)
        cab_item.setData(0, Qt.ItemDataRole.UserRole, ("cab", addr_name, cab_name))
        return cab_item

    def _next_address_index(self) -> int:
        """Максимальный N по существующим «Адрес N» + 1 (единый счётчик
        ручного добавления и импорта сканера; strip — см. _next_camera_index)."""
        existing = 0
        root = self._cam_tree.invisibleRootItem()
        for i in range(root.childCount()):
            name = (root.child(i).text(COL_ADDR) or "").strip()
            m = _NUMBERED_NAME_RE.fullmatch(name)
            if m and m.group(1) == "Адрес":
                existing = max(existing, int(m.group(2)))
        return existing + 1

    def _next_cabinet_index(self, addr_item: QTreeWidgetItem) -> int:
        """Максимальный N по существующим «Кабинет N» внутри адреса + 1."""
        existing = 0
        for i in range(addr_item.childCount()):
            name = (addr_item.child(i).text(COL_ADDR) or "").strip()
            m = _NUMBERED_NAME_RE.fullmatch(name)
            if m and m.group(1) == "Кабинет":
                existing = max(existing, int(m.group(2)))
        return existing + 1

    def _next_camera_index(self, cab_item: QTreeWidgetItem) -> int:
        existing = 0
        for i in range(cab_item.childCount()):
            # strip(): _collect_cameras сохраняет имена после strip,
            # непарсимое имя («Камера 1 » с хвостом) вернуло бы 1 → дубликат.
            name = (cab_item.child(i).text(COL_CAM) or "").strip()
            if name.startswith("Камера "):
                try:
                    num = int(name.split(maxsplit=1)[1])
                    existing = max(existing, num)
                except (ValueError, IndexError):
                    pass
        return existing + 1

    def _selected_address_item(self) -> Optional[QTreeWidgetItem]:
        item = self._cam_tree.currentItem()
        if item is None:
            return None
        while item.parent() is not None:
            item = item.parent()
        return item

    def _selected_cabinet_item(self) -> Optional[QTreeWidgetItem]:
        item = self._cam_tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and data and data[0] == "cab":
            return item
        if (
            isinstance(data, tuple)
            and data
            and data[0] == "cam"
            and item.parent() is not None
        ):
            return item.parent()
        return None

    def _on_camera_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._suppress_item_changed:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(data, tuple) or not data:
            return
        kind = data[0]
        # Блокируем рекурсию: setData(UserRole) ниже снова триггерит itemChanged.
        self._suppress_item_changed = True
        try:
            if kind == "addr":
                new_addr = item.text(COL_ADDR)
                item.setData(0, Qt.ItemDataRole.UserRole, ("addr", new_addr))
                for i in range(item.childCount()):
                    cab = item.child(i)
                    cab_data = cab.data(0, Qt.ItemDataRole.UserRole)
                    if isinstance(cab_data, tuple) and cab_data[0] == "cab":
                        cab.setData(
                            0, Qt.ItemDataRole.UserRole, ("cab", new_addr, cab_data[2])
                        )
                        for j in range(cab.childCount()):
                            cam = cab.child(j)
                            cam_data = cam.data(0, Qt.ItemDataRole.UserRole)
                            if isinstance(cam_data, tuple) and cam_data[0] == "cam":
                                cam.setData(
                                    0,
                                    Qt.ItemDataRole.UserRole,
                                    ("cam", new_addr, cab_data[2], cam_data[3]),
                                )
            elif kind == "cab":
                new_cab = item.text(COL_ADDR)
                addr = data[1]
                item.setData(0, Qt.ItemDataRole.UserRole, ("cab", addr, new_cab))
                for j in range(item.childCount()):
                    cam = item.child(j)
                    cam_data = cam.data(0, Qt.ItemDataRole.UserRole)
                    if isinstance(cam_data, tuple) and cam_data[0] == "cam":
                        cam.setData(
                            0,
                            Qt.ItemDataRole.UserRole,
                            ("cam", addr, new_cab, cam_data[3]),
                        )
            elif kind == "cam":
                addr = data[1]
                cab = data[2]
                new_cam = item.text(COL_CAM)
                item.setData(0, Qt.ItemDataRole.UserRole, ("cam", addr, cab, new_cam))
        finally:
            self._suppress_item_changed = False

    # ---------- Сохранение ----------

    def _collect_cameras(self) -> dict:
        cameras: dict = {}
        self._collected_ids = {}
        root = self._cam_tree.invisibleRootItem()
        for i in range(root.childCount()):
            addr_item = root.child(i)
            addr = addr_item.text(COL_ADDR).strip()
            if not addr:
                raise ValueError("Укажите имя адреса")
            if addr in cameras:
                raise ValueError(f"Адрес «{addr}» повторяется")
            cameras[addr] = {}
            cabinets: dict = {}
            for j in range(addr_item.childCount()):
                cab_item = addr_item.child(j)
                cab = cab_item.text(COL_ADDR).strip()
                if not cab:
                    raise ValueError(f"Укажите кабинет в адресе «{addr}»")
                if cab in cabinets:
                    raise ValueError(f"Кабинет «{addr} / {cab}» повторяется")
                cabinets[cab] = {}
                cams: dict = {}
                for k in range(cab_item.childCount()):
                    cam_item = cab_item.child(k)
                    name = cam_item.text(COL_CAM).strip()
                    ip = cam_item.text(COL_IP).strip()
                    if not name or not ip:
                        raise ValueError("У каждой камеры должны быть имя и адрес RTSP")
                    if name in cams:
                        raise ValueError(
                            f"Камера «{addr} / {cab} / {name}» повторяется"
                        )
                    iid = cam_item.data(0, Qt.ItemDataRole.UserRole + 1) or uuid4().hex
                    cam_item.setData(0, Qt.ItemDataRole.UserRole + 1, iid)
                    self._collected_ids[f"{addr}::{cab}::{name}"] = iid
                    cams[name] = ip
                if cams:
                    cabinets[cab] = cams
            if cabinets:
                cameras[addr] = cabinets
        return cameras

    def _on_accept(self) -> None:
        try:
            cameras = self._collect_cameras()
        except ValueError as exc:
            QMessageBox.warning(self, "Настройки камер", str(exc))
            return
        if not cameras:
            ok = QMessageBox.question(
                self,
                "Сохранение",
                "Список камер пуст. Сохранить настройки без камер?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ok != QMessageBox.Yes:
                return
        seen = set()
        for addr, cabs in cameras.items():
            for cab, cams in cabs.items():
                for name, ip in cams.items():
                    cid = f"{addr}::{cab}::{name}"
                    if cid in seen:
                        QMessageBox.warning(
                            self,
                            "Дубликат",
                            f"Камера «{cid}» повторяется. Имена должны быть уникальны.",
                        )
                        return
                    seen.add(cid)
        cfg = CameraConfig(
            login=self._login_edit.text().strip(),
            password=self._password_edit.text(),
            player=self._player_combo.currentText().strip(),
            preview_backend=self._preview_backend_combo.currentData(),
            preview_transport=self._preview_transport_combo.currentData(),
            preview_decoding=self._preview_decoding_combo.currentData(),
            preview_fps=self._preview_fps_combo.currentData(),
            preview_resolution=self._preview_resolution_combo.currentData(),
            record_video=self._record_video_combo.currentData(),
            record_encoder=self._record_encoder_combo.currentData(),
            record_audio=self._record_audio_combo.currentData(),
            record_preset=self._record_preset_combo.currentData(),
            record_resolution=self._record_resolution_combo.currentData(),
            record_audio_rate=self._record_audio_rate_combo.currentData(),
            record_audio_channels=self._record_audio_channels_combo.currentData(),
            timeout=int(self._timeout_spin.value()),
            ping_interval=int(self._ping_interval_spin.value()),
            output=self._output_edit.text().strip(),
            path_mask=self._path_mask_edit.text().strip(),
            file_mask=self._file_mask_edit.text().strip(),
            max_record_hours=float(self._max_hours_spin.value()),
            min_free_gb=float(self._min_free_spin.value()),
            cameras=cameras,
            camera_ids=self._collected_ids,
            emergency_free_gb=self._emergency_free_spin.value(),
            **{key: spin.value() for key, spin in self._extra_fields.items()},
        )
        try:
            cfg = normalize_camera_config(asdict(cfg))
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Настройки камер", str(exc))
            return
        if not self._svc.update_settings(cfg):
            QMessageBox.critical(
                self, "Ошибка", "Не удалось сохранить настройки камер."
            )
            return
        self.accept()


def _deep_copy_cameras(cams: dict) -> dict:
    out: dict = {}
    for addr, cabs in (cams or {}).items():
        out[addr] = {}
        for cab, items in (cabs or {}).items():
            out[addr][cab] = dict(items or {})
    return out


def _item_kind(item):
    if item is None:
        return None
    data = item.data(0, Qt.ItemDataRole.UserRole)
    if isinstance(data, tuple) and data:
        return data[0]
    return None


class _CamerasTree(QTreeWidget):
    """QTreeWidget с поддержкой internal drag'n'drop.

    Правила перемещения:
    - Адрес (top-level) → только между top-level узлами.
    - Кабинет → только внутри Адреса (любого).
    - Камера → только внутри Кабинета (любого).

    Запрещено бросать узел в его же потомка (цикл).
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QTreeWidget.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)

    def mouseDoubleClickEvent(self, event) -> None:
        """При двойном клике редактировать колонку с текстом узла.

        Адрес и кабинет → колонка COL_ADDR (текст там). Камера →
        редактируется та колонка, по которой кликнули (COL_CAM или COL_IP).
        """
        item = self.itemAt(event.pos())
        if item is None:
            super().mouseDoubleClickEvent(event)
            return
        kind = _item_kind(item)
        if kind in ("addr", "cab"):
            self.editItem(item, COL_ADDR)
            event.accept()
            return
        if kind == "cam":
            col = self.columnAt(event.pos().x())
            if col in (COL_CAM, COL_IP):
                self.editItem(item, col)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def dropEvent(self, event) -> None:
        src = self.currentItem()
        if src is None:
            event.ignore()
            return
        src_kind = _item_kind(src)
        if src_kind is None:
            event.ignore()
            return

        target = self.itemAt(
            event.position().toPoint() if hasattr(event, "position") else event.pos()
        )
        if target is None:
            if src_kind == "addr":
                super().dropEvent(event)
            else:
                event.ignore()
            return

        if target is src:
            event.ignore()
            return

        if _is_descendant(src, target):
            event.ignore()
            return

        tgt_kind = _item_kind(target)
        if src_kind == "addr":
            parent = target.parent()
            if parent is not None:
                event.ignore()
                return
            parent = self.invisibleRootItem()
            index = parent.indexOfChild(target)
        elif src_kind == "cab":
            if tgt_kind == "addr":
                target_root = target
            elif tgt_kind == "cab":
                target_root = target.parent()
            elif tgt_kind == "cam":
                target_root = (
                    target.parent().parent() if target.parent() is not None else None
                )
            else:
                target_root = None
            if target_root is None:
                event.ignore()
                return
            parent = target_root
            index = (
                parent.indexOfChild(target)
                if tgt_kind == "cab"
                else parent.childCount()
            )
        elif src_kind == "cam":
            if tgt_kind == "cab":
                target_cab = target
            elif tgt_kind == "cam":
                target_cab = target.parent()
            else:
                target_cab = None
            if target_cab is None:
                event.ignore()
                return
            parent = target_cab
            index = (
                parent.indexOfChild(target)
                if tgt_kind == "cam"
                else parent.childCount()
            )
        else:
            event.ignore()
            return
        # Перемещаем узел сами: стандартный drop может вложить камеру в камеру.
        old_parent = src.parent() or self.invisibleRootItem()
        old_index = old_parent.indexOfChild(src)
        blocked = self.blockSignals(True)
        try:
            old_parent.takeChild(old_index)
            if old_parent is parent and old_index < index:
                index -= 1
            parent.insertChild(max(0, index), src)
            root = self.invisibleRootItem()
            for a in range(root.childCount()):
                addr = root.child(a)
                addr_name = addr.text(COL_ADDR)
                addr.setData(0, Qt.ItemDataRole.UserRole, ("addr", addr_name))
                for b in range(addr.childCount()):
                    cab = addr.child(b)
                    cab_name = cab.text(COL_ADDR)
                    cab.setData(
                        0, Qt.ItemDataRole.UserRole, ("cab", addr_name, cab_name)
                    )
                    for c in range(cab.childCount()):
                        cam = cab.child(c)
                        cam.setData(
                            0,
                            Qt.ItemDataRole.UserRole,
                            ("cam", addr_name, cab_name, cam.text(COL_CAM)),
                        )
            parent.setExpanded(True)
            self.setCurrentItem(src)
        finally:
            self.blockSignals(blocked)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()


def _is_descendant(maybe_parent, maybe_child) -> bool:
    node = maybe_child
    while node is not None:
        if node is maybe_parent:
            return True
        node = node.parent()
    return False


def _reparent_cab(cab_item: QTreeWidgetItem, new_addr: QTreeWidgetItem) -> None:
    old_addr = cab_item.parent()
    if old_addr is new_addr:
        return
    addr_name = new_addr.text(COL_ADDR)
    data = cab_item.data(0, Qt.ItemDataRole.UserRole)
    if isinstance(data, tuple) and data[0] == "cab":
        cab_item.setData(0, Qt.ItemDataRole.UserRole, ("cab", addr_name, data[2]))
        for i in range(cab_item.childCount()):
            cam = cab_item.child(i)
            cam_data = cam.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(cam_data, tuple) and cam_data[0] == "cam":
                cam.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    ("cam", addr_name, cam_data[2], cam_data[3]),
                )


def _reparent_cam(cam_item: QTreeWidgetItem, new_cab: QTreeWidgetItem) -> None:
    old_cab = cam_item.parent()
    if old_cab is new_cab:
        return
    addr_name = new_cab.parent().text(COL_ADDR) if new_cab.parent() is not None else ""
    cab_name = new_cab.text(COL_ADDR)
    data = cam_item.data(0, Qt.ItemDataRole.UserRole)
    if isinstance(data, tuple) and data[0] == "cam":
        cam_item.setData(
            0,
            Qt.ItemDataRole.UserRole,
            ("cam", addr_name, cab_name, data[3]),
        )
