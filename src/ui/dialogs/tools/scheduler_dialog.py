from __future__ import annotations

from typing import TYPE_CHECKING
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
)

from src.services.commands.scheduler_service import ScheduleRule, TaskSchedulerService

if TYPE_CHECKING:
    from src.views.main_window import MainWindow

DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


class SchedulerDialog(QDialog):
    """Диалог управления расписанием задач и автоматического питания."""

    def __init__(self, scheduler_service: TaskSchedulerService, parent: "MainWindow" = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Расписание задач и питания")
        self.resize(750, 420)
        self._svc = scheduler_service
        self._rules = self._svc.get_rules()

        layout = QVBoxLayout(self)

        self._table = QTableWidget(self)
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels([
            "Вкл",
            "Название",
            "Дни",
            "Время",
            "Действие",
            "Последний запуск",
        ])
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(self._table)

        btn_layout = QHBoxLayout()
        self._add_btn = QPushButton("Добавить правило", self)
        self._add_btn.clicked.connect(self._add_rule)
        btn_layout.addWidget(self._add_btn)

        self._del_btn = QPushButton("Удалить", self)
        self._del_btn.clicked.connect(self._del_rule)
        btn_layout.addWidget(self._del_btn)

        btn_layout.addStretch()

        self._close_btn = QPushButton("Закрыть", self)
        self._close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self._close_btn)
        layout.addLayout(btn_layout)

        self._refresh_table()

    def _refresh_table(self) -> None:
        self._table.setRowCount(len(self._rules))
        for row, rule in enumerate(self._rules):
            chk_item = QTableWidgetItem()
            chk_item.setCheckState(Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked)
            self._table.setItem(row, 0, chk_item)

            self._table.setItem(row, 1, QTableWidgetItem(rule.name))

            days_str = ", ".join(DAY_NAMES[d] for d in sorted(rule.days_of_week) if 0 <= d <= 6)
            self._table.setItem(row, 2, QTableWidgetItem(days_str))

            self._table.setItem(row, 3, QTableWidgetItem(rule.time_hhmm))

            action_desc = f"{rule.action_type}:{rule.action_id}"
            if rule.action_id == "powerdown":
                action_desc = "Выключение ПК (powerdown)"
            elif rule.action_id == "power_on":
                action_desc = "Включение Wake-on-LAN"
            elif rule.action_id == "reboot":
                action_desc = "Перезагрузка ПК"
            self._table.setItem(row, 4, QTableWidgetItem(action_desc))

            self._table.setItem(row, 5, QTableWidgetItem(rule.last_run_date or "Никогда"))

        self._table.itemChanged.connect(self._on_item_changed)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            row = item.row()
            if 0 <= row < len(self._rules):
                self._rules[row].enabled = (item.checkState() == Qt.CheckState.Checked)
                self._svc.set_rules(self._rules)

    def _add_rule(self) -> None:
        name, ok = QInputDialog.getText(self, "Новое правило", "Название правила:")
        if not ok or not name.strip():
            return
        rule = ScheduleRule(
            name=name.strip(),
            enabled=True,
            days_of_week=[0, 1, 2, 3, 4],
            time_hhmm="19:30",
            action_type="feature",
            action_id="powerdown",
        )
        self._rules.append(rule)
        self._svc.set_rules(self._rules)
        self._table.itemChanged.disconnect(self._on_item_changed)
        self._refresh_table()

    def _del_rule(self) -> None:
        row = self._table.currentRow()
        if 0 <= row < len(self._rules):
            del self._rules[row]
            self._svc.set_rules(self._rules)
            self._table.itemChanged.disconnect(self._on_item_changed)
            self._refresh_table()
