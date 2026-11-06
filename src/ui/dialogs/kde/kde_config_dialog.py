"""Интерфейс редактора KDE: представление поверх адресного контроллера."""

from __future__ import annotations

import difflib
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QSignalBlocker, QSize, Qt, QTimer
from PySide6.QtGui import (
    QCloseEvent,
    QFont,
    QFontDatabase,
    QKeySequence,
    QPalette,
    QResizeEvent,
    QShortcut,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.config.kde_labels import group_title
from src.config.kde_schema import (
    KDE_FILES,
    catalog_status,
)
from src.di import get_container
from src.domain.kde import KdeKey, KdeProfile, KdeSnapshot
from src.domain.models.device import DeviceModel
from src.services.hosts.kde_service import KdeConnection, KdeService
from src.ui.dialogs.kde.controller import KdeController
from src.ui.dialogs.kde.delegates import KdeItemDelegate
from src.ui.dialogs.kde.setting_row import KdeSettingRow
from src.ui.dialogs.kde.settings_model import KdeSettingsFilter, KdeSettingsModel
from src.ui.icons import _icon_from_key

NAVIGATION = (
    "Обзор",
    "Ограничения доступа",
    "Оформление",
    "Приложения",
    "Экспертные параметры",
    "Изменения",
    "Восстановление",
)


def _label(text: str = "") -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def _plain(text: str = "") -> QPlainTextEdit:
    widget = QPlainTextEdit()
    widget.setReadOnly(True)
    widget.setPlainText(text)
    widget.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    return widget


class KDEConfigDialog(QDialog):
    """Виджеты не выполняют SSH и не являются хранилищем черновика."""

    def __init__(
        self,
        device: DeviceModel,
        parent: QWidget | None = None,
        *,
        service: KdeService | None = None,
    ) -> None:
        super().__init__(parent)
        self.device = device
        self.controller = KdeController(
            KdeConnection.from_device(device),
            service or get_container().resolve(KdeService),
            self,
        )
        self._started = False
        self._closing = False
        self._close_when_idle = False
        self._after_save: Callable[[], None] | None = None
        self._snapshot: KdeSnapshot | None = None
        self._profiles: tuple[KdeProfile, ...] = ()
        self.setting_rows: dict[KdeKey, KdeSettingRow] = {}
        self._row_signature: tuple[KdeKey, ...] = ()
        self._row_limit = 40
        self._catalog_file = ""
        self._status_text = ""
        self._visible_sections = tuple(range(len(NAVIGATION)))
        self._navigation_labels = list(NAVIGATION)
        self.setWindowTitle(f"Настройки KDE — {device.name or device.host}")
        self.setWindowIcon(_icon_from_key("menu_kde"))
        self.setSizeGripEnabled(True)
        screen = self.screen()
        area = screen.availableGeometry() if screen else None
        self.setMinimumSize(1040, 700)
        self.resize(
            min(1180, area.width() - 32) if area else 1180,
            min(800, area.height() - 48) if area else 800,
        )
        self._build()
        self.setMinimumWidth(
            max(1040, self.sidebar.minimumWidth() + self.pages.minimumWidth() + 65)
        )
        self._connect()
        self._refresh()
        self.navigation.setCurrentRow(1)

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 12)
        content = QWidget()
        self.content_scroll = QScrollArea(self)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setWidget(content)
        outer.addWidget(self.content_scroll, 1)
        root = QVBoxLayout(content)
        root.setContentsMargins(14, 10, 14, 6)
        root.setSpacing(12)
        heading = QHBoxLayout()
        title = _label("Настройки KDE")
        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF(), 10) * 1.5)
        font.setBold(True)
        title.setFont(font)
        heading.addWidget(title, 1)
        self.refresh_button = QPushButton("Обновить")
        self.actions_button = QPushButton("Действия")
        menu = QMenu(self.actions_button)
        self.export_baseline_action = menu.addAction("Экспорт исходного снимка…")
        self.export_draft_action = menu.addAction("Экспорт текущего черновика…")
        menu.addSeparator()
        self.catalog_action = menu.addAction("Совместимость каталога…")
        self.help_action = menu.addAction("Сохранение и применение")
        self.actions_button.setMenu(menu)
        heading.addWidget(self.refresh_button)
        heading.addWidget(self.actions_button)
        root.addLayout(heading)
        self.context_label = _label()
        self.context_label.setWordWrap(False)
        self.context_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.sidebar = QWidget()
        self.sidebar.setMinimumWidth(260)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(10)
        side.addWidget(self.context_label)
        self.user_combo = QComboBox()
        self.user_combo.setMinimumContentsLength(14)
        self.user_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.user_combo.setAccessibleName("Целевой пользователь KDE")
        self.user_combo.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.user_combo.addItem("Выберите пользователя…", None)
        self.users_button = QPushButton()
        self.users_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.users_button.setToolTip("Обновить список пользователей")
        self.users_button.setAccessibleName("Обновить список пользователей")
        self.file_combo = QComboBox()
        self.file_combo.setMinimumContentsLength(12)
        self.file_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.file_combo.setAccessibleName("Конфигурационный файл пользователя")
        self.file_combo.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        for filename, description in KDE_FILES.items():
            self.file_combo.addItem(description, filename)
            self.file_combo.setItemData(
                self.file_combo.count() - 1, filename, Qt.ItemDataRole.ToolTipRole
            )
        selectors = QGridLayout()
        for row, (title_text, field) in enumerate(
            (
                ("Пользователь:", self.user_combo),
                ("Файл:", self.file_combo),
            )
        ):
            label = QLabel(title_text)
            label.setBuddy(field)
            selectors.addWidget(label, row, 0)
            if field is self.user_combo:
                selectors.addWidget(field, row, 1)
                selectors.addWidget(self.users_button, row, 2)
            else:
                selectors.addWidget(field, row, 1, 1, 2)
        selectors.setColumnStretch(1, 1)
        side.addLayout(selectors)
        self.file_id_label = _label()
        self.file_id_label.setWordWrap(False)
        side.addWidget(self.file_id_label)
        self.columns = QSplitter(Qt.Orientation.Horizontal)
        self.columns.setChildrenCollapsible(False)
        self.columns.setHandleWidth(12)
        self.columns.setAccessibleName("Ширина панели выбора и области настроек")
        self.navigation = QListWidget()
        self.navigation.addItems(NAVIGATION)
        for index in range(self.navigation.count()):
            self.navigation.item(index).setSizeHint(QSize(170, 42))
        self.navigation.setMinimumWidth(175)
        self.navigation.setWordWrap(True)
        self.navigation.setAccessibleName("Разделы настроек")
        self.navigation.setFrameShape(QFrame.Shape.NoFrame)
        self.navigation.setSpacing(4)
        self.navigation.setCurrentRow(0)
        side.addWidget(self.navigation, 1)
        self.columns.addWidget(self.sidebar)
        self.pages = QStackedWidget()
        self.overview = _plain()
        self.overview.setFont(self.font())
        self.pages.addWidget(self.overview)
        self.pages.addWidget(self._settings_page())
        side.addWidget(self.policy_note)
        self.pages.addWidget(self._review_page())
        self.pages.addWidget(self._recovery_page())
        self.pages.setMinimumWidth(max(680, self.filters_layout.minimumSize().width()))
        self.profile_pages = QStackedWidget()
        self.profile_pages.setMinimumWidth(self.pages.minimumWidth())
        self.empty_profile = QWidget()
        empty = QVBoxLayout(self.empty_profile)
        empty.addStretch(1)
        self.empty_profile_label = _label(
            "Выберите пользователя, чтобы загрузить настройки KDE."
        )
        self.empty_profile_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(self.empty_profile_label)
        empty.addStretch(1)
        self.profile_pages.addWidget(self.empty_profile)
        self.profile_pages.addWidget(self.pages)
        self.columns.addWidget(self.profile_pages)
        self.columns.setStretchFactor(0, 0)
        self.columns.setStretchFactor(1, 1)
        self.columns.setSizes([300, 850])
        handle = self.columns.handle(1)
        handle.setToolTip("Перетащите, чтобы изменить ширину колонок")
        root.addWidget(self.columns, 1)
        self.status_label = _label()
        self.status_label.setWordWrap(False)
        self.status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.status_label.setAccessibleName("Результат операции KDE")
        root.addWidget(self.status_label)
        self.operation_row = QWidget()
        progress = QHBoxLayout(self.operation_row)
        progress.setContentsMargins(0, 0, 0, 0)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setMaximumHeight(8)
        self.cancel_button = QPushButton("Остановить операцию")
        self.compare_button = QPushButton("Сравнить с хостом")
        self.details_button = QPushButton("Подробности ошибки…")
        progress.addWidget(self.progress_bar, 1)
        progress.addStretch(1)
        progress.addWidget(self.compare_button)
        progress.addWidget(self.details_button)
        progress.addWidget(self.cancel_button)
        root.addWidget(self.operation_row)
        footer = QHBoxLayout()
        self.change_label = _label()
        footer.addWidget(self.change_label, 1)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Close
            | QDialogButtonBox.StandardButton.Reset,
            self,
        )
        save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        close = self.buttons.button(QDialogButtonBox.StandardButton.Close)
        reset = self.buttons.button(QDialogButtonBox.StandardButton.Reset)
        if save is None or close is None or reset is None:
            raise RuntimeError("Не удалось создать стандартные кнопки диалога.")
        self.save_button, self.close_button, self.discard_button = save, close, reset
        self.save_button.setText("Сохранить на хост")
        self.save_button.setDefault(True)
        self.close_button.setText("Закрыть")
        self.discard_button.setText("Отменить правки")
        self.review_button = self.buttons.addButton(
            "Проверить изменения", QDialogButtonBox.ButtonRole.ActionRole
        )
        for button in (
            self.discard_button,
            self.review_button,
            self.save_button,
            self.close_button,
        ):
            button.setAutoDefault(False)
        footer.addWidget(self.buttons)
        outer.addLayout(footer)
        for view in (self.navigation, self.review_table, self.backup_list):
            view.setItemDelegate(KdeItemDelegate(view))

    def _settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.section_label = _label("Ограничения доступа")
        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF(), 10) * 1.3)
        font.setBold(True)
        self.section_label.setFont(font)
        layout.addWidget(self.section_label)
        self.section_description = _label()
        layout.addWidget(self.section_description)
        self.policy_note = _label(
            "Личный файл не является защищённой административной политикой. Системные правила показаны для справки; изменить их здесь нельзя."
        )
        self.policy_note.setObjectName("kdePolicyNote")
        self.policy_note.setAutoFillBackground(True)
        self.policy_note.setBackgroundRole(QPalette.ColorRole.AlternateBase)
        self.policy_note.setContentsMargins(12, 10, 12, 10)
        self.policy_note.setContentsMargins(8, 8, 8, 8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по названию, описанию или ключу…  Ctrl+F")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Поиск параметра в текущем файле")
        layout.addWidget(self.search)
        self.restriction_group = QComboBox()
        self.restriction_group.setAccessibleName("Группа параметров KDE")
        for title, group in (
            ("Все категории", ""),
            ("Действия и запуск", "KDE Action Restrictions"),
            ("Модули системных настроек", "KDE Control Module Restrictions"),
            ("Ресурсы", "KDE Resource Restrictions"),
        ):
            self.restriction_group.addItem(title, group)
        self.restriction_group.hide()
        group_policy = self.restriction_group.sizePolicy()
        group_policy.setRetainSizeWhenHidden(True)
        self.restriction_group.setSizePolicy(group_policy)
        self.filters_layout = filters = QHBoxLayout()
        filters.setContentsMargins(0, 0, 0, 0)
        filters.addWidget(self.restriction_group, 1)
        self.changed_filter = QCheckBox("Только изменённые")
        self.local_filter = QCheckBox("Заданные локально")
        self.locked_filter = QCheckBox("Системные ограничения")
        filters.addWidget(self.changed_filter)
        filters.addWidget(self.local_filter)
        filters.addWidget(self.locked_filter)
        layout.addLayout(filters)
        self.model = KdeSettingsModel(self.controller)
        self.proxy = KdeSettingsFilter(self.controller)
        self.proxy.setSourceModel(self.model)
        self.rows_scroll = QScrollArea()
        self.rows_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.rows_scroll.setWidgetResizable(True)
        self.rows_scroll.setMinimumHeight(140)
        self.rows_scroll.setAccessibleName(
            "Параметры KDE: название, значение и источник"
        )
        self.rows_content = QWidget()
        self.rows_content.setObjectName("kdeRows")
        self.rows_layout = QVBoxLayout(self.rows_content)
        self.rows_layout.setContentsMargins(0, 0, 4, 0)
        self.rows_layout.setSpacing(6)
        self.rows_scroll.setWidget(self.rows_content)
        layout.addWidget(self.rows_scroll, 1)
        self.results_label = _label()
        layout.addWidget(self.results_label)
        self.inherit_note = _label(
            "Наследовать — удалить личное переопределение и использовать правила выше. Действие ограничения зависит от поддержки соответствующим приложением KDE."
        )
        layout.addWidget(self.inherit_note)
        return page

    def _review_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_label("Проверка изменений перед записью"))
        self.review_context = _label()
        layout.addWidget(self.review_context)
        layout.addWidget(
            _label(
                "Запись файла и применение к сеансу — разные этапы. Для части настроек потребуется повторный вход. Сеанс не будет прерван автоматически."
            )
        )
        self.review_table = QTableWidget(0, 4)
        self.review_table.setHorizontalHeaderLabels(
            ("Параметр", "Было", "Станет", "Применение")
        )
        self.review_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.review_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.review_table.verticalHeader().hide()
        layout.addWidget(self.review_table, 1)
        self.diff_view = _plain()
        self.diff_view.setAccessibleName("Изменения исходного текста KConfig")
        self.diff_toggle = QCheckBox("Показать технические различия")
        self.diff_toggle.toggled.connect(self.diff_view.setVisible)
        layout.addWidget(self.diff_toggle)
        self.diff_view.hide()
        layout.addWidget(self.diff_view, 1)
        layout.addWidget(
            _label(
                "Перед заменой проверяется версия, создаётся копия, выполняется атомарная замена и повторное чтение. Другие приложения могут изменять файл независимо от dno-tool."
            )
        )
        return page

    def _recovery_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_label("Резервные копии текущего пользователя и файла"))
        layout.addWidget(
            _label(
                "На хосте хранятся до 10 копий на файл. Восстановление показывает различия и создаёт копию текущего состояния. Старый kdeglobals.backup не изменяется."
            )
        )
        self.backup_list = QListWidget()
        self.backup_list.setAccessibleName("Версии для восстановления")
        layout.addWidget(self.backup_list, 1)
        row = QHBoxLayout()
        self.load_backups_button = QPushButton("Обновить копии")
        self.preview_backup_button = QPushButton("Сравнить выбранную копию…")
        row.addWidget(self.load_backups_button)
        row.addWidget(self.preview_backup_button)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def _connect(self) -> None:
        self.controller.changed.connect(self._refresh)
        self.controller.progress.connect(self._set_status)
        self.controller.error.connect(self._operation_error)
        self.controller.saved.connect(self._saved)
        self.controller.conflict_ready.connect(self._resolve_conflict)
        self.controller.preview_ready.connect(self._show_backup)
        self.controller.idle.connect(self._idle)
        self.navigation.currentRowChanged.connect(self._navigate)
        self.user_combo.currentIndexChanged.connect(self._user_selected)
        self.file_combo.currentIndexChanged.connect(self._file_selected)
        self.users_button.clicked.connect(lambda: self._guard(self.controller.discover))
        self.refresh_button.clicked.connect(self._reload)
        self.cancel_button.clicked.connect(self.controller.cancel)
        self.compare_button.clicked.connect(self.controller.compare_remote)
        self.details_button.clicked.connect(self._show_error_details)
        self.review_button.clicked.connect(lambda: self.navigation.setCurrentRow(5))
        self.save_button.clicked.connect(self.controller.save)
        self.discard_button.clicked.connect(self._discard)
        self.close_button.clicked.connect(self.reject)
        self.export_baseline_action.triggered.connect(lambda: self._export(False))
        self.export_draft_action.triggered.connect(lambda: self._export(True))
        self.catalog_action.triggered.connect(self._show_catalog)
        self.help_action.triggered.connect(self._show_help)
        self.search.textChanged.connect(self._filter)
        self.restriction_group.currentIndexChanged.connect(self._filter)
        for checkbox in (self.changed_filter, self.local_filter, self.locked_filter):
            checkbox.toggled.connect(self._filter)
        self.load_backups_button.clicked.connect(self.controller.list_backups)
        self.preview_backup_button.clicked.connect(self._preview_backup)
        for sequence, callback in (
            ("Ctrl+F", self._focus_search),
            ("Ctrl+S", lambda: self.navigation.setCurrentRow(5)),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(callback)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._started:
            self._started = True
            QTimer.singleShot(0, self.controller.discover)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "operation_row"):
            self._update_context()
            self._set_status(self._status_text)

    def _navigate(self, index: int, *, load_backups: bool = True) -> None:
        if index not in self._visible_sections:
            self.navigation.setCurrentRow(self._default_section())
            return
        if index != 5:
            self._after_save = None
        self.pages.setCurrentIndex(
            0 if index == 0 else 2 if index == 5 else 3 if index == 6 else 1
        )
        if index in (1, 2, 3, 4):
            self.restriction_group.show()
            self.proxy.category = {
                1: "restrictions",
                2: "appearance",
                3: "applications",
                4: "expert",
            }[index]
            groups = sorted(
                {
                    setting.key.group
                    for setting in self.model.rows
                    if index == 4 or setting.category == self.proxy.category
                }
            )
            selected_group = self.restriction_group.currentData()
            with QSignalBlocker(self.restriction_group):
                self.restriction_group.clear()
                self.restriction_group.addItem("Все группы", "")
                for group in groups:
                    self.restriction_group.addItem(group_title(group), group)
                self.restriction_group.setCurrentIndex(
                    max(0, self.restriction_group.findData(selected_group))
                )
            self.restriction_group.setEnabled(len(groups) > 1)
            self.section_label.setText(self._navigation_labels[index])
            self.inherit_note.setText(
                "Наследовать — удалить личное переопределение и использовать правила выше. "
                + (
                    "Действие ограничения зависит от поддержки соответствующим приложением KDE."
                    if index == 1
                    else "Применение настройки зависит от приложения и версии KDE."
                )
            )
            self.section_description.setText(
                {
                    1: "Управляйте возможностями пользователя и смотрите источник каждого правила.",
                    2: "Настройте цвета, шрифты и темы на выбранном компьютере.",
                    3: "Изменяйте поведение приложений и компонентов KDE.",
                    4: "Все параметры текущего файла. Непроверенные записи доступны только для просмотра.",
                }[index]
            )
            self._filter()
        if index == 5:
            self._update_review()
        if (
            index == 6
            and load_backups
            and self.controller.ready
            and not self.controller.busy
        ):
            self.controller.list_backups()
        self._refresh_actions()

    def _default_section(self) -> int:
        if not self.controller.ready:
            return 0
        return next(
            (index for index in (1, 2, 3, 4) if index in self._visible_sections), 0
        )

    def _sync_navigation(self, filename: str, *, reset: bool) -> None:
        """Файл определяет задачи; номера общих страниц остаются стабильными."""
        categories = {setting.category for setting in self.model.rows}
        visible = (
            (0,)
            + tuple(
                index
                for index, category in (
                    (1, "restrictions"),
                    (2, "appearance"),
                    (3, "applications"),
                )
                if category in categories
            )
            + (4, 5, 6)
        )
        labels = list(NAVIGATION)
        labels[2] = (
            "Тема рабочего стола" if filename == "plasmarc" else "Цвета, шрифты и темы"
        )
        labels[3] = {
            "dolphinrc": "Поведение Dolphin",
            "kwinrc": "Поведение окон",
            "konsolerc": "Окна и вкладки",
            "krunnerrc": "Поиск и окно",
        }.get(filename, "Параметры приложения")
        self._visible_sections = visible
        self._navigation_labels = labels
        current = self.navigation.currentRow()
        selected = (
            self._default_section() if reset or current not in visible else current
        )
        with QSignalBlocker(self.navigation):
            for index, title in enumerate(labels):
                self.navigation.item(index).setHidden(index not in visible)
                self.navigation.item(index).setText(title)
            self.navigation.setCurrentRow(selected)
        self.file_id_label.setText(filename)
        self.file_id_label.setToolTip(filename)
        self._navigate(selected, load_backups=False)

    def _filter(self) -> None:
        self.proxy.query = self.search.text().strip()
        self.proxy.restriction_group = str(self.restriction_group.currentData())
        self.proxy.only_changed = self.changed_filter.isChecked()
        self.proxy.only_local = self.local_filter.isChecked()
        self.proxy.only_locked = self.locked_filter.isChecked()
        self.proxy.invalidate()
        count = self.proxy.rowCount()
        summary = f"Найдено: {count}."
        if self.proxy.query:
            summary += " Поиск по всем разделам текущего файла."
        elif self.proxy.category != "expert":
            summary += " Другие параметры доступны в экспертном просмотре."
        self.results_label.setText(summary)
        if not self.controller.ready:
            self.results_label.clear()
        self._row_limit = 40
        self._sync_rows()

    def _sync_rows(self, *, force: bool = False) -> None:
        definitions = [
            self.model.rows[self.proxy.mapToSource(self.proxy.index(row, 0)).row()]
            for row in range(self.proxy.rowCount())
            if self.controller.ready
        ]
        visible = definitions[: self._row_limit]
        signature = tuple(definition.key for definition in visible)
        if force or signature != self._row_signature:
            scroll = self.rows_scroll.verticalScrollBar().value()
            self._row_signature = signature
            while self.rows_layout.count():
                item = self.rows_layout.takeAt(0)
                if item is not None and (widget := item.widget()) is not None:
                    widget.hide()
                    widget.setParent(None)
                    widget.deleteLater()
            self.setting_rows.clear()
            for definition in visible:
                row = KdeSettingRow(definition, self.controller)
                row.edited.connect(
                    lambda value, key=definition.key: self._edit(key, value)
                )
                row.revert_requested.connect(
                    lambda key=definition.key: self._revert_key(key)
                )
                self.setting_rows[definition.key] = row
                self.rows_layout.addWidget(row)
            if not visible:
                self.rows_layout.addWidget(
                    _label(
                        "Нет параметров по выбранным условиям. Измените поиск, фильтр или файл."
                    )
                )
            if len(definitions) > len(visible):
                more = QPushButton(f"Показать ещё ({len(definitions) - len(visible)})")
                more.clicked.connect(self._more_rows)
                self.rows_layout.addWidget(more)
            self.rows_layout.addStretch(1)
            self.rows_scroll.verticalScrollBar().setValue(scroll)
        for row in self.setting_rows.values():
            row.sync()

    def _more_rows(self) -> None:
        self._row_limit += 40
        self._sync_rows()

    def _edit(self, key: KdeKey, value: str | None) -> None:
        try:
            self.controller.set_value(key, value)
        except ValueError as error:
            self._set_status(str(error))

    def _revert_key(self, key: KdeKey) -> None:
        if self.controller.draft:
            entry = self.controller.draft.snapshot.document.entries.get(key)
            self._edit(key, entry.value if entry else None)
            self._sync_rows()

    def _refresh(self) -> None:
        controller = self.controller
        if controller.profiles != self._profiles:
            self._profiles = controller.profiles
            with QSignalBlocker(self.user_combo):
                self.user_combo.clear()
                self.user_combo.addItem("Выберите пользователя…", None)
                for profile in self._profiles:
                    suffix = (
                        " · " + ", ".join(profile.sessions) if profile.sessions else ""
                    )
                    self.user_combo.addItem(
                        f"{profile.username} (UID {profile.uid}){suffix}", profile
                    )
                if controller.draft:
                    self._set_profile_combo(controller.draft.snapshot.target.profile)
        snapshot = controller.draft.snapshot if controller.draft else None
        changed_snapshot = snapshot is not self._snapshot
        if snapshot is not self._snapshot or (
            snapshot is None and controller.catalog_filename != self._catalog_file
        ):
            previous_snapshot = self._snapshot
            self._snapshot = snapshot
            self._catalog_file = controller.catalog_filename
            self.model.reload()
            filename = (
                PurePosixPath(snapshot.target.path).name
                if snapshot
                else controller.catalog_filename
            )
            changed_file = (
                previous_snapshot is None
                or snapshot is None
                or PurePosixPath(previous_snapshot.target.path).name != filename
            )
            if snapshot:
                self._restore_selectors()
            self._sync_navigation(filename, reset=changed_file)
        else:
            self.model.refresh()
        self._set_status(controller.message)
        self._update_context()
        self._update_overview()
        self._sync_rows(force=changed_snapshot)
        self._refresh_actions()
        if self.navigation.currentRow() == 5:
            self._update_review()
        selected = self.backup_list.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.backup_list.clear()
        for backup in controller.backups:
            meta = backup["meta"]
            self.backup_list.addItem(
                f"{backup['created_at']} · "
                + (
                    "исходный файл отсутствовал"
                    if not meta["exists"]
                    else str(meta["fingerprint"])[:12]
                )
            )
            item = self.backup_list.item(self.backup_list.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, backup["id"])
            if backup["id"] == selected_id:
                self.backup_list.setCurrentItem(item)

    def _refresh_actions(self) -> None:
        controller = self.controller
        loaded = bool(controller.ready and controller.draft)
        self.profile_pages.setCurrentWidget(
            self.pages if loaded else self.empty_profile
        )
        self.navigation.setEnabled(loaded)
        self.policy_note.setVisible(loaded)
        self.file_id_label.setVisible(loaded)
        self.catalog_action.setEnabled(loaded)
        self.change_label.setVisible(loaded)
        self.discard_button.setVisible(loaded)
        if not loaded:
            self.results_label.clear()
            self.empty_profile_label.setText(
                "Загрузка настроек пользователя…"
                if controller.busy and self.user_combo.currentData() is not None
                else (
                    "Не удалось загрузить настройки. Повторите загрузку или выберите другого пользователя."
                    if self.user_combo.currentData() is not None
                    else "Выберите пользователя, чтобы загрузить настройки KDE."
                )
            )
        count = len(
            set(controller.draft.changes if controller.draft else ())
            | set(controller.invalid)
        )
        self.change_label.setText(
            f"Несохранённых изменений: {count}"
            if count
            else "Нет несохранённых изменений"
        )
        self.navigation.item(5).setText(
            f"Изменения ({count})" if loaded else "Изменения"
        )
        reviewing = self.navigation.currentRow() == 5
        self.save_button.setVisible(loaded and reviewing)
        self.save_button.setEnabled(
            controller.ready
            and not controller.busy
            and controller.dirty
            and not controller.invalid
        )
        self.review_button.setText(
            f"Проверить изменения ({count})" if count else "Проверить изменения"
        )
        self.review_button.setVisible(loaded and not reviewing)
        self.review_button.setEnabled(bool(controller.draft) and not controller.busy)
        self.discard_button.setEnabled(controller.dirty and not controller.busy)
        self.user_combo.setEnabled(not controller.busy and bool(controller.profiles))
        self.file_combo.setEnabled(not controller.busy)
        self.users_button.setEnabled(not controller.busy)
        self.refresh_button.setEnabled(not controller.busy)
        self.cancel_button.setVisible(controller.busy)
        self.progress_bar.setVisible(controller.busy)
        self.details_button.setVisible(
            bool(controller.error_details or controller.error_code)
        )
        self.compare_button.setVisible(controller.error_code in ("conflict", "unknown"))
        self.compare_button.setEnabled(not controller.busy and bool(controller.draft))
        self.export_baseline_action.setEnabled(
            bool(controller.draft) and not controller.busy
        )
        self.export_draft_action.setEnabled(
            bool(controller.draft) and not controller.invalid and not controller.busy
        )
        self.load_backups_button.setEnabled(controller.ready and not controller.busy)
        self.preview_backup_button.setEnabled(
            controller.ready and not controller.busy and bool(controller.backups)
        )

    def _update_overview(self) -> None:
        controller = self.controller
        if not controller.ready:
            self.overview.clear()
            return
        paragraphs = [
            "НАСТРОЙКИ KDE",
            f"Хост: {self.device.host}",
            f"SSH: {controller.ssh_username or 'не определён'}",
            f"Plasma: {controller.version or 'не обнаружена или версия недоступна'}",
        ]
        if controller.draft:
            snapshot = controller.draft.snapshot
            profile = snapshot.target.profile
            paragraphs.extend(
                (
                    "",
                    f"Профиль: {profile.username}, UID {profile.uid}, GID {profile.gid}",
                    f"Домашний каталог: {profile.home}",
                    f"Файл: {snapshot.target.path}",
                    f"Прочитан: {snapshot.loaded_at}",
                    "Сеансы: " + ("; ".join(profile.sessions) or "не обнаружены"),
                    "",
                    snapshot.environment_note,
                )
            )
            if snapshot.document.issues:
                paragraphs.extend(
                    ("", "ДОСТУПЕН ТОЛЬКО ПРОСМОТР", *snapshot.document.issues)
                )
            if not snapshot.exists:
                paragraphs.append(
                    "Личный файл отсутствует. Он появится только после явной правки."
                )
            paragraphs.extend(("", "ИСТОЧНИКИ СИСТЕМНЫХ ЗНАЧЕНИЙ"))
            paragraphs.extend(
                f"{item.path}: {item.error or ('отсутствует' if item.fingerprint == 'missing' else 'прочитан')}"
                for item in snapshot.sources
            )
        paragraphs.extend(
            (
                "",
                "СОХРАНЕНИЕ И ПРИМЕНЕНИЕ",
                "Черновик не отправляется на хост до сохранения. Записываются только явные изменения текущего файла.",
                "Личный файл не является защищённой политикой организации. Системные immutable-правила не переопределяются.",
                "После записи файл проверяется повторно. Действие настройки зависит от приложения и версии KDE; текущий сеанс автоматически не перезапускается.",
                "Поддержанные параметры доступны и без личного файла: это каталог возможностей, а не список записей на диске. Файл создаётся после явной правки. Непроверенные ключи каталога не добавляются автоматически.",
            )
        )
        text = "\n".join(paragraphs)
        if self.overview.toPlainText() != text:
            self.overview.setPlainText(text)

    def _update_context(self) -> None:
        """Две постоянные строки: загрузка не добавляет новые строки в шапку."""
        controller = self.controller
        host = f"{self.device.name or self.device.host} · {self.device.host} · SSH: {controller.ssh_username or 'не определён'}"
        snapshot = (
            controller.draft.snapshot if controller.draft and controller.ready else None
        )
        if snapshot is None:
            profile = "Выберите пользователя · личные значения не загружены"
            if self.user_combo.currentData() is not None:
                profile = "Настройки пользователя ещё не загружены"
        else:
            target = snapshot.target
            prefix = (
                "Снимок" if controller.ready else "Предыдущий снимок (только просмотр)"
            )
            profile = f"{target.profile.username} · {prefix} · UID {target.profile.uid} · {target.path}"
        self.context_label.setFixedHeight(
            self.context_label.fontMetrics().lineSpacing() * 2 + 4
        )
        width = max(40, self.context_label.width())
        self.context_label.setText(
            "\n".join(
                self.context_label.fontMetrics().elidedText(
                    text, Qt.TextElideMode.ElideRight, width
                )
                for text in (host, profile)
            )
        )
        self.context_label.setToolTip(host + "\n" + profile)
        self.context_label.setAccessibleDescription(host + "\n" + profile)
        self.operation_row.setFixedHeight(
            max(
                button.sizeHint().height()
                for button in (
                    self.cancel_button,
                    self.compare_button,
                    self.details_button,
                )
            )
        )

    def _set_status(self, message: str) -> None:
        """Длинная ошибка доступна в подробностях, но не меняет высоту окна."""
        self._status_text = message
        self.status_label.setFixedHeight(
            self.status_label.fontMetrics().lineSpacing() + 4
        )
        self.status_label.setText(
            self.status_label.fontMetrics().elidedText(
                message.replace("\n", " "),
                Qt.TextElideMode.ElideRight,
                max(40, self.status_label.width()),
            )
        )
        self.status_label.setToolTip(message)
        self.status_label.setAccessibleDescription(message)

    def _update_review(self) -> None:
        draft = self.controller.draft
        if not draft:
            self.review_context.setText("Сначала загрузите профиль.")
            self.review_table.setRowCount(0)
            self.diff_view.clear()
            return
        snapshot = draft.snapshot
        self.review_context.setText(
            f"{snapshot.target.host} · {snapshot.target.profile.username} (UID {snapshot.target.profile.uid})\n{snapshot.target.path}"
        )
        changes = sorted(draft.changes.items())
        self.review_table.setRowCount(len(changes))
        for row, (key, value) in enumerate(changes):
            entry = snapshot.document.entries.get(key)
            definition = next(
                (item for item in self.model.rows if item.key == key), None
            )
            for column, text in enumerate(
                (
                    definition.title if definition else key.name,
                    (
                        self.model.format_value(
                            definition, entry.value if entry else None
                        )
                        if definition
                        else entry.value if entry else "Не задано"
                    ),
                    (
                        self.model.format_value(definition, value)
                        if definition
                        else value if value is not None else "Удалить локальную запись"
                    ),
                    definition.apply_hint if definition else "Не определено",
                )
            ):
                item = QTableWidgetItem(text)
                item.setToolTip(key.label)
                self.review_table.setItem(row, column, item)
        if self.controller.invalid:
            self.diff_view.setPlainText(
                "Исправьте ошибки:\n"
                + "\n".join(
                    f"{key.label}: {error}"
                    for key, (_, error) in self.controller.invalid.items()
                )
            )
        else:
            try:
                self.diff_view.setPlainText(
                    self._diff(
                        snapshot.raw, draft.render(), "Исходный снимок", "Черновик"
                    )
                    or "Нет изменений для записи."
                )
            except ValueError as error:
                self.diff_view.setPlainText(str(error))

    @staticmethod
    def _diff(before: bytes, after: bytes, left: str, right: str) -> str:
        return "".join(
            difflib.unified_diff(
                before.decode("utf-8-sig", "replace").splitlines(True),
                after.decode("utf-8-sig", "replace").splitlines(True),
                fromfile=left,
                tofile=right,
            )
        )

    def _set_profile_combo(self, profile: KdeProfile) -> None:
        for index in range(1, self.user_combo.count()):
            item = self.user_combo.itemData(index)
            if (
                isinstance(item, KdeProfile)
                and item.username == profile.username
                and item.uid == profile.uid
            ):
                self.user_combo.setCurrentIndex(index)
                return

    def _restore_selectors(self) -> None:
        if self.controller.draft:
            with QSignalBlocker(self.user_combo):
                self._set_profile_combo(self.controller.draft.snapshot.target.profile)
            with QSignalBlocker(self.file_combo):
                self.file_combo.setCurrentIndex(
                    max(
                        0,
                        self.file_combo.findData(
                            PurePosixPath(
                                self.controller.draft.snapshot.target.path
                            ).name
                        ),
                    )
                )

    def _user_selected(self, index: int) -> None:
        profile = self.user_combo.itemData(index)
        if isinstance(profile, KdeProfile):
            filename = str(self.file_combo.currentData())
            self._guard(
                lambda: self.controller.load(profile, filename), self._restore_selectors
            )
        elif profile is None:
            self._guard(self.controller.clear_profile, self._restore_selectors)

    def _file_selected(self, index: int) -> None:
        profile = self.user_combo.currentData()
        if isinstance(profile, KdeProfile):
            filename = str(self.file_combo.itemData(index))
            self._guard(
                lambda: self.controller.load(profile, filename), self._restore_selectors
            )
        elif self.controller.draft is None:
            filename = str(self.file_combo.itemData(index))
            self.controller.catalog_filename = filename
            self.controller.changed.emit()
            self.navigation.setCurrentRow(
                1 if filename == "kdeglobals" else 2 if filename == "plasmarc" else 3
            )
            self._filter()

    def _reload(self) -> None:
        profile = self.user_combo.currentData()
        if isinstance(profile, KdeProfile):
            filename = str(self.file_combo.currentData())
            self._guard(lambda: self.controller.load(profile, filename))
        else:
            self._guard(self.controller.discover)

    def _guard(
        self, action: Callable[[], None], cancelled: Callable[[], None] | None = None
    ) -> None:
        if self.controller.busy:
            if cancelled:
                cancelled()
            return
        if not self.controller.dirty:
            action()
            return
        box = QMessageBox(self)
        box.setWindowTitle("Несохранённые изменения")
        box.setText("Сохранить черновик перед переходом?")
        box.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if self.controller.invalid or not self.controller.ready:
            box.button(QMessageBox.StandardButton.Save).setEnabled(False)
        result = box.exec()
        if result == QMessageBox.StandardButton.Save:
            if cancelled:
                cancelled()
            self._after_save = action
            self.navigation.setCurrentRow(5)
        elif result == QMessageBox.StandardButton.Discard:
            self.controller.discard()
            action()
        elif cancelled:
            cancelled()

    def _discard(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Отменить правки",
                "Вернуть черновик к исходному снимку?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.controller.discard()
            self._after_save = None
            self._sync_rows()

    def _saved(self) -> None:
        action, self._after_save = self._after_save, None
        if action:
            QTimer.singleShot(0, action)

    def _operation_error(self, code: str, message: str) -> None:
        self._after_save = None
        self._set_status(message)

    def _resolve_conflict(self, remote: KdeSnapshot) -> None:
        local = self.controller.draft
        if not local:
            return
        _, conflicts = local.rebase(remote)
        dialog = QDialog(self)
        dialog.setWindowTitle("Сравнение с текущим файлом хоста")
        dialog.resize(950, 650)
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            _label(
                f"{remote.target.profile.username} · {remote.target.path}\nНезависимые правки объединятся. Для конфликтующих ключей выберите источник."
            )
        )
        tabs = QTabWidget()
        for title, raw in (
            ("Исходный снимок", local.snapshot.raw),
            ("Сейчас на хосте", remote.raw),
            ("Ваш черновик", local.render()),
        ):
            tabs.addTab(_plain(raw.decode("utf-8-sig", "replace")), title)
        layout.addWidget(tabs, 1)
        choices: dict[KdeKey, QComboBox] = {}
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        for key in conflicts:
            choice = QComboBox()
            choice.addItem("Использовать хост", False)
            if not remote.policy(key)[2]:
                choice.addItem("Оставить мой черновик", True)
            form.addRow(key.label, choice)
            choices[key] = choice
        if conflicts:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(form_widget)
            layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Объединить в черновик"
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.controller.resolve_conflict(
                    remote,
                    {key for key, choice in choices.items() if choice.currentData()},
                )
            except ValueError as error:
                self._set_status(str(error))

    def _preview_backup(self) -> None:
        item = self.backup_list.currentItem()
        if item:
            backup_id = str(item.data(Qt.ItemDataRole.UserRole))
            self._guard(lambda: self.controller.preview_backup(backup_id))

    def _show_backup(self, raw: bytes, exists: bool, backup_id: str) -> None:
        if not self.controller.draft:
            return
        snapshot = self.controller.draft.snapshot
        dialog = QDialog(self)
        dialog.setWindowTitle("Проверка восстановления")
        dialog.resize(900, 620)
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            _label(
                f"{snapshot.target.host} · {snapshot.target.profile.username}\n{snapshot.target.path}\n"
                + (
                    "Восстановить содержимое копии."
                    if exists
                    else "Изначально файла не было. Он будет удалён после проверки текущей версии."
                )
            )
        )
        layout.addWidget(
            _plain(
                self._diff(snapshot.raw, raw, "Сейчас", "Выбранная копия")
                or "Содержимое совпадает; могут отличаться метаданные или наличие файла."
            ),
            1,
        )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.RestoreDefaults
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setText(
            "Восстановить эту версию"
        )
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            dialog.accept
        )
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.controller.restore(backup_id)

    def _export(self, draft: bool) -> None:
        if not self.controller.draft:
            return
        snapshot = self.controller.draft.snapshot
        try:
            raw = self.controller.draft.render() if draft else snapshot.raw
            name = f"{PurePosixPath(snapshot.target.path).name}_{snapshot.target.profile.username}_{'draft' if draft else 'snapshot'}.conf"
            filename, _ = QFileDialog.getSaveFileName(
                self,
                "Экспорт черновика" if draft else "Экспорт исходного снимка",
                name,
                "Конфигурация (*.conf);;Все файлы (*)",
            )
            if filename:
                Path(filename).write_bytes(raw)
                self._set_status(
                    f"Экспортирован {'черновик' if draft else 'снимок'} пользователя {snapshot.target.profile.username}."
                )
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Экспорт не выполнен", str(error))

    def _show_catalog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Совместимость исторического каталога")
        dialog.resize(920, 620)
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            _label(
                "Все 587 записей сверены 26.10.2026 с исходниками Plasma 6.8.1, Frameworks 6.31.0 и Gear 26.08.2. Совпадение имени ещё не подтверждает маршрут. Выберите строку для описания и источников."
            )
        )
        entries = catalog_status()
        search = QLineEdit()
        search.setPlaceholderText(
            "Поиск среди всех записей: название, описание, ключ или статус…"
        )
        search.setAccessibleName("Поиск по результатам сверки каталога KDE")
        layout.addWidget(search)
        table = QTableWidget(len(entries), 5)
        table.setHorizontalHeaderLabels(
            ("Название", "Группа KDE", "Ключ KDE", "Сверка", "Редактор")
        )
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, entry in enumerate(entries):
            for column, value in enumerate(
                (
                    entry["title"],
                    entry["group"],
                    entry["key"],
                    entry["audit_status"],
                    (
                        "Редактирование"
                        if entry["status"] == "route_checked"
                        else "Только просмотр"
                    ),
                )
            ):
                item = QTableWidgetItem(value)
                item.setToolTip(entry["description"])
                table.setItem(row, column, item)
        layout.addWidget(table, 1)
        explanation = _plain()
        explanation.setAccessibleName(
            "Назначение настройки, актуальность и источники KDE"
        )
        explanation.setMaximumHeight(200)
        layout.addWidget(explanation)

        def show_entry() -> None:
            row = table.currentRow()
            if row >= 0:
                entry = entries[row]
                explanation.setPlainText(
                    entry["title"]
                    + "\n"
                    + entry["description"]
                    + "\n\nСверка: "
                    + entry["audit_status"]
                    + "\nНайденные маршруты: "
                    + (entry["routes"] or "Не подтверждены")
                    + "\nИсточники:\n"
                    + (entry["source"] or "Совпадений в проверенных исходниках нет.")
                )

        def filter_entries(query: str) -> None:
            parts = query.casefold().split()
            for row, entry in enumerate(entries):
                haystack = " ".join(entry.values()).casefold()
                table.setRowHidden(row, not all(part in haystack for part in parts))

        table.currentCellChanged.connect(lambda *_: show_entry())
        search.textChanged.connect(filter_entries)
        table.selectRow(0)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _show_help(self) -> None:
        QMessageBox.information(
            self,
            "Настройки KDE",
            "Выберите пользователя и файл, проверьте UID и путь. Найдите параметр и измените его в правой панели.\n\nВ разделе «Изменения» проверьте будущую запись и сохраните её. Редактор создаст копию и проверит файл после замены.\n\n«Наследовать» убирает личное переопределение. Системная политика может запрещать изменение.\n\nПрименение зависит от приложения KDE. Оболочка и сеанс автоматически не перезапускаются. Если нужен повторный вход, сначала сохраните открытые документы.\n\nПри конфликте или неизвестном результате выберите «Сравнить с хостом». Предыдущие версии находятся в разделе «Восстановление».",
        )

    def _show_error_details(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Подробности ошибки KDE")
        dialog.resize(760, 440)
        layout = QVBoxLayout(dialog)
        layout.addWidget(_label(self.controller.message))
        layout.addWidget(
            _plain(self.controller.error_details or self.controller.message), 1
        )
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _focus_search(self) -> None:
        if self.navigation.currentRow() not in (1, 2, 3, 4):
            self.navigation.setCurrentRow(4)
        self.search.setFocus()
        self.search.selectAll()

    def _idle(self) -> None:
        if self._close_when_idle and not self.controller.busy:
            self._close_when_idle = False
            QTimer.singleShot(0, self.reject)

    def reject(self) -> None:
        if self._closing:
            if not self.controller.busy:
                super().reject()
        elif self.controller.busy:
            self._close_when_idle = True
            self.controller.cancel()
            self._set_status("Закрываем окно после остановки операции…")
        else:
            self._guard(self._close_clean)

    def _close_clean(self) -> None:
        self._closing = True
        if self.controller.shutdown():
            super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.reject()
