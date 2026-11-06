"""Табличная модель настроек и поиск по текущему адресному снимку."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QFont

from src.config.kde_schema import (
    KdeSetting,
    setting_for,
    settings_for_snapshot,
)
from src.domain.kde import KdeKey
from src.ui.dialogs.kde.controller import KdeController

ROOT_INDEX = QModelIndex()


class KdeSettingsModel(QAbstractTableModel):
    """Показывает реальные записи и подтверждённые возможности без записи defaults."""

    def __init__(self, controller: KdeController) -> None:
        super().__init__(controller)
        self.controller = controller
        self.rows: list[KdeSetting] = []

    def reload(self) -> None:
        self.beginResetModel()
        settings: dict[KdeKey, KdeSetting] = {}
        if self.controller.draft:
            snapshot = self.controller.draft.snapshot
            filename = PurePosixPath(snapshot.target.path).name
            settings = {
                definition.key: definition
                for definition in settings_for_snapshot(snapshot)
            }
            for key in snapshot.document.entries:
                settings.setdefault(key, setting_for(filename, key))
            for _, document in snapshot.source_documents:
                for key in document.entries:
                    settings.setdefault(key, setting_for(filename, key))
        self.rows = sorted(
            settings.values(),
            key=lambda definition: (
                definition.category,
                definition.key.group.casefold(),
                definition.title.casefold(),
                definition.key.name,
            ),
        )
        self.endResetModel()

    def refresh(self) -> None:
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, 2))

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = ROOT_INDEX) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex = ROOT_INDEX
    ) -> int:
        return 0 if parent.isValid() else 3

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
        ):
            return ("Параметр", "Локальное значение", "Источник / правило")[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        definition = self.rows[index.row()]
        if self.controller.draft is None:
            if role == Qt.ItemDataRole.UserRole:
                return definition
            if role == Qt.ItemDataRole.ToolTipRole:
                return f"{definition.key.label}\n{definition.description}\nПрофиль пока не прочитан."
            if role == Qt.ItemDataRole.DisplayRole:
                return (definition.title, "Не прочитано", "Значение не загружено")[
                    index.column()
                ]
            return None
        snapshot = self.controller.draft.snapshot
        key = definition.key
        local = self.controller.value(key)
        effective, source, locked = snapshot.policy(key)
        if role == Qt.ItemDataRole.UserRole:
            return definition
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{definition.title}\n{snapshot.target.path}\n{key.label}\n{definition.description}"
        if role == Qt.ItemDataRole.FontRole and (
            key in self.controller.draft.changes or key in self.controller.invalid
        ):
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return definition.title + (
                    " *"
                    if key in self.controller.draft.changes
                    or key in self.controller.invalid
                    else ""
                )
            if index.column() == 1:
                if key in self.controller.invalid:
                    return "Ошибка: " + str(local)
                return self.format_value(definition, local)
            if locked:
                return "Системная блокировка · " + source
            if local is not None:
                return "Личный файл · " + key.group
            return (
                source
                if effective is not None
                else "Наследование; действие не проверено"
            )
        return None

    @staticmethod
    def format_value(definition: KdeSetting, value: str | None) -> str:
        if value is None:
            return "Не задано (наследовать)"
        if definition.kind == "restriction" and value.lower() in ("true", "false"):
            return "Запретить" if value.lower() == "false" else "Разрешить"
        if definition.kind == "bool" and value.lower() in ("true", "false"):
            return "Да" if value.lower() == "true" else "Нет"
        if definition.kind == "enum":
            return next(
                (title for title, stored in definition.choices if stored == value),
                value,
            )
        return value


class KdeSettingsFilter(QSortFilterProxyModel):
    """Поиск не перестраивает поля и не меняет черновик."""

    def __init__(self, controller: KdeController) -> None:
        super().__init__(controller)
        self.controller = controller
        self.query = ""
        self.category = "restrictions"
        self.only_changed = False
        self.only_local = False
        self.only_locked = False
        self.restriction_group = ""

    def filterAcceptsRow(
        self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex
    ) -> bool:
        source = self.sourceModel()
        if not isinstance(source, KdeSettingsModel):
            return False
        definition = source.rows[source_row]
        if (
            not self.query
            and self.category != "expert"
            and definition.category != self.category
        ):
            return False
        if (
            not self.query
            and self.restriction_group
            and definition.key.group != self.restriction_group
        ):
            return False
        if (
            self.only_changed
            and (
                self.controller.draft is None
                or definition.key not in self.controller.draft.changes
            )
            and definition.key not in self.controller.invalid
        ):
            return False
        if self.only_local and self.controller.value(definition.key) is None:
            return False
        if self.only_locked and (
            self.controller.draft is None
            or not self.controller.draft.snapshot.policy(definition.key)[2]
        ):
            return False
        haystack = f"{definition.title} {definition.description} {definition.key.label} {definition.filename}".casefold()
        return all(part in haystack for part in self.query.casefold().split())
