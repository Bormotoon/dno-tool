"""Диалог выбора камеры ЕЦХД с деревом и фильтром."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.domain.models.camera import CameraModel
from src.ui.icons import _icon_from_key


class CameraSelectionDialog(QDialog):
    """Фильтровать полный путь и IP, разрешая выбрать только камеру."""

    def __init__(
        self, cameras: list[CameraModel], iid: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Выбор камеры ЕЦХД")
        self.resize(600, 480)
        self.setMinimumSize(440, 320)
        self.setSizeGripEnabled(True)
        self._items: dict[str, QTreeWidgetItem] = {}
        self._search: dict[str, str] = {}
        self._groups: dict[tuple[str, ...], QTreeWidgetItem] = {}
        self._expanded: dict[tuple[str, ...], bool] | None = None
        layout = QVBoxLayout(self)
        self._filter = QLineEdit(self)
        self._filter.setPlaceholderText("Фильтр: адрес, кабинет, камера или IP")
        self._filter.setClearButtonEnabled(True)
        self._filter.setAccessibleName("Фильтр камер")
        layout.addWidget(self._filter)
        self._tree = QTreeWidget(self)
        self._tree.setHeaderLabels(["Камера", "Адрес RTSP"])
        self._tree.setUniformRowHeights(True)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        layout.addWidget(self._tree, 1)
        self._empty = QLabel("Камеры не найдены", self)
        self._empty.hide()
        layout.addWidget(self._empty)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Выбрать")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)
        for camera in cameras:
            address = (camera.address,)
            cabinet = (camera.address, camera.cabinet)
            for key in (address, cabinet):
                if key not in self._groups:
                    parent_item = self._tree if len(key) == 1 else self._groups[address]
                    node = QTreeWidgetItem(parent_item, [key[-1]])
                    node.setIcon(0, _icon_from_key("folder"))
                    self._groups[key] = node
            item = QTreeWidgetItem(
                self._groups[cabinet], [camera.camera_name, camera.ip_address]
            )
            item.setIcon(0, _icon_from_key("camera"))
            item.setToolTip(0, camera.caption)
            item.setData(0, Qt.ItemDataRole.UserRole, camera.identifier)
            self._items[camera.identifier] = item
            self._search[camera.identifier] = (
                f"{camera.caption} {camera.ip_address}".casefold()
            )
        self._tree.currentItemChanged.connect(self._selection_changed)
        self._tree.itemActivated.connect(self._activate)
        self._filter.textChanged.connect(self._apply_filter)
        selected = self._items.get(iid)
        if selected is not None:
            ancestor = selected.parent()
            while ancestor is not None:
                ancestor.setExpanded(True)
                ancestor = ancestor.parent()
            self._tree.setCurrentItem(selected)
            self._tree.scrollToItem(selected)
        else:
            for key, group in self._groups.items():
                group.setExpanded(len(key) == 1)
        self._empty.setVisible(not cameras)
        self._selection_changed()
        self._filter.setFocus()

    def selected_iid(self) -> str | None:
        item = self._tree.currentItem()
        if item is None or item.isHidden():
            return None
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _selection_changed(self) -> None:
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self.selected_iid() is not None
        )

    def _activate(self, item: QTreeWidgetItem, column: int) -> None:
        if item.data(0, Qt.ItemDataRole.UserRole) is not None:
            self._tree.setCurrentItem(item)
            self.accept()

    def accept(self) -> None:
        if self.selected_iid() is not None:
            super().accept()

    def _apply_filter(self, text: str) -> None:
        terms = text.casefold().split()
        if terms and self._expanded is None:
            self._expanded = {
                key: item.isExpanded() for key, item in self._groups.items()
            }
        visible = []
        for iid, item in self._items.items():
            matches = all(term in self._search[iid] for term in terms)
            item.setHidden(not matches)
            if matches:
                visible.append(item)
        for key in sorted(self._groups, key=len, reverse=True):
            group = self._groups[key]
            group.setHidden(
                all(group.child(i).isHidden() for i in range(group.childCount()))
            )
            if terms and not group.isHidden():
                group.setExpanded(True)
            elif not terms and self._expanded is not None:
                group.setExpanded(self._expanded.get(key, False))
        if not terms:
            self._expanded = None
        if self.selected_iid() is None and visible:
            self._tree.setCurrentItem(visible[0])
        self._empty.setVisible(not visible)
        self._selection_changed()
