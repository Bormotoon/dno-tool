"""Редактор команд: создание, редактирование, импорт/экспорт и управление.tasks и категориями."""

from PySide6.QtWidgets import *
from PySide6.QtCore import *
from PySide6.QtGui import *
import copy
import tempfile
from pathlib import Path

from src.domain.models.task import CommandEditModel, TASK_DATA_ROLE
from src.domain.task_options import desktop_session_for_step
from src.ui.icons import _icon_from_key, qicon_from_path, set_themed_icon  # zip-темы: иконки из памяти
from src.ui.widgets.device_list_toolbar import FLAT_TOOLBUTTON_QSS
from src.ui.dialogs.editors.bash_edit import BashEditorDialog
from src.ui.widgets.command_preview import CommandPreviewDelegate
from src.ui.widgets.command_modes import CommandModesDelegate, CommandTypeDelegate
from src.ui.widgets.script_editor import script_font
from src.ui.dialogs.common.common import ExportCommandDlg, ImportCommandDlg
from .sftp_edit import SFTPCommandEditorDialog
from ..common.params import ParamsInputDialog
import json
from src.workers import Command
from src.logger import logger
from src.config import config, DEFAULT_COMMANDS_FILE, ICONS
from src.utils.text.natural_sort import natural_key

# ARCHITECTURE: Импорт сервисов и DI контейнера
from src.di import get_container
from src.services import CommandService


class ClearableTreeView(QTreeView):
    """QTreeView: используем стандартные визуальные эффекты drag'n'drop,
    но запрещаем помещать элементы внутрь не-папок (т.е. разрешаем drop
    только в категорию или в корень).
    """

    data_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Используем стандартное поведение drag'n'drop и стандартные визуальные эффекты
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDropIndicatorShown(True)

    def dropEvent(self, event: QDropEvent):
        """Разрешаем drop только в папку (категорию) или в корень; в остальных
        случаях игнорируем drop, сохраняя стандартные визуальные эффекты во время
        перемещения.
        """
        drop_index = self.indexAt(event.pos())
        model = self.model()

        # Определяем целевой parent для дропа: если индикатор указывает "на элемент",
        # то parent == этот элемент (т.е. помещаем внутрь), иначе parent == родитель этого индекса
        drop_pos = self.dropIndicatorPosition()
        if drop_pos == QAbstractItemView.DropIndicatorPosition.OnItem:
            target_parent = drop_index
        else:
            target_parent = drop_index.parent()

        # Разрешаем дроп, если target_parent невалиден (корень) или является категорией
        if (
            target_parent.isValid()
            and hasattr(model, "is_category")
            and not model.is_category(target_parent)
        ):
            event.ignore()
            return

        # В остальных случаях передаём событие стандартной обработке и эмитим сигнал изменений
        super().dropEvent(event)
        self.data_changed.emit()

    def mousePressEvent(self, event: QMouseEvent):
        """Клик по пустому месту дерева снимает выделение - после этого
        «Добавить папку/команду» кладёт элемент в корень."""
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self.indexAt(event.pos()).isValid()
        ):
            self.clearSelection()
            self.setCurrentIndex(QModelIndex())
        super().mousePressEvent(event)


class CommandEditorDialog(QDialog):
    _STEP_TYPES = {
        "ssh": ("SSH", "command_ssh", "Команда на удалённом хосте через SSH"),
        "sftp": ("SFTP", "command_sftp", "Передача файлов через SFTP"),
        "local": ("Локально", "command_local", "Команда на компьютере оператора"),
    }

    def _populate_commands_table(self, commands: list) -> None:
        """Одинаково отобразить шаги при выборе задания и после его редактирования."""
        self.commandsTable.setRowCount(len(commands))
        for row, command in enumerate(commands):
            step = command if isinstance(command, dict) else {"type": "ssh", "text": str(command)}
            kind = step.get("type", "ssh")
            label, icon, tooltip = self._STEP_TYPES.get(kind, (kind, "command", kind))
            type_item = QTableWidgetItem()
            type_item.setData(Qt.ItemDataRole.UserRole, kind)
            type_item.setData(Qt.ItemDataRole.AccessibleTextRole, label)
            type_item.setToolTip(f"{label}: {tooltip}")
            set_themed_icon(type_item, icon)
            self.commandsTable.setItem(row, 0, type_item)
            self.commandsTable.setItem(row, 1, QTableWidgetItem(step.get("text", "")))

            modes = {
                "desktop_session": desktop_session_for_step(step),
                "background": kind == "ssh" and step.get("background") is True,
            }
            labels = [title for flag, _, title in CommandModesDelegate.MODES if modes[flag]]
            mode_item = QTableWidgetItem()
            mode_item.setData(Qt.ItemDataRole.UserRole, modes)
            mode_item.setData(Qt.ItemDataRole.AccessibleTextRole, "; ".join(labels) or "Обычное выполнение")
            mode_item.setToolTip("\n".join(labels) or (
                "SSH: настроенная учётная запись, ожидание завершения" if kind == "ssh" else "Режимы SSH неприменимы"
            ))
            self.commandsTable.setItem(row, 2, mode_item)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Редактор команд")
        self.setMinimumSize(480, 480)
        self.resize(825, 540)

        # ARCHITECTURE: Получаем сервис команд через DI контейнер
        self.command_service = get_container().resolve(CommandService)

        self.modified = False
        # Поля формы заполняются из модели: их сигналы в это время не являются правкой.
        self._loading = False
        # Правка поля пишет в модель; фильтр дерева в этот момент не пересчитывается,
        # иначе переименованный элемент пропал бы из-под курсора.
        self._committing = False
        self._download_worker = None
        # Original data for cancel functionality
        self._original_data = None

        self.model = CommandEditModel(self)
        try:
            # ARCHITECTURE: Используем сервис вместо datastore
            self.initial_data_structure = self.command_service.get_all_commands()
            self.model.load_data(self.initial_data_structure)
            logger.debug(
                f"CommandEditorDialog: Model loaded. Root row count: {self.model.rowCount()}"
            )
            if self.model.rowCount() == 0 and len(self.initial_data_structure) > 0:
                logger.debug(
                    "CommandEditorDialog: WARNING - Data provided but model is empty after load!"
                )

            # ARCHITECTURE: Сохраняем оригинальные данные для отмены изменений
            self._original_data = copy.deepcopy(self.initial_data_structure)

        except Exception as e:
            logger.debug(f"CommandEditorDialog: Error loading data into model: {e}")
            QMessageBox.critical(
                self,
                "Ошибка загрузки данных",
                f"Не удалось загрузить данные в модель:\n{e}",
            )
            self.model.load_data([])
            self._original_data = []

        # --- Правая панель: Дерево ---
        self.treeView = ClearableTreeView()
        self.treeView.setModel(self.model)
        self.treeView.setHeaderHidden(True)
        self.treeView.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.treeView.setContentsMargins(4, 4, 4, 4)
        logger.debug("CommandEditorDialog: TreeView created and model set.")

        # ARCHITECTURE: Подключаем сигнал drag'n'drop для сохранения изменений
        self.treeView.data_changed.connect(self._on_tree_data_changed)

        # --- Панель над деревом: тулбар (иконки как в главном окне) + фильтр ---
        # Без боковых отступов: края тулбара/фильтра совпадают с рамкой дерева.
        treeToolbar = QHBoxLayout()
        treeToolbar.setContentsMargins(0, 4, 0, 0)
        treeToolbar.setSpacing(2)

        self.addFolderButton = QToolButton()
        self.addFolderButton.setIcon(_icon_from_key("menu_add_folder"))
        self.addFolderButton.setToolTip(
            "Добавить категорию (папку). Ctrl+клик - в корень дерева"
        )
        self.addFolderButton.setAutoRaise(False)

        self.addCommandButton = QToolButton()
        self.addCommandButton.setIcon(_icon_from_key("menu_add_command"))
        self.addCommandButton.setToolTip(
            "Добавить новую команду. Ctrl+клик - в корень дерева"
        )
        self.addCommandButton.setAutoRaise(False)

        self.sortButton = QToolButton()
        self.sortButton.setIcon(_icon_from_key("menu_sort"))
        self.sortButton.setToolTip(
            "Сортировать по алфавиту: выбранную папку или всё дерево"
        )
        self.sortButton.setAutoRaise(False)

        self.deleteItemButton = QToolButton()
        self.deleteItemButton.setIcon(_icon_from_key("menu_delete"))
        self.deleteItemButton.setToolTip(
            "Удалить выбранный элемент (папку или команду)"
        )
        self.deleteItemButton.setAutoRaise(False)
        self.deleteItemButton.setEnabled(False)

        self.expandAllButton = QToolButton()
        self.expandAllButton.setIcon(_icon_from_key("toolbar_expand_all"))
        self.expandAllButton.setToolTip("Развернуть всё дерево")
        self.expandAllButton.setAutoRaise(False)

        self.collapseAllButton = QToolButton()
        self.collapseAllButton.setIcon(_icon_from_key("toolbar_collapse_all"))
        self.collapseAllButton.setToolTip("Свернуть всё дерево")
        self.collapseAllButton.setAutoRaise(False)

        self.importServerButton = QToolButton()
        self.importServerButton.setIcon(_icon_from_key("menu_download"))
        self.importServerButton.setToolTip("Импортировать задачи с сервера…")
        self.importServerButton.setAccessibleName("Импортировать задачи с сервера")
        self.importServerButton.setAutoRaise(False)

        for btn in (
            self.addFolderButton,
            self.addCommandButton,
            self.sortButton,
            self.deleteItemButton,
            self.expandAllButton,
            self.collapseAllButton,
            self.importServerButton,
        ):
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            btn.setIconSize(QSize(32, 32))
            btn.setFixedSize(QSize(32, 32))
            btn.setStyleSheet(FLAT_TOOLBUTTON_QSS)

        treeToolbar.addWidget(self.addFolderButton)
        treeToolbar.addWidget(self.addCommandButton)
        treeToolbar.addWidget(self.sortButton)
        treeToolbar.addWidget(self.importServerButton)
        treeToolbar.addStretch(1)
        treeToolbar.addWidget(self.expandAllButton)
        treeToolbar.addWidget(self.collapseAllButton)
        treeToolbar.addWidget(self.deleteItemButton)

        self.filterEdit = QLineEdit()
        self.filterEdit.setPlaceholderText("Поиск команд...")
        self.filterEdit.setClearButtonEnabled(True)
        self.filterEdit.setToolTip("Фильтр по имени команды или пути (Папка → Команда)")

        # Создаем контейнер для дерева
        treeContainer = QVBoxLayout()
        treeContainer.addLayout(treeToolbar)
        treeContainer.addWidget(self.filterEdit)
        treeContainer.addWidget(self.treeView)
        treeContainer.setContentsMargins(0, 0, 0, 0)
        treeContainer.setSpacing(4)

        # Контекстное меню для дерева
        self.treeView.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.treeView.customContextMenuRequested.connect(self._show_tree_context_menu)

        # --- Левая панель: Форма редактирования и кнопки ---
        # Виджет-контейнер для левой панели
        leftPanelWidget = QWidget()
        # Layout для этого виджета-контейнера
        leftPanelLayout = QVBoxLayout(
            leftPanelWidget
        )  # Устанавливаем layout для leftPanelWidget
        leftPanelLayout.setContentsMargins(4, 0, 4, 4)
        leftPanelLayout.setSpacing(8)

        # Группа для полей редактирования
        self.editorFrame = QGroupBox("Редактирование элемента")
        editorFrameLayout = QVBoxLayout()  # Layout для содержимого GroupBox
        editorFrameLayout.setContentsMargins(8, 8, 8, 8)
        editorFrameLayout.setSpacing(8)
        self.editorFrame.setLayout(editorFrameLayout)

        # Поля (как раньше)
        self.nameLabel = QLabel("Имя:")
        self.nameEdit = QLineEdit()
        self.nameEdit.setPlaceholderText("Введите значение...")
        self.descLabel = QLabel("Описание:")
        self.descEdit = QLineEdit()
        self.descEdit.setPlaceholderText("Введите значение...")
        self.timeoutLabel = QLabel("Таймаут (сек):")
        self.timeoutSpinBox = QSpinBox()
        self.timeoutSpinBox.setRange(0, 7200)
        self.timeoutSpinBox.setValue(config.app.ssh.command_timeout)
        self.timeoutSpinBox.setSuffix(" сек")

        # Параметры
        paramsLayout = QHBoxLayout()
        paramsLayout.setContentsMargins(4, 4, 4, 4)
        self.paramsLabel = QLabel("Параметры:")
        self.paramsButton = QPushButton("Редактировать...")
        self.paramsButton.clicked.connect(self._edit_params)
        self.paramsButton.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        paramsLayout.addWidget(self.paramsLabel)
        paramsLayout.addStretch(1)  # Добавляем растягивающийся промежуток
        paramsLayout.addWidget(self.paramsButton)
        # Команды
        self.commandsLabel = QLabel("Команды для выполнения:")

        self.commandsTable = QTableWidget()
        self.commandsTable.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.commandsTable.setItemDelegateForColumn(0, CommandTypeDelegate(self.commandsTable))
        self.commandsTable.setItemDelegateForColumn(1, CommandPreviewDelegate(self.commandsTable))
        self.commandsTable.setItemDelegateForColumn(2, CommandModesDelegate(self.commandsTable))
        self.commandsTable.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.commandsTable.setColumnCount(3)
        self.commandsTable.setHorizontalHeaderLabels(["Тип", "Команда", "Режим"])
        self.commandsTable.setIconSize(QSize(24, 24))
        # Центрируем заголовок и содержимое первой колонки
        self.commandsTable.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        self.commandsTable.horizontalHeaderItem(0).setTextAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        # center_delegate = CenterAlignDelegate()
        # self.commandsTable.setItemDelegateForColumn(0, center_delegate)
        self.commandsTable.setToolTip(
            "Первые три строки каждого шага. Двойной клик или F2 — редактировать, Del — удалить"
        )
        self.commandsTable.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.commandsTable.setColumnWidth(0, 48)
        self.commandsTable.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.commandsTable.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Fixed
        )
        self.commandsTable.setColumnWidth(2, 76)
        self.commandsTable.horizontalHeaderItem(2).setToolTip(
            "Окружение пользователя рабочего стола и запуск без ожидания. "
            "Режимы независимы и могут быть включены вместе."
        )
        self.commandsTable.verticalHeader().setVisible(False)
        self.commandsTable.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.commandsTable.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        # Команды - моноширинным, как в импорте Ansible и Bash-редакторе.
        mono = script_font()
        self.commandsTable.setFont(mono)

        commandButtonsLayout = QHBoxLayout()
        self.addCmdButton = QPushButton("Добавить SSH-команду")
        self.addSftpButton = QPushButton("Добавить SFTP-команду")
        self.addLocalButton = QPushButton("Добавить локальную команду")
        set_themed_icon(self.addCmdButton, "command_ssh")
        set_themed_icon(self.addSftpButton, "command_sftp")
        set_themed_icon(self.addLocalButton, "command_local")
        self.removeCmdButton = QPushButton("Удалить команду")
        set_themed_icon(self.removeCmdButton, "menu_delete")
        commandButtonsLayout.addWidget(self.addCmdButton, stretch=1)
        commandButtonsLayout.addWidget(
            self.addSftpButton, stretch=1, alignment=Qt.AlignmentFlag.AlignCenter
        )
        commandButtonsLayout.addWidget(
            self.addLocalButton, stretch=1, alignment=Qt.AlignmentFlag.AlignRight
        )
        commandButtonsLayout.addStretch()

        ImportExportButtonsLayout = QHBoxLayout()
        self.ImportButton = QPushButton("Импорт команд")
        self.ExportButton = QPushButton("Экспорт команд")
        self.AnsibleButton = QPushButton("Импорт Ansible")
        ImportExportButtonsLayout.addWidget(self.ImportButton)
        ImportExportButtonsLayout.addWidget(self.ExportButton)
        ImportExportButtonsLayout.addWidget(self.AnsibleButton)
        ImportExportButtonsLayout.addStretch()
        ImportExportButtonsLayout.setStretch(0, 1)
        ImportExportButtonsLayout.setStretch(1, 1)
        ImportExportButtonsLayout.setStretch(2, 1)

        # Сборка формы редактирования внутри GroupBox
        editorFrameLayout.addWidget(self.nameLabel)
        editorFrameLayout.addWidget(self.nameEdit)
        editorFrameLayout.addWidget(self.descLabel)
        editorFrameLayout.addWidget(self.descEdit)
        editorFrameLayout.addWidget(self.timeoutLabel)
        editorFrameLayout.addWidget(self.timeoutSpinBox)
        self.sessionCheckBox = QCheckBox("Одна SSH-сессия на задачу")
        self.sessionCheckBox.setToolTip(
            "Выполнять все ssh-шаги задачи в общем persistent shell: "
            "переменные register/set_fact живут между шагами. "
            "На нодах без session-поддержки - отказ с просьбой обновить ноду."
        )
        editorFrameLayout.addWidget(self.sessionCheckBox)
        editorFrameLayout.addLayout(paramsLayout)
        editorFrameLayout.addWidget(self.commandsLabel)
        editorFrameLayout.addWidget(self.commandsTable, stretch=1)
        editorFrameLayout.addLayout(commandButtonsLayout, stretch=0)
        editorFrameLayout.addWidget(
            self.removeCmdButton, stretch=0, alignment=Qt.AlignmentFlag.AlignCenter
        )
        editorFrameLayout.addLayout(ImportExportButtonsLayout)
        editorFrameLayout.addStretch()

        leftPanelLayout.addWidget(self.editorFrame)
        leftPanelLayout.setStretchFactor(
            self.editorFrame, 1
        )  # editorFrame растягивается

        # --- Сплиттер ---
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(4)

        # Создаем виджет для размещения дерева и его кнопок
        treeWidget = QWidget()
        treeWidget.setLayout(treeContainer)

        self.splitter.addWidget(
            treeWidget
        )  # Добавляем левую панель (дерево с кнопками)
        self.splitter.addWidget(
            leftPanelWidget
        )  # Добавляем правую панель (уже с layout'ом)
        self.splitter.setSizes([300, 500])  # Устанавливаем размеры

        # --- Кнопки OK/Cancel ---
        # self.dialogButtonBox = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        # self.dialogButtonBox.accepted.connect(self.accept)
        # self.dialogButtonBox.rejected.connect(self.reject)
        self.dialogButtonBox = QHBoxLayout()
        self.dialogButtonBox.setContentsMargins(0, 8, 0, 0)
        self.dialogButtonBox.setSpacing(8)
        self.dialogButtonBox.setAlignment(Qt.AlignmentFlag.AlignRight)

        self.dialogButtonOk = QPushButton("Ok")
        self.dialogButtonCancel = QPushButton("Отмена")
        self.dialogButtonApply = QPushButton("Применить")

        for button in (self.dialogButtonOk, self.dialogButtonApply, self.dialogButtonCancel):
            self.dialogButtonBox.addWidget(button)

        self.dialogButtonOk.clicked.connect(self._apply_changes_and_close)
        self.dialogButtonCancel.clicked.connect(self.reject)
        self.dialogButtonApply.clicked.connect(lambda: self._apply_changes())

        # --- Основной Layout диалога ---
        mainDialogLayout = QVBoxLayout(
            self
        )  # Устанавливаем QVBoxLayout как основной layout ДИАЛОГА
        mainDialogLayout.setContentsMargins(8, 8, 8, 8)
        mainDialogLayout.setSpacing(8)
        mainDialogLayout.addWidget(self.splitter)  # Добавляем сплиттер
        mainDialogLayout.addLayout(self.dialogButtonBox)  # Добавляем кнопки OK/Cancel
        # self.setLayout(mainDialogLayout) # Этот вызов уже сделан при создании QVBoxLayout(self)

        # Connect signals
        # Каждая правка поля сразу попадает в модель: смена выделения, сортировка
        # и операции с шагами больше не теряют набранный текст.
        self.nameEdit.textEdited.connect(lambda text: self._commit_field("name", text))
        self.nameEdit.editingFinished.connect(self._restore_empty_name)
        self.descEdit.textEdited.connect(
            lambda text: self._commit_field("description", text)
        )
        self.timeoutSpinBox.valueChanged.connect(
            lambda value: self._commit_field("timeout", value)
        )
        self.sessionCheckBox.toggled.connect(
            lambda checked: self._commit_field("session", checked)
        )
        self.treeView.selectionModel().currentChanged.connect(
            self._on_selection_changed
        )
        self.addCmdButton.clicked.connect(
            lambda: self._add_command_to_list_of_type("ssh")
        )
        self.addSftpButton.clicked.connect(
            lambda: self._add_command_to_list_of_type("sftp")
        )
        self.addLocalButton.clicked.connect(
            lambda: self._add_command_to_list_of_type("local")
        )
        self.removeCmdButton.clicked.connect(self._delete_selected_command)
        self.ImportButton.clicked.connect(self._import_commands)
        self.ExportButton.clicked.connect(self._export_commands)
        self.AnsibleButton.clicked.connect(self._import_ansible)

        # Кнопки тулбара дерева и фильтр
        self.addFolderButton.clicked.connect(
            lambda: self._add_category(to_root=self._ctrl_held())
        )
        self.addCommandButton.clicked.connect(
            lambda: self._add_command(to_root=self._ctrl_held())
        )
        self.sortButton.clicked.connect(self._sort_tree)
        self.importServerButton.clicked.connect(self._import_commands_from_server)
        self.deleteItemButton.clicked.connect(self._delete_item)
        self.expandAllButton.clicked.connect(self.treeView.expandAll)
        self.collapseAllButton.clicked.connect(self.treeView.collapseAll)
        self.filterEdit.textChanged.connect(self._apply_tree_filter)

        # Пере-применение фильтра после структурных изменений модели
        self.model.structureChanged.connect(self._apply_tree_filter)

        # Connect table signals
        self.commandsTable.itemDoubleClicked.connect(
            lambda item: self._edit_command(item.row())
        )
        def handle_key(event):
            if event.key() == Qt.Key.Key_Delete:
                self._delete_selected_command()
            elif event.key() in (Qt.Key.Key_F2, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._edit_command(self.commandsTable.currentRow())
            else:
                QTableWidget.keyPressEvent(self.commandsTable, event)

        self.commandsTable.keyPressEvent = handle_key

        # Enter в поле имени или описания не должен нажимать кнопки окна: раньше
        # он срабатывал как «Отмена». Без кнопки по умолчанию QDialog при показе
        # назначил бы ею первую autoDefault-кнопку, поэтому флаг снят у всех.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

        # --- Инициализация состояния ---
        if config.app.expand:
            self.treeView.expandAll()  # Раскрываем узлы

        # ARCHITECTURE: Сбрасываем выделение чтобы ничего не было выбрано при открытии
        self.treeView.clearSelection()
        self.treeView.setCurrentIndex(QModelIndex())

        # Вызываем обработчик выбора для инициализации полей (с пустым выделением)
        self._on_selection_changed(QModelIndex(), QModelIndex())

        # ARCHITECTURE: Сбрасываем modified после инициализации чтобы не показывать диалог без причин
        self.modified = False
        logger.debug(
            "CommandEditorDialog: Initialization complete, no selection, modified flag reset"
        )

    @Slot(QModelIndex, QModelIndex)
    def _on_selection_changed(self, current: QModelIndex, previous: QModelIndex):
        # Если попытка перейти на тот же элемент - ничего не делать.
        # Невалидный -> невалидный пропускаем дальше, чтобы при открытии
        # и при сбросе выделения правая панель гасла как при клике мимо дерева.
        if current == previous and current.isValid():
            return
        self._update_delete_button_state(current)
        self._load_editor(current)

    def _load_editor(self, current: QModelIndex) -> None:
        """Заполнить форму из модели; сигналы полей в это время не считаются правкой."""
        is_category = self.model.is_category(current)
        item = self.model.itemFromIndex(current) if current.isValid() else None
        item_data = self.model.get_item_data(current)
        can_edit = current.isValid()
        can_edit_command_details = can_edit and not is_category

        self.editorFrame.setEnabled(can_edit)
        self.nameEdit.setEnabled(can_edit)
        for widget in (
            self.descEdit,
            self.timeoutSpinBox,
            self.sessionCheckBox,
            self.commandsTable,
            self.addCmdButton,
            self.addSftpButton,
            self.addLocalButton,
            self.removeCmdButton,
            self.ImportButton,
            self.ExportButton,
            self.paramsButton,
        ):
            widget.setEnabled(can_edit_command_details)

        self._loading = True
        try:
            self.nameEdit.setText(item.text() if item else "")
            data = item_data if can_edit_command_details and item_data else {}
            self.descEdit.setText(data.get("description", ""))
            self.timeoutSpinBox.setValue(
                data.get("timeout", config.app.ssh.command_timeout)
            )
            self.sessionCheckBox.setChecked(bool(data.get("session", False)))
            params = data.get("params", [])
            self.paramsLabel.setText(
                f"Параметры: {', '.join(params) if params else ''}"
            )
            self.commandsTable.setRowCount(0)
            self._populate_commands_table(data.get("commands", []))
        finally:
            self._loading = False

    def _commit_field(self, key: str, value) -> None:
        """Записать правку поля формы в выбранный элемент модели."""
        if self._loading:
            return
        index = self.treeView.currentIndex()
        if not index.isValid():
            return
        if key == "name":
            value = value.strip()
            if not value:
                # Пустое имя не принимается; поле вернётся к имени из модели.
                return
        if self.model.is_category(index):
            if key != "name":
                return
            new_data = {value: []}  # категория в модели только переименовывается
        else:
            new_data = self.model.get_item_data(index)
            if not new_data:
                return
            if key == "description":
                value = value.strip()
            if key == "session":
                # Ключ session пишется только при True.
                if value:
                    new_data["session"] = True
                else:
                    new_data.pop("session", None)
            else:
                new_data[key] = value
        self._committing = True
        try:
            changed = self.model.update_item(index, new_data)
        finally:
            self._committing = False
        if changed:
            self.set_modified()

    def _restore_empty_name(self) -> None:
        """После ввода пустого имени показать имя, которое осталось в модели."""
        if self.nameEdit.text().strip():
            return
        index = self.treeView.currentIndex()
        item = self.model.itemFromIndex(index) if index.isValid() else None
        if item is not None:
            self._loading = True
            try:
                self.nameEdit.setText(item.text())
            finally:
                self._loading = False

    def _get_current_or_root_index(self) -> QModelIndex:
        current_index = self.treeView.currentIndex()
        # logger.debug(f"_get_current_or_root_index: current_index.isValid() = {current_index.isValid()}") # Debug
        if current_index.isValid():
            # Если выбран элемент, определяем, куда добавлять:
            # - В родителя, если выбрана команда
            # - В саму категорию, если выбрана категория
            if not self.model.is_category(current_index):
                # logger.debug("  -> Adding to parent of selected command") # Debug
                return current_index.parent()
            else:
                # logger.debug("  -> Adding to selected category") # Debug
                return current_index
        else:
            # Если НИЧЕГО не выбрано (current_index невалиден), добавляем в корень
            # logger.debug("  -> Adding to root") # Debug
            return QModelIndex()  # Возвращаем невалидный индекс для корня

    # --- Фильтр дерева ---
    def _match_filter(self, index: QModelIndex) -> bool:
        """Проверка совпадения элемента (имя + полный путь) с фильтром."""
        query = self.filterEdit.text().casefold()
        if not query:
            return True
        model = self.model
        parts = []
        walk = index
        while walk.isValid():
            parts.append(str(model.data(walk, Qt.ItemDataRole.DisplayRole) or ""))
            walk = walk.parent()
        path = " ".join(reversed(parts))
        return query in path.casefold()

    def _apply_tree_filter(self, *_):
        """Скрыть в дереве элементы, не совпавшие с фильтром (рекурсивно).

        Используем setRowHidden вместо proxy-модели: все пути диалога работают
        через treeView.currentIndex(), скрытие строк не ломает индексы и DnD.
        Папка видима, если совпала сама или хотя бы один потомок.
        """
        if self._committing:
            return
        # Пустой запрос: снять скрытие на ВСЕХ уровнях после предыдущего
        # непустого фильтра, НЕ трогая collapse/expand-состояние дерева
        # (иначе каждый structureChanged - add/rename/импорт - разворачивал
        # всё дерево). Рекурсивно: скрытие ставилось и на вложенные строки.
        if not self.filterEdit.text():

            def unhide(parent: QModelIndex) -> None:
                model = self.model
                for row in range(model.rowCount(parent)):
                    index = model.index(row, 0, parent)
                    self.treeView.setRowHidden(row, parent, False)
                    unhide(index)

            unhide(QModelIndex())
            return

        def walk(parent: QModelIndex) -> bool:
            model = self.model
            any_visible = False
            for row in range(model.rowCount(parent)):
                index = model.index(row, 0, parent)
                item = model.itemFromIndex(index)
                if not item:
                    continue
                self.treeView.setRowHidden(row, parent, False)
                self.treeView.collapse(index)
                child_matched = walk(index)
                matched = self._match_filter(index) or child_matched
                self.treeView.setRowHidden(row, parent, not matched)
                if matched:
                    any_visible = True
                    if child_matched:
                        self.treeView.expand(index)
            return any_visible

        walk(QModelIndex())
        # При активном фильтре выделение могло скрыться - сбрасываем его,
        # чтобы редактор не ссылался на невидимый элемент.
        if (
            self.filterEdit.text()
            and self.treeView.currentIndex().isValid()
            and self.treeView.isRowHidden(
                self.treeView.currentIndex().row(),
                self.treeView.currentIndex().parent(),
            )
        ):
            self.treeView.clearSelection()
            self.treeView.setCurrentIndex(QModelIndex())
            self.blockSignals(True)
            self._on_selection_changed(QModelIndex(), QModelIndex())
            self.blockSignals(False)

    # --- Контекстное меню дерева ---
    def _show_tree_context_menu(self, position: QPoint):
        """Показать контекстное меню для элемента дерева."""
        index = self.treeView.indexAt(position)
        menu = QMenu(self)

        add_category_action = menu.addAction(
            _icon_from_key("menu_folder"), "Добавить категорию"
        )
        add_command_action = menu.addAction(
            _icon_from_key("command"), "Добавить команду"
        )
        menu.addSeparator()
        sort_action = menu.addAction(_icon_from_key("menu_sort"), "Сортировать")
        menu.addSeparator()
        delete_action = menu.addAction(_icon_from_key("menu_delete"), "Удалить элемент")
        delete_action.setEnabled(index.isValid())

        action = menu.exec(self.treeView.viewport().mapToGlobal(position))
        if not action:
            return
        # Добавление следует за кликнутым элементом: ПКМ по папке → внутрь неё,
        # ПКМ по пустому месту → корень (не завися от старого выделения).
        if index.isValid() and not self.model.is_category(index):
            add_parent = index.parent()
        else:
            add_parent = index  # категория → внутрь, пустое место (invalid) → корень
        if action == add_category_action:
            self._add_category(parent_index=add_parent)
        elif action == add_command_action:
            self._add_command(parent_index=add_parent)
        elif action == sort_action:
            self._sort_tree(target=index)
        elif action == delete_action:
            self._delete_item()

    def _update_delete_button_state(self, current: QModelIndex):
        """Активность кнопки «Удалить» - только при валидном выделении."""
        if hasattr(self, "deleteItemButton"):
            self.deleteItemButton.setEnabled(current.isValid())

    # --- Сортировка дерева ---
    @staticmethod
    def _resolve_sort_target(index: QModelIndex) -> QModelIndex:
        """Папка для сортировки по элементу: категория → она сама,
        команда → её родитель, невалидный индекс → корень (невалидный)."""
        if not index.isValid():
            return QModelIndex()
        if not index.model().data(index, TASK_DATA_ROLE):  # категория
            return index
        return index.parent()

    def _sort_group_in_place(self, parent_index: QModelIndex):
        """In-place сортировка строк одной группы: категории раньше команд,
        внутри - natural-порядок без учёта регистра."""
        model = self.model
        n = model.rowCount(parent_index)
        if n < 2:
            return
        parent_item = model.itemFromIndex(parent_index) or model.invisibleRootItem()
        rows = [parent_item.takeRow(n - 1 - i) for i in range(n)]
        rows.reverse()  # восстановить исходный порядок перед сортировкой

        def sort_key(row):
            item = row[0]
            # Команда (есть данные задачи) - после категорий
            if item.data(TASK_DATA_ROLE) is not None:
                return (1, natural_key(item.text() or ""))
            return (0, natural_key(item.text() or ""))

        rows.sort(key=sort_key)
        for row in rows:
            parent_item.appendRow(row)

    def _sort_tree_recursive(self, parent_index: QModelIndex):
        """Сортировка группы + рекурсивно всех вложенных папок."""
        self._sort_group_in_place(parent_index)
        model = self.model
        for row in range(model.rowCount(parent_index)):
            child = model.index(row, 0, parent_index)
            if model.rowCount(child) > 0:
                self._sort_tree_recursive(child)

    def _sort_tree(self, target: QModelIndex | None = None):
        """Сортировка по кнопке/ПКМ: выбранная папка (или родитель команды),
        при отсутствии выделения - всё дерево."""
        if target is None:
            target = self._resolve_sort_target(self.treeView.currentIndex())
        else:
            target = self._resolve_sort_target(target)

        is_whole_tree = not target.isValid()
        # Раскрытость и выделение теряются при takeRow/appendRow - сохраняем
        expanded = self._collect_expanded_paths() if not config.app.expand else None
        selected_path = self._index_path_text(self.treeView.currentIndex())

        if is_whole_tree:
            self._sort_tree_recursive(QModelIndex())
        else:
            self._sort_group_in_place(target)

        # Пересоздание строк сбрасывает раскрытость/скрытия - восстанавливаем
        if config.app.expand:
            self.treeView.expandAll()
        else:
            self._restore_expanded_paths(expanded)
        self._apply_tree_filter()
        self._restore_selection_by_path(selected_path)
        self.set_modified()
        if is_whole_tree:
            logger.info("CommandEditorDialog: whole tree sorted")
        else:
            logger.info(
                f"CommandEditorDialog: group sorted: "
                f"{self.model.data(target, Qt.ItemDataRole.DisplayRole) or ''}"
            )

    def _index_path_text(self, index: QModelIndex) -> list[str] | None:
        """Текстовый путь элемента [корень, ..., элемент] для восстановления
        выделения после сортировки; None для невалидного индекса."""
        if not index.isValid():
            return None
        parts = []
        walk = index
        while walk.isValid():
            parts.append(str(self.model.data(walk, Qt.ItemDataRole.DisplayRole) or ""))
            walk = walk.parent()
        return list(reversed(parts))

    def _restore_selection_by_path(self, path: list[str] | None):
        """Восстановить выделение по текстовому пути; не найден - сброс."""
        if not path:
            self.treeView.clearSelection()
            self.treeView.setCurrentIndex(QModelIndex())
            self.blockSignals(True)
            self._on_selection_changed(QModelIndex(), QModelIndex())
            self.blockSignals(False)
            return
        index = QModelIndex()
        for name in path:
            match = QModelIndex()
            model = self.model
            for row in range(model.rowCount(index)):
                candidate = model.index(row, 0, index)
                if model.data(
                    candidate, Qt.ItemDataRole.DisplayRole
                ) == name and not self.treeView.isRowHidden(row, index):
                    match = candidate
                    break
            if not match.isValid():
                self.treeView.clearSelection()
                self.treeView.setCurrentIndex(QModelIndex())
                self.blockSignals(True)
                self._on_selection_changed(QModelIndex(), QModelIndex())
                self.blockSignals(False)
                return
            index = match
        self.treeView.setCurrentIndex(index)

    def _collect_expanded_paths(self) -> set[tuple[str, ...]]:
        """Пути раскрытых папок (текстовые, от корня) для восстановления."""
        expanded: set[tuple[str, ...]] = set()

        def walk(parent: QModelIndex):
            model = self.model
            for row in range(model.rowCount(parent)):
                index = model.index(row, 0, parent)
                if model.rowCount(index) == 0:
                    continue
                path = tuple(self._index_path_text(index) or [])
                if self.treeView.isExpanded(index):
                    expanded.add(path)
                    walk(index)

        walk(QModelIndex())
        return expanded

    def _restore_expanded_paths(self, expanded: set[tuple[str, ...]] | None):
        """Восстановить раскрытость папок по сохранённым путям."""
        if expanded is None:
            return
        paths = {path for path in expanded if path}

        def walk(parent: QModelIndex):
            model = self.model
            for row in range(model.rowCount(parent)):
                index = model.index(row, 0, parent)
                if model.rowCount(index) == 0:
                    continue
                path = tuple(self._index_path_text(index) or [])
                if path in paths:
                    self.treeView.expand(index)
                walk(index)

        walk(QModelIndex())

    def _resolve_add_parent(
        self, to_root: bool = False, parent_index: QModelIndex | None = None
    ) -> QModelIndex:
        """Куда класть новый элемент: явный parent (ПКМ) → корень (to_root) →
        выделение (категория = внутрь, команда = её родитель) → корень."""
        if parent_index is not None:
            return parent_index
        if to_root:
            return QModelIndex()
        return self._get_current_or_root_index()

    @staticmethod
    def _ctrl_held() -> bool:
        """Зажат ли Ctrl в момент клика по кнопке (QApplication-модификаторы)."""
        return bool(
            QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier
        )

    def _add_category(
        self, to_root: bool = False, parent_index: QModelIndex | None = None
    ):
        parent_index = self._resolve_add_parent(to_root, parent_index)
        name, ok = QInputDialog.getText(
            self, "Добавить категорию", "Введите имя новой категории:"
        )
        name = name.strip()
        if ok and name:
            category_data = {name: []}
            if self.model.add_item(parent_index, category_data, is_category=True):
                self.treeView.expand(parent_index)
                self._select_last_child(parent_index)
                self.set_modified()
        elif ok and not name:
            QMessageBox.warning(self, "Ошибка", "Имя категории не может быть пустым.")

    def _add_command(
        self, to_root: bool = False, parent_index: QModelIndex | None = None
    ):
        """Add new command to tree"""
        parent_index = self._resolve_add_parent(to_root, parent_index)
        # Проверка: нельзя добавлять команду в команду
        if parent_index.isValid() and not self.model.is_category(parent_index):
            QMessageBox.warning(
                self,
                "Ошибка",
                "Команду можно добавить только в категорию или в корень дерева.",
            )
            return
        name, ok = QInputDialog.getText(
            self, "Добавить команду", "Введите имя новой команды:"
        )
        name = name.strip()
        if ok and name:
            command_data = copy.deepcopy(self.model.default_command)
            command_data["name"] = name
            command_data["commands"] = []
            if self.model.add_item(parent_index, command_data, is_category=False):
                self.treeView.expand(parent_index)
                self._select_last_child(parent_index)
                self.set_modified()
        elif ok and not name:
            QMessageBox.warning(self, "Ошибка", "Имя команды не может быть пустым.")

    def _select_last_child(self, parent_index: QModelIndex) -> None:
        """Выделить только что добавленный элемент, чтобы сразу его редактировать.

        Скрытый фильтром элемент не выделяется: форма не должна ссылаться
        на невидимую строку.
        """
        row = self.model.rowCount(parent_index) - 1
        if row < 0 or self.treeView.isRowHidden(row, parent_index):
            return
        self.treeView.setCurrentIndex(self.model.index(row, 0, parent_index))

    def _delete_item(self):
        current_index = self.treeView.currentIndex()
        if not current_index.isValid():
            QMessageBox.warning(
                self, "Ошибка", "Сначала выберите элемент для удаления."
            )
            return

        item_name = self.model.data(current_index, Qt.DisplayRole)
        reply = QMessageBox.question(
            self,
            "Подтверждение удаления",
            f"Вы уверены, что хотите удалить '{item_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.Yes:
            if self.model.remove_item(current_index):
                # ARCHITECTURE: Не вызываем _on_selection_changed здесь,
                # чтобы не триггерить диалог сохранения
                # modified будет установлен при следующем изменении
                self.modified = True
                logger.debug("CommandEditorDialog: Item deleted, marked as modified")
                # Снятие выделения после удаления + обновление состояния кнопки
                self.treeView.clearSelection()
                self.treeView.setCurrentIndex(QModelIndex())
                self._update_delete_button_state(QModelIndex())
            else:
                QMessageBox.critical(self, "Ошибка", "Не удалось удалить элемент.")

    def _apply_changes(self) -> bool:
        """Сохранить задачи через сервис; True, если несохранённых правок не осталось.

        Поля формы уже записаны в модель при вводе, отдельный перенос не нужен.
        """
        if not self.modified:
            return True
        success, error = self.command_service.save_commands(self.get_data())
        if not success:
            QMessageBox.critical(
                self,
                "Ошибка",
                f"Не удалось сохранить задачи.\n\n{error}",
            )
            return False
        export_success, message = self.command_service.export_commands_to_file(
            DEFAULT_COMMANDS_FILE
        )
        if not export_success:
            QMessageBox.critical(
                self, "Ошибка", f"Не удалось сохранить в файл: {message}"
            )
            return False
        self.modified = False
        logger.info("CommandEditorDialog: Commands saved successfully")
        return True

    def _on_tree_data_changed(self):
        """Обработчик изменения данных дерева (drag'n'drop)"""
        logger.debug("CommandEditorDialog: Tree data changed via drag'n'drop")
        self.modified = True

    def _apply_changes_and_close(self):
        # При ошибке сохранения окно остаётся открытым с несохранёнными правками.
        if self._apply_changes():
            self.accept()

    def _get_current_params(self) -> list:
        """Получить параметры текущей команды"""
        current_item = self._get_selected_command_item()
        if not current_item:
            return []
        item_data = self.model.get_item_data(current_item)
        if not item_data:
            return []
        return item_data.get("params", [])

    def _export_commands(self):
        """Экспорт полной команды со всей информацией (кроме имени)"""
        current_item = self._get_selected_command_item()
        if not current_item:
            QMessageBox.warning(
                self, "Предупреждение", "Сначала выберите команду для экспорта"
            )
            return

        item_data = self.model.get_item_data(current_item)
        if not item_data:
            QMessageBox.warning(
                self, "Предупреждение", "Не удалось получить данные команды"
            )
            return

        # Копируем данные без имени и iid: импорт в другую задачу не должен
        # получить чужую идентичность.
        export_data = copy.deepcopy(item_data)
        export_data.pop("name", None)
        export_data.pop("iid", None)

        dlg = ExportCommandDlg(self, export_data)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            pass

    def _import_commands_from_server(self) -> None:
        """Загрузить набор в черновик редактора, не меняя сохранённые задачи."""
        if self._download_worker is not None:
            return
        from src.workers.command_download import CommandDownloadWorker
        from src.workers.qthread_registry import track_qthread

        # Поток переживает закрытие редактора: сетевой запрос не прервать мгновенно.
        worker = track_qthread(CommandDownloadWorker(), "Загрузка команд")
        self._download_worker = worker
        self.importServerButton.setEnabled(False)
        worker.ready.connect(self._on_server_commands_downloaded)
        worker.finished.connect(self._on_server_download_finished)
        worker.start()

    def _on_server_download_finished(self) -> None:
        self._download_worker = None
        self.importServerButton.setEnabled(True)

    def _on_server_commands_downloaded(self, result: dict) -> None:
        from src.services.configuration.setup_draft import CommandDraft
        from .import_dialog import CommandImportDialog
        from .conflict_dialog import CommandConflictDialog

        if not result["success"]:
            QMessageBox.critical(self, "Ошибка импорта", result["message"])
            return

        try:
            draft = CommandDraft(self.get_data())
            with tempfile.TemporaryDirectory(prefix="dno-command-import-") as directory:
                path = str(Path(directory) / "commands.json")
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump(result["data"], stream, ensure_ascii=False)
                ok, message, preview = draft.preview_import_from_file(path)
                if not ok:
                    QMessageBox.critical(self, "Ошибка импорта", message)
                    return
                dialog = CommandImportDialog(
                    self, preview_stats=preview, local_count=draft.get_tasks_count()
                )
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return
                mode = dialog.selected_mode()
                resolution = None
                if mode == CommandImportDialog.MODE_MERGE:
                    conflicts = draft.find_import_conflicts(path)
                    if conflicts:
                        conflict_dialog = CommandConflictDialog(self, conflicts=conflicts)
                        if conflict_dialog.exec() != QDialog.DialogCode.Accepted:
                            return
                        resolution = conflict_dialog.resolution()
                ok, message, _ = draft.import_commands_from_file(path, mode, resolution)
            if not ok:
                QMessageBox.critical(self, "Ошибка импорта", message)
                return
            self.model.load_data(draft.get_all_commands())
            self._on_selection_changed(QModelIndex(), QModelIndex())
            self._apply_tree_filter()
            self.modified = True
        except Exception:
            logger.exception("Ошибка импорта задач с сервера")
            QMessageBox.critical(self, "Ошибка импорта", "Не удалось импортировать задачи")

    def done(self, result: int) -> None:
        if self._download_worker is not None:
            self._download_worker.requestInterruption()
            self._download_worker.ready.disconnect(self._on_server_commands_downloaded)
        super().done(result)

    def _import_commands(self):
        """
        Импорт команд.
        Автоматически определяет формат:
        - Полная команда: обновляет текущую выбранную команду (кроме имени)
        - Список команд: добавляет команды в текущую задачу
        """
        dlg = ImportCommandDlg(self)
        try:
            if dlg.exec() == QDialog.DialogCode.Accepted:
                result = dlg.get_decoded_command()

                if result.get("error"):
                    QMessageBox.critical(self, "Ошибка", result["error"])
                    return

                data = result["data"]
                is_full_command = result["is_full_command"]

                if is_full_command:
                    # Импорт полной команды - обновляем текущую выбранную команду
                    current_item = self._get_selected_command_item()
                    if not current_item:
                        QMessageBox.warning(
                            self,
                            "Предупреждение",
                            "Сначала выберите команду для обновления",
                        )
                        return

                    # Получаем текущие данные и сохраняем имя
                    current_data = self.model.get_item_data(current_item)
                    if not current_data:
                        return

                    # Обновляем все поля кроме имени. iid из старого экспорта
                    # отбрасывается: update_item сохранит iid текущей задачи.
                    data["name"] = current_data["name"]
                    data.pop("iid", None)

                    # Обновляем модель
                    self.model.update_item(current_item, data)
                    self._update_editor_from_selection()
                    self.set_modified()
                    QMessageBox.information(self, "Успех", f"Команда успешно обновлена")
                else:
                    # Импорт списка команд - добавляем в текущую задачу
                    current_item = self._get_selected_command_item()
                    if not current_item:
                        QMessageBox.warning(
                            self,
                            "Предупреждение",
                            "Сначала выберите команду для импорта",
                        )
                        return

                    item_data = copy.deepcopy(self.model.get_item_data(current_item))
                    if not item_data:
                        return

                    if "commands" not in item_data:
                        item_data["commands"] = []

                    # Добавляем импортированные команды
                    if isinstance(data, list):
                        for cmd in data:
                            if (
                                isinstance(cmd, dict)
                                and "type" in cmd
                                and "text" in cmd
                            ):
                                item_data["commands"].append(
                                    {**copy.deepcopy(cmd), "type": cmd["type"].lower()}
                                )

                    self.model.update_item(current_item, item_data)
                    self._update_editor_from_selection()
                    self.set_modified()
                    QMessageBox.information(
                        self, "Успех", "Команды успешно импортированы"
                    )

        except Exception as e:
            QMessageBox.critical(
                self, "Ошибка", f"Не удалось импортировать команды: {e}"
            )
            logger.exception("Импорт команд: %s", e)

    def _import_ansible(self):
        """Импорт Ansible-заданий: диалог вставки YAML -> задачи в дерево.

        Выбрана задача - импорт пишется внутрь нее (имя и все поля
        заменяются, iid сохраняется; непустые команды - с вопросом).
        Выбрана папка, корень или ничего - создается новая задача
        (внутрь папки, в родителя команды, в корень).
        """
        from .ansible_import_dialog import AnsibleImportDialog

        dlg = AnsibleImportDialog(self)
        try:
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            tasks = dlg.get_tasks()
            if not tasks:
                QMessageBox.warning(self, "Предупреждение", "Нет задач для импорта")
                return
            current = self.treeView.currentIndex()
            # Внутрь выбранной - только одиночный импорт; поштучный
            # всегда создает новые задачи (однозначность).
            if (
                len(tasks) == 1
                and current.isValid()
                and not self.model.is_category(current)
            ):
                self._import_ansible_into(current, tasks[0], dlg)
                return
            parent_index = self._resolve_add_parent()
            if parent_index.isValid() and not self.model.is_category(parent_index):
                QMessageBox.warning(
                    self,
                    "Ошибка",
                    "Команду можно добавить только в категорию или в корень дерева.",
                )
                return
            added = 0
            for task_data in tasks:
                if self.model.add_item(parent_index, task_data, is_category=False):
                    added += 1
            if added:
                self.treeView.expand(parent_index)
                self._update_editor_from_selection()
                self.set_modified()
                self._show_ansible_success(dlg, added)
        except Exception as e:
            QMessageBox.critical(
                self, "Ошибка", f"Не удалось импортировать Ansible: {e}"
            )
            logger.exception("Импорт Ansible: %s", e)

    def _import_ansible_into(self, index: QModelIndex, task_data: dict, dlg) -> None:
        """Записать импорт внутрь выбранной задачи.

        Имя и все поля заменяются данными импорта, iid сохраняется
        (update_item хранит старый iid только без ключа - его удаляем).
        Непустые команды - с вопросом, отмена оставляет все как было.
        """
        old_data = self.model.get_item_data(index)
        if not old_data:
            return
        if old_data.get("commands"):
            reply = QMessageBox.question(
                self,
                "Задача не пуста",
                f"В задаче '{old_data.get('name', 'Без имени')}' уже есть "
                f"команды ({len(old_data['commands'])}). Заменить их импортом?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        new_data = dict(task_data)
        # iid удалить: иначе задача сменит идентичность (см. update_item).
        new_data.pop("iid", None)
        if self.model.update_item(index, new_data):
            self._update_editor_from_selection()
            self.set_modified()
            self._show_ansible_success(dlg, 1)

    def _show_ansible_success(self, dlg, added: int) -> None:
        """Итоговое сообщение импорта Ansible с предупреждениями (до 10)."""
        warnings = dlg.get_warnings()
        text = f"Импортировано задач: {added}"
        if warnings:
            shown = "\n".join(warnings[:10])
            if len(warnings) > 10:
                shown += f"\n... и еще {len(warnings) - 10}"
            text += f"\n\nПредупреждения:\n{shown}"
        QMessageBox.information(self, "Успех", text)

    def get_data(self) -> list | None:
        return self.model.get_data_structure()

    def _edit_params(self):
        """Редактирование списка параметров выбранной задачи."""
        current_item = self._get_selected_command_item()
        if not current_item:
            return
        item_data = self.model.get_item_data(current_item)
        if not item_data:
            return
        params = item_data.get("params", [])
        dlg = ParamsInputDialog(params, self)
        try:
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            new_params = dlg.get_params()
        finally:
            dlg.deleteLater()
        if new_params == params:
            return
        item_data["params"] = new_params
        if self.model.update_item(current_item, item_data):
            self._update_editor_from_selection()
            self.set_modified()

    def _add_command_to_list_of_type(self, cmd_type: str):
        """Add a new command of specified type"""
        current_item = self._get_selected_command_item()
        if not current_item:
            return

        item_data = self.model.get_item_data(current_item)
        if not item_data:
            return  # Create empty command of specified type
        new_cmd = {"type": cmd_type, "text": ""}

        if cmd_type == "sftp":
            dlg = SFTPCommandEditorDialog(self, custom_params=self._get_current_params())
        elif cmd_type == "ssh":
            dlg = BashEditorDialog(
                self, "", self._get_current_params(), background=False, desktop_session=False
            )
        elif cmd_type == "local":
            dlg = BashEditorDialog(self, "", self._get_current_params())
            dlg.setWindowTitle("Команды на локальном компьютере")
        else:
            return
        try:
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            new_cmd["text"] = dlg.getText()
            if cmd_type == "ssh":
                new_cmd["background"] = dlg.get_background()
                new_cmd["desktop_session"] = dlg.get_desktop_session()
        finally:
            # Окно шага принадлежит редактору: без удаления копилось бы до его закрытия.
            dlg.deleteLater()

        # Add new command to item's commands list
        if "commands" not in item_data:
            item_data["commands"] = []
        item_data["commands"].append(new_cmd)

        # Update model and view
        self.model.update_item(current_item, item_data)
        self._update_editor_from_selection()

        self.set_modified()

    def _edit_command(self, row: int):
        """Edit command at specified row"""
        current_item = self._get_selected_command_item()
        if not current_item:
            return

        item_data = self.model.get_item_data(current_item)
        if not item_data or "commands" not in item_data:
            return

        commands = item_data["commands"]
        if not 0 <= row < len(commands):
            return

        cmd = commands[row]
        cmd_type = cmd.get("type", "ssh")
        cmd_text = cmd.get("text", "")

        if cmd_type == "sftp":
            dlg = SFTPCommandEditorDialog(self, cmd_text, custom_params=self._get_current_params())  # Pass initial_text
        elif cmd_type in ("ssh", "local"):
            dlg = BashEditorDialog(
                self, cmd_text, self._get_current_params(),
                background=cmd.get("background", False) if cmd_type == "ssh" else None,
                desktop_session=desktop_session_for_step(cmd) if cmd_type == "ssh" else None,
            )
            if cmd_type == "local":
                dlg.setWindowTitle("Команды на локальном компьютере")
        else:
            return

        try:
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            new_text = dlg.getText()
            new_background = dlg.get_background() if cmd_type == "ssh" else False
            new_desktop = dlg.get_desktop_session() if cmd_type == "ssh" else False
        finally:
            dlg.deleteLater()
        if (
            new_text == cmd_text and new_background == cmd.get("background", False)
            and new_desktop == desktop_session_for_step(cmd)
        ):
            return
        commands[row]["text"] = new_text
        if cmd_type == "ssh":
            commands[row]["background"] = new_background
            commands[row]["desktop_session"] = new_desktop

        # Update model and view
        self.model.update_item(current_item, item_data)
        self._update_editor_from_selection()

        self.set_modified()

    def _update_editor_from_selection(self):
        """Перечитать форму из модели для текущего выделения."""
        self._load_editor(self.treeView.currentIndex())

    def _delete_selected_command(self):
        """Delete currently selected command from the list"""
        # Вопрос задаётся только когда есть что удалять.
        current_row = self.commandsTable.currentRow()
        if current_row < 0:
            return
        if (
            QMessageBox.question(
                self,
                "Удаление команды",
                "Действительно удалить команду?",
                QMessageBox.Yes | QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return

        current_item = self._get_selected_command_item()
        if not current_item:
            return

        item_data = self.model.get_item_data(current_item).copy()  # Create a copy
        if not item_data or "commands" not in item_data:
            return

        # Remove command and update model
        if 0 <= current_row < len(item_data["commands"]):
            commands = item_data["commands"].copy()  # Create a copy of commands list
            commands.pop(current_row)
            item_data["commands"] = commands
            self.model.update_item(current_item, item_data)
            self._update_editor_from_selection()

        self.set_modified()

    def _get_selected_command_item(self) -> QModelIndex:
        """Get currently selected command item"""
        current = self.treeView.currentIndex()
        if not current.isValid():  # or self.model.is_category(current):
            return None
        return current

    def set_modified(self, force: bool = False):
        """Пометить данные как измененные."""
        if self.modified and not force:
            return
        self.modified = True

    def reject(self):
        """Закрытие окна (Отмена, Esc, крестик): спросить о несохранённых правках.

        QDialog.closeEvent сам вызывает reject и не закрывает окно,
        если оно осталось видимым.
        """
        if not self.modified:
            super().reject()
            return
        result = QMessageBox.question(
            self,
            "Предупреждение",
            "Сохранить изменения?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        result = QMessageBox.StandardButton(result)
        if result == QMessageBox.StandardButton.Yes:
            # При ошибке сохранения окно остаётся открытым.
            if self._apply_changes():
                self.accept()
        elif result == QMessageBox.StandardButton.No:
            logger.info("CommandEditorDialog: Discarding changes")
            super().reject()
        else:
            logger.debug("CommandEditorDialog: Close cancelled by user")
