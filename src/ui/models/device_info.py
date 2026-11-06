"""Ленивая табличная модель: строки не создают тысячи отдельных QWidget/items."""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QBrush, QColor

from src.domain.device_info import InfoCell

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
EMPTY_INDEX = QModelIndex()


class DeviceInfoTableModel(QAbstractTableModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.columns: tuple[str, ...] = ("Свойство", "Значение")
        self.rows: tuple[tuple[InfoCell, ...], ...] = ()
        self.query = ""
        self.sort_keys: tuple[tuple[tuple[int, str | float | int], ...], ...] = ()

    def set_rows(
        self, columns: tuple[str, ...], rows: tuple[tuple[InfoCell, ...], ...]
    ) -> None:
        self.beginResetModel()
        self.columns, self.rows = columns, rows
        self.sort_keys = tuple(
            tuple(self._sort_key(cell.value) for cell in row) for row in rows
        )
        self.endResetModel()

    @staticmethod
    def _sort_key(value: object) -> tuple[int, str | float | int]:
        if value is None:
            return 2, ""
        if isinstance(value, (int, float)):
            return 0, value
        return 1, str(value).casefold()

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex = EMPTY_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex = EMPTY_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if (
            not index.isValid()
            or index.row() >= len(self.rows)
            or index.column() >= len(self.columns)
        ):
            return None
        cell = self.rows[index.row()][index.column()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return cell.display
        if role == SORT_ROLE:
            return cell.value
        if (
            role == Qt.ItemDataRole.BackgroundRole
            and self.query
            and self.query.casefold() in cell.display.casefold()
        ):
            return QBrush(QColor(220, 180, 40, 65))
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal and section < len(self.columns):
                return self.columns[section]
            return section + 1
        return None


class DeviceInfoFilter(QSortFilterProxyModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.setFilterKeyColumn(-1)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setSortRole(SORT_ROLE)

    def lessThan(
        self,
        left: QModelIndex | QPersistentModelIndex,
        right: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        source = self.sourceModel()
        if isinstance(source, DeviceInfoTableModel):
            return (
                source.sort_keys[left.row()][left.column()]
                < source.sort_keys[right.row()][right.column()]
            )
        return super().lessThan(left, right)
