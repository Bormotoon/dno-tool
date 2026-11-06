"""Загрузка набора команд с сервера вне GUI-потока."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal


class CommandDownloadWorker(QThread):
    """Скачать ``commands.json``; итог передаётся сигналом ``ready``, если нет отмены."""

    ready = Signal(object)

    def run(self) -> None:
        from src.services.updater import download_commands_json

        try:
            result = download_commands_json()
        except Exception:  # noqa: BLE001 - граница потока: любой сбой показывается как отказ
            result = {"success": False, "message": "Не удалось загрузить команды"}
        if not self.isInterruptionRequested():
            self.ready.emit(result)
