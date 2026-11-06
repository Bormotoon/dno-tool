"""Редактор значений defaults в .policy на удалённом хосте через SSH."""

from copy import deepcopy
from html import escape

from PySide6.QtCore import Qt, QSortFilterProxyModel, QTimer, QModelIndex, Slot
from PySide6.QtGui import QStandardItemModel, QStandardItem, QCloseEvent
from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QSplitter, QTreeView,
    QLabel, QPushButton, QMessageBox, QComboBox, QLineEdit, QHeaderView,
    QWidget, QFormLayout, QGroupBox, QProgressBar, QScrollArea,
    QDialogButtonBox, QTextEdit, QLayout,
)

from src.ui.icons import _icon_from_key
from src.domain.models.device import DeviceModel
from src.utils.security.polkit_policy import PERMISSION_VALUES, PolkitPolicy
from src.workers.polkit import PolkitLoadThread, PolkitSaveThread

PERM_KEYS = [
    ("allow_any", "allow_any (любой клиент)"),
    ("allow_inactive", "allow_inactive (неактивная локальная сессия)"),
    ("allow_active", "allow_active (активная локальная сессия)"),
]


class PolkitEditorDialog(QDialog):
    """Диалог для просмотра и редактирования политик Polkit на удалённом хосте"""

    def __init__(self, device: DeviceModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.device = device
        self.policies_data: dict[str, dict] = {}
        self._load_thread: PolkitLoadThread | None = None
        self._save_thread: PolkitSaveThread | None = None
        self._closing = False
        self._close_result = int(QDialog.DialogCode.Rejected)
        self._drafts: dict[str, dict] = {}
        self._current_action_id: str | None = None
        self.combo_boxes: dict[str, QComboBox] = {}

        self.setWindowTitle(f"Редактор политик Polkit — {device.name}")
        self.resize(900, 600)

        self._init_ui()
        self._load_policies()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        top_layout = QHBoxLayout()

        search_label = QLabel("🔍 Поиск:")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Фильтр по имени политики...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._filter_policies)

        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.setIcon(_icon_from_key("menu_refresh"))
        self.refresh_btn.clicked.connect(self._load_policies)

        top_layout.addWidget(search_label)
        top_layout.addWidget(self.search_edit, 1)
        top_layout.addWidget(self.refresh_btn)
        layout.addLayout(top_layout)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        # Левый столбец: список политик
        policy_widget = QWidget()
        policy_layout = QVBoxLayout(policy_widget)
        policy_layout.setContentsMargins(0, 0, 0, 0)
        policy_layout.addWidget(QLabel("<b>Политики</b>"))

        self.policy_model = QStandardItemModel(self)
        self.policy_model.setHorizontalHeaderLabels(["Политика"])

        self.policy_proxy = QSortFilterProxyModel(self)
        self.policy_proxy.setSourceModel(self.policy_model)
        self.policy_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.policy_proxy.setFilterKeyColumn(0)

        self.policy_view = QTreeView()
        self.policy_view.setModel(self.policy_proxy)
        self.policy_view.setSortingEnabled(True)
        self.policy_view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.policy_view.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.policy_view.setSelectionMode(QTreeView.SelectionMode.SingleSelection)
        self.policy_view.setUniformRowHeights(True)
        self.policy_view.header().setStretchLastSection(True)
        self.policy_view.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.policy_view.selectionModel().currentChanged.connect(
            self._on_policy_selected
        )

        policy_layout.addWidget(self.policy_view)
        self.splitter.addWidget(policy_widget)

        # Правый столбец: детали и редактирование
        self.right_widget = QWidget()
        self.right_layout = QVBoxLayout(self.right_widget)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        self.right_layout.setSpacing(0)

        self.right_splitter = QSplitter(Qt.Orientation.Vertical)

        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.detail_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self.detail_container = QWidget()
        self.detail_layout = QVBoxLayout(self.detail_container)
        self.detail_layout.setContentsMargins(4, 4, 4, 4)
        self.detail_layout.setSpacing(8)
        self.detail_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.placeholder_label = QLabel("Выберите политику из списка слева")
        self.placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_layout.addWidget(self.placeholder_label)

        self.detail_scroll.setWidget(self.detail_container)
        self.right_splitter.addWidget(self.detail_scroll)

        # Команда использует тот же механизм записи, что и кнопка сохранения.
        command_container = QWidget()
        command_layout = QVBoxLayout(command_container)
        command_layout.setContentsMargins(0, 10, 0, 0)
        command_layout.setSpacing(4)

        command_label = QLabel("Команда для выбранного действия (на хосте нужен python3):")
        command_layout.addWidget(command_label)

        command_row = QHBoxLayout()
        self.command_edit = QTextEdit()
        self.command_edit.setReadOnly(True)
        self.command_edit.setPlaceholderText(
            "Измените права доступа для формирования команды"
        )
        command_row.addWidget(self.command_edit, 1)

        self.command_copy_btn = QPushButton("📋")
        self.command_copy_btn.setToolTip("Копировать команду в буфер обмена")
        self.command_copy_btn.setFixedWidth(36)
        self.command_copy_btn.clicked.connect(self._copy_command)
        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(self._reset_copy_button)
        command_row.addWidget(self.command_copy_btn)
        command_layout.addLayout(command_row)

        self.right_splitter.addWidget(command_container)
        self.right_splitter.setStretchFactor(0, 1)
        self.right_splitter.setStretchFactor(1, 0)

        # Начальные пропорции панели деталей и команды.
        self.right_splitter.setSizes([500, 120])

        self.right_layout.addWidget(self.right_splitter)

        self.splitter.addWidget(self.right_widget)
        self.splitter.setSizes([380, 720])
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)

        layout.addWidget(self.splitter, 1)

        bottom_bar = QHBoxLayout()

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setFixedWidth(200)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        bottom_bar.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setMinimumWidth(200)
        self.status_label.setWordWrap(True)
        bottom_bar.addWidget(self.status_label, 1)

        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setIcon(_icon_from_key("menu_save"))
        self.save_btn.clicked.connect(self._save_current_policy)
        self.save_btn.setMinimumHeight(28)
        self.save_btn.setVisible(False)
        bottom_bar.addWidget(self.save_btn)

        btn_box = QDialogButtonBox(QDialogButtonBox.Close)
        btn_box.rejected.connect(self.reject)
        bottom_bar.addWidget(btn_box)

        layout.addLayout(bottom_bar)
        notice = QLabel(
            "Редактируются значения defaults в пакетных .policy. Обновление пакета может их заменить. "
            "Правила .rules могут менять итоговое решение Polkit. При записи создаётся резервная копия."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)

    def _clear_detail_widgets(self) -> None:
        while self.detail_layout.count():
            item = self.detail_layout.takeAt(0)
            w = item.widget()
            if w and w is not self.placeholder_label:
                w.deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())
        self.detail_layout.setSpacing(8)

    def _clear_layout(self, layout: QLayout) -> None:
        for i in reversed(range(layout.count())):
            item = layout.itemAt(i)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    def _show_policy_details(self, action_id: str) -> None:
        data = self.policies_data.get(action_id, {})
        self._current_action_id = action_id
        self.combo_boxes = {}

        self._clear_detail_widgets()
        self.placeholder_label.setVisible(False)

        title_label = QLabel(f"<h3>{escape(action_id)}</h3>")
        title_label.setWordWrap(True)
        self.detail_layout.addWidget(title_label)

        description = data.get("description", "")
        message = data.get("message", "")

        if description:
            desc_label = QLabel(f"<b>Описание:</b> {escape(description)}")
            desc_label.setWordWrap(True)
            self.detail_layout.addWidget(desc_label)

        if message:
            msg_label = QLabel(f"<b>Сообщение:</b> {escape(message)}")
            msg_label.setWordWrap(True)
            self.detail_layout.addWidget(msg_label)

        # Идентификатор
        id_label = QLabel(f"<b>Идентификатор:</b> <code>{escape(action_id)}</code>")
        id_label.setWordWrap(True)
        self.detail_layout.addWidget(id_label)

        # Поставщик
        vendor = data.get("vendor", "")
        vendor_url = data.get("vendor_url", "")
        if vendor or vendor_url:
            info_group = QGroupBox("Информация")
            info_layout = QFormLayout(info_group)
            if vendor:
                vendor_label = QLabel(vendor)
                vendor_label.setTextFormat(Qt.TextFormat.PlainText)
                vendor_label.setWordWrap(True)
                info_layout.addRow("Поставщик:", vendor_label)
            if vendor_url:
                url_label = QLabel(vendor_url)
                url_label.setTextFormat(Qt.TextFormat.PlainText)
                url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                url_label.setWordWrap(True)
                info_layout.addRow("URL:", url_label)
            self.detail_layout.addWidget(info_group)

        # Права доступа — редактируемые комбобоксы
        source_label = QLabel(f"Файл: {data.get('_source_file', '')}")
        source_label.setTextFormat(Qt.TextFormat.PlainText)
        source_label.setWordWrap(True)
        source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_layout.addWidget(source_label)
        defaults = self._drafts.get(action_id, data.get("defaults", {}))
        perm_group = QGroupBox("Права доступа")
        perm_layout = QFormLayout(perm_group)
        perm_layout.setSpacing(8)
        perm_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        for key, label in PERM_KEYS:
            current_value = defaults.get(key)
            combo = QComboBox()
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(20)

            combo.addItem("Не задано в XML", None)
            for val, display in PERMISSION_VALUES.items():
                combo.addItem(display, val)

            idx = combo.findData(current_value)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.addItem(current_value, current_value)
                combo.setCurrentIndex(combo.count() - 1)

            self.combo_boxes[key] = combo
            perm_layout.addRow(label, combo)

        self.detail_layout.addWidget(perm_group)

        # Аннотации
        annotations = data.get("annotations", {})
        if annotations:
            ann_group = QGroupBox("Аннотации")
            ann_layout = QFormLayout(ann_group)
            for key, value in annotations.items():
                key_label = QLabel(key)
                key_label.setTextFormat(Qt.TextFormat.PlainText)
                value_label = QLabel(value)
                value_label.setTextFormat(Qt.TextFormat.PlainText)
                value_label.setWordWrap(True)
                ann_layout.addRow(key_label, value_label)
            self.detail_layout.addWidget(ann_group)

        self.save_btn.setVisible(True)
        for combo in self.combo_boxes.values():
            combo.currentIndexChanged.connect(self._update_command)
        self._update_command()

    def _current_defaults(self) -> dict[str, str]:
        return {key: combo.currentData() for key, combo in self.combo_boxes.items()
                if combo.currentData() is not None}

    def _generate_command(self) -> str:
        action_id = self._current_action_id
        if action_id not in self._drafts:
            return ""
        data = self.policies_data[action_id]
        command = PolkitPolicy.update_command(
            data["_source_file"], action_id, self._drafts[action_id], data["defaults"],
        )
        # В терминале sudo само запросит пароль; секреты в команду не входят.
        return f"sudo -- {command}"

    def _update_command(self) -> None:
        action_id = self._current_action_id
        if action_id in self.policies_data:
            defaults = self._current_defaults()
            if defaults != self.policies_data[action_id]["defaults"]:
                self._drafts[action_id] = defaults
            else:
                self._drafts.pop(action_id, None)
        self.command_edit.setPlainText(self._generate_command())
        self._sync_busy_state()

    def _copy_command(self) -> None:
        command = self._generate_command()
        if command:
            QApplication.clipboard().setText(command)
            self.command_copy_btn.setText("✓")
            self._copy_timer.start(1200)

    def _reset_copy_button(self) -> None:
        self.command_copy_btn.setText("📋")

    def _sync_busy_state(self) -> None:
        busy = self._load_thread is not None or self._save_thread is not None or self._closing
        self.refresh_btn.setEnabled(not busy)
        self.splitter.setEnabled(not busy)
        self.search_edit.setEnabled(not busy)
        self.save_btn.setEnabled(not busy and self._current_action_id in self._drafts)
        self.command_copy_btn.setEnabled(not busy and bool(self.command_edit.toPlainText()))
        self.save_btn.setText("Сохранение..." if self._save_thread is not None else "Сохранить")

    def _confirm_discard(self, ignored_action: str | None = None) -> bool:
        count = sum(action_id != ignored_action for action_id in self._drafts)
        if not count:
            return True
        return QMessageBox.warning(
            self, "Несохранённые изменения",
            f"Есть изменения в {count} политик(ах). Отбросить их?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) == QMessageBox.StandardButton.Discard

    def _save_current_policy(self) -> None:
        action_id = self._current_action_id
        if (action_id not in self._drafts or self._load_thread is not None
                or self._save_thread is not None or self._closing):
            return
        data = deepcopy(self.policies_data[action_id])
        data["defaults"] = dict(self._drafts[action_id])
        self.status_label.setText(f"Сохранение политики {action_id}...")
        self._save_thread = PolkitSaveThread(
            self.device, action_id, data, self.policies_data[action_id]["defaults"], self,
        )
        self._save_thread.result.connect(self._on_policy_saved)
        self._save_thread.finished.connect(self._on_save_finished)
        self._sync_busy_state()
        self._save_thread.start()

    def _load_policies(self) -> None:
        if (self._load_thread is not None or self._save_thread is not None
                or self._closing or not self._confirm_discard()):
            return
        self._drafts.clear()
        self.policies_data.clear()
        self._current_action_id = None
        self.combo_boxes = {}
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.status_label.setText("Подключение...")
        self.policy_model.clear()
        self.policy_model.setHorizontalHeaderLabels(["Политика"])
        self._clear_detail_widgets()
        self.detail_layout.addWidget(self.placeholder_label)
        self.placeholder_label.setVisible(True)
        self.save_btn.setVisible(False)
        self.command_edit.clear()
        self._load_thread = PolkitLoadThread(self.device, self)
        self._load_thread.progress.connect(self._on_load_progress)
        self._load_thread.result.connect(self._on_policies_loaded)
        self._load_thread.finished.connect(self._on_load_finished)
        self._sync_busy_state()
        self._load_thread.start()

    @Slot(int, str)
    def _on_load_progress(self, percent: int, message: str) -> None:
        if not self._closing:
            self.progress_bar.setValue(percent)
            self.status_label.setText(message)

    @Slot(dict, str)
    def _on_policies_loaded(self, policies: dict, error: str) -> None:
        if self._closing:
            return
        self.progress_bar.setVisible(False)
        self.policies_data = policies
        self._populate_policy_list()
        self.status_label.setText(f"Политик: {len(policies)}" + (". Есть ошибки загрузки" if error else ""))
        if error:
            if policies:
                QMessageBox.warning(self, "Часть политик не загружена", error)
            else:
                QMessageBox.critical(self, "Ошибка загрузки", error)

    @Slot()
    def _on_load_finished(self) -> None:
        thread, self._load_thread = self._load_thread, None
        if thread is not None:
            thread.deleteLater()
        self.progress_bar.setVisible(False)
        self._sync_busy_state()
        self._finish_close()

    def _populate_policy_list(self) -> None:
        self.policy_model.clear()
        self.policy_model.setHorizontalHeaderLabels(["Политика"])
        icon = _icon_from_key("menu_polkit")
        for action_id in sorted(self.policies_data):
            item = QStandardItem(action_id)
            item.setData(action_id, Qt.ItemDataRole.UserRole)
            item.setEditable(False)
            item.setIcon(icon)
            item.setToolTip(self.policies_data[action_id].get("description", ""))
            self.policy_model.appendRow(item)

    def _filter_policies(self, text: str) -> None:
        self.policy_proxy.setFilterFixedString(text)

    def _on_policy_selected(self, current: QModelIndex, previous: QModelIndex) -> None:
        if not current.isValid():
            return
        item = self.policy_model.itemFromIndex(self.policy_proxy.mapToSource(current))
        if item is not None:
            action_id = item.data(Qt.ItemDataRole.UserRole)
            if action_id in self.policies_data and action_id != self._current_action_id:
                self._show_policy_details(action_id)

    @Slot(bool, str)
    def _on_policy_saved(self, success: bool, message: str) -> None:
        thread = self._save_thread
        if thread is None:
            return
        if success:
            self.policies_data[thread.action_id] = thread.data
            self._drafts.pop(thread.action_id, None)
            if not self._closing:
                if thread.action_id == self._current_action_id:
                    self._show_policy_details(thread.action_id)
                self.status_label.setText(message)
                QMessageBox.information(self, "Успешно", message)
        else:
            # Ошибка записи отменяет отложенное закрытие, чтобы черновик не потерялся.
            self._closing = False
            self.status_label.setText(f"Ошибка: {message}")
            self._sync_busy_state()
            QMessageBox.critical(self, "Ошибка сохранения", message)

    @Slot()
    def _on_save_finished(self) -> None:
        thread, self._save_thread = self._save_thread, None
        if thread is not None:
            thread.deleteLater()
        self._sync_busy_state()
        self._finish_close()

    def _request_close(self, result: int) -> None:
        if not self._closing:
            saving = self._save_thread.action_id if self._save_thread is not None else None
            if not self._confirm_discard(saving):
                return
            self._closing = True
            self._close_result = result
            self._copy_timer.stop()
            if self._load_thread is not None:
                self._load_thread.abort()
            if self._load_thread is not None or self._save_thread is not None:
                self.status_label.setText("Ожидание завершения операции перед закрытием...")
            self._sync_busy_state()
        self._finish_close()

    def _finish_close(self) -> None:
        # Ссылки освобождаются только после настоящего QThread.finished.
        if self._closing and self._load_thread is None and self._save_thread is None:
            super().done(self._close_result)

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self._request_close(int(QDialog.DialogCode.Rejected))

    def accept(self) -> None:
        self.done(int(QDialog.DialogCode.Accepted))

    def reject(self) -> None:
        self.done(int(QDialog.DialogCode.Rejected))

    def done(self, result: int) -> None:
        self._request_close(result)
