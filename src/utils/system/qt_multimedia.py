"""Настройка декодирования и безопасная остановка QtMultimedia."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtMultimedia import QMediaPlayer

_configured = False


def configure_video_decoding(*, software: bool | None = None) -> None:
    """Зафиксировать настройку декодирования, сохранив явный выбор окружения.

    software=True отключает аппаратное декодирование для диагностического
    запуска. Политика фиксируется один раз, до создания плееров.
    """
    global _configured
    if _configured:
        return
    from src.utils.system.media_runtime import configure_media_runtime

    configure_media_runtime()
    from src.utils.system.camera_decoding import camera_decoding

    decoding = camera_decoding()
    _configured = True
    if software is True:
        os.environ["QT_FFMPEG_DECODING_HW_DEVICE_TYPES"] = ","
    else:
        os.environ.setdefault(
            "QT_FFMPEG_DECODING_HW_DEVICE_TYPES", decoding.device_type or ","
        )


def stop_media_player(player: QMediaPlayer) -> None:
    """Остановить плеер в его потоке, освободив GIL на время вызова Qt.

    Прямая обёртка QMediaPlayer.stop в проверенной PySide6 6.11 удерживает GIL.
    Аудиорендерер Qt может ждать Python-обработчик сигнала, а stop — сам
    рендерер. Обёртка invokeMethod освобождает GIL; DirectConnection
    сохраняет синхронный порядок остановки и смены источника.
    """
    from PySide6.QtCore import QMetaObject, Qt, QThread

    if player.thread() != QThread.currentThread():
        raise RuntimeError("Остановка плеера должна выполняться в его потоке.")
    if not QMetaObject.invokeMethod(player, "stop", Qt.ConnectionType.DirectConnection):
        raise RuntimeError("Не удалось вызвать остановку плеера Qt.")
