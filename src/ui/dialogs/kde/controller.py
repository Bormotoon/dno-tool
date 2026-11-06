"""Состояние редактора KDE: адресный снимок, независимый черновик и операции."""

from __future__ import annotations

import base64
from pathlib import PurePosixPath
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from src.config.kde_schema import editable_setting
from src.domain.kde import KdeDraft, KdeKey, KdeProfile, KdeSnapshot
from src.services.hosts.kde_service import KdeConnection, KdeOperationError, KdeService
from src.workers.kde.operation import KDEConfigWorker


class KdeController(QObject):
    """Не содержит виджетов; каждый ответ проверяется по контексту запроса."""

    changed = Signal()
    progress = Signal(str)
    error = Signal(str, str)
    saved = Signal()
    conflict_ready = Signal(object)
    preview_ready = Signal(bytes, bool, str)
    idle = Signal()

    def __init__(
        self,
        connection: KdeConnection,
        service: KdeService,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.connection = connection
        self.service = service
        self.profiles: tuple[KdeProfile, ...] = ()
        self.draft: KdeDraft | None = None
        self.invalid: dict[KdeKey, tuple[str | None, str]] = {}
        self.backups: list[dict[str, Any]] = []
        self.ssh_username = ""
        self.version = ""
        self.message = "Подключение ещё не выполнено."
        self.error_code = ""
        self.ready = False
        self.error_details = ""
        self.catalog_filename = "kdeglobals"
        self._worker: KDEConfigWorker | None = None
        self._operation = ""
        self._requested: dict[str, Any] = {}
        self._closing = False
        self._restoring: str | None = None

    @property
    def busy(self) -> bool:
        return self._worker is not None

    @property
    def dirty(self) -> bool:
        return bool(self.invalid or (self.draft and self.draft.dirty))

    def value(self, key: KdeKey) -> str | None:
        if key in self.invalid:
            return self.invalid[key][0]
        return self.draft.value(key) if self.draft else None

    def discover(self) -> None:
        self._start("discover", {"operation": "discover"})

    def clear_profile(self) -> None:
        """Снять выбор профиля после проверки несохранённых правок в представлении."""
        if self.busy:
            return
        self.draft = None
        self.invalid.clear()
        self.backups.clear()
        self.ready = False
        self.error_code = ""
        self.error_details = ""
        self.message = "Выберите пользователя."
        self.changed.emit()

    def load(self, profile: KdeProfile, filename: str = "kdeglobals") -> None:
        self.catalog_filename = filename
        self._start(
            "load",
            {
                "operation": "load",
                "username": profile.username,
                "uid": profile.uid,
                "filename": filename,
            },
        )

    def set_value(self, key: KdeKey, value: str | None) -> None:
        if not self.ready or self.busy or self.draft is None:
            raise ValueError("Дождитесь загрузки выбранного профиля.")
        definition = editable_setting(self.draft.snapshot, key)
        if definition is None:
            raise ValueError(
                "Семантика параметра не подтверждена; доступен только просмотр."
            )
        try:
            entry = self.draft.snapshot.document.entries.get(key)
            if value != (entry.value if entry else None):
                definition.validate(value)
            self.draft.set(key, value)
        except ValueError as error:
            self.invalid[key] = (value, str(error))
        else:
            self.invalid.pop(key, None)
        self.changed.emit()

    def discard(self) -> None:
        if self.draft and not self.busy:
            self.draft = KdeDraft(self.draft.snapshot)
            self.invalid.clear()
            self.changed.emit()

    def save(self) -> bool:
        if (
            not self.ready
            or self.busy
            or self.draft is None
            or self.error_code == "unknown"
        ):
            return False
        if self.invalid:
            self.error.emit(
                "validation", "Исправьте значения полей с ошибками перед сохранением."
            )
            return False
        try:
            request = self.service.save_request(self.connection, self.draft)
        except (KdeOperationError, ValueError) as error:
            self.error.emit("validation", str(error))
            return False
        return self._start("save", request)

    def list_backups(self) -> None:
        if self.draft and self.ready:
            self._start(
                "backups", self.service.target_request(self.draft.snapshot, "backups")
            )

    def preview_backup(self, backup_id: str) -> None:
        if self.draft and self.ready:
            request = self.service.target_request(self.draft.snapshot, "backup_content")
            request["backup_id"] = backup_id
            self._start("backup_content", request)

    def restore(self, backup_id: str) -> None:
        if (
            not self.draft
            or not self.ready
            or self.dirty
            or self.error_code == "unknown"
        ):
            return
        request = self.service.target_request(self.draft.snapshot, "restore")
        request.update(
            {
                "backup_id": backup_id,
                "expected": self.draft.snapshot.fingerprint,
                "policies": {
                    item.path: item.fingerprint for item in self.draft.snapshot.sources
                },
            }
        )
        self._restoring = backup_id
        self._start("restore", request)

    def compare_remote(self) -> None:
        if not self.draft or self.busy:
            return
        request = self.service.target_request(self.draft.snapshot, "load")
        self._start("compare", request)

    def resolve_conflict(self, remote: KdeSnapshot, keep_local: set[KdeKey]) -> None:
        if self.draft is None or remote.target.identity != self.connection.identity:
            return
        before = self.draft
        rebased, conflicts = before.rebase(remote)
        for key in conflicts:
            if key in keep_local:
                rebased.set(key, before.changes[key])
        self.draft = rebased
        self.invalid.clear()
        self.ready = True
        self.error_code = ""
        self.error_details = ""
        self.message = (
            "Версии объединены в черновик. Проверьте изменения перед сохранением."
        )
        self.changed.emit()

    def cancel(self) -> None:
        if self._worker:
            self.message = "Остановка операции…"
            self._worker.abort()
            self.changed.emit()

    def shutdown(self) -> bool:
        self._closing = True
        self.cancel()
        return not self.busy

    def _start(self, operation: str, request: dict[str, Any]) -> bool:
        if self.busy or self._closing:
            return False
        self._operation = operation
        self._requested = dict(request)
        self.error_details = ""
        if operation == "load":
            self.ready = False
        self.error_code = ""
        self.message = "Начинаем операцию…"
        worker = KDEConfigWorker(self.service, self.connection, request, self)
        self._worker = worker
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        self.changed.emit()
        worker.start()
        return True

    @Slot(str)
    def _on_progress(self, message: str) -> None:
        if self.sender() is self._worker:
            self.message = message
            self.progress.emit(message)

    @Slot()
    def _on_finished(self) -> None:
        worker = self._worker
        if worker is None or self.sender() is not worker:
            return
        outcome = worker.outcome
        self._worker = None
        worker.deleteLater()
        if self._closing:
            self.idle.emit()
            return
        if outcome.data is None:
            self.error_code = outcome.code
            self.error_details = outcome.details
            self.message = outcome.message
            if outcome.code == "unknown":
                self.ready = False
            self.error.emit(outcome.code, outcome.message)
        else:
            try:
                self._apply(outcome.data)
            except (ValueError, KeyError, TypeError, KdeOperationError) as error:
                self.ready = False
                self.error_code = (
                    error.code if isinstance(error, KdeOperationError) else "protocol"
                )
                self.message = str(error)
                self.error.emit(
                    "protocol", "Не удалось проверить ответ хоста: " + str(error)
                )
        self.changed.emit()
        self.idle.emit()

    def _apply(self, data: dict[str, Any]) -> None:
        operation = self._operation
        if operation == "discover":
            self.profiles = tuple(
                KdeProfile.from_dict(item) for item in data["profiles"]
            )
            self.ssh_username = str(data.get("ssh_username", ""))
            self.version = str(data.get("version", "")).strip()
            self.message = (
                f"Найдено пользователей: {len(self.profiles)}. Выберите профиль."
            )
            return
        if operation == "backups":
            self.backups = list(data.get("backups", []))
            self.message = f"Резервных копий текущего файла: {len(self.backups)}."
            return
        if operation == "backup_content":
            self.preview_ready.emit(
                base64.b64decode(data["raw"], validate=True),
                bool(data["exists"]),
                str(self._requested["backup_id"]),
            )
            return
        previous = self.draft.snapshot if self.draft else None
        snapshot = self.service.decode_snapshot(
            self.connection, data["snapshot"], previous if operation != "load" else None
        )
        if operation == "load" and (
            snapshot.target.profile.username != self._requested["username"]
            or snapshot.target.profile.uid != self._requested["uid"]
            or PurePosixPath(snapshot.target.path).name != self._requested["filename"]
        ):
            raise KdeOperationError(
                "target", "Ответ не соответствует выбранному пользователю или файлу."
            )
        if operation == "compare":
            self.conflict_ready.emit(snapshot)
            return
        if (
            operation == "save"
            and previous is not None
            and self.draft
            and snapshot.raw != self.draft.render()
        ):
            raise KdeOperationError(
                "unknown", "Проверенный ответ не совпадает с отправленными изменениями."
            )
        self.draft = KdeDraft(snapshot)
        self.invalid.clear()
        self.backups = []
        self.ready = True
        self.version = snapshot.version or self.version
        if operation in ("save", "restore"):
            self.message = "Файл сохранён и проверен. Применение к приложениям может потребовать повторного входа."
            self.saved.emit()
        else:
            self.message = (
                "Файл прочитан."
                if snapshot.exists
                else "Личного файла нет. Меняйте параметры — файл будет создан при сохранении."
            )
