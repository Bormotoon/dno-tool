"""Независимый просмотр импортированного файла и выбор фрагмента."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, QTimer, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)

from src.domain.video.models import MediaAsset, SourceSelection
from src.utils.system.qt_multimedia import stop_media_player
from src.workers.video_editor.metadata import ProbeMetadata
from .preview_widget import _mk_tool_btn, _FrameView
from .frame_mailbox import FrameMailbox


class SourceMonitor(QWidget):
    """Плеер исходника со своим временем и границами, отдельными от монтажа."""

    append_requested = Signal(str, object)
    playback_started = Signal()
    status: QLabel
    source_in: QDoubleSpinBox
    source_out: QDoubleSpinBox

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.asset: MediaAsset | None = None
        self._metadata: ProbeMetadata = {}
        self._closed = False
        self._range_limit = 0.0
        self._priming = False
        self._pending_seek: int | None = None
        self._prime_target = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel("Исходник — выберите импортированный файл", self)
        self.title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        layout.addWidget(self.title)
        self.video = _FrameView(self)
        self.video.empty_text = "Выберите импортированный файл"
        self.video.show_image(None)
        self.video.setMinimumSize(160, 90)
        self.video.setAccessibleName("Просмотр исходника")
        layout.addWidget(self.video, 1)
        self.controls = QWidget(self)
        layout.addWidget(self.controls)
        layout = QVBoxLayout(self.controls)
        layout.setContentsMargins(0, 0, 0, 0)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(1)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoOutput(self.sink)
        self.frame_mailbox = FrameMailbox(self)
        self.sink.videoFrameChanged.connect(
            self.frame_mailbox.offer, Qt.ConnectionType.DirectConnection
        )
        self.frame_mailbox.available.connect(
            self._consume_frame, Qt.ConnectionType.QueuedConnection
        )
        self._prime_timer = QTimer(self)
        self._prime_timer.setSingleShot(True)
        self._prime_timer.setInterval(1500)
        self._prime_timer.timeout.connect(self._finish_prime)
        self.player.mediaStatusChanged.connect(self._media_status)
        self.player.positionChanged.connect(self._position)
        self.player.tracksChanged.connect(self._tracks)
        self.player.errorOccurred.connect(lambda error, text: self._set_status(text))
        self.time_label = QLabel("00:00:00.000 / 00:00:00.000", self)
        time_row = QHBoxLayout()
        time_row.addWidget(self.time_label)
        time_row.addStretch(1)
        self.range_label = QLabel("In/Out: —", self)
        time_row.addWidget(self.range_label)
        layout.addLayout(time_row)
        from .audio_meter import AudioMeter

        self.audio_meter = AudioMeter(self.player)
        layout.addWidget(self.audio_meter)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.status.hide()
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, 10000)
        self.slider.setAccessibleName("Перемотка исходника")
        self.slider.sliderMoved.connect(self._seek_slider)
        layout.addWidget(self.slider)
        self.button_row = QGridLayout()
        self.button_row.setContentsMargins(0, 0, 0, 0)
        self.button_row.setHorizontalSpacing(4)
        self.button_row.setColumnStretch(0, 1)
        self.button_row.setColumnStretch(2, 1)
        self.transport = QWidget(self.controls)
        transport = QHBoxLayout(self.transport)
        transport.setContentsMargins(0, 0, 0, 0)
        transport.setSpacing(4)
        self.btn_start = _mk_tool_btn("ve_to_start", "В начало исходника", self)
        self.btn_play = _mk_tool_btn(
            "ve_play", "Исходник: воспроизведение / пауза", self
        )
        self.btn_pause = _mk_tool_btn("ve_pause", "Исходник: пауза", self)
        self.btn_stop = _mk_tool_btn(
            "ve_stop", "Исходник: стоп и возврат в начало", self
        )
        self.btn_prev = _mk_tool_btn("ve_prev", "Исходник: кадр назад", self)
        self.btn_next = _mk_tool_btn("ve_next", "Исходник: кадр вперёд", self)
        self.btn_start.clicked.connect(lambda: self.seek(0))
        self.btn_play.clicked.connect(self.toggle)
        self.btn_pause.clicked.connect(self.pause)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_prev.clicked.connect(lambda: self.step_frame(-1))
        self.btn_next.clicked.connect(lambda: self.step_frame(1))
        for button in (
            self.btn_start,
            self.btn_play,
            self.btn_pause,
            self.btn_stop,
            self.btn_prev,
            self.btn_next,
        ):
            transport.addWidget(button)
        self.range_controls = QWidget(self.controls)
        marks = QHBoxLayout(self.range_controls)
        marks.setContentsMargins(0, 0, 0, 0)
        marks.setSpacing(4)
        self.btn_in = _mk_tool_btn("ve_mark_in", "In исходника у курсора", self)
        self.btn_out = _mk_tool_btn("ve_mark_out", "Out исходника у курсора", self)
        self.btn_in.clicked.connect(
            lambda: self.source_in.setValue(self.player.position() / 1000)
        )
        self.btn_out.clicked.connect(
            lambda: self.source_out.setValue(self.player.position() / 1000)
        )
        marks.addWidget(self.btn_in)
        marks.addWidget(self.btn_out)
        self.btn_add = _mk_tool_btn(
            "ve_add_timeline", "Добавить фрагмент In/Out на таймлайн", self
        )
        self.btn_add.clicked.connect(self._append)
        self.details_toggle = _mk_tool_btn(
            "menu_settings", "Границы и потоки исходника", self
        )
        self.details_toggle.setCheckable(True)
        self.source_actions = QWidget(self.controls)
        actions = QHBoxLayout(self.source_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(4)
        actions.addWidget(self.btn_add)
        actions.addWidget(self.details_toggle)
        side_width = max(
            self.range_controls.sizeHint().width(), self.source_actions.sizeHint().width()
        )
        self.button_row.setColumnMinimumWidth(0, side_width)
        self.button_row.setColumnMinimumWidth(2, side_width)
        self.button_row.addWidget(
            self.range_controls, 0, 0, Qt.AlignmentFlag.AlignLeft
        )
        self.button_row.addWidget(self.transport, 0, 1, Qt.AlignmentFlag.AlignCenter)
        self.button_row.addWidget(
            self.source_actions, 0, 2, Qt.AlignmentFlag.AlignRight
        )
        layout.addLayout(self.button_row)
        self.details = QWidget(self)
        form = QFormLayout(self.details)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.source_in, self.source_out = QDoubleSpinBox(self), QDoubleSpinBox(self)
        for field, name in (
            (self.source_in, "In исходника"),
            (self.source_out, "Out исходника"),
        ):
            field.setDecimals(9)
            field.setSuffix(" с")
            field.setAccessibleName(name)
            field.valueChanged.connect(self._range_changed)
        form.addRow("In:", self.source_in)
        form.addRow("Out:", self.source_out)
        self.video_stream, self.audio_stream = QComboBox(self), QComboBox(self)
        self.video_stream.setAccessibleName("Видеопоток исходника")
        self.audio_stream.setAccessibleName("Аудиопоток исходника")
        self.video_stream.currentIndexChanged.connect(self.player.setActiveVideoTrack)
        self.video_stream.currentIndexChanged.connect(self._limit_range)
        self.audio_stream.currentIndexChanged.connect(self.player.setActiveAudioTrack)
        form.addRow("Видео:", self.video_stream)
        form.addRow("Звук:", self.audio_stream)
        layout.addWidget(self.details)
        self.details.hide()
        self.details_toggle.toggled.connect(self.details.setVisible)
        self._enable_controls(False)

    @staticmethod
    def _time(seconds: float) -> str:
        milliseconds = max(0, round(seconds * 1000))
        seconds_int, millis = divmod(milliseconds, 1000)
        minutes, seconds_int = divmod(seconds_int, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02}:{minutes:02}:{seconds_int:02}.{millis:03}"

    def load_asset(
        self, asset: MediaAsset, metadata: ProbeMetadata | None = None
    ) -> None:
        if self._closed:
            return
        if self.asset is not None and (self.asset.iid, self.asset.path) == (
            asset.iid,
            asset.path,
        ):
            self.asset = asset
            self._metadata = metadata or {}
            self.title.setText(f"Исходник — {asset.name}")
            self._tracks()
            return
        self.clear()
        self.asset = asset
        self._range_limit = asset.duration
        self._metadata = metadata or {}
        self.title.setText(f"Исходник — {asset.name}")
        self.title.setToolTip(asset.path)
        self._set_status("Аудиофайл" if not asset.has_video else "")
        for field, value in ((self.source_in, 0.0), (self.source_out, asset.duration)):
            field.setRange(0, asset.duration)
            field.setValue(value)
        self._enable_controls(True)
        self._pending_seek = 0
        self.player.setSource(QUrl.fromLocalFile(asset.path))
        self._tracks()
        self._position(0)

    def _enable_controls(self, enabled: bool) -> None:
        for widget in (
            self.slider,
            self.btn_start,
            self.btn_play,
            self.btn_pause,
            self.btn_stop,
            self.btn_prev,
            self.btn_next,
            self.btn_in,
            self.btn_out,
            self.btn_add,
            self.details_toggle,
        ):
            widget.setEnabled(enabled)

    def _tracks(self) -> None:
        if self.asset is None:
            return
        for combo, streams, count, selected in (
            (
                self.video_stream,
                self._metadata.get("video_streams", []),
                len(self.player.videoTracks()),
                self.asset.video_stream,
            ),
            (
                self.audio_stream,
                self._metadata.get("audio_streams", []),
                len(self.player.audioTracks()),
                self.asset.audio_stream,
            ),
        ):
            count = max(count, len(streams))
            previous = combo.currentIndex() if combo.count() else selected
            combo.blockSignals(True)
            combo.clear()
            for index in range(count):
                codec = (
                    streams[index].get("codec_name", "") if index < len(streams) else ""
                )
                combo.addItem(f"{index + 1}" + (f" — {codec}" if codec else ""))
            combo.setCurrentIndex(min(previous, count - 1) if count else -1)
            combo.setEnabled(count > 1)
            combo.blockSignals(False)
        if self.video_stream.currentIndex() >= 0:
            self.player.setActiveVideoTrack(self.video_stream.currentIndex())
        if self.audio_stream.currentIndex() >= 0:
            self.player.setActiveAudioTrack(self.audio_stream.currentIndex())
        self._limit_range()

    def _limit_range(self) -> None:
        if self.asset is None:
            return
        duration = self.asset.duration
        streams = self._metadata.get("video_streams", [])
        index = self.video_stream.currentIndex()
        if 0 <= index < len(streams):
            try:
                stream_duration = float(streams[index].get("duration", 0) or 0)
                if 0 < stream_duration < duration:
                    duration = stream_duration
            except (ValueError, TypeError):
                pass
        self.source_in.setMaximum(duration)
        self.source_out.setMaximum(duration)
        self._range_limit = duration
        self._range_changed()

    def _range_changed(self) -> None:
        if not hasattr(self, "range_label"):
            return
        self.range_label.setText(
            f"In {self.source_in.value():.2f} · Out {self.source_out.value():.2f}"
        )
        self.range_label.setToolTip(
            f"In {self._time(self.source_in.value())} · Out {self._time(self.source_out.value())}"
        )
        self.btn_add.setEnabled(
            self.asset is not None and self.source_out.value() > self.source_in.value()
        )

    def _position(self, milliseconds: int) -> None:
        duration = self.asset.duration if self.asset else 0
        self.time_label.setText(
            f"{self._time(milliseconds / 1000)} / {self._time(duration)}"
        )
        if not self.slider.isSliderDown():
            self.slider.setValue(
                min(10000, round(milliseconds / max(duration * 1000, 1) * 10000))
            )

    def _seek_slider(self, value: int) -> None:
        if self.asset:
            self.seek(round(value / 10000 * self.asset.duration * 1000))

    def seek(self, milliseconds: int) -> None:
        if self.asset is None or self._closed:
            return
        milliseconds = max(0, min(round(self.asset.duration * 1000), milliseconds))
        if self.player.mediaStatus() in (
            QMediaPlayer.MediaStatus.NoMedia,
            QMediaPlayer.MediaStatus.LoadingMedia,
        ):
            self._pending_seek = milliseconds
            return
        playing = (
            self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            and not self._priming
        )
        self.player.setPosition(milliseconds)
        if not playing and self.asset.has_video:
            self._priming = True
            self._prime_target = milliseconds
            self.audio.setMuted(True)
            self.player.play()
            self._prime_timer.start()

    def _media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if (
            status
            in (
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
            )
            and self._pending_seek is not None
        ):
            pending, self._pending_seek = self._pending_seek, None
            self.seek(pending)

    @Slot()
    def _consume_frame(self) -> None:
        frame = self.frame_mailbox.take()
        if frame is None or self._closed or self.asset is None or not frame.isValid():
            return
        image = frame.toImage()
        if not image.isNull():
            self.video.show_image(image)
        if self._priming and (
            frame.startTime() < 0 or frame.startTime() / 1000 >= self._prime_target - 50
        ):
            self._finish_prime()

    def _finish_prime(self) -> None:
        self._prime_timer.stop()
        if self._priming:
            self._priming = False
            self.player.pause()
            self.audio.setMuted(False)

    def toggle(self) -> None:
        if self.asset is None or self._closed:
            return
        if self._priming:
            self._finish_prime()
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.playback_started.emit()
            self.player.play()

    def pause(self) -> None:
        self._finish_prime()
        self.player.pause()

    def stop(self) -> None:
        """Остановить исходник и показать его начало, сохранив границы In/Out."""
        if self.asset is None or self._closed:
            return
        self._finish_prime()
        self._pending_seek = None
        self.frame_mailbox.clear()
        stop_media_player(self.player)
        self.seek(0)
        self._position(0)

    def step_frame(self, direction: int) -> None:
        if self.asset:
            self.pause()
            self.seek(
                max(
                    0,
                    min(
                        round(self.asset.duration * 1000),
                        self.player.position()
                        + round(direction * 1000 / (self.asset.fps or 25)),
                    ),
                )
            )

    def _append(self) -> None:
        if self.asset and self.source_out.value() > self.source_in.value():
            self.pause()
            self.append_requested.emit(self.asset.iid, self.values())

    def values(self) -> SourceSelection:
        return SourceSelection(
            min(self.source_in.value(), self._range_limit),
            min(self.source_out.value(), self._range_limit),
            max(0, self.video_stream.currentIndex()),
            max(0, self.audio_stream.currentIndex()),
        )

    def clear(self) -> None:
        self._prime_timer.stop()
        self._priming = False
        self._pending_seek = None
        self.frame_mailbox.clear()
        self.video.show_image(None)
        stop_media_player(self.player)
        self.player.setSource(QUrl())
        self.frame_mailbox.clear()
        self.audio.setMuted(False)
        self.asset = None
        self._metadata = {}
        for combo in (self.video_stream, self.audio_stream):
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
        self.title.setText("Исходник — выберите импортированный файл")
        self.title.setToolTip("")
        self._set_status("")
        self.range_label.clear()
        self._position(0)
        self._enable_controls(False)

    def shutdown(self) -> None:
        if not self._closed:
            self._closed = True
            self.frame_mailbox.close()
            self.audio_meter.close()
            self.clear()

    def _set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))
