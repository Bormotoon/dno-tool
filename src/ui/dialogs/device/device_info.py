"""Представление системной информации; сбором и поколениями владеет координатор."""

from __future__ import annotations

import csv
import io
import time
from datetime import datetime
from typing import Any

from PySide6.QtCore import QByteArray, QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QResizeEvent, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QTextBrowser,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from src.domain.device_identity import device_identity
from src.domain.device_info import (
    STATUS_LABELS,
    DeviceInfoSnapshot,
    InfoCell,
    InfoStatus,
)
from src.domain.models.device import DeviceModel
from src.logger import logger
from src.services.hosts.device_info import DeviceInfoService, get_device_info_service
from src.services.hosts.device_info.baseline_store import HardwareBaselineStore
from src.services.hosts.device_info.catalog import (
    CATEGORY_ICONS,
    INFO_CATEGORIES,
    TREE_GROUPS,
)
from src.services.hosts.device_info.export import serialize_report, write_report
from src.ui.dialogs.device.device_info_controller import DeviceInfoController
from src.ui.icons import _icon_from_key
from src.ui.models.device_info import DeviceInfoFilter, DeviceInfoTableModel


class DeviceInfoDialog(QDialog):
    def __init__(
        self,
        device: DeviceModel,
        parent: Any = None,
        node_fetch: bool = False,
        service: DeviceInfoService | None = None,
    ) -> None:
        super().__init__(parent)
        self.device = device
        self.service = service or get_device_info_service()
        self.controller = DeviceInfoController(device, self.service, self)
        self._cache_request_id = (
            self.controller.snapshot.request_id
            if self.controller.snapshot is not None
            else ""
        )
        self._closing = False
        self._completion_code: int = QDialog.DialogCode.Rejected
        self._node_mode = device_identity(device)[0] != "local"
        self._last_render: object = None
        self._sorts: dict[str, tuple[int, Qt.SortOrder]] = {}
        self._shown_key = ""
        self._category_items: dict[str, QTreeWidgetItem] = {}
        self.baseline_store = HardwareBaselineStore()
        self.setWindowTitle(f"Информация об устройстве — {device.name}")
        self.setSizeGripEnabled(True)
        self._column_fit_timer = QTimer(self)
        self._column_fit_timer.setSingleShot(True)
        self._column_fit_timer.timeout.connect(self._fit_columns)
        self._setup_ui()
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(50)
        self._render_timer.timeout.connect(self._render)
        self.controller.changed.connect(self._schedule_render)
        self.controller.ended.connect(self._worker_ended)
        self.controller.progress_changed.connect(self._progress)
        self._start_timer = QTimer(self)
        self._start_timer.setSingleShot(True)
        self._start_timer.setInterval(100)
        self._start_timer.timeout.connect(self._start_fetch)
        self._restore_geometry()
        self._render()
        self._start_timer.start()

    @property
    def worker(self) -> Any:
        return self.controller.worker

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel(f"{self.device.name} — {self.device.host}")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        search_bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по свойствам и значениям…")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Поиск информации об устройстве")
        self.search.textChanged.connect(self._search_changed)
        search_bar.addWidget(self.search, 1)
        self.everywhere = QCheckBox("Во всех разделах")
        self.everywhere.toggled.connect(self._search_changed)
        search_bar.addWidget(self.everywhere)
        self.previous_btn = QAction("Предыдущий снимок (устарел)", self)
        self.previous_btn.setCheckable(True)
        self.previous_btn.toggled.connect(self._schedule_render)
        layout.addLayout(search_bar)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAccessibleName("Разделы информации об устройстве")
        self.tree.setMinimumWidth(120)
        self.tree.currentItemChanged.connect(self._selection_changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.splitter.addWidget(self.tree)
        self.table = QTableView()
        self.model = DeviceInfoTableModel(self)
        self.proxy = DeviceInfoFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table.setModel(self.proxy)
        self.table.setAccessibleName("Данные выбранного раздела")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._copy_menu)
        self.table.verticalHeader().hide()
        self.table.viewport().installEventFilter(self)
        self.splitter.addWidget(self.table)
        layout.addWidget(self.splitter, 1)
        self.category_note = QLabel()
        self.category_note.setTextFormat(Qt.TextFormat.PlainText)
        self.category_note.setWordWrap(True)
        note_bar = QHBoxLayout()
        note_bar.addWidget(self.category_note, 1)
        self.baseline_warning_btn = QPushButton("⚠️ Отличия от эталона")
        self.baseline_warning_btn.setStyleSheet("color: #d9534f; font-weight: bold;")
        self.baseline_warning_btn.hide()
        self.baseline_warning_btn.clicked.connect(self._show_baseline_diff_dialog)
        note_bar.addWidget(self.baseline_warning_btn)
        self.details_btn = QToolButton()
        self.details_btn.setText("Диагностика")
        self.details_btn.setCheckable(True)
        note_bar.addWidget(self.details_btn)
        layout.addLayout(note_bar)
        self.details = QTextBrowser()
        self.details.setMaximumHeight(120)
        self.details.hide()
        self.details_btn.toggled.connect(self.details.setVisible)
        layout.addWidget(self.details)
        bottom = QHBoxLayout()
        self.status_progress = QProgressBar()
        self.status_progress.setMaximumWidth(150)
        bottom.addWidget(self.status_progress)
        self.status_label = QLabel("Сведения ещё не запрашивались")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        bottom.addWidget(self.status_label, 1)
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.clicked.connect(self._refresh_or_stop)
        bottom.addWidget(self.refresh_btn)
        self.baseline_btn = QPushButton("Зафиксировать как эталон")
        self.baseline_btn.clicked.connect(self._save_baseline)
        bottom.addWidget(self.baseline_btn)
        self.save_btn = QPushButton("Сохранить отчёт…")
        self.save_btn.setIcon(_icon_from_key("menu_save"))
        self.save_btn.clicked.connect(self._save_report)
        bottom.addWidget(self.save_btn)
        close_btn = QPushButton("Закрыть")
        close_btn.clicked.connect(self.reject)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)
        self._populate_tree()
        for key, action in (
            ("F5", self._start_fetch),
            ("Ctrl+F", self.search.setFocus),
            ("Ctrl+C", self._copy_selection),
            ("Ctrl+S", self._save_report),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(action)
        self.setTabOrder(self.tree, self.table)
        self.setTabOrder(self.table, self.search)

    def _populate_tree(self) -> None:
        summary = QTreeWidgetItem(self.tree, ["Сводка"])
        summary.setData(0, Qt.ItemDataRole.UserRole, "__summary__")
        for group, names in TREE_GROUPS.items():
            parent = QTreeWidgetItem(self.tree, [group])
            parent.setData(0, Qt.ItemDataRole.UserRole, f"__group__:{group}")
            parent.setToolTip(0, group)
            for name in names:
                item = QTreeWidgetItem(parent, [name])
                item.setData(0, Qt.ItemDataRole.UserRole, name)
                item.setIcon(0, _icon_from_key(CATEGORY_ICONS[name]))
                item.setToolTip(0, name)
                self._category_items[name] = item
        self.tree.expandAll()
        self.tree.setCurrentItem(summary)

    def _key(self) -> str:
        item = self.tree.currentItem()
        return (
            str(item.data(0, Qt.ItemDataRole.UserRole))
            if item is not None
            else "__summary__"
        )

    def _owner_name(self) -> str:
        if not self._node_mode:
            return "Локальный экземпляр"
        from src.di import get_container
        from src.services import NodeService

        service = get_container().resolve_optional(NodeService)
        return str(
            service.get_display_name(self.device.node_iid)
            if service
            else self.device.node_iid
        )

    def _device_is_current(self) -> bool:
        parent = self.parent()
        tree = getattr(parent, "treeview", None)
        if tree is None:
            return True
        item = tree._find_tree_item_by_iid(
            self.device.iid, node_iid=getattr(self.device, "node_iid", None)
        )
        if item is None:
            return False
        current = getattr(item, "device", None)
        return current is not None and self.service.signature(
            current
        ) == self.service.signature(self.device)

    def _start_fetch(self) -> None:
        if self._closing or self.worker is not None:
            return
        self._start_timer.stop()
        if not self._device_is_current():
            self.status_label.setText(
                "Устройство удалено или изменено; откройте карточку заново"
            )
            return
        self.previous_btn.setChecked(False)
        names = self.service.names_for_profile("full")
        self.controller.start("full", names, self._owner_name())
        self._render()

    def _refresh_or_stop(self) -> None:
        if self.worker is not None:
            self._stop_fetch()
        else:
            self._start_fetch()

    def _tree_menu(self, point: Any) -> None:
        item = self.tree.itemAt(point)
        if item is not None:
            self.tree.setCurrentItem(item)
        menu = QMenu(self)
        refresh = menu.addAction("Обновить раздел", self._refresh_category)
        if refresh is not None:
            refresh.setEnabled(self.worker is None and self._key() in INFO_CATEGORIES)
        menu.addAction("Сравнить снимки…", self._compare)
        menu.addAction(self.previous_btn)
        menu.exec(self.tree.viewport().mapToGlobal(point))

    def _refresh_category(self) -> None:
        key = self._key()
        if (
            key in INFO_CATEGORIES
            and self.worker is None
            and not self._closing
            and self._device_is_current()
        ):
            self._start_timer.stop()
            self.previous_btn.setChecked(False)
            self.controller.start("section", (key,), self._owner_name())
            self._render()

    def _stop_fetch(self) -> None:
        self._start_timer.stop()
        self.controller.stop()

    def _progress(self, done: int, total: int, message: str) -> None:
        if self._closing:
            return
        self.status_progress.setRange(0, total)
        self.status_progress.setValue(min(done, max(0, total - 1)))
        self.status_label.setText(f"{message} ({done}/{total})")

    def _schedule_render(self, *_args: Any) -> None:
        if not hasattr(self, "_render_timer"):
            return
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _selection_changed(self, *_args: Any) -> None:
        self._last_render = None
        if hasattr(self, "_render_timer"):
            self._render()

    def _search_changed(self, *_args: Any) -> None:
        self._last_render = None
        self._schedule_render()

    def _display_snapshot(self) -> DeviceInfoSnapshot | None:
        return (
            self.controller.previous
            if self.previous_btn.isChecked()
            else self.controller.snapshot
        )

    def _summary_rows(
        self, snapshot: DeviceInfoSnapshot | None
    ) -> tuple[tuple[InfoCell, ...], ...]:
        rows = [
            ("Имя устройства", self.device.name),
            ("Хост", self.device.host),
            ("Владелец", self._owner_name()),
            (
                "SSH endpoint",
                (
                    snapshot.endpoint
                    if snapshot
                    else (
                        "Неизвестен (настройки владельца)"
                        if self._node_mode
                        else self.service.snapshot(self.device, "brief", ()).endpoint
                    )
                ),
            ),
            (
                "Последний статус хоста",
                (
                    "Онлайн"
                    if self.device.is_online
                    else "Офлайн (можно проверить вручную)"
                ),
            ),
        ]
        if snapshot:
            rows += [
                ("Источник", snapshot.transport),
                ("Время снимка", snapshot.started_at),
                ("Результат", STATUS_LABELS[snapshot.status]),
                ("Проблемных разделов", str(snapshot.problem_count)),
            ]
            wanted = {
                "Имя хоста",
                "Операционная система",
                "Ядро",
                "Модель процессора",
                "Ядра",
                "Потоки",
                "Общая память",
                "Доступная память",
            }
            for category in snapshot.categories.values():
                if category.columns == ("Свойство", "Значение"):
                    rows.extend(
                        (row[0].display, row[1].display)
                        for row in category.rows
                        if row[0].display in wanted
                    )
            for name in ("Накопители", "Сеть", "Дисковое пространство"):
                summary_category = snapshot.categories.get(name)
                if summary_category is not None:
                    rows.append(
                        (
                            name,
                            f"{len(summary_category.rows)} записей; {STATUS_LABELS[summary_category.status]}",
                        )
                    )
        return tuple(
            (InfoCell(str(key), str(key)), InfoCell(str(value), str(value)))
            for key, value in rows
        )

    def _render(self) -> None:
        if self._closing:
            return
        start = time.monotonic()
        if not self._device_is_current():
            self.controller.invalidate()
            self.status_label.setText(
                "Устройство удалено или изменено; новые данные не применяются"
            )
            return
        snapshot = self._display_snapshot()
        busy = self.worker is not None
        cancelling = (
            busy
            and self.controller.snapshot is not None
            and self.controller.snapshot.status == InfoStatus.CANCELLING
        )
        self.refresh_btn.setText("Остановить" if busy else "Обновить")
        self.refresh_btn.setEnabled(not cancelling)
        self.previous_btn.setEnabled(self.controller.previous is not None)
        self.status_progress.setVisible(busy)
        if snapshot and (not busy or snapshot.status == InfoStatus.CANCELLING):
            self.status_label.setText(
                f"{STATUS_LABELS[snapshot.status]}; разделов {len(snapshot.categories)}/{len(snapshot.expected_categories)}, проблемных: {snapshot.problem_count}"
            )
        if self.previous_btn.isChecked() and snapshot:
            self.status_label.setText(
                f"Устаревший снимок от {snapshot.started_at}; {STATUS_LABELS[snapshot.status]}"
            )
        elif snapshot and snapshot.request_id == self._cache_request_id and not busy:
            freshness = (
                "свежий"
                if self.service.fresh(snapshot, snapshot.expected_categories)
                else "устарел"
            )
            self.status_label.setText(
                f"Кэш от {snapshot.started_at} ({freshness}); {STATUS_LABELS[snapshot.status]}"
            )
        key, query = self._key(), self.search.text()
        for name, item in self._category_items.items():
            category = snapshot.categories.get(name) if snapshot else None
            status = STATUS_LABELS[category.status] if category else "Не запрашивалось"
            label = f"{name} ({len(category.rows) if category else 0})"
            if category is None or category.status != InfoStatus.COMPLETE:
                label += f" — {status}"
            item.setText(0, label)
            item.setToolTip(
                0, f"{name}\n{status}\n{category.diagnostic if category else ''}"
            )
            match = (
                not query
                or query.casefold() in name.casefold()
                or (
                    category is not None
                    and any(
                        query.casefold() in cell.display.casefold()
                        for row in category.rows
                        for cell in row
                    )
                )
            )
            item.setHidden(bool(self.everywhere.isChecked() and not match))
        for i in range(1, self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            if group is not None:
                group.setHidden(
                    all(
                        child.isHidden()
                        for j in range(group.childCount())
                        if (child := group.child(j)) is not None
                    )
                )
        category = snapshot.categories.get(key) if snapshot else None
        columns: tuple[str, ...] = ("Свойство", "Значение")
        rows: tuple[tuple[InfoCell, ...], ...]
        if query and self.everywhere.isChecked() and snapshot:
            columns = ("Раздел", "Свойство / строка", "Значение")
            rows = tuple(
                (
                    InfoCell(name, name),
                    row[0],
                    InfoCell(" | ".join(c.display for c in row[1:])),
                )
                for name, cat in snapshot.categories.items()
                for row in cat.rows
                if query.casefold() in name.casefold()
                or any(query.casefold() in c.display.casefold() for c in row)
            )
            note = "Поиск по всем полученным разделам; сетевые запросы не выполняются"
        elif key == "__summary__":
            rows = self._summary_rows(snapshot)
            note = (
                snapshot.diagnostic
                if snapshot
                else "Нажмите «Обновить» для проверки хоста"
            )
        elif category is not None:
            assert snapshot is not None
            columns, rows = category.columns, category.rows
            note = f"{STATUS_LABELS[category.status]}; источник: {snapshot.owner}; {category.timestamp}. {category.diagnostic}"
        elif key.startswith("__group__:"):
            rows = tuple(
                (
                    InfoCell(name),
                    InfoCell(
                        STATUS_LABELS[snapshot.categories[name].status]
                        if snapshot and name in snapshot.categories
                        else "Не запрашивалось"
                    ),
                )
                for name in TREE_GROUPS.get(key.split(":", 1)[1], [])
            )
            note = "Выберите раздел стрелками или мышью"
        else:
            columns = tuple(
                INFO_CATEGORIES.get(key, {}).get("table_columns") or columns
            )
            rows = ()
            note = (
                "Ожидание сбора раздела…"
                if busy
                else "Данные раздела не получены. Нажмите «Обновить» для повторного сбора."
            )
        render_key = key, columns, rows, query, self.everywhere.isChecked()
        if render_key != self._last_render:
            old_key = self._shown_key
            if old_key:
                header = self.table.horizontalHeader()
                self._sorts[old_key] = (
                    header.sortIndicatorSection(),
                    header.sortIndicatorOrder(),
                )
            selected = [
                (self.proxy.index(index.row(), 0).data(), index.column())
                for index in self.table.selectionModel().selectedIndexes()
            ]
            scroll = self.table.verticalScrollBar().value()
            self.table.setSortingEnabled(False)
            self.proxy.sort(-1)
            self.model.query = query
            self.model.set_rows(columns, rows)
            self.proxy.setFilterFixedString(
                "" if self.everywhere.isChecked() else query
            )
            if key != "__summary__":
                self.table.setSortingEnabled(True)
                col, order = self._sorts.get(key, (0, Qt.SortOrder.AscendingOrder))
                self.table.sortByColumn(min(col, len(columns) - 1), order)
            if old_key == key:
                from PySide6.QtCore import QItemSelectionModel

                selected_keys = set(selected)
                if selected_keys:
                    for row in range(self.proxy.rowCount()):
                        first_value = self.proxy.index(row, 0).data()
                        for col in range(self.proxy.columnCount()):
                            if (first_value, col) in selected_keys:
                                self.table.selectionModel().select(
                                    self.proxy.index(row, col),
                                    QItemSelectionModel.SelectionFlag.Select,
                                )
                self.table.verticalScrollBar().setValue(scroll)
            self._shown_key = key
            self._last_render = render_key
            self._column_fit_timer.start(0)
        self.category_note.setText(note)
        if snapshot:
            self.details.setPlainText(
                "\n".join(
                    [
                        snapshot.diagnostic,
                        f"Запрос: {snapshot.request_id}",
                        f"Метрики: {snapshot.metrics}",
                    ]
                    + [
                        f"{name}: {cat.diagnostic}"
                        for name, cat in snapshot.categories.items()
                        if cat.diagnostic
                    ]
                )
            )
        logger.debug(
            f"DeviceInfo render rows={len(rows)} elapsed_sec={time.monotonic() - start:.4f}"
        )
        if not busy and snapshot:
            self._check_baseline_changes()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.Resize
            and isinstance(event, QResizeEvent)
            and event.size().width() != event.oldSize().width()
            and not self._closing
        ):
            self._column_fit_timer.start(0)
        return super().eventFilter(watched, event)

    def _fit_columns(self) -> None:
        """Подогнать столбцы по содержимому, ограничивая чрезмерно длинные поля."""
        if self._closing:
            return
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setResizeContentsPrecision(200)
        self.table.resizeColumnsToContents()
        available = max(1, self.table.viewport().width())
        for column in range(self.proxy.columnCount()):
            maximum = max(120, int(available * (0.45 if column == 0 else 0.65)))
            if header.sectionSize(column) > maximum:
                header.resizeSection(column, maximum)
        header.setStretchLastSection(True)

    def _worker_ended(self) -> None:
        if self._closing:
            self._finish_close()
        else:
            self._render()
            self._check_baseline_changes()

    def _save_baseline(self) -> None:
        """Зафиксировать текущий снимок конфигурации как эталон."""
        snapshot = self._display_snapshot()
        if not snapshot:
            QMessageBox.warning(self, "Эталон", "Снимок устройства ещё не получен.")
            return
        self.baseline_store.save_baseline(snapshot)
        self.baseline_warning_btn.hide()
        QMessageBox.information(
            self,
            "Эталон сохранён",
            f"Конфигурация устройства '{snapshot.name}' успешно зафиксирована как эталон.",
        )

    def _check_baseline_changes(self) -> None:
        """Проверить текущий снимок на расхождения с эталоном оборудования."""
        snapshot = self._display_snapshot()
        if not snapshot:
            self.baseline_warning_btn.hide()
            return
        diffs = self.baseline_store.diff_against_baseline(snapshot, hardware_only=True)
        if diffs:
            self.baseline_warning_btn.setText(f"⚠️ Отличия от эталона ({len(diffs)})")
            self.baseline_warning_btn.show()
        else:
            self.baseline_warning_btn.hide()

    def _show_baseline_diff_dialog(self) -> None:
        """Показать диалог различий между эталоном и текущим снимком оборудования."""
        snapshot = self._display_snapshot()
        if not snapshot:
            return
        baseline = self.baseline_store.load_baseline(snapshot.device_key)
        if not baseline:
            return
        from src.services.hosts.device_info.compare import compare_snapshots

        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("Отличия от эталонной конфигурации")
        dialog.setSizeGripEnabled(True)
        layout = QVBoxLayout(dialog)
        note = QLabel(
            f"Эталон: {baseline.name}, сохранён {baseline.finished_at or baseline.started_at}\n"
            f"Текущий снимок: {snapshot.name}, получен {snapshot.started_at}"
        )
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setWordWrap(True)
        layout.addWidget(note)
        table = QTableView(dialog)
        model = DeviceInfoTableModel(table)
        diffs = self.baseline_store.diff_against_baseline(snapshot, hardware_only=True)
        model.set_rows(
            ("Раздел", "Поле / строка", "Эталон", "Сейчас", "Изменение"),
            tuple(
                tuple(InfoCell(value, value) for value in row)
                for row in diffs
            ),
        )
        proxy = DeviceInfoFilter(table)
        proxy.setSourceModel(model)
        table.setModel(proxy)
        table.setSortingEnabled(True)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        button = QPushButton("Закрыть", dialog)
        button.clicked.connect(dialog.close)
        layout.addWidget(button)
        available = self.screen().availableGeometry()
        dialog.resize(
            min(1000, available.width() - 20), min(600, available.height() - 40)
        )
        dialog.show()

    def _compare(self) -> None:
        from src.services.hosts.device_info.compare import compare_snapshots

        current = self._display_snapshot()
        if current is None:
            return
        candidates = self.service.available_snapshots()
        if self.controller.previous is not None:
            candidates.insert(0, self.controller.previous)
        candidates = list(
            {
                snapshot.request_id: snapshot
                for snapshot in candidates
                if snapshot.request_id != current.request_id
            }.values()
        )
        if not candidates:
            QMessageBox.information(
                self,
                "Сравнение",
                "Откройте другое устройство или обновите текущий снимок для сравнения с предыдущим.",
            )
            return
        labels = [
            f"{item.name} — {item.owner} — {item.started_at} — {STATUS_LABELS[item.status]} ({item.request_id[:8]})"
            for item in candidates
        ]
        selected, accepted = QInputDialog.getItem(
            self,
            "Сравнить снимки",
            "С чем сравнить текущий снимок:",
            labels,
            editable=False,
        )
        if not accepted:
            return
        previous = candidates[labels.index(selected)]
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("Сравнение снимков информации об устройстве")
        dialog.setSizeGripEnabled(True)
        layout = QVBoxLayout(dialog)
        note = QLabel(
            f"Раньше: {previous.name}, {previous.owner}, {previous.started_at}, {STATUS_LABELS[previous.status]}\nСейчас: {current.name}, {current.owner}, {current.started_at}, {STATUS_LABELS[current.status]}"
        )
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setWordWrap(True)
        layout.addWidget(note)
        table = QTableView(dialog)
        model = DeviceInfoTableModel(table)
        model.set_rows(
            ("Раздел", "Поле / строка", "Раньше", "Сейчас", "Изменение"),
            tuple(
                tuple(InfoCell(value, value) for value in row)
                for row in compare_snapshots(previous, current)
            ),
        )
        proxy = DeviceInfoFilter(table)
        proxy.setSourceModel(model)
        table.setModel(proxy)
        table.setSortingEnabled(True)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        if not model.rows:
            layout.addWidget(QLabel("Различий в полученных данных нет"))
        button = QPushButton("Закрыть", dialog)
        button.clicked.connect(dialog.close)
        layout.addWidget(button)
        available = self.screen().availableGeometry()
        dialog.resize(
            min(1000, available.width() - 20), min(600, available.height() - 40)
        )
        dialog.show()

    def _copy_text(self, rows: list[list[str]]) -> None:
        stream = io.StringIO(newline="")
        csv.writer(stream, delimiter="\t").writerows(rows)
        QApplication.clipboard().setText(stream.getvalue())

    def _copy_selection(self) -> None:
        indexes = self.table.selectionModel().selectedIndexes()
        if not indexes:
            return
        by_row: dict[int, list[Any]] = {}
        for index in sorted(indexes, key=lambda index: (index.row(), index.column())):
            by_row.setdefault(index.row(), []).append(index.data() or "")
        self._copy_text(list(by_row.values()))

    def _copy_rows(self, all_rows: bool = False) -> None:
        rows = (
            range(self.proxy.rowCount())
            if all_rows
            else sorted(
                {index.row() for index in self.table.selectionModel().selectedIndexes()}
            )
        )
        self._copy_text(
            [
                [
                    str(self.proxy.index(row, col).data() or "")
                    for col in range(self.proxy.columnCount())
                ]
                for row in rows
            ]
        )

    def _copy_menu(self, point: Any) -> None:
        menu = QMenu(self)
        menu.addAction("Копировать выделенные ячейки", self._copy_selection)
        menu.addAction("Копировать строки", self._copy_rows)
        menu.addAction("Копировать раздел", lambda: self._copy_rows(True))
        menu.exec(self.table.viewport().mapToGlobal(point))

    def _save_report(self) -> None:
        snapshot = self._display_snapshot() or self.service.snapshot(
            self.device, "brief", ()
        )
        key = self._key()
        scope = QMenu(self)
        all_action = scope.addAction(
            "Все полученные разделы (включая персональные и сетевые сведения)"
        )
        current_action = scope.addAction("Текущий раздел")
        assert current_action is not None
        current_action.setEnabled(key in snapshot.categories)
        scope.addAction("Видимые разделы")
        selected = scope.exec(
            self.save_btn.mapToGlobal(self.save_btn.rect().bottomLeft())
        )
        if selected is None:
            return
        names = (
            None
            if selected == all_action
            else (
                {key}
                if selected == current_action
                else {
                    name
                    for name, item in self._category_items.items()
                    if not item.isHidden()
                }
            )
        )
        formats = "Текст (*.txt);;JSON (*.json)"
        if names is not None and len(names) == 1:
            formats += ";;CSV (*.csv);;TSV (*.tsv)"
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Сохранить отчёт",
            f"device_info_{datetime.now().astimezone():%Y%m%d_%H%M%S}.txt",
            formats,
        )
        if not path:
            return
        try:
            suffix = path.rsplit(".", 1)[-1].lower()
            format = suffix if suffix in {"txt", "json", "csv", "tsv"} else "txt"
            text = serialize_report(snapshot, format=format, names=names)
            write_report(path, text)
            self.status_label.setText(f"Отчёт сохранён: {path}")
        except Exception as error:  # noqa: BLE001
            # В обработку входят и сериализация, и запись.
            QMessageBox.critical(
                self,
                "Ошибка",
                f"Не удалось сохранить отчёт ({type(error).__name__}). Проверьте путь и права записи.",
            )

    def _restore_geometry(self) -> None:
        from src.architecture import ViewState
        from src.di import get_container

        self._view_state = get_container().resolve_optional(ViewState)
        state: dict[str, Any] = {}
        if self._view_state is not None:
            self._view_state.register("device_info", persist=True)
            state = self._view_state.get("device_info") or {}
        available = self.screen().availableGeometry()
        self.setMinimumSize(min(640, available.width()), min(420, available.height()))
        self.resize(
            min(1100, available.width() - 20), min(700, available.height() - 40)
        )
        if state.get("geometry"):
            self.restoreGeometry(
                QByteArray.fromBase64(state["geometry"].encode("ascii"))
            )
        self.resize(
            min(self.width(), available.width()), min(self.height(), available.height())
        )
        rect = self.frameGeometry()
        if not available.contains(rect):
            rect.moveCenter(available.center())
            self.move(rect.topLeft())
        self.splitter.setSizes(state.get("splitter", [240, 760]))

    def _save_geometry(self) -> None:
        if self._view_state is not None:
            data = {
                "geometry": bytes(self.saveGeometry().toBase64().data()).decode(
                    "ascii"
                ),
                "splitter": self.splitter.sizes(),
            }
            self._view_state.update("device_info", data, track_change=False)
            self._view_state._save_persistent("device_info", data)

    def has_pending_workers(self) -> bool:
        return self.worker is not None

    def done(self, result: int) -> None:
        if not self._closing:
            self._closing = True
            self._completion_code = result
            self._start_timer.stop()
            self._render_timer.stop()
            self._column_fit_timer.stop()
            self.controller.close()
        if self.has_pending_workers():
            self.status_label.setText("Ожидание завершения отмены…")
            self.setEnabled(False)
            return
        self._finish_close()

    def _finish_close(self) -> None:
        self._save_geometry()
        super().done(self._completion_code)

    def reject(self) -> None:
        self.done(QDialog.DialogCode.Rejected)

    def accept(self) -> None:
        self.done(QDialog.DialogCode.Accepted)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.done(QDialog.DialogCode.Rejected)
        if self.has_pending_workers():
            event.ignore()
        else:
            event.accept()
