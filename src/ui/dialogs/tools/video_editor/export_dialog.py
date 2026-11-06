"""Диалог экспорта: Copy без перекодирования или re-encode с настройками."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar, cast

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Slot
from PySide6.QtGui import QCloseEvent, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.video.render_plan import RenderPlan
from src.workers.video_editor.capabilities import (
    VIDEO_ENCODERS,
    CapabilitySnapshot,
    CapabilityWorker,
)
from src.workers.video_editor.copy_preflight import CopyPreflightWorker
from src.workers.video_editor.metadata import ProbeMetadata


class ExportDialog(QDialog):
    VIDEO_CODECS = VIDEO_ENCODERS
    ENCODER_TITLES: ClassVar[dict[str, str]] = {
        "libx264": "Процессор — H.264",
        "libx265": "Процессор — H.265 / HEVC",
        "h264_nvenc": "Видеокарта NVIDIA — H.264 / NVENC",
        "hevc_nvenc": "Видеокарта NVIDIA — H.265 / NVENC",
        "h264_qsv": "Видеокарта Intel — H.264 / QSV",
        "hevc_qsv": "Видеокарта Intel — H.265 / QSV",
        "h264_amf": "Видеокарта AMD — H.264 / AMF",
        "hevc_amf": "Видеокарта AMD — H.265 / AMF",
        "h264_vaapi": "Видеокарта Linux — H.264 / VAAPI",
        "hevc_vaapi": "Видеокарта Linux — H.265 / VAAPI",
    }

    def __init__(
        self,
        parent: QWidget | None = None,
        has_text: bool = False,
        compat_ok: bool = True,
        compat_msg: str = "",
        copy_sources: tuple[str, ...] = (),
        duration: float = 0.0,
        mark_range: tuple[float, float] | None = None,
        clip_range: tuple[float, float] | None = None,
        position: float = 0.0,
        audio_project: bool = False,
        profile_index: int | None = None,
        plan: RenderPlan | None = None,
    ) -> None:
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Экспорт")
        self.setModal(True)
        self._compat_ok = compat_ok
        self._compat_msg = compat_msg
        self._copy_sources = copy_sources
        self._copy_worker: CopyPreflightWorker | None = None
        self._copy_verified: set[str] = set()
        self._overwrite = False
        self._capability_worker: CapabilityWorker | None = None
        self._capabilities: CapabilitySnapshot | None = None
        self._verified_codecs: set[str] = set()
        self._preferred_codec = "libx264"
        self._encoder_errors: dict[str, str] = {}
        self._availability_complete = False
        self._restart_availability = False
        self._finish_availability_for_export = False
        self._pending_accept = False
        self._pending_close = False
        self._audio_project = audio_project
        self._plan = plan
        self._metadata: dict[str, ProbeMetadata] = {}
        self._duration = duration
        lay = QVBoxLayout(self)

        self.mode = QComboBox()
        self.mode.addItem("Без перекодирования (нарезка + склейка)", "copy")
        self.mode.addItem("С перекодированием H.264/H.265", "reencode")
        if not compat_ok:
            lay.addWidget(QLabel(f"Copy недоступен: {compat_msg}"))
            self.mode.setCurrentIndex(1)
            cast(QStandardItemModel, self.mode.model()).item(0).setEnabled(False)
        lay.addWidget(QLabel("Режим:"))
        lay.addWidget(self.mode)
        note = QLabel(
            "Copy режет по ключевым кадрам. Для точных границ выберите перекодирование."
        )
        note.setWordWrap(True)
        lay.addWidget(note)

        form = QFormLayout()
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        self.format = QComboBox()
        self.format.addItems(["mp4", "mkv"])
        self.format.addItems(["wav", "mp3", "aac", "m4a"])
        self.vcodec = QComboBox()
        self.vcodec.setPlaceholderText("Проверка кодировщиков…")
        self.encoder = QComboBox(self)
        self.encoder.setPlaceholderText("Проверка кодировщиков…")
        self.encoder.setAccessibleName("Кодирование на процессоре или видеокарте")
        self.hw_info = QLabel("Проверка возможностей FFmpeg…")
        self.hw_info.setWordWrap(True)
        self.output_width = QSpinBox()
        self.output_width.setRange(0, 7680)
        self.output_width.setSpecialValueText("исходное")
        self.output_height = QSpinBox()
        self.output_height.setRange(0, 4320)
        self.output_height.setSpecialValueText("исходное")
        self.fps = QDoubleSpinBox()
        self.fps.setRange(0, 120)
        self.fps.setSpecialValueText("исходный")
        self.vb = QComboBox()
        self.vb.setEditable(True)
        self.vb.addItems(["", "2M", "5M", "8M", "12M"])
        self.ab = QComboBox()
        self.ab.setEditable(True)
        self.ab.addItems(["128k", "192k", "256k", "320k"])
        self.profile = QComboBox(self)
        self.profile.addItems(
            [
                "Совместимый MP4",
                "Компактный",
                "Качество исходника с обработкой",
                "Быстрая склейка",
                "Настроить вручную",
            ]
        )
        self.crf = QSpinBox(self)
        self.crf.setRange(0, 51)
        self.crf.setValue(23)
        self.preset = QComboBox(self)
        self.preset.addItems(["ultrafast", "veryfast", "fast", "medium", "slow"])
        self.preset.setCurrentText("medium")
        self.range = QComboBox(self)
        self.range.addItem("Весь проект", None)
        if duration > 0:
            start = min(max(0, position), max(0, duration - 0.04))
            self.range.addItem(
                "Пробные 10 секунд от курсора", (start, min(duration, start + 10))
            )
        if mark_range and 0 <= mark_range[0] < mark_range[1] <= duration:
            self.range.addItem("Диапазон In/Out", mark_range)
        if clip_range:
            self.range.addItem("Выбранный клип", clip_range)
        form.addRow("Профиль:", self.profile)
        form.addRow("Диапазон:", self.range)
        form.addRow("Формат:", self.format)
        form.addRow("Кодирование:", self.encoder)
        self.advanced = QToolButton(self)
        self.advanced.setText("Дополнительные параметры")
        self.advanced.setCheckable(True)
        self.advanced.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        lay.addLayout(form)
        lay.addWidget(self.advanced)
        self.advanced_fields = QWidget(self)
        self.advanced_fields.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self.advanced_fields.setVisible(False)
        form = QFormLayout(self.advanced_fields)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setContentsMargins(0, 0, 0, 0)
        self.advanced.toggled.connect(self.advanced_fields.setVisible)
        form.addRow("Видеокодек:", self.vcodec)
        self.gpu_device = QLineEdit(self)
        self.gpu_device.setPlaceholderText(
            "Linux: /dev/dri/renderD128; пусто — устройство по умолчанию"
        )
        self.gpu_device.setAccessibleName("DRM-устройство Linux для VAAPI/QSV")
        self.gpu_device.textChanged.connect(self._device_changed)
        form.addRow("Устройство VAAPI/QSV:", self.gpu_device)
        form.addRow("Ширина:", self.output_width)
        form.addRow("Высота:", self.output_height)
        form.addRow("FPS:", self.fps)
        form.addRow("Битрейт видео:", self.vb)
        form.addRow("Битрейт аудио:", self.ab)
        form.addRow("Качество CRF (меньше — выше):", self.crf)
        form.addRow("Скорость кодирования:", self.preset)
        lay.addWidget(self.advanced_fields)
        lay.addWidget(self.hw_info)
        if has_text:
            lay.addWidget(
                QLabel("Текст в углу: применится только в режиме перекодирования.")
            )

        frow = QHBoxLayout()
        self.path_edit = QLineEdit()
        btn_browse = QPushButton("...")
        btn_browse.clicked.connect(self._browse)
        frow.addWidget(self.path_edit, 1)
        frow.addWidget(btn_browse)
        lay.addWidget(QLabel("Файл результата:"))
        lay.addLayout(frow)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        lay.addWidget(self.summary)
        lay.addStretch(1)

        self.mode.currentIndexChanged.connect(self._sync)
        self.format.currentTextChanged.connect(self._sync)
        self.range.currentIndexChanged.connect(self._sync)
        self.profile.currentIndexChanged.connect(self._apply_profile)
        self.encoder.currentIndexChanged.connect(self._select_encoder)
        self.vcodec.currentTextChanged.connect(self._sync_encoder)
        for signal in (
            self.vcodec.currentTextChanged,
            self.output_width.valueChanged,
            self.output_height.valueChanged,
            self.fps.valueChanged,
            self.vb.currentTextChanged,
            self.ab.currentTextChanged,
            self.crf.valueChanged,
            self.preset.currentTextChanged,
        ):
            signal.connect(self._update_summary)
        if audio_project:
            self.format.setCurrentText("wav")
        self.profile.setCurrentIndex(3 if compat_ok and not audio_project else 0)
        if type(profile_index) is int and 0 <= profile_index < self.profile.count():
            self.profile.setCurrentIndex(profile_index)
        self._apply_profile(self.profile.currentIndex())
        self._update_encoder_items()
        self._sync()

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self._buttons = btns
        # Наполнение списков и результаты фоновой проверки не меняют геометрию окна.
        for combo in self.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(20)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for label, lines in ((note, 2), (self.hw_info, 2), (self.summary, 3)):
            label.ensurePolished()
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            label.setFixedHeight(
                label.fontMetrics().lineSpacing() * lines + 2 * label.margin()
            )
        self.advanced_fields.setMinimumHeight(self.advanced_fields.sizeHint().height())
        self.setMinimumWidth(560)
        self.resize(680, self.sizeHint().height())
        QTimer.singleShot(0, self._start_capabilities)

    def _start_capabilities(self, codec: str = "") -> None:
        if self._capability_worker is not None or self._pending_close:
            return
        self._availability_complete = False
        worker = CapabilityWorker(
            codec,
            self,
            plan=self._plan,
            device=self.gpu_device.text().strip(),
            codecs=self.VIDEO_CODECS if not codec else (),
        )
        self._capability_worker = worker
        self.hw_info.setText(
            "Проверка кодировщика на этой машине…"
            if codec
            else "Проверка возможностей FFmpeg…"
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        worker.finished.connect(self._capabilities_finished)
        worker.snapshot_ready.connect(self._capabilities_ready)
        worker.encoder_checked.connect(self._encoder_checked)
        worker.start()

    @Slot()
    def _device_changed(self) -> None:
        self._verified_codecs.clear()
        self._encoder_errors.clear()
        self._availability_complete = False
        self._pending_accept = False
        self._finish_availability_for_export = False
        self._update_encoder_items()
        self._sync()
        if self._capability_worker is not None:
            self._restart_availability = True
            self._capability_worker.cancelled.set()
        else:
            self._start_capabilities()

    @Slot(object)
    def _capabilities_ready(self, snapshot: CapabilitySnapshot) -> None:
        worker = self._capability_worker
        if (
            worker is None
            or self._pending_close
            or self._restart_availability
            or worker.device != self.gpu_device.text().strip()
        ):
            return
        self._capabilities = snapshot
        self._metadata = worker.metadata
        self._update_encoder_items()
        self._sync()

    @Slot(str, str)
    def _encoder_checked(self, codec: str, error: str) -> None:
        worker = self._capability_worker
        if (
            worker is None
            or self._pending_close
            or self._restart_availability
            or worker.device != self.gpu_device.text().strip()
        ):
            return
        self._encoder_errors[codec] = error
        if not error:
            self._verified_codecs.add(codec)
        else:
            self._verified_codecs.discard(codec)
        self._update_encoder_items()
        self.hw_info.setText(
            "Проверка кодировщиков на этой машине… Доступны только успешно проверенные варианты."
        )
        self._sync()

    def _update_encoder_items(self) -> None:
        available = [
            codec for codec in self.VIDEO_CODECS if codec in self._verified_codecs
        ]
        selected = (
            self._preferred_codec
            if self._preferred_codec in available
            else self.vcodec.currentText()
        )
        if selected not in available:
            selected = available[0] if available else ""
        with QSignalBlocker(self.vcodec), QSignalBlocker(self.encoder):
            self.vcodec.clear()
            self.encoder.clear()
            for codec in available:
                self.vcodec.addItem(codec)
                self.encoder.addItem(self.ENCODER_TITLES[codec], codec)
            self.vcodec.setCurrentIndex(self.vcodec.findText(selected))
            self.encoder.setCurrentIndex(self.encoder.findData(selected))
            placeholder = (
                "Нет доступных кодировщиков"
                if self._availability_complete
                else "Проверка кодировщиков…"
            )
            self.vcodec.setPlaceholderText(placeholder)
            self.encoder.setPlaceholderText(placeholder)

        for index in range(3):
            cast(QStandardItemModel, self.profile.model()).item(index).setEnabled(
                "libx264" in self._verified_codecs
            )

    def _select_encoder(self) -> None:
        codec = self.encoder.currentData()
        if codec:
            self.profile.setCurrentIndex(4)
            self.mode.setCurrentIndex(1)
            self.vcodec.setCurrentText(codec)

    def _sync_encoder(self) -> None:
        if self.vcodec.currentText():
            self._preferred_codec = self.vcodec.currentText()
        index = self.encoder.findData(self.vcodec.currentText())
        self.encoder.blockSignals(True)
        self.encoder.setCurrentIndex(index)
        self.encoder.blockSignals(False)
        self._sync()

    @Slot()
    def _capabilities_finished(self) -> None:
        worker = self._capability_worker
        if worker is None:
            return
        self._capability_worker = None
        worker.deleteLater()
        if self._pending_close:
            if self._copy_worker is None:
                super().reject()
            return
        if self._restart_availability:
            self._restart_availability = False
            self._start_capabilities()
            return
        cancelled_for_export = self._finish_availability_for_export
        self._finish_availability_for_export = False
        self._availability_complete = True
        self._update_encoder_items()
        self._sync()
        if worker.error and not cancelled_for_export:
            self.hw_info.setText(worker.error)
            self._pending_accept = False
            if worker.codec:
                QMessageBox.warning(self, "Кодировщик", worker.error)
            return
        if worker.snapshot is None:
            self.hw_info.setText("Проверка FFmpeg не вернула результат.")
            self._pending_accept = False
            return
        self._capabilities = worker.snapshot
        self._metadata = worker.metadata
        if worker.codec and worker.device == self.gpu_device.text().strip():
            self._verified_codecs.add(worker.codec)
        self._update_encoder_items()
        self.hw_info.setText(
            "Доступны только кодировщики, проверенные на этой машине."
            if self._verified_codecs
            else "На этой машине не удалось запустить ни один видеокодировщик."
        )
        self.hw_info.setToolTip(worker.snapshot.version)
        self._sync()
        if self._copy_worker is not None:
            self.format.setEnabled(False)
            self.mode.setEnabled(False)
        if self._pending_accept:
            self._pending_accept = False
            self.accept()

    def _sync(self) -> None:
        audio_only = self.format.currentText() in ("wav", "mp3", "aac", "m4a")
        can_copy = (
            self._compat_ok and not audio_only and self.range.currentData() is None
        )
        cast(QStandardItemModel, self.mode.model()).item(0).setEnabled(can_copy)
        if not can_copy and self.mode.currentData() == "copy":
            self.mode.setCurrentIndex(1)
        is_copy = self.mode.currentData() == "copy"
        # format активен и в Copy: контейнер определяется суффиксом файла
        self.format.setEnabled(True)
        for w in (
            self.vcodec,
            self.output_width,
            self.output_height,
            self.fps,
            self.vb,
            self.ab,
            self.crf,
            self.preset,
        ):
            w.setEnabled(not is_copy)
        self.vcodec.setEnabled(not is_copy and bool(self._verified_codecs))
        self.encoder.setEnabled(
            not is_copy and not audio_only and bool(self._verified_codecs)
        )
        if hasattr(self, "_buttons"):
            self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
                self._copy_worker is None
                and (
                    is_copy
                    or (audio_only and self._capabilities is not None)
                    or self.vcodec.currentText() in self._verified_codecs
                )
            )
        if audio_only:
            for widget in (
                self.vcodec,
                self.output_width,
                self.output_height,
                self.fps,
                self.vb,
                self.crf,
                self.preset,
            ):
                widget.setEnabled(False)
        self._update_summary()

    def _update_summary(self) -> None:
        if not hasattr(self, "summary"):
            return
        values = self.values()
        selected_range = values["range"]
        duration = (
            selected_range[1] - selected_range[0] if selected_range else self._duration
        )
        codec = {
            "wav": "PCM 16-bit / 48 kHz / stereo",
            "mp3": "MP3",
            "aac": "AAC",
            "m4a": "AAC",
        }.get(values["format"], (values["vcodec"] or "проверка кодировщика") + " + AAC")
        parts = [
            values["format"].upper(),
            "исходные потоки / Copy" if values["mode"] == "copy" else codec,
            f"{duration:.3f} с",
        ]
        if not values["audio_only"] and self._plan is not None and self._metadata:
            try:
                from src.workers.video_editor.render_command import RenderCommand

                width, height, fps = RenderCommand.canvas(
                    self._plan,
                    self._metadata,
                    values if values["mode"] != "copy" else {},
                )
                if values["mode"] == "copy":
                    reference = self._plan.canvas_source or (
                        self._plan.video[0] if self._plan.video else None
                    )
                    metadata = self._metadata[reference.path] if reference else {}
                    streams = metadata.get("video_streams", [])
                    stream = (
                        streams[reference.stream] if reference and streams else metadata
                    )
                    width, height = (
                        stream.get("width", width),
                        stream.get("height", height),
                    )
                parts.extend(
                    (
                        f"{width}×{height}",
                        f"FPS {fps}"
                        + (" (исходник)" if values["mode"] == "copy" else ""),
                    )
                )
            except (KeyError, ValueError) as error:
                parts.append(str(error))

        def bitrate(value: str) -> float:
            match = re.fullmatch(r"([1-9]\d*(?:\.\d+)?)([kKmM]?)", value)
            return (
                float(match[1]) * {"": 1, "k": 1000, "m": 1000000}[match[2].lower()]
                if match
                else 0
            )

        video_rate = 0 if values["audio_only"] else bitrate(values["vb"])
        audio_rate = (
            48000 * 2 * 16 if values["format"] == "wav" else bitrate(values["ab"])
        )
        if (
            values["mode"] == "reencode"
            and (values["audio_only"] or video_rate)
            and audio_rate
        ):
            parts.append(
                f"оценка ≈{(video_rate + audio_rate) * duration / 8 / 1048576:.1f} МиБ по заданному битрейту, без накладных расходов контейнера"
            )
        else:
            parts.append(
                "объём неизвестен до кодирования"
                if values["mode"] == "reencode"
                else "нарезка по ключевым кадрам"
            )
        self.summary.setText(" · ".join(parts))
        self.summary.setToolTip(self.summary.text())

    def _apply_profile(self, index: int) -> None:
        if index == 4:
            return
        if index == 3:
            if (
                self._compat_ok
                and self.format.currentText() in ("mp4", "mkv")
                and self.range.currentData() is None
            ):
                self.mode.setCurrentIndex(0)
            else:
                self.hw_info.setText(
                    "Быстрая склейка недоступна: требуется перекодирование."
                )
            return
        self.mode.setCurrentIndex(1)
        self._preferred_codec = "libx264"
        self.vcodec.setCurrentText("libx264")
        self.crf.setValue((23, 28, 18)[index])
        self.preset.setCurrentText(("medium", "fast", "slow")[index])
        self.vb.setCurrentText("")
        self.output_width.setValue(1280 if index == 1 else 0)
        self.output_height.setValue(0)
        self.fps.setValue(0)
        if not self._audio_project:
            self.format.setCurrentText("mp4")

    @Slot()
    def _copy_finished(self) -> None:
        worker = self._copy_worker
        if worker is None:
            return
        self._copy_worker = None
        worker.deleteLater()
        if self._pending_close:
            if self._capability_worker is None:
                super().reject()
            return
        self.format.setEnabled(True)
        self.mode.setEnabled(True)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        if worker.error:
            self.hw_info.setText(worker.error)
            QMessageBox.warning(self, "Copy недоступен", worker.error)
            return
        self._copy_verified.add(worker.container)
        self.hw_info.setText(f"Copy в {worker.container.upper()} проверен.")
        self.accept()

    def _browse(self) -> None:
        p, chosen = QFileDialog.getSaveFileName(
            self,
            "Файл результата",
            "",
            ";;".join(
                f"{suffix.upper()} (*.{suffix})"
                for suffix in ("mp4", "mkv", "wav", "mp3", "aac", "m4a")
            ),
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if p:
            # разделение фильтров: дописать расширение выбранного фильтра,
            # если пользователь его не указал
            if not Path(p).suffix:
                p += "." + chosen.split(" ")[0].lower()
            self.path_edit.setText(p)
            # и синхронизировать выбор формата
            self.format.setCurrentText(Path(p).suffix.lower()[1:])

    def values(self) -> dict[str, Any]:
        out = self.path_edit.text().strip()
        # расширение не указано - дописываем по выбранному формату
        if out and not Path(out).suffix:
            out += f".{self.format.currentText()}"
        return {
            "mode": self.mode.currentData(),
            "format": self.format.currentText(),
            "vcodec": self.vcodec.currentText(),
            "gpu_device": self.gpu_device.text().strip(),
            "width": self.output_width.value(),
            "height": self.output_height.value(),
            "fps": self.fps.value(),
            "vb": self.vb.currentText().strip(),
            "ab": self.ab.currentText().strip(),
            "out": out,
            "overwrite": self._overwrite,
            "audio_only": self.format.currentText() in ("wav", "mp3", "aac", "m4a"),
            "range": self.range.currentData(),
            "crf": self.crf.value(),
            "preset": self.preset.currentText(),
            "profile": self.profile.currentIndex(),
        }

    def accept(self) -> None:
        if self._pending_close:
            return
        values = self.values()
        if values["mode"] == "copy" and not self._compat_ok:
            QMessageBox.warning(
                self,
                "Экспорт",
                self._compat_msg or "Copy недоступен для этого монтажа.",
            )
            return
        if not values["out"]:
            QMessageBox.warning(self, "Экспорт", "Укажите файл результата.")
            return
        path = Path(values["out"])
        if path.suffix.lower() != f".{values['format']}":
            QMessageBox.warning(
                self, "Экспорт", "Расширение файла не совпадает с выбранным форматом."
            )
            return
        if (
            values["mode"] == "copy"
            and self._copy_sources
            and values["format"] not in self._copy_verified
        ):
            if self._copy_worker is not None:
                return
            worker = CopyPreflightWorker(self._copy_sources, values["format"], self)
            self._copy_worker = worker
            self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            self.format.setEnabled(False)
            self.mode.setEnabled(False)
            self.hw_info.setText("Проверка потоков и контейнера Copy…")
            worker.finished.connect(self._copy_finished)
            worker.start()
            return
        if values["mode"] == "reencode":
            if any(values[key] % 2 for key in ("width", "height")):
                QMessageBox.warning(
                    self, "Экспорт", "Ширина и высота должны быть чётными."
                )
                return
            if any(
                value and not re.fullmatch(r"[1-9]\d*(?:\.\d+)?[kKmM]?", value)
                for value in (values["vb"], values["ab"])
            ):
                QMessageBox.warning(
                    self,
                    "Экспорт",
                    "Укажите положительный битрейт: например, 5M или 192k.",
                )
                return
            if self._capabilities is None:
                self.hw_info.setText("Дождитесь проверки FFmpeg.")
                return
            if (
                not values["audio_only"]
                and values["vcodec"] not in self._verified_codecs
            ):
                error = self._encoder_errors.get(values["vcodec"])
                if error:
                    QMessageBox.warning(self, "Кодировщик", error)
                else:
                    self.hw_info.setText(
                        "Дождитесь проверки и выберите доступный кодировщик."
                    )
                return
        if self._capability_worker is not None:
            self._pending_accept = True
            if self._capability_worker.codecs:
                self._finish_availability_for_export = True
                self._capability_worker.cancelled.set()
            self.hw_info.setText("Завершение фоновой проверки…")
            return
        self._overwrite = False
        if path.exists():
            answer = QMessageBox.question(
                self,
                "Заменить результат?",
                f"Файл «{path.name}» уже существует. Заменить его после успешного экспорта?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._overwrite = True
        super().accept()

    def reject(self) -> None:
        if self._capability_worker is not None or self._copy_worker is not None:
            self._pending_close = True
            self._pending_accept = False
            for worker in (self._capability_worker, self._copy_worker):
                if worker is not None:
                    worker.cancelled.set()
            self.setEnabled(False)
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._capability_worker is not None or self._copy_worker is not None:
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)
