"""Измерение выходного PCM с ограниченной частотой обновления индикатора."""

from __future__ import annotations

import array
import math
import time

from PySide6.QtCore import Slot
from PySide6.QtMultimedia import (
    QAudioBuffer,
    QAudioBufferOutput,
    QAudioFormat,
    QMediaPlayer,
)
from PySide6.QtWidgets import QLabel


class AudioMeter(QLabel):
    def __init__(self, player: QMediaPlayer) -> None:
        super().__init__("Уровень: —", player.parent())
        self.setAccessibleName("Пиковый уровень звука и клиппинг")
        self.setToolTip(
            "Пик декодированного микса, dBFS. Клиппинг удерживается 2 секунды. Для защиты включите лимитер."
        )
        self.output = QAudioBufferOutput(self)
        player.setAudioBufferOutput(self.output)
        self.output.audioBufferReceived.connect(self._receive)
        self._last = 0.0
        self._peak = 0.0
        self._clipped_until = 0.0

    @Slot(QAudioBuffer)
    def _receive(self, buffer: QAudioBuffer) -> None:
        if not buffer.isValid():
            return
        format = buffer.format().sampleFormat()
        kind, scale, bias = {
            QAudioFormat.SampleFormat.UInt8: ("B", 128, 128),
            QAudioFormat.SampleFormat.Int16: ("h", 32768, 0),
            QAudioFormat.SampleFormat.Int32: ("i", 2147483648, 0),
            QAudioFormat.SampleFormat.Float: ("f", 1, 0),
        }.get(format, ("", 1, 0))
        if not kind:
            return
        samples = array.array(kind)
        samples.frombytes(bytes(buffer.constData())[: buffer.byteCount()])
        peak = max((abs(value - bias) / scale for value in samples), default=0.0)
        now = time.monotonic()
        if peak >= 0.999:
            self._clipped_until = now + 2
        self._peak = max(self._peak, peak)
        if now - self._last >= 0.1:
            level = (
                f"{20 * math.log10(self._peak):.1f} dBFS"
                if self._peak > 1e-6
                else "−∞ dBFS"
            )
            self.setText(
                f"Уровень: {level}"
                + (" · клиппинг" if now < self._clipped_until else "")
            )
            self._last, self._peak = now, 0.0
