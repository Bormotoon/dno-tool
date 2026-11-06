"""Shell мастера настройки: окно, навигация, кнопки, guards.

Логика шагов живет в setups/*, персист - в core/store.py.
Новый сервис = новый файл в setups/ + запись в core/registry.py,
этот файл не меняется.
"""

from PySide6.QtCore import Qt, QSize, QTimer
from copy import deepcopy
import time
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QFrame,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QButtonGroup,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.config import config
from src.config.settings import DEFAULT_VNC_SERVER
from src.logger import logger
from src.ui.icons import _icon_from_key

from .core.context import WizardContext, VncState
from .core.guard import ThreadGuard
from .core.registry import GROUP_TITLES, STEP_ORDER, active_steps, describe
from .core.store import save_all
from .setups.hosts_commands import CommandsPage, HostsPage
from .setups.packages import PackagesPage
from .setups.remote_install import RemoteInstallPage
from .setups.ssh import SshSetupPage
from .setups.veyon import VeyonSetupPage
from .setups.vnc import VncSetupPage


class SetupWizardDialog(QDialog):
    """Пошаговый мастер первоначальной настройки с древовидной навигацией."""

    def __init__(self, parent=None, main_window=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Мастер настройки DNO Tool")
        self.resize(960, 660)
        self.setMinimumSize(760, 520)

        # main_window сохраняется вызывателями, но мастер его не читает -
        # параметр оставлен для совместимости вызовов.
        self.main_window = main_window
        self.ctx = WizardContext()
        self.guard = ThreadGuard()

        # Сервисы из DI-контейнера (те же синглтоны, что и MainWindow)
        self.device_service = None
        self.command_service = None
        self.veyon_service = None
        self.event_bus = None
        try:
            from src.di import get_container
            from src.services import CommandService, DeviceService, VeyonService
            from src.architecture import EventBus

            container = get_container()
            self.device_service = container.resolve(DeviceService)
            self.command_service = container.resolve(CommandService)
            self.veyon_service = container.resolve(VeyonService)
            self.event_bus = container.resolve(EventBus)
        except Exception as e:
            logger.error(f"SetupWizard: cannot resolve services: {e}")

        from src.services.configuration.config_service import ConfigService
        from src.data import datastore

        self.config_service = get_container().resolve(ConfigService)
        self.ctx.baseline = self.config_service.setup_snapshot(datastore)
        from copy import copy
        from src.services.configuration.credential_service import CredentialService

        self.wizard_credential_service = copy(
            get_container().resolve(CredentialService)
        )
        credential_config = copy(config)
        credential_config.app = deepcopy(self.ctx.baseline["app"])
        credential_config.wizard_draft = True
        credential_config.save = lambda: None
        self.wizard_credential_service._config = credential_config
        self.wizard_credential_service._event_bus = None
        self.ctx.credentials = credential_config.app.credentials
        self.ctx.mode = "existing" if any(self.ctx.baseline["data"]) else "fresh"
        initial = self.ctx.baseline["app"].veyon
        self.ctx.vnc = VncState(
            initial.vnc_server, initial.vnc_port, initial.vnc_password
        )
        self.ctx.veyon.key_name = initial.key_name
        self.ctx.veyon.settings = {
            name: getattr(initial, name)
            for name in (
                "key_name",
                "webapi_url",
                "feature_transport",
                "webapi_connection_limit",
                "server_port",
            )
        }
        self._mode_cache = {}
        self._page_errors = set()
        self._closing = False
        self._active_steps = []
        self._current_step = 0
        self._nav_suppress = False

        self._build_pages()
        self.ssh_page._ssh_bundle_selector.refresh()
        self.ssh_page._ssh_bundle_selector.set_current_iid(
            self.ctx.credentials.default_bundle_iid
        )
        self.veyon_page.set_snapshot(self.ctx.baseline["app"])
        self.commands_page._guard = self.guard
        if self.ctx.mode == "existing":
            self.hosts_page.set_data(self.ctx.baseline["data"][0])
            self.commands_page.set_data(self.ctx.baseline["data"][1])
        self._init_ui()
        if self.ctx.mode == "existing":
            self._update_existing_summary()
        self._rebuild_nav_tree()
        self._update_nav_buttons()
        self._activity_timer = QTimer(self)
        self._activity_timer.timeout.connect(self._update_nav_buttons)
        self._activity_timer.start(200)

    # ================================================================
    #  Страницы
    # ================================================================

    def _build_pages(self):
        self.hosts_page = HostsPage(self)
        self.commands_page = CommandsPage(self)
        self.commands_page.set_command_service(self.command_service)
        self.ssh_page = SshSetupPage(self)
        self.veyon_page = VeyonSetupPage(self, veyon_service=self.veyon_service)
        self.vnc_page = VncSetupPage(self)
        self.vnc_page.set_guard(self.guard)
        self.packages_page = PackagesPage(self)
        self.remote_page = RemoteInstallPage(self)
        self.remote_page.bind(self.guard, self._collect_all, self.veyon_service)
        self.remote_page.install_state_changed.connect(
            lambda _active: self._update_nav_buttons()
        )
        self.guard.register_waiter(
            "veyon", self.veyon_page.wait_threads, 15000, self.veyon_page.request_stop
        )
        self.pages = {
            "intro": None,  # строится в _build_intro_page
            "hosts": self.hosts_page,
            "commands": self.commands_page,
            "local_ssh": self.ssh_page,
            "local_veyon": self.veyon_page,
            "local_vnc": self.vnc_page,
            "remote_packages": self.packages_page,
            "remote": self.remote_page,
            "summary": None,  # строится в _build_summary_page
        }
        from src.workers.setup.types import INSTALLERS
        from .setups.service import ServiceOptionsPage
        for descriptor in INSTALLERS:
            if descriptor.parameters:
                page = ServiceOptionsPage(descriptor, self)
                self.pages[page.sid] = page

    def _page_widget(self, sid: str) -> QWidget:
        page = self.pages.get(sid)
        if page is None:
            if sid == "intro":
                page = self._build_intro_page()
            elif sid == "summary":
                page = self._build_summary_page()
            else:
                raise KeyError(f"Unknown wizard step: {sid}")
            self.pages[sid] = page
        return page

    # ================================================================
    #  UI
    # ================================================================

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("wizardHeader")
        header.setFrameShape(QFrame.Shape.NoFrame)
        header.setStyleSheet("background: palette(window);")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        self._title_label = QLabel()
        self._title_label.setStyleSheet("font-size: 14pt; font-weight: bold;")
        header_layout.addWidget(self._title_label)
        header_layout.addStretch()
        self._step_counter_label = QLabel()
        self._step_counter_label.setStyleSheet("color: gray;")
        header_layout.addWidget(self._step_counter_label)
        layout.addWidget(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: palette(mid);")
        layout.addWidget(sep)

        central = QSplitter(Qt.Orientation.Horizontal)
        central.setHandleWidth(1)
        central.setChildrenCollapsible(False)

        self._nav_tree = QTreeWidget()
        self._nav_tree.setHeaderHidden(True)
        self._nav_tree.setFixedWidth(200)
        self._nav_tree.setIconSize(QSize(20, 20))
        self._nav_tree.setRootIsDecorated(False)
        self._nav_tree.setIndentation(16)
        self._nav_tree.currentItemChanged.connect(self._on_nav_tree_changed)
        central.addWidget(self._nav_tree)

        self._stack = QStackedWidget()
        self._stack_widgets = {}  # sid -> index
        for step in STEP_ORDER:
            widget = self._page_widget(step.sid)
            idx = self._stack.addWidget(widget)
            self._stack_widgets[step.sid] = idx
        central.addWidget(self._stack)
        central.setStretchFactor(1, 1)

        layout.addWidget(central, 1)

        btn_sep = QFrame()
        btn_sep.setFrameShape(QFrame.Shape.HLine)
        btn_sep.setStyleSheet("color: palette(mid);")
        layout.addWidget(btn_sep)

        btn_frame = QFrame()
        btn_frame.setFrameShape(QFrame.Shape.NoFrame)
        btn_layout = QHBoxLayout(btn_frame)
        btn_layout.setContentsMargins(16, 10, 16, 10)

        self._cancel_btn = QPushButton("Отмена")
        self._cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._cancel_btn)

        btn_layout.addStretch()

        self._back_btn = QPushButton("Назад")
        self._back_btn.clicked.connect(self._go_back)
        btn_layout.addWidget(self._back_btn)

        self._next_btn = QPushButton("Далее")
        self._next_btn.clicked.connect(self._go_next)
        self._next_btn.setDefault(True)
        btn_layout.addWidget(self._next_btn)

        self._finish_btn = QPushButton("Готово")
        self._finish_btn.clicked.connect(self._finish)
        self._finish_btn.setVisible(False)
        btn_layout.addWidget(self._finish_btn)

        layout.addWidget(btn_frame)

    # --- Древовидная навигация ---

    def _rebuild_nav_tree(self):
        """Построить дерево навигации на основе выбранных сервисов."""
        self._nav_suppress = True
        self._nav_tree.clear()

        self._active_steps = active_steps(self.ctx.services)

        def add_top(sid):
            item = QTreeWidgetItem([describe(sid).nav_title])
            item.setData(0, Qt.ItemDataRole.UserRole, sid)
            item.setIcon(0, _icon_from_key(describe(sid).icon_key))
            self._nav_tree.addTopLevelItem(item)
            return item

        def add_group(title):
            item = QTreeWidgetItem([title])
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            f = item.font(0)
            f.setBold(True)
            item.setFont(0, f)
            item.setIcon(0, _icon_from_key("folder"))
            self._nav_tree.addTopLevelItem(item)
            return item

        def add_child(parent, sid):
            desc = describe(sid)
            child = QTreeWidgetItem(parent, [desc.nav_title])
            child.setData(0, Qt.ItemDataRole.UserRole, sid)
            child.setIcon(0, _icon_from_key(desc.icon_key))
            return child

        groups = {}  # group -> QTreeWidgetItem
        for step in STEP_ORDER:
            if step.sid not in self._active_steps:
                continue
            if step.group is None:
                add_top(step.sid)
            else:
                if step.group not in groups:
                    # Вставить группу перед первым ее шагом: группы идут
                    # после top-шагов intro/hosts/commands по порядку реестра.
                    groups[step.group] = add_group(
                        GROUP_TITLES.get(step.group, step.group)
                    )
                add_child(groups[step.group], step.sid)

        self._nav_tree.expandAll()
        self._nav_suppress = False

        if self._current_step >= len(self._active_steps):
            self._current_step = len(self._active_steps) - 1
        self._show_step(self._current_step)

    # --- Страница intro ---

    def _build_intro_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        intro_label = QLabel(
            "Этот мастер поможет выполнить первоначальную настройку DNO Tool.\n"
            "Вы можете пройти все шаги или пропустить ненужные - настройка\n"
            "данные сохранятся после «Готово». Создание ключей и установка служб применяются сразу."
        )
        intro_label.setWordWrap(True)
        layout.addWidget(intro_label)

        mode_group_label = QLabel("Выберите режим:")
        mode_group_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(mode_group_label)

        self._mode_fresh = QRadioButton("Чистая установка (начать с пустого списка)")
        self._mode_existing = QRadioButton(
            "Подхватить текущие настройки (хосты/команды)"
        )
        self._mode_fresh.setChecked(self.ctx.mode == "fresh")
        self._mode_existing.setChecked(self.ctx.mode == "existing")

        mode_group = QButtonGroup(self)
        mode_group.addButton(self._mode_fresh)
        mode_group.addButton(self._mode_existing)
        mode_group.buttonClicked.connect(self._on_mode_changed)

        layout.addWidget(self._mode_fresh)
        layout.addWidget(self._mode_existing)

        self._existing_summary = QLabel()
        self._existing_summary.setWordWrap(True)
        self._existing_summary.setStyleSheet("color: gray; padding-left: 20px;")
        self._existing_summary.setVisible(False)
        layout.addWidget(self._existing_summary)

        svc_label = QLabel("Устанавливаемые сервисы:")
        svc_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(svc_label)

        from .core.registry import INSTALLERS

        self._service_checkboxes = {}
        for descriptor in INSTALLERS:
            checkbox = QCheckBox(descriptor.label + " — установка и настройка")
            checkbox.setChecked(self.ctx.services.get(descriptor.key, False))
            checkbox.toggled.connect(self._on_services_changed)
            self._service_checkboxes[descriptor.key] = checkbox
            setattr(self, "_svc_" + descriptor.key + "_checkbox", checkbox)
            layout.addWidget(checkbox)

        server_row = QHBoxLayout()
        server_row.setContentsMargins(20, 0, 0, 0)
        server_label = QLabel("VNC-сервер:")
        server_row.addWidget(server_label)
        from src.ui.dialogs.common.vnc_settings import SERVER_GUIDE, SERVER_LABELS

        self._vnc_server_combo = QComboBox()
        for server, label in SERVER_LABELS.items():
            self._vnc_server_combo.addItem(label, server)
        saved = getattr(config.app.veyon, "vnc_server", DEFAULT_VNC_SERVER) or DEFAULT_VNC_SERVER
        idx = self._vnc_server_combo.findData(saved)
        self._vnc_server_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.ctx.vnc_server = self._vnc_server_combo.currentData() or DEFAULT_VNC_SERVER
        self._vnc_server_combo.currentIndexChanged.connect(self._on_vnc_server_changed)
        self._vnc_server_combo.setToolTip("Какой VNC-сервер установить на хосты")
        server_row.addWidget(self._vnc_server_combo)
        server_row.addStretch()
        layout.addLayout(server_row)

        # Когда выбирать сервер и чем он ограничен - сразу под выбором.
        self._vnc_server_guide = QLabel()
        self._vnc_server_guide.setWordWrap(True)
        self._vnc_server_guide.setTextFormat(Qt.RichText)
        self._vnc_server_guide.setContentsMargins(20, 0, 0, 0)
        self._vnc_server_guide_texts = SERVER_GUIDE
        layout.addWidget(self._vnc_server_guide)
        self._update_vnc_server_guide()

        layout.addStretch()
        return page

    def _update_vnc_server_guide(self) -> None:
        guide = getattr(self, "_vnc_server_guide", None)
        if guide is None:
            return
        server = self._vnc_server_combo.currentData() or DEFAULT_VNC_SERVER
        guide.setText(self._vnc_server_guide_texts.get(server, ""))
        checkbox = getattr(self, "_svc_vnc_checkbox", None)
        guide.setVisible(checkbox is None or checkbox.isChecked())

    def _on_vnc_server_changed(self, _idx: int):
        self.ctx.vnc_server = self._vnc_server_combo.currentData() or DEFAULT_VNC_SERVER
        self._update_vnc_server_guide()

    # --- Страница summary ---

    def _build_summary_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        title = QLabel("Проверьте данные перед сохранением")
        title.setStyleSheet("font-size: 13pt; font-weight: bold;")
        layout.addWidget(title)

        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        self._summary_label.setTextFormat(Qt.RichText)
        layout.addWidget(self._summary_label)

        layout.addSpacing(12)

        self._save_checkbox = QRadioButton("Сохранить настройки и закрыть")
        self._save_checkbox.setChecked(True)
        layout.addWidget(self._save_checkbox)

        self._skip_save_checkbox = QRadioButton("Закрыть без сохранения")
        layout.addWidget(self._skip_save_checkbox)

        layout.addStretch()
        return page

    def _update_summary(self):
        if not self._collect_all(validate=False):
            return
        mode_text = (
            "Чистая установка"
            if self.ctx.mode == "fresh"
            else "Подхват текущих настроек"
        )
        html = (
            f"<p><b>Режим:</b> {mode_text}</p>"
            f"<p>{self.hosts_page.summary_html(self.ctx)}</p>"
            f"<p>{self.commands_page.summary_html(self.ctx)}</p>"
        )
        if self.ctx.services.get("ssh"):
            html += f"<p><b>SSH-ключи:</b> {self.ssh_page.summary_html(self.ctx)}</p>"
        else:
            html += "<p><b>SSH-ключи:</b> не выбран</p>"
        if self.ctx.services.get("veyon"):
            html += f"<p><b>Veyon:</b> {self.veyon_page.summary_html(self.ctx)}</p>"
        else:
            html += "<p><b>Veyon:</b> не выбран</p>"
        if self.ctx.services.get("vnc"):
            html += f"<p><b>VNC:</b> {self.vnc_page.summary_html(self.ctx)}</p>"
        else:
            html += "<p><b>VNC:</b> не выбран</p>"
        html += f"<p>{self.remote_page.summary_html(self.ctx)}</p>"
        html += "<br><p>Нажмите «Готово», чтобы сохранить настройки.</p>"
        html += "<p>Отмена отбрасывает черновик. Выполненные действия с ключами, службами и пакетами сохраняются.</p>"
        self._summary_label.setText(html)

    # ================================================================
    #  Навигация
    # ================================================================

    def _collect_page(self, sid, validate=True):
        if sid in self._page_errors:
            if validate:
                QMessageBox.warning(
                    self,
                    "Ошибка страницы",
                    f"Шаг «{describe(sid).nav_title}» не удалось открыть. "
                    "Продолжение и сохранение недоступны, пока ошибка не устранена.",
                )
            return False
        page = self.pages.get(sid)
        if page is None or not hasattr(page, "collect"):
            return True
        try:
            page.collect(self.ctx)
            if validate:
                ok, message = page.validate(self.ctx)
                if not ok:
                    QMessageBox.warning(self, "Проверьте настройки", message)
                    return False
                if message:
                    QMessageBox.information(self, "Настройки", message)
            return True
        except Exception as error:
            logger.error("Wizard page %s failed: %s", sid, error)
            QMessageBox.critical(
                self,
                "Ошибка страницы",
                f"Не удалось собрать данные: {describe(sid).nav_title}\n{error}",
            )
            return False

    def _collect_all(self, validate=True):
        return all(self._collect_page(sid, validate) for sid in self._active_steps)

    def _transition(self, target):
        if self.guard.is_active() or self._closing:
            return False
        if not self._collect_page(self._active_steps[self._current_step]):
            return False
        if self._active_steps[target] == "remote" and not self._collect_all():
            return False
        self._show_step(target)
        return True

    def _go_next(self):
        if self._current_step < len(self._active_steps) - 1:
            self._transition(self._current_step + 1)

    def _go_back(self):
        if self._current_step > 0:
            self._transition(self._current_step - 1)

    def _show_step(self, step: int):
        self._current_step = step
        sid = self._active_steps[step]
        self._stack.setCurrentIndex(self._stack_widgets[sid])

        self._nav_suppress = True
        self._select_nav_item(sid)
        self._nav_suppress = False

        desc = describe(sid)
        self._title_label.setText(desc.page_title)
        self._step_counter_label.setText(f"Шаг {step + 1} из {len(self._active_steps)}")
        self._update_nav_buttons()

        page = self.pages.get(sid)
        if page is not None and hasattr(page, "on_enter"):
            try:
                page.on_enter(self.ctx)
                self._page_errors.discard(sid)
            except Exception as e:
                self._page_errors.add(sid)
                logger.error(f"SetupWizard: on_enter {sid} failed: {e}")
                QMessageBox.critical(
                    self, "Ошибка страницы", f"Не удалось открыть {desc.nav_title}"
                )
                self._next_btn.setEnabled(False)
                self._finish_btn.setEnabled(False)
        if sid == "summary":
            self._update_summary()

    def _select_nav_item(self, sid: str):
        """Найти элемент дерева с данным sid и сделать его текущим."""
        iter_root = self._nav_tree.invisibleRootItem()
        for i in range(iter_root.childCount()):
            top = iter_root.child(i)
            if top.data(0, Qt.ItemDataRole.UserRole) == sid:
                self._nav_tree.setCurrentItem(top)
                return
            for j in range(top.childCount()):
                child = top.child(j)
                if child.data(0, Qt.ItemDataRole.UserRole) == sid:
                    self._nav_tree.setCurrentItem(child)
                    return

    def _on_nav_tree_changed(self, current, _previous):
        """Обработчик клика по дереву навигации - переход к этапу."""
        if self._nav_suppress or current is None:
            return
        if self.guard.is_active():
            self._nav_suppress = True
            self._select_nav_item(self._active_steps[self._current_step])
            self._nav_suppress = False
            return
        sid = current.data(0, Qt.ItemDataRole.UserRole)
        if sid is None:
            self._nav_suppress = True
            self._select_nav_item(self._active_steps[self._current_step])
            self._nav_suppress = False
            return
        if sid in self._active_steps:
            if not self._transition(self._active_steps.index(sid)):
                self._nav_suppress = True
                self._select_nav_item(self._active_steps[self._current_step])
                self._nav_suppress = False

    def _update_nav_buttons(self):
        is_last = self._current_step == len(self._active_steps) - 1
        is_first = self._current_step == 0
        nav_locked = self.guard.is_active()

        self._back_btn.setEnabled(not is_first and not nav_locked)
        self._next_btn.setVisible(not is_last)
        self._next_btn.setEnabled(
            not nav_locked
            and self._active_steps[self._current_step] not in self._page_errors
        )
        self._finish_btn.setVisible(is_last)
        self._finish_btn.setEnabled(
            not nav_locked and not self._page_errors.intersection(self._active_steps)
        )

    # ================================================================
    #  Обработчики intro
    # ================================================================

    def _on_mode_changed(self, button):
        mode = "fresh" if button == self._mode_fresh else "existing"
        if mode == self.ctx.mode:
            return
        self._mode_cache[self.ctx.mode] = (
            deepcopy(self.hosts_page.get_hosts_data()),
            self.commands_page._command_service.get_all_commands(),
        )
        data = self._mode_cache.get(
            mode, ([], []) if mode == "fresh" else self.ctx.baseline["data"]
        )
        self.ctx.mode = mode
        self.hosts_page.set_data(data[0])
        self.commands_page.set_data(data[1])
        self._existing_summary.setVisible(mode == "existing")
        if mode == "existing":
            self._update_existing_summary()

    def _on_services_changed(self):
        """Обработчик изменения галочек сервисов - перестроить дерево."""
        self.ctx.services.update(
            {
                key: checkbox.isChecked()
                for key, checkbox in self._service_checkboxes.items()
            }
        )
        combo = getattr(self, "_vnc_server_combo", None)
        if combo is not None:
            combo.setEnabled(self.ctx.services["vnc"])
        self._update_vnc_server_guide()
        self.remote_page.set_services(dict(self.ctx.services))
        self._rebuild_nav_tree()

    def _load_existing_data(self):
        """Загрузить текущие хосты и команды (режим подхвата)."""
        if self.device_service is not None:
            try:
                self.hosts_page.load_existing_data(self.device_service)
            except Exception as e:
                logger.error(f"SetupWizard: cannot load existing hosts: {e}")
        try:
            self.commands_page.load_existing_data()
        except Exception as e:
            logger.error(f"SetupWizard: cannot load existing commands: {e}")

    def _update_existing_summary(self):
        try:
            hosts_count = self.hosts_page.count_hosts()
            commands_count = self.commands_page.tasks_count()
            text = (
                "Текущие настройки:\n"
                f"  • Хостов: {hosts_count}\n"
                f"  • Команд: {commands_count}"
            )
        except Exception as e:
            text = f"Не удалось получить текущие настройки: {e}"
        self._existing_summary.setText(text)
        self._existing_summary.setVisible(True)

    # ================================================================
    #  Завершение
    # ================================================================

    def _finish(self):
        if self.guard.is_active():
            QMessageBox.warning(
                self,
                "Установка выполняется",
                "Дождитесь завершения установки или нажмите «Прервать».",
            )
            return
        if self._skip_save_checkbox.isChecked():
            self.reject()
            return

        if not self._collect_all():
            return
        cleared = []
        if (
            self.hosts_page._count_hosts_in_data(self.ctx.baseline["data"][0])
            and not self.hosts_page.count_hosts()
        ):
            cleared.append("хосты")
        if self.ctx.baseline["data"][1] and not self.ctx.commands_data:
            cleared.append("команды")
        if cleared:
            if (
                QMessageBox.question(
                    self,
                    "Очистка данных",
                    "Будут удалены существующие "
                    + ", ".join(cleared)
                    + ". Сохранить пустой список?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return

        # Валидация: sudo без пароля в выбранной связке - предупреждение.
        warn = None
        try:
            warn = self.ssh_page.sudo_warning() if self.ctx.services.get("ssh") else None
        except Exception:
            warn = None
        if warn:
            btn = QMessageBox.warning(
                self,
                "Пароль для sudo",
                warn + "\n\n"
                "Установка пакетов на хостах с sudo потребует пароль. "
                "Без пароля пакеты не смогут установиться.\n\n"
                "• «Указать связку» - вернуться в мастер.\n"
                "• «Сохранить как есть» - если на хостах sudo без пароля "
                "или для каждого хоста задана отдельная связка с паролем.",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if btn != QMessageBox.StandardButton.Save:
                return

        from src.data import datastore

        if not self.guard.shutdown_all():
            self._warn_close_delayed()
            return

        try:
            ok, err = save_all(
                self.ctx,
                {
                    "hosts": self.hosts_page,
                    "veyon": self.veyon_page,
                    "vnc": self.vnc_page,
                    "ssh": self.ssh_page,
                },
                self.device_service,
                self.command_service,
                datastore,
                config,
                self.event_bus,
            )
        except Exception:
            logger.exception("SetupWizard: unexpected save failure")
            ok, err = False, "Не удалось сохранить настройки. Подробности в журнале."
        if not ok:
            QMessageBox.critical(self, "Ошибка", err)
            return
        self.veyon_page.request_stop()
        self.accept()

    # ================================================================
    #  Завершение потоков при закрытии
    # ================================================================

    def _warn_close_delayed(self):
        """Сообщение при невозможности закрыть диалог (потоки еще живы)."""
        QMessageBox.warning(
            self,
            "Завершение фоновых операций",
            "Фоновые операции ещё не завершились (установка, авторизация "
            "или диагностика Veyon).\n"
            "Подождите несколько секунд и закройте окно снова.",
        )

    def reject(self):
        if self._closing:
            return
        if self.guard.is_active():
            if (
                QMessageBox.question(
                    self,
                    "Мастер настройки",
                    "Остановить фоновые операции и отбросить черновик? Уже выполненные системные действия сохранятся.",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return
            self._closing = True
            self._close_started = time.monotonic()
            self._cancel_btn.setText("Остановка…")
            self._cancel_btn.setEnabled(False)
            self.guard.request_stop()
            QTimer.singleShot(50, self._poll_close)
            return
        self.vnc_page.cleanup()
        self.veyon_page.request_stop()
        super().done(QDialog.DialogCode.Rejected)

    def _poll_close(self):
        if not self.guard.is_active():
            self._closing = False
            self.vnc_page.cleanup()
            super().done(QDialog.DialogCode.Rejected)
            return
        if time.monotonic() - self._close_started >= 20:
            self._closing = False
            self._cancel_btn.setText("Отмена")
            self._cancel_btn.setEnabled(True)
            self._warn_close_delayed()
            return
        QTimer.singleShot(100, self._poll_close)

    def done(self, result):
        if result == QDialog.DialogCode.Rejected:
            self.reject()
        elif not self.guard.is_active():
            super().done(result)

    def closeEvent(self, event):
        event.ignore()
        self.reject()
