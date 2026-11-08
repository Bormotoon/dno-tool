from __future__ import annotations

from typing import TYPE_CHECKING
from PySide6.QtCore import QObject, Signal

from src.logger import logger
from src.workers.node.client import NodeClientWorker
from src.workers.node.status import get_node_status_manager
from src.workers.node.thumbnails import get_node_thumbnail_manager
from src.ui.widgets.device_tree import DEVICE_NODE_DATAROLE, LOCAL_NODE_IID

if TYPE_CHECKING:
    from src.views.main_window import MainWindow


class NodeTreeController(QObject):
    """Управляет жизненным циклом клиентов нод и синхронизацией их секций с деревом устройств."""

    status_message = Signal(str, str, str, str)  # text, state, hint, mood

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self._win = window

    @property
    def node_service(self):
        return getattr(self._win, "node_service", None)

    @property
    def treeview(self):
        return getattr(self._win, "treeview", None)

    def init_nodes(self) -> None:
        if not self.node_service or not self.treeview:
            return
        if not getattr(self.treeview, "show_nodes", False):
            return
        nodes = [r for r in self.node_service.get_nodes() if getattr(r, "autoconnect", True)]
        for rec in nodes:
            try:
                self.treeview.get_or_create_node_section(rec.iid, rec.name or rec.host)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"NodeTreeController: init section {rec.iid}: {exc}")
            self.start_client(rec)

    def start_client(self, rec) -> None:
        if not self.node_service:
            return
        existing = self.node_service.get_client(rec.iid)
        if existing is not None:
            try:
                if existing.isRunning():
                    return
            except Exception:
                pass
        try:
            client = NodeClientWorker(
                node_iid=rec.iid,
                host=rec.host,
                port=rec.port,
                pubkey=rec.pubkey,
                node_service=self.node_service,
                parent=None,
            )
            client.hosts_updated.connect(self._win._on_node_hosts_updated)
            client.node_online.connect(self._win._on_node_online)
            client.node_name_updated.connect(self._win._on_node_name_updated)
            self.node_service.set_client(rec.iid, client)
            client.start()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"NodeTreeController: не удалось запустить NodeClient {rec.iid}: {exc}")

    def stop_trackers(self, node_iid: str) -> None:
        try:
            get_node_status_manager().stop_node(node_iid)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"stop_node status timers failed: {exc}")
        try:
            get_node_thumbnail_manager().stop_node(node_iid)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"stop_node thumbnail timers failed: {exc}")

    def reload_sections(self) -> None:
        if not self.node_service or not self.treeview:
            return
        show_nodes = bool(getattr(self.treeview, "show_nodes", False))
        if not show_nodes:
            for rec in self.node_service.get_nodes():
                self.node_service.stop_client(rec.iid)
                self.stop_trackers(rec.iid)
            return

        known = {r.iid: r for r in self.node_service.get_nodes()}
        root = self.treeview._model.invisibleRootItem()
        for i in range(root.rowCount() - 1, -1, -1):
            child = root.child(i)
            if child and getattr(child, "is_node_section", False):
                nid = child.data(DEVICE_NODE_DATAROLE) or ""
                if nid == LOCAL_NODE_IID:
                    continue
                rec = known.get(nid)
                if rec is None or not getattr(rec, "autoconnect", True):
                    self.node_service.stop_client(nid)
                    self.stop_trackers(nid)
                    if hasattr(self._win, "listview"):
                        self._win.listview.remove_devices_of_node(nid)
                    self.treeview.remove_node_section(nid)

        for rec in known.values():
            if not getattr(rec, "autoconnect", True):
                self.node_service.stop_client(rec.iid)
                self.stop_trackers(rec.iid)
                continue
            sec = self.treeview.get_or_create_node_section(rec.iid, rec.name or rec.host)
            sec.setText(self.node_service.get_display_name(rec.iid))
            self.start_client(rec)
