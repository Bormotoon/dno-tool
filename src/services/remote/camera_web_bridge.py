"""Передача веб-команд общему менеджеру записи в GUI-потоке."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from PySide6.QtCore import QObject, Qt, Signal, Slot

from src.logger import logger

if TYPE_CHECKING:
    from .camera_recording import CameraRecordingManager
    from .camera_service import CameraService

RecordingAction = Literal["state", "start", "stop_all"]


@dataclass(frozen=True)
class RecordingRequest:
    """Запрос без QObject и виджетов, принадлежащих вызывающему потоку."""

    action: RecordingAction
    iids: tuple[str, ...]
    future: Future[dict[str, Any]]


class CameraWebBridge(QObject):
    """Ограниченная очередь команд; создание и вызов менеджера только в GUI."""

    requested = Signal(object)

    def __init__(self, service: CameraService, parent: QObject) -> None:
        super().__init__(parent)
        self._service = service
        self._slots = threading.BoundedSemaphore(32)
        self.requested.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)

    def submit(
        self, action: RecordingAction, iids: tuple[str, ...] = ()
    ) -> Future[dict[str, Any]]:
        """Поставить операцию в очередь без ожидания GUI в HTTP-потоке."""
        if not self._slots.acquire(blocking=False):
            raise RuntimeError("Очередь команд записи занята")
        future: Future[dict[str, Any]] = Future()
        try:
            self.requested.emit(RecordingRequest(action, iids, future))
        except RuntimeError:
            self._slots.release()
            raise
        return future

    @Slot(object)
    def _dispatch(self, request: RecordingRequest) -> None:
        """Не выполнять отменённую команду, ожидавшую занятого GUI."""
        try:
            if not request.future.set_running_or_notify_cancel():
                return
            manager = self._service.recording_manager()
            results: list[dict[str, Any]] = []
            if request.action == "start":
                if any(
                    iid not in manager.records or manager.records[iid].removed
                    for iid in request.iids
                ):
                    raise ValueError("Камера не найдена в каталоге")
                for iid in dict.fromkeys(request.iids):
                    record = manager.records[iid]
                    try:
                        if not (record.worker or record.intent):
                            manager.start(iid)
                        if record.status == "error":
                            raise ValueError("Не удалось начать запись")
                        results.append({"iid": iid, "success": True})
                    except ValueError as error:
                        results.append(
                            {"iid": iid, "success": False, "message": str(error)}
                        )
            elif request.action == "stop_all":
                manager.stop_all()
            result = self._snapshot(manager)
            result["results"] = results
            request.future.set_result(result)
        except ValueError as error:
            request.future.set_exception(error)
        except Exception:  # noqa: BLE001
            # Исключения мультимедиа могут содержать RTSP-пароли и пути записи.
            logger.error("Не удалось выполнить веб-команду записи камер")
            request.future.set_exception(RuntimeError("Сервис записи недоступен"))
        finally:
            self._slots.release()

    @staticmethod
    def _snapshot(manager: CameraRecordingManager) -> dict[str, Any]:
        """Передавать только состояния, без путей, URL и учётных данных."""
        return {
            "ready": not manager._shutdown,
            "records": [
                {
                    "iid": iid,
                    "status": record.status,
                    "online": record.online,
                    "active": bool(record.worker or record.intent),
                    "elapsed": max(
                        0,
                        int(
                            time.time() - record.started
                            if record.worker and record.started
                            else record.elapsed
                        ),
                    ),
                }
                for iid, record in manager.records.items()
                if not record.removed or record.worker or record.intent
            ],
        }
