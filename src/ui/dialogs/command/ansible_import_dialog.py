"""Диалог импорта Ansible-заданий в редактор команд.

В окно вставляется YAML (playbook или список tasks), разбор идет
автоматически при вводе, кнопка "Импорт" возвращает распарсенные
задачи в редактор. Кнопки: справа "Импорт" и "Отмена".
"""

import os
from typing import Any, Dict, List

from PySide6.QtCore import QMimeData, QSettings, Qt, QTimer
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.services.commands.ansible_import import (
    MAX_YAML_CHARS, ImportOptions, can_import, parse_ansible_yaml,
)
from src.ui.widgets.command_preview import CommandPreviewDelegate
from src.ui.widgets.script_editor import ScriptEditor, ShellCodeEditor
from src.workers.tracked_pool import TrackedThreadPoolExecutor

_YAML_SUFFIXES = (".yml", ".yaml")

_SETTINGS_PREFIX = "ansible_import/"
_parse_pool: TrackedThreadPoolExecutor | None = None


def _get_parse_pool() -> TrackedThreadPoolExecutor:
    """Пул создаётся при первом разборе: иначе монитор показывал бы его всегда."""
    global _parse_pool
    if _parse_pool is None:
        _parse_pool = TrackedThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="ansible-preview",
            label="Разбор Ansible",
        )
    return _parse_pool


def _narrow(widget: QWidget, min_width: int = 80) -> QWidget:
    """Поле правой колонки формы (как в топологии сети).

    Поле тянется до правого края формы без зазоров, все поля одной
    ширины (общая колонка формы). Узкая колонка достигается стартовой
    шириной самой панели, не версткой строк.
    """
    widget.setMinimumWidth(min_width)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return widget


def _dropped_yaml_file(mime: QMimeData) -> str | None:
    """Вернуть путь первого .yml/.yaml файла в дропе или None."""
    if not mime.hasUrls():
        return None
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = url.toLocalFile()
        if path.lower().endswith(_YAML_SUFFIXES):
            return path
    return None


class AnsibleYamlEdit(ShellCodeEditor):
    """Поле ввода YAML с приемом .yml/.yaml файлов через drag'n'drop."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Колбэк диалога: путь дропнутого файла (каталог для локальных src).
        self.on_file_dropped = None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if _dropped_yaml_file(event.mimeData()) is not None:
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        path = _dropped_yaml_file(event.mimeData())
        if path is None:
            super().dropEvent(event)
            return
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read(MAX_YAML_CHARS + 1)
                if len(text) > MAX_YAML_CHARS:
                    raise ValueError(f"Файл превышает {MAX_YAML_CHARS} символов")
                self.setPlainText(text)
        except (OSError, UnicodeError, ValueError) as exc:
            QMessageBox.warning(self, "Ошибка", f"Не удалось прочитать файл: {exc}")
            return
        event.acceptProposedAction()
        if self.on_file_dropped is not None:
            self.on_file_dropped(path)


_EXAMPLE_HINT = (
    "Вставьте YAML, перетащите .yml файл или напишите tasks вручную. "
    "Пример: - name: Установить nginx / apt: {name: nginx}"
)


class AnsibleImportDialog(QDialog):
    """Вставка Ansible YAML, проверка и импорт как задач."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.setSizeGripEnabled(True)
        # Каталог дропнутого .yml - база для локальных src (тихий автодетект).
        self._dropped_dir: str = ""
        self._revision = 0
        self._parsed_revision = -1
        self._parse_future = None
        self._parse_revision = -1
        self._parse_pending = False
        self._parse_closed = False
        self._parse_poll = QTimer(self)
        self._parse_poll.setInterval(50)
        self._parse_poll.timeout.connect(self._poll_parse)
        self.finished.connect(self._stop_parse)
        self._result: Dict[str, Any] = {
            "tasks": [],
            "warnings": [],
            "info": [],
            "skipped": [],
            "errors": [],
            "stats": {},
        }
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("Импорт Ansible")
        self.setMinimumSize(600, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        info = QLabel("Вставьте YAML или перетащите .yml файл - разбор автоматический.")
        info.setWordWrap(True)
        info.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        info.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.addWidget(info)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter_v = splitter

        # Автопарсинг с дебаунсом: превью обновляется само при вводе.
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(500)
        self._check_timer.timeout.connect(self._on_check)

        self._input_view = ScriptEditor(editor_class=AnsibleYamlEdit, show_menu=False)
        self._input_view.set_language('YAML')
        self._input = self._input_view.editor
        self._input.setPlaceholderText(_EXAMPLE_HINT)
        self._input.on_file_dropped = self._on_yml_dropped
        self._input.textChanged.connect(self._schedule_check)
        self._highlighter = self._input.highlighter

        self._setup_options_panel()
        options_scroll = QScrollArea()
        options_scroll.setWidgetResizable(True)
        options_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        options_scroll.setMinimumWidth(300)
        options_scroll.setWidget(self._options_box)
        top_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter_h = top_splitter
        top_splitter.addWidget(self._input_view)
        top_splitter.addWidget(options_scroll)
        top_splitter.setStretchFactor(0, 1)
        top_splitter.setStretchFactor(1, 0)
        # Настройки можно задвинуть сплиттером совсем; минимум - 250.
        top_splitter.setCollapsible(0, False)
        top_splitter.setCollapsible(1, True)
        top_splitter.setSizes([800, 250])
        splitter.addWidget(top_splitter)

        self._summary = QLabel("Вставьте YAML - разбор автоматический.")
        self._summary.setWordWrap(True)
        self._summary.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._summary.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

        tabs = QTabWidget()

        self._preview = QTreeWidget()
        self._preview.setHeaderLabels(["Тип", "Задача / шаг", "Сведения"])
        self._preview.setRootIsDecorated(True)
        self._preview.setAlternatingRowColors(True)
        self._preview.setItemDelegateForColumn(1, CommandPreviewDelegate(self._preview))
        self._preview.header().setStretchLastSection(False)
        self._preview.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._preview.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._preview.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._preview.currentItemChanged.connect(self._show_preview_item)

        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_row = QHBoxLayout()
        self._detail_title = QLabel("Выберите задачу или шаг")
        self._detail_title.setWordWrap(True)
        self._detail_title.setTextFormat(Qt.TextFormat.PlainText)
        detail_row.addWidget(self._detail_title, 1)
        self._copy_step = QPushButton("Копировать")
        self._copy_step.setAutoDefault(False)
        self._copy_step.setEnabled(False)
        self._copy_step.clicked.connect(self._copy_preview_item)
        detail_row.addWidget(self._copy_step)
        detail_layout.addLayout(detail_row)
        self._detail_view = ScriptEditor(read_only=True, show_menu=False)
        self._detail = self._detail_view.editor
        self._detail.setPlaceholderText("Здесь появится полный текст выбранного шага.")
        detail_layout.addWidget(self._detail_view, 1)
        tasks_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter_preview = tasks_splitter
        tasks_splitter.addWidget(self._preview)
        tasks_splitter.addWidget(detail_widget)
        tasks_splitter.setChildrenCollapsible(False)
        tasks_splitter.setSizes([400, 400])
        tabs.addTab(tasks_splitter, "Задачи")

        dump_box = QVBoxLayout()
        dump_box.setContentsMargins(0, 0, 0, 0)
        dump_row = QHBoxLayout()
        dump_row.setContentsMargins(0, 0, 0, 0)
        dump_hint = QLabel("Полный результат: команды, параметры и замечания.")
        dump_hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self._copy_button = QPushButton("Копировать")
        self._copy_button.setAutoDefault(False)
        self._copy_button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
        self._copy_button.clicked.connect(self._on_copy_dump)
        dump_row.addWidget(dump_hint)
        dump_row.addWidget(self._copy_button)
        dump_box.addLayout(dump_row)
        self._dump_view = ScriptEditor(read_only=True, show_menu=False)
        self._dump = self._dump_view.editor
        self._dump.setPlaceholderText("Здесь появится полный результат преобразования.")
        dump_box.addWidget(self._dump_view)
        dump_widget = QWidget()
        dump_widget.setLayout(dump_box)
        tabs.addTab(dump_widget, "Текст")

        bottom_box = QVBoxLayout()
        bottom_box.setContentsMargins(0, 0, 0, 0)
        bottom_box.addWidget(self._summary)
        bottom_box.addWidget(tabs, stretch=1)

        bottom_widget = QWidget()
        bottom_widget.setLayout(bottom_box)
        splitter.addWidget(bottom_widget)
        splitter.setSizes([220, 300])

        layout.addWidget(splitter, stretch=1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._import_button = QPushButton("Импорт")
        self._import_button.setEnabled(False)
        self._import_button.clicked.connect(self._on_import)
        self._cancel_button = QPushButton("Отмена")
        self._cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self._import_button)
        buttons.addWidget(self._cancel_button)
        layout.addLayout(buttons)

        self._restore_geometry()

    def _restore_geometry(self) -> None:
        """Размер окна и сплиттеры из QSettings (свободная ширина)."""
        store = QSettings("DNO", "DNOTool")
        geo = store.value(_SETTINGS_PREFIX + "geometry_compact")
        migrate_size = geo is None
        if migrate_size:
            geo = store.value(_SETTINGS_PREFIX + "geometry")
        if geo is not None:
            try:
                if not self.restoreGeometry(geo):
                    self.resize(600, 420)
                elif migrate_size:
                    self.resize(round(self.width() * 0.75), round(self.height() * 0.75))
            except Exception:  # noqa: BLE001 - битая геометрия: дефолт ниже
                self.resize(600, 420)
        else:
            self.resize(600, 420)
        for key, splitter in (
            ("splitter_h", self._splitter_h),
            ("splitter_v", self._splitter_v),
            ("splitter_preview", self._splitter_preview),
        ):
            state = store.value(_SETTINGS_PREFIX + key)
            if state is not None:
                try:
                    splitter.restoreState(state)
                except Exception:  # noqa: BLE001 - оставить дефолтные sizes
                    pass

    def _save_geometry(self) -> None:
        """Записать размер окна и сплиттеры в QSettings."""
        store = QSettings("DNO", "DNOTool")
        store.setValue(_SETTINGS_PREFIX + "geometry_compact", self.saveGeometry())
        store.setValue(_SETTINGS_PREFIX + "splitter_h", self._splitter_h.saveState())
        store.setValue(_SETTINGS_PREFIX + "splitter_v", self._splitter_v.saveState())
        store.setValue(_SETTINGS_PREFIX + "splitter_preview", self._splitter_preview.saveState())

    def accept(self) -> None:
        self._save_geometry()
        super().accept()

    def reject(self) -> None:
        self._save_geometry()
        super().reject()

    def _setup_options_panel(self) -> None:
        """Панель настроек преобразования справа от поля вставки.

        Как в топологии сети: строки "подпись слева + поле справа",
        поля тянутся на всю колонку без зазоров. Узкая колонка -
        шириной самой панели. Полные формулировки везде, длинные
        значения в закрытых комбо режутся многоточием (полностью -
        в раскрытом списке, тултипе и строке сводки).
        """
        self._options_box = QGroupBox("Преобразование")
        self._options_box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self._options_box.setMinimumWidth(280)
        box_layout = QVBoxLayout(self._options_box)
        box_layout.setContentsMargins(8, 8, 8, 8)
        box_layout.setSpacing(2)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        box_layout.addLayout(form)
        box_layout.addStretch(1)

        saved = self._load_options()

        self._privileges = QComboBox()
        self._privileges.addItem("Как в playbook", "playbook")
        self._privileges.addItem("Всегда sudo", "sudo")
        self._privileges.addItem("Всегда su", "su")
        self._privileges.addItem("Запуск от root", "none")
        self._privileges.setToolTip(
            "Привилегии: кто выполняет шаги - как написано в playbook "
            "(become) или принудительно для всех шагов. "
            "«Запуск от root» - без повышения: шаги идут от пользователя "
            "подключения (подразумевается root)."
        )
        form.addRow("Привилегии:", _narrow(self._privileges))

        self._when_mode = QComboBox()
        self._when_mode.addItem("Встроить в шаги", "inline")
        self._when_mode.addItem("Комментарием", "comment")
        self._when_mode.setToolTip(
            "Проверки when: shell-проверки внутри шагов или "
            "только пометка комментарием (режим аудита)."
        )
        form.addRow("Проверки when:", _narrow(self._when_mode))

        self._unknown_modules = QComboBox()
        self._unknown_modules.addItem("Пропустить", "skip")
        self._unknown_modules.addItem("Заглушка TODO", "todo")
        self._unknown_modules.setToolTip(
            "Модули без shell-эквивалента: пропустить или вставить "
            "шаг-заглушку для ручного выполнения."
        )
        unknown_caption = QLabel("Неизвестные модули:")
        unknown_caption.setToolTip(
            "Модули без shell-эквивалента: пропустить или вставить "
            "шаг-заглушку для ручного выполнения."
        )
        unknown_caption.setWordWrap(True)
        form.addRow(unknown_caption, _narrow(self._unknown_modules))

        self._register_mode = QComboBox()
        self._register_mode.addItem("Комментарий (безопасно)", "comment")
        self._register_mode.addItem("Склейка в один блок", "fusion")
        self._register_mode.addItem("Общий shell на задачу", "session")
        self._register_mode.setToolTip(
            "Переменные register и set_fact: как переносить в задачу. "
            "Комментарий - шаги с register комментируются. "
            "Склейка - цепочка в один блок. "
            "Общий shell - задача выполняется в persistent shell."
        )
        form.addRow("Переменные register:", _narrow(self._register_mode))

        self._soften_errors = QCheckBox("Глушить ошибки")
        self._soften_errors.setToolTip(
            "ignore_errors и failed_when: false гасят ошибки шагов (|| true). "
            "Выкл - шаги без глушения."
        )
        form.addRow(self._soften_errors)

        self._single_task = QCheckBox("Слить в одну задачу")
        self._single_task.setToolTip(
            "Весь playbook - одна задача. Выкл - задача на каждую "
            "задачу Ansible (в выбранную пишется только одиночный импорт)."
        )
        form.addRow(self._single_task)

        self._host_facts = QCheckBox("Сведения о хосте")
        self._host_facts.setToolTip(
            "Задача setup - шаг с выводом сведений о хосте. Выкл - пропуск как раньше."
        )
        form.addRow(self._host_facts)

        self._comments = QCheckBox("Комментарии к шагам")
        self._comments.setToolTip(
            "Шаги-подсказки (# handler, # when, # delegate_to). "
            "Выкл - убрать, команды не трогаем."
        )
        form.addRow(self._comments)

        self._handlers = QCheckBox("Обработчики — вручную")
        self._handlers.setToolTip(
            "Handlers всегда пропускаются с причиной: notify/changed не моделируются."
        )
        self._handlers.setEnabled(False)
        form.addRow(self._handlers)

        self._normalize_dnf4 = QCheckBox("Заменять dnf4 на dnf")
        self._normalize_dnf4.setToolTip(
            "Использовать штатную команду dnf для Ansible backend dnf4 "
            "в пакетных операциях и проверках ansible_pkg_mgr."
        )
        form.addRow(self._normalize_dnf4)

        self._stage_dir = QLineEdit()
        self._stage_dir.setPlaceholderText("/tmp")
        self._stage_dir.setToolTip(
            "Временный каталог на хосте для заливки файлов "
            "(copy, script, unarchive, patch)."
        )
        stage_caption = QLabel("Временный каталог:")
        stage_caption.setToolTip(
            "Каталог временных файлов на хосте для заливки "
            "(copy, script, unarchive, patch)."
        )
        stage_caption.setWordWrap(True)
        form.addRow(stage_caption, _narrow(self._stage_dir))

        self._apply_options(saved)
        self._privileges.currentIndexChanged.connect(self._on_option_changed)
        self._when_mode.currentIndexChanged.connect(self._on_option_changed)
        self._unknown_modules.currentIndexChanged.connect(self._on_option_changed)
        self._register_mode.currentIndexChanged.connect(self._on_option_changed)
        self._soften_errors.toggled.connect(self._on_option_changed)
        self._single_task.toggled.connect(self._on_option_changed)
        self._host_facts.toggled.connect(self._on_option_changed)
        self._comments.toggled.connect(self._on_option_changed)
        self._handlers.toggled.connect(self._on_option_changed)
        self._normalize_dnf4.toggled.connect(self._on_option_changed)
        self._stage_dir.textChanged.connect(self._on_option_changed)
        # Полное текущее значение - в тултип (самые длинные пункты
        # могут резаться; раскрытый список и сводка показывают целиком).
        self._combo_tips = {
            self._privileges: self._privileges.toolTip(),
            self._when_mode: self._when_mode.toolTip(),
            self._unknown_modules: self._unknown_modules.toolTip(),
            self._register_mode: self._register_mode.toolTip(),
        }
        for combo in self._combo_tips:
            combo.currentIndexChanged.connect(
                lambda _=0, c=combo: self._refresh_combo_tip(c)
            )
            self._refresh_combo_tip(combo)

    def _refresh_combo_tip(self, combo: QComboBox) -> None:
        """Дописать текущее значение в тултип комбобокса."""
        base = self._combo_tips.get(combo, "")
        combo.setToolTip(f"{base}\nСейчас: {combo.currentText()}")

    def _load_options(self) -> ImportOptions:
        """Настройки панели из QSettings (с санитизацией)."""
        store = QSettings("DNO", "DNOTool")
        get = lambda key, default: store.value(_SETTINGS_PREFIX + key, default)
        raw_stage = str(get("stage_dir", "/tmp") or "/tmp")
        opts = ImportOptions(
            privileges=str(get("privileges", "playbook")),
            soften_errors=str(get("soften_errors", "1")) == "1",
            when_mode=str(get("when_mode", "inline")),
            single_task=str(get("single_task", "1")) == "1",
            host_facts=str(get("host_facts", "1")) == "1",
            comments=str(get("comments", "1")) == "1",
            handlers=str(get("handlers", "1")) == "1",
            normalize_dnf4=str(get("normalize_dnf4", "1")) == "1",
            unknown_modules=str(get("unknown_modules", "skip")),
            stage_dir=raw_stage,
            register_mode=str(get("register_mode", "comment")),
        )
        return opts.normalized()

    def _save_options(self) -> None:
        """Записать текущие настройки панели в QSettings."""
        opts = self._options()
        store = QSettings("DNO", "DNOTool")
        store.setValue(_SETTINGS_PREFIX + "privileges", opts.privileges)
        store.setValue(
            _SETTINGS_PREFIX + "soften_errors", "1" if opts.soften_errors else "0"
        )
        store.setValue(_SETTINGS_PREFIX + "when_mode", opts.when_mode)
        store.setValue(
            _SETTINGS_PREFIX + "single_task", "1" if opts.single_task else "0"
        )
        store.setValue(_SETTINGS_PREFIX + "host_facts", "1" if opts.host_facts else "0")
        store.setValue(_SETTINGS_PREFIX + "comments", "1" if opts.comments else "0")
        store.setValue(_SETTINGS_PREFIX + "handlers", "1" if opts.handlers else "0")
        store.setValue(_SETTINGS_PREFIX + "normalize_dnf4", "1" if opts.normalize_dnf4 else "0")
        store.setValue(_SETTINGS_PREFIX + "unknown_modules", opts.unknown_modules)
        store.setValue(_SETTINGS_PREFIX + "stage_dir", opts.stage_dir)
        store.setValue(_SETTINGS_PREFIX + "register_mode", opts.register_mode)

    def _apply_options(self, opts: ImportOptions) -> None:
        """Разложить настройки по виджетам панели."""
        self._set_combo(self._privileges, opts.privileges)
        self._set_combo(self._when_mode, opts.when_mode)
        self._set_combo(self._unknown_modules, opts.unknown_modules)
        self._set_combo(self._register_mode, opts.register_mode)
        self._soften_errors.setChecked(opts.soften_errors)
        self._single_task.setChecked(opts.single_task)
        self._host_facts.setChecked(opts.host_facts)
        self._comments.setChecked(opts.comments)
        self._handlers.setChecked(opts.handlers)
        self._normalize_dnf4.setChecked(opts.normalize_dnf4)
        self._stage_dir.setText(opts.stage_dir)

    @staticmethod
    def _set_combo(combo: QComboBox, data: str) -> None:
        """Выбрать пункт по userData, fallback - первый."""
        idx = combo.findData(data)
        combo.setCurrentIndex(max(idx, 0))

    def _options(self) -> ImportOptions:
        """Собрать настройки из виджетов панели."""
        return ImportOptions(
            privileges=str(self._privileges.currentData() or "playbook"),
            soften_errors=self._soften_errors.isChecked(),
            when_mode=str(self._when_mode.currentData() or "inline"),
            single_task=self._single_task.isChecked(),
            host_facts=self._host_facts.isChecked(),
            comments=self._comments.isChecked(),
            handlers=self._handlers.isChecked(),
            normalize_dnf4=self._normalize_dnf4.isChecked(),
            unknown_modules=str(self._unknown_modules.currentData() or "skip"),
            stage_dir=self._stage_dir.text().strip() or "/tmp",
            register_mode=str(self._register_mode.currentData() or "comment"),
        ).normalized()

    def _on_option_changed(self, *_: Any) -> None:
        """Настройка изменена: сохранить и перепроверить."""
        self._save_options()
        self._schedule_check()

    @staticmethod
    def _options_summary(opts: ImportOptions) -> str:
        """Отличия от дефолтов одной строкой для сводки."""
        parts = []
        priv_labels = {
            "sudo": "всегда sudo",
            "su": "всегда su",
            "none": "root (без повышения)",
        }
        if opts.privileges in priv_labels:
            parts.append(f"привилегии: {priv_labels[opts.privileges]}")
        if not opts.soften_errors:
            parts.append("без глушения ошибок")
        if opts.when_mode == "comment":
            parts.append("when: только комментарием")
        if not opts.single_task:
            parts.append("поштучно")
        if not opts.host_facts:
            parts.append("без сведений о хосте")
        if not opts.comments:
            parts.append("без комментариев")
        if not opts.handlers:
            parts.append("без обработчиков")
        if not opts.normalize_dnf4:
            parts.append("dnf4 сохранён")
        if opts.unknown_modules == "todo":
            parts.append("неизвестное: заглушка TODO")
        if opts.register_mode != "comment":
            reg_names = {"fusion": "склейка в блок", "session": "общий shell"}
            parts.append(
                f"переменные register: {reg_names.get(opts.register_mode, '')}"
            )
        if opts.stage_dir != "/tmp":
            parts.append(f"врем. каталог: {opts.stage_dir}")
        return ("Настройки: " + "; ".join(parts) + ".") if parts else ""

    def _schedule_check(self) -> None:
        """Перезапустить дебаунс автопарсинга."""
        self._revision += 1
        if hasattr(self, "_import_button"):
            self._import_button.setEnabled(False)
        self._check_timer.start()

    def _on_yml_dropped(self, path: str) -> None:
        """Дроп .yml: запомнить каталог файла для локальных src."""
        self._dropped_dir = os.path.dirname(os.path.abspath(path))
        self._schedule_check()

    def _base_dir(self) -> str:
        """Каталог локальных файлов: папка .yml при дропе, иначе cwd."""
        return self._dropped_dir or os.getcwd()

    def _on_check(self) -> None:
        """One active parse per dialog; coalesce edits rather than queue stale jobs."""
        if self._parse_closed:
            return
        self._import_button.setEnabled(False)
        self._summary.setText("Разбор YAML…")
        if self._parse_future is not None:
            self._parse_pending = True
            return
        self._parse_revision = self._revision
        self._parse_pending = False
        self._parse_future = _get_parse_pool().submit(
            parse_ansible_yaml,
            self._input.toPlainText(),
            self._base_dir(),
            self._options(),
            metadata={"type": "ansible_preview"},
        )
        self._parse_poll.start()

    def _poll_parse(self) -> None:
        future = self._parse_future
        if future is None or not future.done():
            return
        self._parse_future = None
        self._parse_poll.stop()
        try:
            result = future.result()
        except Exception as exc:
            result = {"tasks": [], "warnings": [], "info": [], "skipped": [],
                      "errors": [f"Ошибка разбора: {exc}"], "stats": {}}
        if self._parse_closed:
            return
        if self._parse_revision == self._revision:
            self._result = result
            self._parsed_revision = self._revision
            self._render_preview()
            self._import_button.setEnabled(can_import(result))
        if self._parse_pending:
            self._on_check()

    def _stop_parse(self, *_):
        self._parse_closed = True
        self._check_timer.stop()
        self._parse_poll.stop()
        if self._parse_future is not None:
            self._parse_future.cancel()

    def _on_import(self) -> None:
        """Принять импорт (перед этим проверить актуальный текст)."""
        self._check_timer.stop()
        if self._parsed_revision != self._revision:
            self._on_check()
            return
        if not can_import(self._result):
            return
        self.accept()

    def _on_copy_dump(self) -> None:
        """Скопировать текстовый дамп в буфер обмена."""
        QApplication.clipboard().setText(self._dump.toPlainText())

    @staticmethod
    def _format_step(step: Dict[str, Any]) -> str:
        """Строка шага с типом; sftp показывает стрелкой вместо '::'."""
        ctype = step.get("type", "ssh")
        text = step.get("text", "")
        if ctype == "sftp":
            text = text.replace("::", " => ")
        return f"[{ctype}] {text}"

    def _show_preview_item(self, current, previous=None) -> None:
        """Полный текст хранится отдельно от короткого превью в дереве."""
        payload = current.data(0, Qt.ItemDataRole.UserRole) if current else None
        self._detail.highlighter.setDocument(None)
        self._detail.setPlainText(payload["text"] if payload else "")
        if payload and payload.get("shell"):
            self._detail.highlighter.setDocument(self._detail.document())
            self._detail.highlighter.refresh_theme(self._detail.palette())
        self._detail_title.setText(payload["title"] if payload else "Выберите задачу или шаг")
        self._copy_step.setEnabled(bool(payload and payload["text"]))

    def _copy_preview_item(self) -> None:
        QApplication.clipboard().setText(self._detail.toPlainText())

    @staticmethod
    def _comment_text(text: str) -> str:
        return "\n".join("# " + line for line in str(text).split("\n"))

    @staticmethod
    def _skip_details(item: Dict[str, Any]) -> str:
        reason = item.get("reason", "")
        source = item.get("source")
        if source:
            path = ".".join(map(str, source.get("block_path", ())))
            reason += (f"\nИсточник: play #{source.get('play_id', 0)}, "
                       f"{source.get('section', 'tasks')} [{path}], "
                       f"строка {source.get('line', 0)}")
        return reason

    def _full_result_text(self) -> str:
        """Команды остаются полным shell-текстом; метаданные — комментарии."""
        lines = []
        for task in self._result.get("tasks", []):
            lines.append(self._comment_text(f"=== {task.get('name', 'Без имени')} ==="))
            if task.get("session"):
                lines.append("# Общий shell: переменные сохраняются между шагами")
            for i, name in enumerate(task.get("params", []), 1):
                lines.append(self._comment_text(f"%param{i}% = {name}"))
            for i, step in enumerate(task.get("commands", []), 1):
                kind = step.get("type", "ssh")
                lines.extend(("", self._comment_text(f"Шаг {i} · {kind.upper()}")))
                text = step.get("text", "")
                if kind == "ssh":
                    lines.append(text)
                elif kind == "sftp" and "::" in text:
                    source, destination = text.split("::", 1)
                    lines.append(self._comment_text(f"Источник: {source}\nНазначение: {destination}"))
                else:
                    lines.append(self._comment_text(text))
            lines.append("")
        for key, caption in (("errors", "Ошибки"), ("warnings", "Предупреждения"),
                             ("info", "Информация")):
            entries = self._result.get(key, [])
            if entries:
                lines.append(self._comment_text(f"=== {caption} ({len(entries)}) ==="))
                lines.extend(self._comment_text(entry) for entry in entries)
                lines.append("")
        skipped = self._result.get("skipped", [])
        if skipped:
            lines.append(self._comment_text(f"=== Пропущено ({len(skipped)}) ==="))
            lines.extend(self._comment_text(
                f"{item.get('task', '?')} [{item.get('module', '?')}]: {self._skip_details(item)}"
            ) for item in skipped)
        return "\n".join(lines).rstrip()

    def _preview_entry(self, parent, kind, caption, text, details="", shell=False):
        item = QTreeWidgetItem([kind, caption, details])
        item.setData(0, Qt.ItemDataRole.UserRole, {
            "title": f"{kind} · {details}" if details else kind,
            "text": text,
            "shell": shell,
        })
        item.setToolTip(1, caption[:1000])
        if shell:
            item.setFont(1, self._detail.font())
        if parent is None:
            self._preview.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    def _render_preview(self) -> None:
        result = self._result
        self._preview.clear()
        stats = result.get("stats", {})
        errors: List[str] = result.get("errors", [])
        tasks: List[Dict[str, Any]] = result.get("tasks", [])
        warnings: List[str] = result.get("warnings", [])
        info: List[str] = result.get("info", [])
        skipped: List[Dict[str, str]] = result.get("skipped", [])

        if errors:
            text = "; ".join(errors)
            if len(text) > 160:
                text = text[:160] + "…"
            self._summary.setText(
                "Ошибки: " + text + (f" Понято задач: {len(tasks)}." if tasks else "")
            )
        else:
            params_total = sum(len(t.get("params", [])) for t in tasks)
            opts = self._options()
            reg_labels = {
                "comment": "переменные register: комментарий",
                "fusion": "переменные register: склейка в блок",
                "session": "переменные register: общий shell",
            }
            summary = (
                f"Задач Ansible: {stats.get('ansible_tasks', 0)}, "
                f"будет создано: {len(tasks)} "
                f"(шагов: {stats.get('steps', 0)}, "
                f"параметров: {params_total}), "
                f"пропущено: {len(skipped)}. "
                f"{reg_labels.get(opts.register_mode, '')}."
            )
            extra = self._options_summary(self._options())
            if extra:
                summary += f" {extra}"
            self._summary.setText(summary)

        for task in tasks:
            commands = task.get("commands", [])
            full_text = "\n\n".join(
                f"# Шаг {i} · {step.get('type', 'ssh').upper()}\n"
                + (step.get("text", "") if step.get("type", "ssh") == "ssh"
                   else "# " + self._format_step(step).replace("\n", "\n# "))
                for i, step in enumerate(commands, 1)
            )
            top = self._preview_entry(
                None, "Задача", task.get("name", "Без имени"), full_text,
                f"Шагов: {len(commands)}", shell=True,
            )
            top.setFont(1, self.font())
            bold = QFont(self.font())
            bold.setBold(True)
            top.setFont(0, bold)
            if task.get("session"):
                self._preview_entry(top, "Shell", "Общая сессия", "Все шаги выполняются в общем shell: переменные сохраняются между шагами.")
            for number, step in enumerate(commands, 1):
                ctype = step.get("type", "ssh")
                text = step.get("text", "")
                detail = f"#{number} · {text.count(chr(10)) + 1} стр."
                if ctype == "sftp":
                    source, separator, destination = text.partition("::")
                    transfer = (f"Источник: {source}\nНазначение: {destination}"
                                if separator else text)
                    self._preview_entry(top, ctype, self._format_step(step), transfer, detail)
                else:
                    self._preview_entry(top, ctype, text, text, detail, shell=ctype == "ssh")
            params = task.get("params", [])
            if params:
                numbered = ", ".join(
                    f"%param{i + 1}% = {name}" for i, name in enumerate(params)
                )
                self._preview_entry(top, "Параметры", numbered, "\n".join(
                    f"%param{i + 1}% = {name}" for i, name in enumerate(params)
                ), f"Всего: {len(params)}")
            top.setExpanded(True)

        if errors:
            err_top = self._preview_entry(None, "Ошибки", "Импорт недоступен", "\n\n".join(errors), str(len(errors)))
            for err in errors:
                self._preview_entry(err_top, "Ошибка", err, err)
            err_top.setExpanded(True)

        if warnings:
            warn_top = self._preview_entry(None, "Внимание", "Проверьте перед импортом", "\n\n".join(warnings), str(len(warnings)))
            for warn in warnings:
                self._preview_entry(warn_top, "Проверить", warn, warn)
            warn_top.setExpanded(True)

        if info:
            info_top = self._preview_entry(None, "Информация", "Преобразования и режимы", "\n\n".join(info), str(len(info)))
            for note in info:
                self._preview_entry(info_top, "Справка", note, note)
            info_top.setExpanded(True)

        if skipped:
            skip_top = self._preview_entry(None, "Пропущено", "Не будет создано", "\n\n".join(
                f"{item.get('task', '?')} [{item.get('module', '?')}]: {self._skip_details(item)}"
                for item in skipped
            ), str(len(skipped)))
            for item in skipped:
                self._preview_entry(
                    skip_top, item.get("module", "?"), item.get("task", "?"),
                    self._skip_details(item), "Вручную",
                )
            skip_top.setExpanded(True)
        self._dump.setPlainText(self._full_result_text())
        self._copy_button.setEnabled(bool(self._dump.toPlainText()))
        if self._preview.topLevelItemCount():
            first = self._preview.topLevelItem(0)
            # Сразу показываем код первого шага, а не пустую правую панель.
            selected = next((first.child(i) for i in range(first.childCount())
                             if first.child(i).text(0) in ("ssh", "sftp")), first)
            self._preview.setCurrentItem(selected)

    def get_tasks(self) -> List[Dict[str, Any]]:
        """Вернуть распарсенные задачи для вставки в редактор.

        Внутренние поля ansible_* - только для превью и связки
        notify, сюда не попадают.
        """
        cleaned: List[Dict[str, Any]] = []
        for task in self._result.get("tasks", []):
            item = dict(task)
            for key in (
                "ansible_source",
                "ansible_name",
                "ansible_play",
                "ansible_notify",
                "ansible_listen",
                "ansible_handler",
            ):
                item.pop(key, None)
            cleaned.append(item)
        return cleaned

    def get_warnings(self) -> List[str]:
        """Вернуть предупреждения последнего разбора."""
        return list(self._result.get("warnings", []))
