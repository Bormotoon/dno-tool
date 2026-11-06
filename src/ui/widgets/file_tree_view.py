"""QTreeView с поддержкой drag & drop для SFTP-командера.

Поддерживаемые сценарии D&D:
  - Межпанельный (локальный <-> удалённый): загрузка/скачивание.
  - Внутрипанельный: переименование/перемещение.
  - Внешний (из Windows Explorer в локальную панель): импорт файлов.
"""

from PySide6.QtCore import QByteArray, QMimeData, Qt, Signal
from PySide6.QtGui import QDrag, QMouseEvent
from PySide6.QtWidgets import QAbstractItemView, QTreeView

MIME_DNO_TOOL_SFTP = "application/x-dno-tool-sftp-items"


class FileTreeView(QTreeView):
    """Tree view с drag & drop.

    Сигнал ``externalDrop(urls, target_path)`` эмитируется при drop внешних URL.
    Сигнал ``internalDrop(source_is_remote, items_json, target_path)`` — при drop
    кастомного MIME из другой/той же панели.
    """

    externalDrop = Signal(list, str)
    internalDrop = Signal(bool, str, str)

    def __init__(self, is_remote: bool, parent=None):
        super().__init__(parent)
        self.is_remote = is_remote
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setUniformRowHeights(True)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setDefaultDropAction(Qt.CopyAction)

    # ------------------------------------------------------------------
    # Источник drag
    # ------------------------------------------------------------------

    def startDrag(self, supportedActions):
        model = self.model()
        if model is None:
            return
        indexes = self.selectionModel().selectedRows(0)
        if not indexes:
            return
        source = self._map_to_source(indexes)
        items_data = []
        for idx in source:
            name_item = (
                model.sourceModel().itemFromIndex(idx)
                if hasattr(model, "sourceModel")
                else model.itemFromIndex(idx)
            )
            if name_item is None:
                continue
            name = name_item.text()
            if name == "..":
                continue
            is_dir = bool(name_item.data(Qt.UserRole + 2))
            panel = self._panel()
            if panel is None:
                continue
            items_data.append(
                {"name": name, "is_dir": is_dir, "path": panel._join_path(name)}
            )

        import json

        payload = json.dumps(
            {
                "is_remote": self.is_remote,
                "session_id": self._panel().dialog.session_id,
                "items": items_data,
            }
        )

        mime = QMimeData()
        mime.setData(MIME_DNO_TOOL_SFTP, QByteArray(payload.encode("utf-8")))

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction if not self.is_remote else Qt.CopyAction)

    def _map_to_source(self, indexes):
        model = self.model()
        if hasattr(model, "mapToSource"):
            return [model.mapToSource(i) for i in indexes]
        return list(indexes)

    # ------------------------------------------------------------------
    # Приёмник drop
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME_DNO_TOOL_SFTP) or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(MIME_DNO_TOOL_SFTP) or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()
        target_path = self._drop_target_path(event)

        if mime.hasUrls() and not mime.hasFormat(MIME_DNO_TOOL_SFTP):
            urls = [u for u in mime.urls() if u.isLocalFile()]
            if urls:
                event.acceptProposedAction()
                paths = [u.toLocalFile() for u in urls]
                self.externalDrop.emit(paths, target_path)
                return

        if mime.hasFormat(MIME_DNO_TOOL_SFTP):
            import json

            payload = bytes(mime.data(MIME_DNO_TOOL_SFTP)).decode("utf-8", "replace")
            try:
                data = json.loads(payload)
            except Exception:
                return
            source_is_remote = bool(data.get("is_remote", False))
            event.acceptProposedAction()
            self.internalDrop.emit(source_is_remote, payload, target_path)
            return

        super().dropEvent(event)

    def _drop_target_path(self, event) -> str:
        """Определить путь-цель drop: каталог под курсором или текущий каталог панели."""
        panel = self._panel()
        if panel is None:
            return ""
        index = self.indexAt(
            event.position().toPoint() if hasattr(event, "position") else event.pos()
        )
        if index.isValid():
            source_model = self.model()
            src_index = (
                source_model.mapToSource(index)
                if hasattr(source_model, "mapToSource")
                else index
            )
            sm = (
                source_model.sourceModel()
                if hasattr(source_model, "sourceModel")
                else source_model
            )
            item = sm.itemFromIndex(src_index)
            if item is not None and item.text() != "..":
                is_dir = bool(item.data(Qt.UserRole + 2))
                if is_dir:
                    return panel._join_path(item.text())
        return panel.current_path

    def _panel(self):
        """Найти FilePanel-родитель."""
        p = self.parent()
        while p is not None:
            if hasattr(p, "current_path") and hasattr(p, "is_remote"):
                return p
            p = p.parent()
        return None

    # ------------------------------------------------------------------
    # Сигнал потери фокуса для переключения активной панели
    # ------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent):
        super().mousePressEvent(event)
        p = self._panel()
        if p is not None and event.button() == Qt.LeftButton:
            try:
                p.activated.emit()
            except Exception:
                pass
