"""Выбор команды из commands.json для кнопки панели."""

from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QDialogButtonBox,
)


def _item_path(item: QTreeWidgetItem | None) -> str:
    """Полный путь элемента от корня для поиска и _path в selected."""
    parts: list[str] = []
    walk = item
    while walk is not None:
        parts.append(walk.text(0))
        walk = walk.parent()
    return " / ".join(reversed(parts))


class CommandPickerDialog(QDialog):
    """Дерево команд с поиском. Возвращает task dict или None."""

    def __init__(self, commands_data, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Выбор команды")
        self.resize(520, 480)
        self.selected = None
        self._tasks_by_iid: dict[str, dict] = {}
        try:
            from src.ui.icons import _icon_from_key

            self._folder_icon = _icon_from_key("folder")
            self._command_icon = _icon_from_key("command")
        except Exception:
            self._folder_icon = None
            self._command_icon = None

        layout = QVBoxLayout(self)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Поиск команды...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setIconSize(QSize(20, 20))
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        layout.addWidget(self.tree)

        self._build_tree(commands_data or [])
        self.tree.expandAll()
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _build_tree(self, items: list) -> None:
        """Построить иерархию: папка - folder, команда - command."""
        self.tree.clear()
        self._tasks_by_iid.clear()
        self._populate(None, items if isinstance(items, list) else [])

    def _populate(self, parent: QTreeWidgetItem | None, items: list) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            if "name" in item and "commands" in item:
                name = str(item.get("name", "Без имени"))
                node = QTreeWidgetItem([name])
                if self._command_icon is not None:
                    node.setIcon(0, self._command_icon)
                node.setData(0, Qt.ItemDataRole.UserRole, item.get("iid", ""))
                iid = str(item.get("iid", "") or "")
                if iid:
                    self._tasks_by_iid[iid] = item
                if parent is None:
                    self.tree.addTopLevelItem(node)
                else:
                    parent.addChild(node)
                continue
            if len(item) == 1:
                folder_name = str(list(item.keys())[0])
                children = item[folder_name]
                if not isinstance(children, list):
                    continue
                node = QTreeWidgetItem([folder_name])
                if self._folder_icon is not None:
                    node.setIcon(0, self._folder_icon)
                node.setData(0, Qt.ItemDataRole.UserRole, "")
                if parent is None:
                    self.tree.addTopLevelItem(node)
                else:
                    parent.addChild(node)
                self._populate(node, children)

    def _apply_filter(self, text: str):
        """Скрыть несовпавшие строки через setRowHidden, папка видна если
        совпала сама или хотя бы один потомок (как в редакторе команд)."""
        query = (text or "").casefold().strip()
        if not query:

            def unhide(item: QTreeWidgetItem | None) -> None:
                count = (
                    self.tree.topLevelItemCount() if item is None else item.childCount()
                )
                for row in range(count):
                    child = (
                        self.tree.topLevelItem(row) if item is None else item.child(row)
                    )
                    self.tree.setRowHidden(
                        row, self.tree.indexFromItem(child).parent(), False
                    )
                    unhide(child)

            unhide(None)
            return

        def walk(item: QTreeWidgetItem | None) -> bool:
            count = self.tree.topLevelItemCount() if item is None else item.childCount()
            any_visible = False
            for row in range(count):
                child = self.tree.topLevelItem(row) if item is None else item.child(row)
                child_matched = walk(child)
                path = _item_path(child)
                matched = query in path.casefold() or child_matched
                parent_idx = self.tree.indexFromItem(child).parent()
                self.tree.setRowHidden(row, parent_idx, not matched)
                if matched:
                    any_visible = True
                    if child_matched:
                        child.setExpanded(True)
            return any_visible

        walk(None)
        current = self.tree.currentItem()
        if current is not None:
            idx = self.tree.indexFromItem(current)
            if self.tree.isRowHidden(idx.row(), idx.parent()):
                self.tree.setCurrentItem(None)

    def _on_double_clicked(self, item: QTreeWidgetItem, _col: int):
        """Двойной клик по папке - раскрыть, по команде - выбрать."""
        if not item.data(0, Qt.ItemDataRole.UserRole):
            item.setExpanded(not item.isExpanded())
            return
        self.accept()

    def accept(self):
        item = self.tree.currentItem()
        if item is None:
            super().reject()
            return
        iid = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
        if not iid:
            return
        task = self._tasks_by_iid.get(iid)
        if task is None:
            super().reject()
            return
        self.selected = dict(task)
        self.selected["_path"] = _item_path(item)
        super().accept()
