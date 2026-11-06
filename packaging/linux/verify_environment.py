"""Проверить ABI Qt, необходимые модули и декодирование видео через FFmpeg."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_MEDIA_BACKEND"] = "ffmpeg"

import PySide6
import shiboken6
from PySide6.QtCore import QLibraryInfo, QTimer, QUrl, qVersion
from PySide6.QtGui import QGuiApplication
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink


def main() -> None:
    """Дождаться реального видеокадра, а не только успешного импорта."""
    versions = (PySide6.__version__, shiboken6.__version__, qVersion())
    if len(set(versions)) != 1:
        raise RuntimeError(f"Несогласованные версии Qt/PySide6/Shiboken: {versions}")
    if tuple(map(int, versions[0].split(".")[:2])) < (6, 12):
        raise RuntimeError(f"Для сборки требуется Qt/PySide6 не ниже 6.12: {versions}")
    modules = ("Core", "Gui", "Widgets", "Network", "DBus", "OpenGL", "OpenGLWidgets", "Svg", "SvgWidgets", "Qml", "Quick", "QuickWidgets", "Multimedia", "MultimediaWidgets", "Test", "PrintSupport")
    for module in modules:
        importlib.import_module("PySide6.Qt" + module)
    translations = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
    if not (translations / "qtbase_ru.qm").is_file():
        raise RuntimeError("В установленном PySide6 нет qtbase_ru.qm")
    app = QGuiApplication([])
    with tempfile.TemporaryDirectory(prefix="dno-ffmpeg-probe-") as folder:
        video = Path(folder) / "probe.mkv"
        import av

        graph = av.filter.Graph()
        source = graph.add("testsrc", "size=160x120:rate=10:duration=1")
        sink_node = graph.add("buffersink")
        source.link_to(sink_node)
        graph.configure()
        with av.open(str(video), "w") as output:
            stream = output.add_stream("ffv1", rate=10)
            stream.width, stream.height, stream.pix_fmt = 160, 120, "yuv420p"
            for _ in range(10):
                frame = sink_node.pull().reformat(format="yuv420p")
                for packet in stream.encode(frame):
                    output.mux(packet)
            for packet in stream.encode(None):
                output.mux(packet)
        player = QMediaPlayer()
        sink = QVideoSink()
        player.setVideoSink(sink)
        result: dict[str, object] = {"frame": False, "error": None}

        def frame_ready(frame: object) -> None:
            """Завершить пробу только после получения валидного кадра."""
            if frame.isValid():
                result["frame"] = True
                app.quit()

        def failed(error: object, message: str) -> None:
            """Сохранить ошибку бэкенда и завершить цикл событий."""
            result["error"] = message
            app.quit()

        sink.videoFrameChanged.connect(frame_ready)
        player.errorOccurred.connect(failed)
        QTimer.singleShot(15000, app.quit)
        player.setSource(QUrl.fromLocalFile(str(video)))
        player.play()
        app.exec()
        player.stop()
        if not result["frame"] or result["error"]:
            raise RuntimeError(f"FFmpeg не декодировал тестовое видео: {result}")
    print(json.dumps({"qt": versions[0], "modules": modules, "ffmpeg_frame": True, "qtbase_ru": True}))


if __name__ == "__main__":
    main()
