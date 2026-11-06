"""Диалог управления списком известных SSH-хостов (known_hosts)."""

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
)
from PySide6.QtCore import Qt
import os
from src.utils.filesystem.fs_utils import ensure_user_owned


class KnownHostsDialog(QDialog):
    def __init__(self, known_hosts_path, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Список known_hosts")
        self.resize(800, 400)
        self.known_hosts_path = known_hosts_path
        self._init_ui()
        self.load_known_hosts()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Хост", "Тип ключа", "Ключ"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

        btns = QHBoxLayout()
        self.delete_btn = QPushButton("Удалить выбранную")
        self.delete_btn.clicked.connect(self.delete_selected)
        self.clear_btn = QPushButton("Очистить всё")
        self.clear_btn.clicked.connect(self.clear_all)
        btns.addWidget(self.delete_btn)
        btns.addWidget(self.clear_btn)
        btns.addStretch()
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.close)
        self.close_btn.setDefault(True)
        btns.addWidget(self.close_btn, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addLayout(btns)

        # Растягиваем третий столбец
        self.table.horizontalHeader().setStretchLastSection(True)

    def load_known_hosts(self):
        self.table.setRowCount(0)
        if not os.path.exists(self.known_hosts_path):
            return
        with open(self.known_hosts_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        self.table.setRowCount(len(lines))
        for i, line in enumerate(lines):
            # known_hosts: hostnames keytype keydata [optional comment]
            parts = line.split()
            if len(parts) >= 3:
                host = parts[0]
                key_type = parts[1]
                key = parts[2]
            else:
                host = key_type = key = ""
            self.table.setItem(i, 0, QTableWidgetItem(host))
            self.table.setItem(i, 1, QTableWidgetItem(key_type))
            self.table.setItem(i, 2, QTableWidgetItem(key))

    def delete_selected(self):
        if (
            QMessageBox.question(
                self,
                "Подтверждение удаления",
                "Вы уверены, что хотите удалить данную запись?",
                QMessageBox.Yes | QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        row = self.table.currentRow()
        if row < 0:
            return
        self.table.removeRow(row)
        self.save_known_hosts()

    def clear_all(self):
        if (
            QMessageBox.question(
                self,
                "Подтверждение удаления",
                "Вы уверены, что хотите очистить список?",
                QMessageBox.Yes | QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        self.table.setRowCount(0)
        self.save_known_hosts()

    def save_known_hosts(self):
        lines = []
        for row in range(self.table.rowCount()):
            host_item = self.table.item(row, 0)
            keytype_item = self.table.item(row, 1)
            key_item = self.table.item(row, 2)
            if host_item and keytype_item and key_item:
                lines.append(
                    f"{host_item.text()} {keytype_item.text()} {key_item.text()}"
                )
        with open(self.known_hosts_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        ensure_user_owned(self.known_hosts_path)
