"""Окно «Своя команда»: разовый SSH-скрипт на отмеченных хостах.

Окно только собирает ``CustomCommand``; запуск выполняет главное окно тем же
конвейером, что и задачи из файла. Последний ввод хранится в памяти до выхода
из программы (в скрипте и переменных могут быть пароли), на диск попадает
только размер окна.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.custom_command import (
    MAX_TIMEOUT_SEC,
    MIN_TIMEOUT_SEC,
    CustomCommand,
    format_duration,
)
from src.logger import logger
from src.ui.dialogs.editors.bash_edit import STANDARD_PARAMS, ParamsHelpDialog
from src.ui.icons import _icon_from_key, set_themed_icon
from src.ui.widgets.device_tree import LOCAL_NODE_IID
from src.ui.widgets.script_editor import ScriptEditor

_TIMEOUT_PRESETS = (30, 60, 300, 900, 1800, 3600, 7200, 14400)
# Скрипт крупнее не читается из файла: окно рассчитано на разовые команды.
_MAX_SCRIPT_FILE_BYTES = 1024 * 1024
_EMPTY_SCRIPT_ERROR = "введите текст команды"


class CustomCommandStore:
    """Последний ввод (в памяти) и геометрия окна (``ViewState``)."""

    STATE_ID = "custom_command"

    def __init__(self, view_state) -> None:
        self._view_state = view_state
        self.draft: CustomCommand | None = None
        if view_state is not None:
            view_state.register(self.STATE_ID, persist=True)

    def geometry(self) -> QByteArray | None:
        if self._view_state is None:
            return None
        value = self._view_state.get(self.STATE_ID, "geometry")
        return value if isinstance(value, QByteArray) and not value.isEmpty() else None

    def save_geometry(self, value: QByteArray) -> None:
        if self._view_state is None:
            return
        try:
            self._view_state.set(self.STATE_ID, "geometry", value, track_change=False)
        except Exception as error:  # noqa: BLE001 - размер окна не мешает запуску
            logger.debug(f"CustomCommandStore: геометрия: {error}")


def _is_node_device(device) -> bool:
    nid = getattr(device, "node_iid", None)
    return bool(nid) and nid != LOCAL_NODE_IID


def _device_title(device) -> str:
    name = (getattr(device, "name", "") or "").strip()
    host = (getattr(device, "host", "") or "").strip()
    if name and host and name != host:
        return f"{name} ({host})"
    return name or host or "без имени"


def _plural_hosts(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "хосте"
    return "хостах"


class CustomCommandDialog(QDialog):
    """Ввод скрипта и параметров запуска; результат - ``command()``."""

    def __init__(
        self,
        devices: Sequence,
        store: CustomCommandStore,
        *,
        default_timeout: int = 300,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Своя команда")
        self.setSizeGripEnabled(True)
        self.setMinimumSize(720, 440)
        self._devices = list(devices)
        self._store = store
        self._default_timeout = min(
            max(int(default_timeout or 300), MIN_TIMEOUT_SEC), MAX_TIMEOUT_SEC
        )
        self._build_ui()
        self._connect()
        self._apply(store.draft)
        geometry = store.geometry()
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(1040, 620)
        QTimer.singleShot(0, self.editor.setFocus)

    # ---------- построение ----------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 10)
        layout.setSpacing(8)

        self.menu_bar = QMenuBar(self)
        self.menu_bar.setNativeMenuBar(False)
        layout.setMenuBar(self.menu_bar)

        top = QHBoxLayout()
        top.setSpacing(6)
        hosts_icon = QLabel(self)
        hosts_icon.setPixmap(_icon_from_key("menu_hosts").pixmap(20, 20))
        top.addWidget(hosts_icon)
        self.hosts_label = QLabel(self)
        self.hosts_label.setTextFormat(Qt.TextFormat.PlainText)
        top.addWidget(self.hosts_label, 1)
        layout.addLayout(top)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.script_editor = ScriptEditor(self)
        self.editor = self.script_editor.editor
        self.editor.setPlaceholderText(
            "Команда или скрипт bash, например:\n\n"
            "uptime\n"
            "df -h /\n\n"
            "%hostname%, %name% и другие параметры подставляются для каждого хоста "
            "(меню «Параметры»)."
        )
        self._build_menus()
        self.splitter.addWidget(self.script_editor)
        self.splitter.addWidget(self._build_options())
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([700, 330])
        layout.addWidget(self.splitter, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.message_label = QLabel(self)
        self.message_label.setWordWrap(True)
        bottom.addWidget(self.message_label, 1)
        self.cancel_button = QPushButton("Отмена", self)
        self.cancel_button.setAutoDefault(False)
        bottom.addWidget(self.cancel_button)
        self.run_button = QPushButton("Выполнить", self)
        set_themed_icon(self.run_button, "result_executing")
        self.run_button.setIconSize(QSize(20, 20))
        self.run_button.setDefault(True)
        self.run_button.setToolTip("Выполнить на отмеченных хостах (Ctrl+Enter)")
        bottom.addWidget(self.run_button)
        layout.addLayout(bottom)

        for sequence in ("Ctrl+Return", "Ctrl+Enter"):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(self._on_run)

    def _build_menus(self) -> None:
        file_menu = self.menu_bar.addMenu("📁 Файл")
        self.open_action = QAction("Открыть скрипт…", self)
        set_themed_icon(self.open_action, "menu_edit_file")
        self.open_action.setShortcut(QKeySequence("Ctrl+O"))
        self.open_action.triggered.connect(self._open_script)
        file_menu.addAction(self.open_action)
        self.addAction(self.open_action)

        self.script_editor.attach_menu_bar(self.menu_bar)

        params_menu = self.menu_bar.addMenu("% Параметры")
        standard = params_menu.addMenu("Подставить параметр хоста")
        standard.setIcon(_icon_from_key("menu_file"))
        for param, description in STANDARD_PARAMS:
            action = standard.addAction(f"{param}  —  {description}")
            action.triggered.connect(
                lambda checked=False, value=param: self._insert_param(value)
            )
        params_menu.addSeparator()
        help_action = params_menu.addAction("Справка по параметрам…")
        set_themed_icon(help_action, "menu_help")
        help_action.triggered.connect(lambda: ParamsHelpDialog(self).exec())

    def _build_options(self) -> QWidget:
        panel = QWidget(self)
        panel.setMinimumWidth(290)
        column = QVBoxLayout(panel)
        column.setContentsMargins(6, 0, 0, 0)
        column.setSpacing(8)

        run = QGroupBox("Запуск", panel)
        form = QFormLayout(run)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.environment_combo = QComboBox(run)
        self.environment_combo.addItem(
            _icon_from_key("command_ssh"), "Пользователь SSH", False
        )
        self.environment_combo.addItem(
            _icon_from_key("command_desktop_session"), "Рабочий стол пользователя", True
        )
        self.environment_combo.setToolTip(
            "«Пользователь SSH» - от учётной записи из связки хоста; sudo и локаль "
            "по настройкам SSH.\n"
            "«Рабочий стол пользователя» - от пользователя активного графического "
            "сеанса, с его DISPLAY и DBus: для notify-send, запуска программ и "
            "настроек рабочего стола."
        )
        form.addRow("Окружение:", self.environment_combo)

        timeout_row = QHBoxLayout()
        timeout_row.setSpacing(4)
        self.timeout_spin = QSpinBox(run)
        self.timeout_spin.setRange(MIN_TIMEOUT_SEC, MAX_TIMEOUT_SEC)
        self.timeout_spin.setSuffix(" сек")
        self.timeout_spin.setAccelerated(True)
        self.timeout_spin.setToolTip(
            "Сколько ждать завершения на каждом хосте. По истечении команда "
            "прерывается и считается ошибкой."
        )
        timeout_row.addWidget(self.timeout_spin, 1)
        self.timeout_presets = QToolButton(run)
        self.timeout_presets.setText("▾")
        self.timeout_presets.setToolTip("Выбрать готовое значение")
        self.timeout_presets.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        presets = QMenu(self.timeout_presets)
        for seconds in _TIMEOUT_PRESETS:
            action = presets.addAction(format_duration(seconds))
            action.triggered.connect(
                lambda checked=False, value=seconds: self.timeout_spin.setValue(value)
            )
        self.timeout_presets.setMenu(presets)
        timeout_row.addWidget(self.timeout_presets)
        form.addRow("Таймаут:", timeout_row)
        self.timeout_hint = QLabel(run)
        self.timeout_hint.setStyleSheet("color: gray;")
        form.addRow("", self.timeout_hint)

        self.background_check = QCheckBox("Запустить и не ждать", run)
        set_themed_icon(self.background_check, "command_background")
        self.background_check.setToolTip(
            "Linux: процесс отделяется от SSH и продолжает работу после отключения. "
            "В результате будут PID и путь к журналу stdout/stderr. Отмена задания "
            "после запуска процесс не останавливает."
        )
        form.addRow(self.background_check)
        column.addWidget(run)

        env_box = QGroupBox("Переменные окружения", panel)
        env_layout = QVBoxLayout(env_box)
        env_layout.setSpacing(4)
        self.env_table = QTableWidget(0, 2, env_box)
        self.env_table.setHorizontalHeaderLabels(["Имя", "Значение"])
        header = self.env_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.env_table.setColumnWidth(0, 110)
        self.env_table.verticalHeader().setVisible(False)
        self.env_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.env_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self.env_table.setMinimumHeight(110)
        env_layout.addWidget(self.env_table, 1)
        env_buttons = QHBoxLayout()
        env_buttons.setSpacing(4)
        self.env_add_button = QPushButton("Добавить", env_box)
        self.env_add_button.setAutoDefault(False)
        self.env_remove_button = QPushButton("Удалить", env_box)
        self.env_remove_button.setAutoDefault(False)
        env_buttons.addWidget(self.env_add_button)
        env_buttons.addWidget(self.env_remove_button)
        env_buttons.addStretch(1)
        env_layout.addLayout(env_buttons)
        env_hint = QLabel("Передаются скрипту через export.", env_box)
        env_hint.setWordWrap(True)
        env_hint.setStyleSheet("color: gray;")
        env_layout.addWidget(env_hint)
        column.addWidget(env_box, 1)
        return panel

    def _connect(self) -> None:
        self.editor.textChanged.connect(self._update_state)
        self.environment_combo.currentIndexChanged.connect(lambda *_: self._update_state())
        self.background_check.toggled.connect(lambda *_: self._update_state())
        self.timeout_spin.valueChanged.connect(lambda *_: self._update_state())
        self.env_table.itemChanged.connect(lambda *_: self._update_state())
        self.env_table.itemSelectionChanged.connect(self._update_env_buttons)
        self.env_add_button.clicked.connect(self._add_env_row)
        self.env_remove_button.clicked.connect(self._remove_env_rows)
        self.cancel_button.clicked.connect(self.reject)
        self.run_button.clicked.connect(self._on_run)

    # ---------- состояние ----------

    def command(self) -> CustomCommand:
        """Текущий ввод окна."""
        env: list[tuple[str, str]] = []
        for row in range(self.env_table.rowCount()):
            name_item = self.env_table.item(row, 0)
            value_item = self.env_table.item(row, 1)
            name = (name_item.text() if name_item else "").strip()
            value = value_item.text() if value_item else ""
            if name or value:
                env.append((name, value))
        return CustomCommand(
            script=self.editor.toPlainText(),
            desktop_session=bool(self.environment_combo.currentData()),
            background=self.background_check.isChecked(),
            timeout=self.timeout_spin.value(),
            env=tuple(env),
        )

    def _apply(self, command: CustomCommand | None) -> None:
        if command is None:
            command = CustomCommand(timeout=self._default_timeout)
        self.editor.setPlainText(command.script)
        self.editor.moveCursor(QTextCursor.MoveOperation.End)
        self.environment_combo.setCurrentIndex(1 if command.desktop_session else 0)
        self.background_check.setChecked(command.background)
        self.timeout_spin.setValue(command.timeout)
        self.env_table.blockSignals(True)
        self.env_table.setRowCount(0)
        for name, value in command.env:
            self._append_env_row(name, value)
        self.env_table.blockSignals(False)
        self._update_state()

    def _update_state(self) -> None:
        command = self.command()
        self._update_env_buttons()
        self.timeout_hint.setText(f"= {format_duration(command.timeout)} на хост")
        self._update_hosts_label()

        errors = command.errors()
        if not self._devices:
            errors.insert(0, "нет хостов: отметьте их в дереве")
        self.run_button.setEnabled(not errors)
        # Пустой скрипт - обычное начальное состояние, это не ошибка.
        visible = [error for error in errors if error != _EMPTY_SCRIPT_ERROR]
        if visible:
            self.message_label.setText(visible[0][0].upper() + visible[0][1:])
            self.message_label.setStyleSheet("color: #d84315;")
            return
        self.message_label.setStyleSheet("color: gray;")
        if errors:
            self.message_label.setText("Введите команду, чтобы выполнить её.")
            return
        notes = []
        if command.desktop_session:
            notes.append("от пользователя рабочего стола")
        if command.background:
            notes.append("процесс продолжит работу после отключения SSH")
        self.message_label.setText(
            ("Готово к запуску: " + ", ".join(notes) + ".")
            if notes
            else "Ctrl+Enter - выполнить."
        )

    def _update_hosts_label(self) -> None:
        count = len(self._devices)
        if not count:
            self.hosts_label.setText("Хосты не выбраны - отметьте их в дереве")
            self.hosts_label.setToolTip("")
            return
        if count == 1:
            text = f"Выполнить на хосте {_device_title(self._devices[0])}"
        else:
            names = ", ".join(_device_title(device) for device in self._devices[:3])
            more = f" и ещё {count - 3}" if count > 3 else ""
            text = f"Выполнить на {count} {_plural_hosts(count)}: {names}{more}"
        nodes = sum(1 for device in self._devices if _is_node_device(device))
        if nodes:
            text += f" (через ноды: {nodes})"
        self.hosts_label.setText(text)
        listed = [_device_title(device) for device in self._devices[:40]]
        if count > 40:
            listed.append(f"… ещё {count - 40}")
        self.hosts_label.setToolTip("\n".join(listed))

    # ---------- переменные окружения ----------

    def _append_env_row(self, name: str = "", value: str = "") -> int:
        row = self.env_table.rowCount()
        self.env_table.insertRow(row)
        self.env_table.setItem(row, 0, QTableWidgetItem(name))
        self.env_table.setItem(row, 1, QTableWidgetItem(value))
        return row

    def _add_env_row(self) -> None:
        row = self._append_env_row()
        self.env_table.setCurrentCell(row, 0)
        self.env_table.editItem(self.env_table.item(row, 0))

    def _remove_env_rows(self) -> None:
        rows = sorted({index.row() for index in self.env_table.selectedIndexes()}, reverse=True)
        if not rows and self.env_table.currentRow() >= 0:
            rows = [self.env_table.currentRow()]
        for row in rows:
            self.env_table.removeRow(row)
        self._update_state()

    def _update_env_buttons(self) -> None:
        self.env_remove_button.setEnabled(self.env_table.currentRow() >= 0)

    # ---------- файлы и параметры ----------

    def _open_script(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Открыть скрипт",
            "",
            "Скрипты (*.sh *.bash *.txt);;Все файлы (*)",
        )
        if not path:
            return
        try:
            with open(path, "rb") as handle:
                data = handle.read(_MAX_SCRIPT_FILE_BYTES + 1)
        except OSError as error:
            QMessageBox.warning(self, "Открыть скрипт", f"Не удалось прочитать файл:\n{error}")
            return
        if len(data) > _MAX_SCRIPT_FILE_BYTES:
            QMessageBox.warning(
                self,
                "Открыть скрипт",
                "Файл больше 1 МиБ. Для таких скриптов используйте SFTP-шаг задачи.",
            )
            return
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            QMessageBox.warning(
                self, "Открыть скрипт", "Файл не в кодировке UTF-8 или не текстовый."
            )
            return
        # Замена через курсор: прежний текст остаётся в Ctrl+Z.
        cursor = self.editor.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(text.replace("\r\n", "\n"))
        self.editor.setFocus()

    def _insert_param(self, param: str) -> None:
        self.editor.textCursor().insertText(param)
        self.editor.setFocus()

    # ---------- завершение ----------

    def _on_run(self) -> None:
        if not self.run_button.isEnabled():
            return
        if self.command().errors() or not self._devices:
            self._update_state()
            return
        self.accept()

    def done(self, result: int) -> None:
        # Ввод сохраняется и при отмене: окно откроется с тем же текстом.
        self._store.draft = self.command()
        self._store.save_geometry(self.saveGeometry())
        super().done(result)
