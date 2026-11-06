"""GPU-вывод отобранных кадров Qt Quick с прежними подписями камеры."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, QSizeF, Qt, QUrl
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPaintEvent, QResizeEvent
from PySide6.QtMultimedia import QVideoFrame, QVideoSink
from PySide6.QtQml import QQmlComponent
from PySide6.QtQuick import QSGRendererInterface
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QStackedLayout, QWidget
from shiboken6 import isValid

from src.services.media.camera_presentation import (
    camera_fit_size,
    camera_frame_size,
)
from src.ui.dialogs.tools.camera_overlay import paint_camera_overlay

_VIDEO_QML = b"""
import QtQuick
import QtMultimedia
Rectangle {
    color: "#10151c"
    property alias videoSink: video.videoSink
    property size renderSize: Qt.size(1, 1)
    property size displaySize: Qt.size(1, 1)
    property real videoOpacity: 1.0
    VideoOutput {
        id: video
        anchors.centerIn: parent
        width: parent.displaySize.width
        height: parent.displaySize.height
        opacity: parent.videoOpacity
        fillMode: VideoOutput.PreserveAspectFit
        layer.enabled: true
        layer.textureSize: parent.renderSize
        layer.smooth: true
    }
}
"""


class _CameraOverlay(QWidget):
    """Подписи QWidget компонуются поверх текстуры без копирования видеокадра."""

    def __init__(self, output: CameraVideoOutput) -> None:
        super().__init__(output)
        self.output = output
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        output = self.output
        if output.software_output:
            painter.fillRect(self.rect(), QColor("#10151c"))
            if not output._image.isNull():
                painter.setOpacity(0.4 if output.stale else 1.0)
                painter.drawImage(output.video_rect(), output._image)
                painter.setOpacity(1.0)
        painter.setFont(output.font())
        paint_camera_overlay(
            painter,
            self.rect(),
            output.caption,
            recording_text=output.recording_text,
            recording=output.recording_active,
            message=output.status_message if output.stale else "",
        )


class CameraVideoOutput(QWidget):
    """Выводить только выбранные кадры; ограничивать текстуру до композиции UI."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.caption = ""
        self.recording_text = ""
        self.recording_active = False
        self.stale = False
        self.status_message = ""
        self._maximum = (1920, 1080)
        self._source = (1, 1)
        self._image = QImage()
        self._quick = QQuickWidget(self)
        self._quick.setClearColor(QColor("#10151c"))
        self._quick.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        component = QQmlComponent(self._quick.engine(), self._quick)
        component.setData(_VIDEO_QML, QUrl())
        root = component.create()
        if root is None:
            errors = "; ".join(error.toString() for error in component.errors())
            raise RuntimeError(f"Не удалось создать GPU-вывод камер Qt Quick: {errors}")
        self._quick.setContent(QUrl(), component, root)
        self._root = root
        self.software_output = (
            self._quick.quickWindow().rendererInterface().graphicsApi()
            == QSGRendererInterface.GraphicsApi.Software
        )
        self._sink: QVideoSink = root.property("videoSink")
        self._overlay = _CameraOverlay(self)
        layout = QStackedLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        layout.addWidget(self._quick)
        layout.addWidget(self._overlay)
        layout.setCurrentWidget(self._overlay)

    def videoSink(self) -> QVideoSink:
        return self._sink

    def video_rect(self) -> QRectF:
        size = self._root.property("displaySize")
        return QRectF(
            (self.width() - size.width()) / 2,
            (self.height() - size.height()) / 2,
            size.width(),
            size.height(),
        )

    def set_maximum(self, size: tuple[int, int]) -> None:
        """Применить потолок вывода без изменения исходника и декодера."""
        self._maximum = size
        self._resize_video()

    def present(self, frame: QVideoFrame) -> None:
        """Не вызывать map/toImage: аппаратная поверхность остаётся в Qt."""
        self._source = frame.width(), frame.height()
        self._resize_video()
        self._sink.setVideoFrame(frame)
        if self.software_output:
            # Без RHI VideoOutput не рисует видео; CPU-путь преобразует только отобранный кадр.
            self._image = frame.toImage().scaled(
                QSize(*camera_frame_size(self._source, self._maximum, self._maximum)),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            self._overlay.update()

    def _resize_video(self) -> None:
        dpr = self.devicePixelRatioF()
        tile = max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr))
        render = camera_frame_size(self._source, tile, self._maximum)
        display = camera_fit_size(self._source, tile)
        self._root.setProperty("renderSize", QSize(*render))
        self._root.setProperty(
            "displaySize", QSizeF(display[0] / dpr, display[1] / dpr)
        )

    def clear_video(self) -> None:
        """Освободить последний кадр при остановке потока."""
        if isValid(self._sink):
            self._sink.setVideoFrame(QVideoFrame())
        self._image = QImage()
        self._overlay.update()

    def set_overlay(
        self,
        *,
        caption: str,
        recordingText: str,
        recordingActive: bool,
        stale: bool,
        statusMessage: str,
        font: QFont,
    ) -> None:
        """Обновить подписи без изменения размеров видеовыхода."""
        current = (
            self.caption,
            self.recording_text,
            self.recording_active,
            self.stale,
            self.status_message,
            self.font(),
        )
        incoming = (caption, recordingText, recordingActive, stale, statusMessage, font)
        if current == incoming:
            return
        self.caption = caption
        self.recording_text = recordingText
        self.recording_active = recordingActive
        self.stale = stale
        self.status_message = statusMessage
        self.setFont(font)
        self._root.setProperty("videoOpacity", 0.4 if stale else 1.0)
        self._overlay.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._resize_video()
