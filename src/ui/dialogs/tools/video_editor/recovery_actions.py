"""Планирование recovery и восстановление аварийно закрытого монтажа."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QTimer, Slot
from PySide6.QtWidgets import QInputDialog, QMessageBox

from src.domain.video.models import new_iid
from src.workers.video_editor.recovery_worker import RecoveryWorker


class VideoRecoveryMixin:
    def _init_recovery_state(self) -> None:
        self._recovery_session = new_iid()
        self._recovery_root = str(Path.home() / ".dno-tool" / "video-recovery")
        self._recovery_thread: RecoveryWorker | None = None
        self._recovery_pending: tuple | None = None
        self._recovery_result: list | None = None
        self._recovery_catalog: list[dict] = []
        self._recovery_was_dirty = False
        self._recovery_restored_path: str | None = None
        self._project_recovery_source: str | None = None
        self._recovery_timer = QTimer(self)
        self._recovery_timer.setSingleShot(True)
        self._recovery_timer.setInterval(750)
        self._recovery_timer.timeout.connect(self._flush_recovery)
        self._recovery_deadline = QTimer(self)
        self._recovery_deadline.setSingleShot(True)
        self._recovery_deadline.setInterval(5000)
        self._recovery_deadline.timeout.connect(self._flush_recovery)

    def _schedule_recovery(self, dirty: bool) -> None:
        if self._closing:
            return
        if dirty:
            self._recovery_was_dirty = True
            self._recovery_timer.start()
            if not self._recovery_deadline.isActive():
                self._recovery_deadline.start()
        elif self._recovery_was_dirty:
            self._recovery_was_dirty = False
            self._recovery_timer.stop()
            self._recovery_deadline.stop()
            self._request_recovery("clear", discard=self._recovery_restored_path)
            self._recovery_restored_path = None

    @Slot()
    def _flush_recovery(self) -> None:
        if self._closing or not self.is_project_dirty():
            return
        snapshot = self._take_snapshot()
        self._recovery_timer.stop()
        self._recovery_deadline.stop()
        self._request_recovery("write", snapshot)

    def _request_recovery(
        self, operation: str, snapshot: dict | None = None, discard: str | None = None
    ) -> None:
        # Ожидающая операция заменяется актуальной; активная всегда доходит
        # до конца, затем clear/close гарантированно идут после записи.
        self._recovery_pending = (operation, snapshot, discard)
        self._pump_recovery()

    def _pump_recovery(self) -> None:
        if self._recovery_thread is not None or self._recovery_pending is None:
            return
        operation, snapshot, discard = self._recovery_pending
        self._recovery_pending = None
        self._recovery_result = None
        worker = RecoveryWorker(
            self._recovery_root,
            self._recovery_session,
            operation,
            snapshot,
            discard,
            self,
        )
        self._recovery_thread = worker
        worker.result.connect(self._on_recovery_result)
        worker.error.connect(self._on_recovery_error)
        worker.finished.connect(self._on_recovery_stopped)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @Slot(object)
    def _on_recovery_result(self, result: list) -> None:
        self._recovery_result = result

    @Slot(str)
    def _on_recovery_error(self, error: str) -> None:
        if not self._closing:
            self._recovery_label.setText("Ошибка автовосстановления")
            self._recovery_label.setToolTip(error)

    @Slot()
    def _on_recovery_stopped(self) -> None:
        result, self._recovery_result = self._recovery_result, None
        worker = self._recovery_thread
        if worker.success and worker.discard:
            self._recovery_catalog = [
                entry
                for entry in self._recovery_catalog
                if entry["path"] != worker.discard
            ]
            self._recovery_action.setEnabled(bool(self._recovery_catalog))
        if worker.success and worker.operation == "write" and not self._closing:
            self._recovery_label.clear()
            self._recovery_label.setToolTip("")
        self._recovery_thread = None
        self._pump_recovery()
        if result is not None and not self._closing:
            self._recovery_catalog = result
            self._recovery_action.setEnabled(bool(result))
            if (
                result
                and self.isVisible()
                and not self.is_project_dirty()
                and self._project_io_thread is None
            ):
                self.offer_recovery()

    @Slot()
    def offer_recovery(self) -> None:
        if (
            self._closing
            or self._project_io_thread is not None
            or not self._recovery_catalog
        ):
            return
        entries = self._recovery_catalog
        if len(entries) == 1:
            entry = entries[0]
            stamp = time.strftime("%d.%m.%Y %H:%M", time.localtime(entry["modified"]))
            choice = QMessageBox.question(
                self,
                "Восстановление монтажа",
                f"Найден несохранённый проект «{entry['name']}» от {stamp}. Восстановить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if choice != QMessageBox.StandardButton.Yes:
                return
        else:
            labels = [
                f"{time.strftime('%d.%m.%Y %H:%M', time.localtime(entry['modified']))} — {entry['name']} ({i + 1})"
                for i, entry in enumerate(entries)
            ]
            label, accepted = QInputDialog.getItem(
                self, "Восстановление монтажа", "Проект:", labels, 0, False
            )
            if not accepted:
                return
            entry = entries[labels.index(label)]

        def restore() -> None:
            self._project_recovery_source = entry["path"]
            self._start_project_io("load", entry["path"])

        self._guard_project_action(restore)

    def _close_recovery(self) -> None:
        self._recovery_timer.stop()
        self._recovery_deadline.stop()
        self._request_recovery("close", discard=self._recovery_restored_path)
