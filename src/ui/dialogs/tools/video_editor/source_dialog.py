"""Просмотр исходника и выбор потоков/In/Out до выкладки на таймлайн."""

from __future__ import annotations

from src.utils.system.qt_multimedia import stop_media_player

from src.workers.video_editor.metadata import ProbeMetadata

from PySide6.QtCore import QUrl, Qt
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QWidget,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QMessageBox,
)
from src.domain.video.models import MediaAsset, SourceSelection


class SourceDialog(QDialog):
    source_in: QDoubleSpinBox
    source_out: QDoubleSpinBox

    def __init__(
        self,
        asset: MediaAsset,
        metadata: ProbeMetadata | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Исходник — {asset.name}")
        self.resize(720, 560)
        self.asset = asset
        self._chosen: SourceSelection | None = None
        self._metadata = metadata or {}
        self._initial_video = asset.video_stream
        self._initial_audio = asset.audio_stream
        layout = QVBoxLayout(self)
        self.video = QVideoWidget(self)
        self.video.setMinimumSize(160, 90)
        layout.addWidget(self.video, 1)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(1)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, 10000)
        self.slider.sliderMoved.connect(
            lambda value: self.player.setPosition(
                round(value / 10000 * self.asset.duration * 1000)
            )
        )
        self.player.positionChanged.connect(self._position)
        layout.addWidget(self.slider)
        row = QHBoxLayout()
        for title, callback in (
            ("Воспроизведение / пауза", self.toggle),
            (
                "Вход у курсора",
                lambda: self.source_in.setValue(self.player.position() / 1000),
            ),
            (
                "Выход у курсора",
                lambda: self.source_out.setValue(self.player.position() / 1000),
            ),
        ):
            button = QPushButton(title, self)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        form = QFormLayout()
        self.source_in, self.source_out = QDoubleSpinBox(self), QDoubleSpinBox(self)
        for field, value, name in (
            (self.source_in, 0, "Вход исходника"),
            (self.source_out, asset.duration, "Выход исходника"),
        ):
            field.setRange(0, asset.duration)
            field.setDecimals(9)
            field.setValue(value)
            field.setAccessibleName(name)
        form.addRow("In, секунды:", self.source_in)
        form.addRow("Out, секунды:", self.source_out)
        self.video_stream, self.audio_stream = QComboBox(self), QComboBox(self)
        form.addRow("Видеопоток:", self.video_stream)
        form.addRow("Аудиопоток:", self.audio_stream)
        self.video_stream.currentIndexChanged.connect(self.player.setActiveVideoTrack)
        self.video_stream.currentIndexChanged.connect(self._limit_source_range)
        self.audio_stream.currentIndexChanged.connect(self.player.setActiveAudioTrack)
        layout.addLayout(form)
        note = QLabel(
            "Просмотр исходника без обработки монтажа. Выбор потоков относится ко всем фрагментам этого файла в проекте и применяется вместе с добавлением фрагмента.",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.player.errorOccurred.connect(lambda error, text: self.status.setText(text))
        self.player.tracksChanged.connect(self._tracks)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Добавить фрагмент на таймлайн"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.player.setSource(QUrl.fromLocalFile(asset.path))
        self._tracks()

    def _tracks(self) -> None:
        for kind, combo, tracks, initial in (
            (
                "video",
                self.video_stream,
                self.player.videoTracks(),
                self._initial_video,
            ),
            (
                "audio",
                self.audio_stream,
                self.player.audioTracks(),
                self._initial_audio,
            ),
        ):
            streams = (
                self._metadata.get("video_streams", [])
                if kind == "video"
                else self._metadata.get("audio_streams", [])
            )
            count = max(len(streams), len(tracks))
            combo.blockSignals(True)
            previous = combo.currentIndex() if combo.count() else initial
            combo.clear()
            for index in range(count):
                stream = streams[index] if index < len(streams) else {}
                description = " · ".join(
                    str(value)
                    for value in (
                        stream.get("codec_name", ""),
                        stream.get("tags", {}).get("language", ""),
                        stream.get("channel_layout", ""),
                    )
                    if value
                )
                combo.addItem(
                    f"{index + 1}" + (f" — {description}" if description else "")
                )
            combo.setCurrentIndex(min(previous, count - 1) if count else -1)
            combo.setEnabled(count > 1)
            combo.blockSignals(False)
        if self.video_stream.currentIndex() >= 0:
            self.player.setActiveVideoTrack(self.video_stream.currentIndex())
        if self.audio_stream.currentIndex() >= 0:
            self.player.setActiveAudioTrack(self.audio_stream.currentIndex())
        self._limit_source_range()

    def _limit_source_range(self) -> None:
        duration = self.asset.duration
        videos = self._metadata.get("video_streams", [])
        index = self.video_stream.currentIndex()
        if 0 <= index < len(videos):
            try:
                selected = float(videos[index].get("duration", 0) or 0)
                if 0 < selected < duration:
                    duration = selected
            except (TypeError, ValueError):
                pass
        self.source_in.setMaximum(duration)
        self.source_out.setMaximum(duration)
        if self.source_in.value() >= self.source_out.value():
            self.source_in.setValue(0)

    def _position(self, milliseconds: int) -> None:
        if not self.slider.isSliderDown() and self.asset.duration > 0:
            self.slider.setValue(
                min(10000, round(milliseconds / self.asset.duration / 1000 * 10000))
            )

    def toggle(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def values(self) -> SourceSelection:
        if self._chosen is not None:
            return self._chosen
        return SourceSelection(
            self.source_in.value(),
            self.source_out.value(),
            max(0, self.video_stream.currentIndex()),
            max(0, self.audio_stream.currentIndex()),
        )

    def accept(self) -> None:
        if self.source_out.value() <= self.source_in.value():
            QMessageBox.warning(self, "Исходник", "Out должен быть позже In.")
            return
        super().accept()

    def done(self, result: int) -> None:
        if result == QDialog.DialogCode.Accepted:
            self._chosen = self.values()
        stop_media_player(self.player)
        self.player.setSource(QUrl())
        super().done(result)
