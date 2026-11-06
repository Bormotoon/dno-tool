"""Диалог управления встроенным веб-сервером.

Позволяет включать/выключать сервер, настраивать host/port и
учётные данные для базовой аутентификации.
"""

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QPushButton,
    QCheckBox,
    QMessageBox,
    QGroupBox,
    QSizePolicy,
    QSlider,
    QWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QToolButton,
    QComboBox,
    QFileDialog,
    QPlainTextEdit,
    QTabWidget,
    QScrollArea,
    QFrame,
)
from PySide6.QtCore import Qt

import shutil
from pathlib import Path

from src.config import config
from src.logger import logger
from src.services import ConfigService
from src.di import get_container
from src.ui.widgets.device_tree import PasswordDelegate, PASSWORD_DATAROLE
from src.ui.widgets.thread_count_slider import ThreadCountSlider
from src.utils.network.urls import web_base_url
from src.utils.security.web_tls import (
    GENERATED_CERT_NAME,
    GENERATED_KEY_NAME,
    CertificateInfo,
    WebTlsError,
    default_names,
    generate_certificate,
    generated_directory,
    inspect_certificate,
    parse_names,
)


class WebServerDialog(QDialog):
    """Диалог настройки и управления веб-сервером управления."""

    # Запуск и остановка ждут секунды и выполняются вне GUI-потока.

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server_pending = False
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Веб-сервер управления")
        self.setMinimumWidth(520)

        self._config_service = get_container().resolve(ConfigService)

        self._import_error: str = ""
        try:
            from src.web import get_web_server

            self._server = get_web_server()
        except Exception as e:
            import traceback

            self._import_error = f"{e}\n\n{traceback.format_exc()}"
            logger.error(
                f"WebServerDialog: модуль веб-сервера недоступен: {self._import_error}"
            )
            self._server = None

        self._build_ui()
        self._load_settings()
        self._update_status()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Статус
        status_box = QGroupBox("Статус")
        status_layout = QHBoxLayout(status_box)
        self._status_label = QLabel("Не запущен")
        self._status_label.setStyleSheet("font-weight: bold;")
        status_layout.addWidget(self._status_label, 1)

        self._toggle_btn = QPushButton("Запустить")
        self._toggle_btn.clicked.connect(self._on_toggle)
        status_layout.addWidget(self._toggle_btn)

        self._open_btn = QPushButton("Открыть в браузере")
        self._open_btn.clicked.connect(self._open_browser)
        status_layout.addWidget(self._open_btn)

        layout.addWidget(status_box)

        # Основное: запуск, адрес и пользователи.
        main_tab = QWidget()
        main_layout = QVBoxLayout(main_tab)
        main_form = QFormLayout()
        main_layout.addLayout(main_form)

        self._enabled_cb = QCheckBox("Запускать при старте приложения")
        main_form.addRow(self._enabled_cb)

        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("0.0.0.0")
        main_form.addRow("Адрес:", self._host_edit)

        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(8765)
        main_form.addRow("Порт:", self._port_spin)

        # Дополнительно: прокси, потоки и хранение результатов.
        advanced_tab = QWidget()
        advanced_layout = QVBoxLayout(advanced_tab)
        form = QFormLayout()
        advanced_layout.addLayout(form)
        advanced_layout.addStretch(1)

        self._public_url_edit = QLineEdit()
        self._public_url_edit.setPlaceholderText("https://example.org/dno")
        self._public_url_edit.setToolTip(
            "Внешний HTTPS адрес reverse proxy для VNC-ссылок нод. "
            "Proxy должен передавать WebSocket и удалять префикс пути. "
            "Пустое поле использует прямой адрес встроенного сервера."
        )
        form.addRow("Внешний HTTPS URL:", self._public_url_edit)

        pool_hint = QLabel(
            "Потоки статусов и команд задаются в общих настройках, миниатюр — "
            "в настройках миниатюр. Изменения применяются после перезапуска программы."
        )
        pool_hint.setWordWrap(True)
        pool_hint.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        form.addRow(pool_hint)
        general_button = QPushButton("Общие настройки - потоки статусов и команд")
        general_button.clicked.connect(self._open_general_settings)
        form.addRow(general_button)
        self._rfb_thread_count = ThreadCountSlider()
        self._rfb_thread_count.setToolTip(
            "Лимит потоков моста удаленного доступа RFB. "
            "Применяется после перезапуска программы."
        )
        form.addRow("Потоки RFB-моста:", self._rfb_thread_count)

        from src.ui.dialogs.common.thumbnail_settings import thumbnail_settings_link

        form.addRow(thumbnail_settings_link(self, self._config_service))

        # Ползунок TTL завершённых заданий (для веб-реестра JobManager).
        # 0 = ∞ (хранить вечно), далее 10м / 30м / 1ч / 3ч / 6ч / 12ч / 24ч.
        self._ttl_steps = (0, 10, 30, 60, 180, 360, 720, 1440)
        self._ttl_labels = ("∞", "10м", "30м", "1ч", "3ч", "6ч", "12ч", "24ч")
        self._ttl_slider = QSlider(Qt.Horizontal)
        self._ttl_slider.setRange(0, len(self._ttl_steps) - 1)
        self._ttl_slider.setSingleStep(1)
        self._ttl_slider.setPageStep(1)
        self._ttl_slider.setTickPosition(QSlider.NoTicks)
        self._ttl_value_label = QLabel()
        self._ttl_value_label.setMinimumWidth(36)
        self._ttl_value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._ttl_slider.valueChanged.connect(
            lambda v: self._ttl_value_label.setText(self._ttl_labels[v])
        )
        ttl_widget = QWidget()
        ttl_layout = QHBoxLayout(ttl_widget)
        ttl_layout.setContentsMargins(0, 0, 0, 0)
        ttl_layout.addWidget(self._ttl_slider, 1)
        ttl_layout.addWidget(self._ttl_value_label, 0)
        form.addRow("Хранить результаты заданий:", ttl_widget)

        # Таблица пользователей (многопользовательский режим).
        # Сервер не запустится без хотя бы одного пользователя.
        users_box = QGroupBox("Пользователи")
        users_layout = QVBoxLayout(users_box)
        users_hint = QLabel(
            "Без пользователей сервер запускается, только если включён вход через LDAP.\n"
            "Каждый пользователь видит только свои задания и результаты."
        )
        users_hint.setWordWrap(True)
        users_layout.addWidget(users_hint)
        self._users_table = QTableWidget(0, 2)
        self._users_table.setHorizontalHeaderLabels(["Логин", "Пароль"])
        self._users_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        self._users_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        self._users_table.verticalHeader().setVisible(False)
        self._users_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Делегат маскирует пароль «••••» вне редактирования и показывает
        # реальное значение только в редакторе (как в мастере на этапе Хосты).
        self._users_table.setItemDelegateForColumn(1, PasswordDelegate(self))
        users_layout.addWidget(self._users_table)
        users_btn_layout = QHBoxLayout()
        self._add_user_btn = QPushButton("Добавить")
        self._add_user_btn.clicked.connect(self._on_add_user)
        self._del_user_btn = QPushButton("Удалить")
        self._del_user_btn.clicked.connect(self._on_del_user)
        users_btn_layout.addStretch(1)
        users_btn_layout.addWidget(self._add_user_btn)
        users_btn_layout.addWidget(self._del_user_btn)
        users_layout.addLayout(users_btn_layout)
        main_layout.addWidget(users_box, 1)

        self._tabs = QTabWidget()
        self._tabs.addTab(main_tab, "Основное")
        self._tabs.addTab(self._build_tls_box(), "HTTPS")
        self._tabs.addTab(self._build_ldap_box(), "LDAP")
        self._tabs.addTab(advanced_tab, "Дополнительно")
        layout.addWidget(self._tabs, 1)

        # Кнопки
        btn_layout = QHBoxLayout()
        self._save_btn = QPushButton("Сохранить")
        self._save_btn.clicked.connect(self._on_save)
        self._close_btn = QPushButton("Закрыть")
        self._close_btn.clicked.connect(self.accept)
        btn_layout.addStretch(1)
        btn_layout.addWidget(self._save_btn)
        btn_layout.addWidget(self._close_btn)
        layout.addLayout(btn_layout)

    def _build_tls_box(self) -> QWidget:
        """Вкладка HTTPS: свой сертификат или выпуск самоподписанного."""
        box = QWidget()
        outer = QVBoxLayout(box)
        form = QFormLayout()
        outer.addLayout(form)
        outer.addStretch(1)

        self._tls_mode = QComboBox()
        self._tls_mode.addItem("Выключен (HTTP)", "off")
        self._tls_mode.addItem("Выпустить сертификат", "generated")
        self._tls_mode.addItem("Свой сертификат", "custom")
        self._tls_mode.currentIndexChanged.connect(self._on_tls_mode_changed)
        self._tls_mode.setToolTip(
            "При включённом HTTPS адреса http:// на этом порту "
            "перенаправляются на https://."
        )
        form.addRow("Режим:", self._tls_mode)

        self._tls_names = QPlainTextEdit()
        self._tls_names.setMaximumHeight(64)
        self._tls_names.setToolTip(
            "Имена и адреса, по которым браузер открывает сервер. "
            "Разделяются пробелом, запятой или новой строкой."
        )
        form.addRow("Имена и адреса:", self._tls_names)
        self._tls_generate_btn = QPushButton("Выпустить сертификат")
        self._tls_generate_btn.clicked.connect(self._on_tls_generate)
        form.addRow(self._tls_generate_btn)

        self._tls_cert_edit = QLineEdit()
        self._tls_cert_edit.setPlaceholderText("Сертификат в формате PEM, с цепочкой")
        self._tls_cert_row = self._path_row(self._tls_cert_edit, "Сертификат")
        form.addRow("Сертификат:", self._tls_cert_row)
        self._tls_key_edit = QLineEdit()
        self._tls_key_edit.setPlaceholderText("Ключ в формате PEM, без пароля")
        self._tls_key_row = self._path_row(self._tls_key_edit, "Закрытый ключ")
        form.addRow("Ключ:", self._tls_key_row)

        self._tls_info = QLabel()
        self._tls_info.setWordWrap(True)
        self._tls_info.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow(self._tls_info)
        self._tls_export_btn = QPushButton("Сохранить сертификат для браузера…")
        self._tls_export_btn.setToolTip(
            "Сохранить только сертификат, без ключа, чтобы добавить его в доверенные."
        )
        self._tls_export_btn.clicked.connect(self._on_tls_export)
        form.addRow(self._tls_export_btn)
        self._tls_form = form
        return box

    def _build_ldap_box(self) -> QWidget:
        """Вкладка LDAP: вход пользователей каталога (Active Directory, OpenLDAP)."""
        content = QWidget()
        outer = QVBoxLayout(content)
        form = QFormLayout()
        outer.addLayout(form)

        self._ldap_enabled = QCheckBox("Разрешить вход через LDAP")
        self._ldap_enabled.toggled.connect(self._on_ldap_mode_changed)
        form.addRow(self._ldap_enabled)
        hint = QLabel(
            "Локальные пользователи проверяются первыми: логин, совпавший с локальным, "
            "в каталог не отправляется. Владелец заданий пользователя каталога — "
            "логин в нижнем регистре."
        )
        hint.setWordWrap(True)
        form.addRow(hint)

        self._ldap_servers = QLineEdit()
        self._ldap_servers.setPlaceholderText("ldaps://dc1.example.org ldaps://dc2.example.org")
        self._ldap_servers.setToolTip(
            "Один или несколько адресов через пробел, опрашиваются по порядку. "
            "Порт по умолчанию: 389 для ldap://, 636 для ldaps://."
        )
        form.addRow("Серверы:", self._ldap_servers)
        self._ldap_start_tls = QCheckBox("StartTLS для адресов ldap://")
        form.addRow(self._ldap_start_tls)
        self._ldap_verify = QCheckBox("Проверять сертификат и имя сервера")
        self._ldap_verify.toggled.connect(self._on_ldap_mode_changed)
        form.addRow(self._ldap_verify)
        self._ldap_ca = QLineEdit()
        self._ldap_ca.setPlaceholderText("Системное хранилище сертификатов")
        self._ldap_ca_row = self._file_row(self._ldap_ca, "Корневой сертификат")
        form.addRow("Корневой сертификат:", self._ldap_ca_row)

        self._ldap_mode = QComboBox()
        self._ldap_mode.addItem("По шаблону логина", "direct")
        self._ldap_mode.addItem("Поиск служебной учётной записью", "search")
        self._ldap_mode.currentIndexChanged.connect(self._on_ldap_mode_changed)
        form.addRow("Учётная запись:", self._ldap_mode)
        self._ldap_template = QLineEdit()
        self._ldap_template.setPlaceholderText("{username}@example.org")
        self._ldap_template.setToolTip(
            "Имя для входа в каталог. {username} заменяется логином. Примеры: "
            "{username}@example.org, EXAMPLE\\{username}, "
            "uid={username},ou=people,dc=example,dc=org"
        )
        form.addRow("Шаблон:", self._ldap_template)
        self._ldap_bind_dn = QLineEdit()
        self._ldap_bind_dn.setPlaceholderText("Пусто — анонимный поиск")
        form.addRow("Служебная запись:", self._ldap_bind_dn)
        self._ldap_bind_password = QLineEdit()
        self._ldap_bind_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Пароль служебной записи:", self._ldap_bind_password)
        self._ldap_base_dn = QLineEdit()
        self._ldap_base_dn.setPlaceholderText("dc=example,dc=org")
        self._ldap_base_dn.setToolTip(
            "Где искать пользователя. Для шаблона нужен только при проверке группы."
        )
        form.addRow("Базовый DN:", self._ldap_base_dn)
        self._ldap_filter = QLineEdit()
        self._ldap_filter.setToolTip(
            "Active Directory: (sAMAccountName={username}); OpenLDAP: (uid={username})"
        )
        form.addRow("Фильтр пользователя:", self._ldap_filter)
        self._ldap_group = QLineEdit()
        self._ldap_group.setPlaceholderText("Пусто — вход любому пользователю каталога")
        self._ldap_group.setToolTip("Полный DN группы, например cn=dno,ou=groups,dc=example,dc=org")
        form.addRow("Разрешённая группа:", self._ldap_group)
        self._ldap_nested = QCheckBox("Учитывать вложенные группы (Active Directory)")
        form.addRow(self._ldap_nested)
        self._ldap_timeout = QSpinBox()
        self._ldap_timeout.setRange(1, 60)
        self._ldap_timeout.setSuffix(" с")
        form.addRow("Таймаут сервера:", self._ldap_timeout)
        self._ldap_cache = QSpinBox()
        self._ldap_cache.setRange(10, 3600)
        self._ldap_cache.setSuffix(" с")
        self._ldap_cache.setToolTip(
            "Сколько действует удачная проверка пароля. Браузер передаёт пароль "
            "с каждым запросом, без запоминания каталог получал бы их все."
        )
        form.addRow("Запоминать вход:", self._ldap_cache)

        check_box = QGroupBox("Проверка входа")
        check_layout = QFormLayout(check_box)
        self._ldap_test_user = QLineEdit()
        check_layout.addRow("Логин:", self._ldap_test_user)
        self._ldap_test_password = QLineEdit()
        self._ldap_test_password.setEchoMode(QLineEdit.EchoMode.Password)
        check_layout.addRow("Пароль:", self._ldap_test_password)
        self._ldap_test_btn = QPushButton("Проверить")
        self._ldap_test_btn.setToolTip("Проверяет настройки из окна, без сохранения")
        self._ldap_test_btn.clicked.connect(self._on_ldap_test)
        check_layout.addRow(self._ldap_test_btn)
        self._ldap_test_result = QLabel()
        self._ldap_test_result.setWordWrap(True)
        self._ldap_test_result.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        check_layout.addRow(self._ldap_test_result)
        outer.addWidget(check_box)
        outer.addStretch(1)

        self._ldap_form = form
        self._ldap_test_pending = False
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        self._ldap_tab = scroll
        return scroll

    def _file_row(self, edit: QLineEdit, title: str) -> QWidget:
        """Поле пути к PEM-файлу с кнопкой выбора."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        browse = QPushButton("Обзор…")

        def choose() -> None:
            path, _filter = QFileDialog.getOpenFileName(
                self, title, edit.text(), "PEM (*.pem *.crt *.cer);;Все файлы (*)"
            )
            if path:
                edit.setText(path)

        browse.clicked.connect(choose)
        layout.addWidget(browse)
        return row

    def _on_ldap_mode_changed(self) -> None:
        search = self._ldap_mode.currentData() == "search"
        for widget, visible in (
            (self._ldap_template, not search),
            (self._ldap_bind_dn, search),
            (self._ldap_bind_password, search),
            (self._ldap_ca_row, self._ldap_verify.isChecked()),
        ):
            self._ldap_form.setRowVisible(widget, visible)

    def _load_ldap(self, data: dict) -> None:
        from src.config.web_ldap import WebLdapConfig

        cfg = WebLdapConfig(**data) if data else WebLdapConfig()
        self._ldap_enabled.setChecked(cfg.enabled)
        self._ldap_servers.setText(cfg.servers)
        self._ldap_start_tls.setChecked(cfg.start_tls)
        self._ldap_verify.setChecked(cfg.verify_cert)
        self._ldap_ca.setText(cfg.ca_file)
        self._ldap_mode.setCurrentIndex(max(0, self._ldap_mode.findData(cfg.bind_mode)))
        self._ldap_template.setText(cfg.user_template)
        self._ldap_bind_dn.setText(cfg.bind_dn)
        self._ldap_bind_password.setText(cfg.bind_password)
        self._ldap_base_dn.setText(cfg.base_dn)
        self._ldap_filter.setText(cfg.user_filter)
        self._ldap_group.setText(cfg.required_group)
        self._ldap_nested.setChecked(cfg.nested_groups)
        self._ldap_timeout.setValue(cfg.timeout_sec)
        self._ldap_cache.setValue(cfg.cache_ttl_sec)
        self._on_ldap_mode_changed()

    def _collect_ldap(self) -> dict:
        return {
            "enabled": self._ldap_enabled.isChecked(),
            "servers": self._ldap_servers.text().strip(),
            "start_tls": self._ldap_start_tls.isChecked(),
            "verify_cert": self._ldap_verify.isChecked(),
            "ca_file": self._ldap_ca.text().strip(),
            "bind_mode": self._ldap_mode.currentData(),
            "user_template": self._ldap_template.text().strip(),
            "bind_dn": self._ldap_bind_dn.text().strip(),
            "bind_password": self._ldap_bind_password.text(),
            "base_dn": self._ldap_base_dn.text().strip(),
            "user_filter": self._ldap_filter.text().strip(),
            "required_group": self._ldap_group.text().strip(),
            "nested_groups": self._ldap_nested.isChecked(),
            "timeout_sec": self._ldap_timeout.value(),
            "cache_ttl_sec": self._ldap_cache.value(),
        }

    def _validate_ldap(self, data: dict | None = None):
        """Настройка LDAP из окна или None с сообщением о причине."""
        from src.config.web_ldap import normalize_ldap_config

        try:
            return normalize_ldap_config(data or self._collect_ldap(), strict=True)
        except ValueError as error:
            self._tabs.setCurrentWidget(self._ldap_tab)
            QMessageBox.warning(self, "LDAP", str(error))
            return None

    def _on_ldap_test(self) -> None:
        """Проверить вход с настройками из окна, вне GUI-потока."""
        from src.workers.gui_relay import post_to_gui
        from src.workers.network.worker_pool import get_service_worker_pool

        data = self._collect_ldap()
        data["enabled"] = True
        cfg = self._validate_ldap(data)
        if cfg is None:
            return
        username = self._ldap_test_user.text().strip()
        password = self._ldap_test_password.text()
        if not username or not password:
            self._ldap_test_result.setText("Введите логин и пароль.")
            return
        self._ldap_test_pending = True
        self._ldap_test_btn.setEnabled(False)
        self._ldap_test_result.setText("Проверка…")
        dialog = self
        apply = self._on_ldap_tested

        def check() -> tuple[bool, str]:
            from src.web.ldap_auth import check_ldap_login

            return check_ldap_login(cfg, username, password)

        try:
            get_service_worker_pool().submit(
                check,
                on_complete=lambda _task, result: post_to_gui(dialog, apply, *result),
                on_error=lambda _task, error: post_to_gui(dialog, apply, False, str(error)),
                metadata={"type": "web_ldap_check"},
            )
        except RuntimeError as error:
            self._on_ldap_tested(False, str(error))

    def _on_ldap_tested(self, ok: bool, message: str) -> None:
        self._ldap_test_pending = False
        self._ldap_test_btn.setEnabled(True)
        color = "#27ae60" if ok else "#c0392b"
        self._ldap_test_result.setStyleSheet(f"color: {color};")
        self._ldap_test_result.setText(message)

    def _path_row(self, edit: QLineEdit, title: str) -> QWidget:
        """Поле пути с кнопкой выбора файла."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        browse = QPushButton("Обзор…")

        def choose() -> None:
            path, _filter = QFileDialog.getOpenFileName(
                self, title, edit.text(), "PEM (*.pem *.crt *.cer *.key);;Все файлы (*)"
            )
            if path:
                edit.setText(path)
                self._refresh_tls_info()

        browse.clicked.connect(choose)
        layout.addWidget(browse)
        edit.editingFinished.connect(self._refresh_tls_info)
        return row

    def _tls_paths(self) -> tuple[Path, Path]:
        """Пути сертификата и ключа для режима, выбранного в окне."""
        if self._tls_mode.currentData() == "generated":
            directory = generated_directory()
            return directory / GENERATED_CERT_NAME, directory / GENERATED_KEY_NAME
        return (
            Path(self._tls_cert_edit.text().strip()),
            Path(self._tls_key_edit.text().strip()),
        )

    def _on_tls_mode_changed(self) -> None:
        mode = self._tls_mode.currentData()
        generated, custom = mode == "generated", mode == "custom"
        for widget, visible in (
            (self._tls_names, generated),
            (self._tls_generate_btn, generated),
            (self._tls_cert_row, custom),
            (self._tls_key_row, custom),
            (self._tls_info, mode != "off"),
            (self._tls_export_btn, mode != "off"),
        ):
            self._tls_form.setRowVisible(widget, visible)
        self._refresh_tls_info()

    def _tls_inspect(self) -> CertificateInfo:
        """Прочитать сведения о сертификате; WebTlsError обрабатывает вызывающий."""
        cert, key = self._tls_paths()
        return inspect_certificate(cert, key)

    def _refresh_tls_info(self) -> None:
        mode = self._tls_mode.currentData()
        if mode == "off":
            self._tls_export_btn.setEnabled(False)
            return
        cert, _key = self._tls_paths()
        if mode == "generated" and not cert.is_file():
            self._tls_info.setText("Сертификат ещё не выпущен.")
            self._tls_export_btn.setEnabled(False)
            return
        if mode == "custom" and not (self._tls_cert_edit.text().strip() and self._tls_key_edit.text().strip()):
            self._tls_info.setText("Укажите файлы сертификата и ключа.")
            self._tls_export_btn.setEnabled(False)
            return
        try:
            info = self._tls_inspect()
        except WebTlsError as error:
            self._tls_info.setText(f"Ошибка: {error}")
            self._tls_export_btn.setEnabled(False)
            return
        expires = info.not_after.astimezone().strftime("%d.%m.%Y %H:%M")
        state = f"Истёк или ещё не действует: {expires}" if info.expired else f"Действует до {expires}"
        names = ", ".join(info.names) or "имена не указаны в сертификате"
        self._tls_info.setText(f"{state}\nИмена: {names}\nSHA-256: {info.fingerprint_sha256}")
        self._tls_export_btn.setEnabled(True)

    def _on_tls_generate(self) -> None:
        cert, _key = self._tls_paths()
        if cert.is_file():
            answer = QMessageBox.question(
                self,
                "Сертификат",
                "Заменить выпущенный сертификат? Браузерам придётся доверять новому.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if self._generate_tls():
            QMessageBox.information(
                self,
                "Сертификат",
                "Сертификат выпущен. Сохраните настройки, чтобы применить его.",
            )

    def _generate_tls(self) -> bool:
        """Выпустить сертификат по именам из окна и показать результат."""
        try:
            generate_certificate(parse_names(self._tls_names.toPlainText()))
        except WebTlsError as error:
            QMessageBox.critical(self, "Сертификат", str(error))
            return False
        self._refresh_tls_info()
        return True

    def _on_tls_export(self) -> None:
        cert, _key = self._tls_paths()
        path, _filter = QFileDialog.getSaveFileName(
            self, "Сохранить сертификат", "dno-tool.crt", "Сертификат (*.crt *.pem)"
        )
        if not path:
            return
        try:
            shutil.copyfile(cert, path)
        except OSError as error:
            QMessageBox.critical(self, "Сертификат", f"Не удалось сохранить: {error}")

    def _validate_port(self) -> bool:
        """Не сохранять порт, который слушает сервер нод: запустится только один."""
        nodes = config.app.nodes
        port = self._port_spin.value()
        if not nodes.listen_enabled or port != int(nodes.listen_port or 8801):
            return True
        QMessageBox.warning(
            self,
            "Веб-сервер",
            f"Порт {port} занят приёмом подключений нод (Настройки → Ноды). "
            "Выберите другой порт.",
        )
        return False

    def _validate_tls(self) -> bool:
        """Не сохранять настройки, с которыми сервер не сможет запуститься."""
        mode = self._tls_mode.currentData()
        if mode == "off":
            return True
        missing = mode == "generated" and not self._tls_paths()[0].is_file()
        if missing and not self._generate_tls():
            return False
        try:
            info = self._tls_inspect()
        except WebTlsError as error:
            QMessageBox.critical(self, "HTTPS", str(error))
            return False
        if info.expired:
            QMessageBox.warning(
                self,
                "HTTPS",
                "Сертификат истёк или ещё не действует. "
                "Браузеры будут показывать предупреждение.",
            )
        return True

    def _load_settings(self):
        cfg = self._config_service.get_web_config()
        names = cfg.get("tls_names") or default_names()
        self._tls_names.setPlainText("\n".join(names))
        self._tls_cert_edit.setText(cfg.get("tls_cert_path", ""))
        self._tls_key_edit.setText(cfg.get("tls_key_path", ""))
        index = self._tls_mode.findData(cfg.get("tls_mode", "off"))
        self._tls_mode.setCurrentIndex(max(0, index))
        self._on_tls_mode_changed()
        self._enabled_cb.setChecked(cfg.get("enabled", False))
        self._host_edit.setText(cfg.get("host", "0.0.0.0"))
        self._port_spin.setValue(cfg.get("port", 8765))
        self._public_url_edit.setText(cfg.get("public_url", ""))
        self._rfb_thread_count.setValue(cfg.get("rfb_thread_count", 4))
        # TTL заданий
        ttl_current = cfg.get("job_ttl_minutes", 30)
        ttl_idx = min(
            range(len(self._ttl_steps)),
            key=lambda i: abs(self._ttl_steps[i] - ttl_current),
        )
        self._ttl_slider.setValue(ttl_idx)
        self._ttl_value_label.setText(self._ttl_labels[ttl_idx])
        # Пользователи
        self._users_table.setRowCount(0)
        for u in cfg.get("users") or []:
            self._add_user_row(u.get("username", ""), u.get("password", ""))
        self._load_ldap(cfg.get("ldap") or {})

    def _add_user_row(self, username: str = "", password: str = "") -> None:
        row = self._users_table.rowCount()
        self._users_table.insertRow(row)
        u_item = QTableWidgetItem(username)
        # Реальный пароль хранится в PASSWORD_DATAROLE; в DisplayRole/EditRole
        # тоже реальный текст — делегат PasswordDelegate маскирует его «••••»
        # вне редактирования и показывает реальное значение только в редакторе.
        p_item = QTableWidgetItem(password)
        p_item.setData(PASSWORD_DATAROLE, password or "")
        self._users_table.setItem(row, 0, u_item)
        self._users_table.setItem(row, 1, p_item)

    def _on_add_user(self) -> None:
        self._add_user_row("", "")

    def _on_del_user(self) -> None:
        rows = self._users_table.selectionModel().selectedRows()
        if not rows:
            return
        # Удаляем снизу вверх, чтобы индексы не сбились.
        for idx in sorted(rows, key=lambda i: i.row(), reverse=True):
            self._users_table.removeRow(idx.row())

    def _collect_settings(self) -> dict:
        return {
            "enabled": self._enabled_cb.isChecked(),
            "host": self._host_edit.text().strip() or "0.0.0.0",
            "port": self._port_spin.value(),
            "public_url": self._public_url_edit.text().strip(),
            "tls_mode": self._tls_mode.currentData(),
            "tls_cert_path": self._tls_cert_edit.text().strip(),
            "tls_key_path": self._tls_key_edit.text().strip(),
            "tls_names": self._tls_names.toPlainText().replace(",", " ").split(),
            "rfb_thread_count": self._rfb_thread_count.value(),
            "job_ttl_minutes": self._ttl_steps[self._ttl_slider.value()],
        }

    def _open_general_settings(self) -> None:
        from src.ui.dialogs.common.common import ConfigDialog

        ConfigDialog(self).exec()

    def _collect_users(self) -> list:
        """Собрать список пользователей из таблицы.

        Реальный пароль всегда хранится в PASSWORD_DATAROLE (через
        PasswordDelegate.setModelData при редактировании либо при начальной
        загрузке строки в _add_user_row). В text() может лежать маска «••••» —
        её игнорируем.
        """
        out = []
        for row in range(self._users_table.rowCount()):
            u_item = self._users_table.item(row, 0)
            p_item = self._users_table.item(row, 1)
            if not u_item:
                continue
            username = u_item.text().strip()
            if not username:
                continue
            password = p_item.data(PASSWORD_DATAROLE) if p_item else ""
            out.append({"username": username, "password": password or ""})
        return out

    def _has_accounts(self) -> bool:
        """Есть ли кому войти по настройкам из окна."""
        return bool(self._collect_users()) or self._ldap_enabled.isChecked()

    def _save_settings(self) -> bool:
        settings = self._collect_settings()
        settings["users"] = self._collect_users()
        settings["ldap"] = self._collect_ldap()
        previous = config.app.web
        ok = self._config_service.update_web_config(**settings)
        if ok:
            ok, _message = self._config_service.save_config()
            if not ok:
                config.app.web = previous
        return ok

    def _on_save(self):
        if (
            not self._validate_port()
            or not self._validate_tls()
            or self._validate_ldap() is None
        ):
            return
        if not self._save_settings():
            QMessageBox.critical(self, "Ошибка", "Не удалось сохранить настройки.")
            return
        # Перезапуск сервера если он запущен (применение новых настроек)
        if self._server is not None and self._server.is_running:
            # Проверить, что войти есть кому, — иначе сервер не стартует.
            if not self._has_accounts():
                QMessageBox.warning(
                    self,
                    "Веб-сервер",
                    "Нет пользователей и выключен вход через LDAP — "
                    "сервер будет остановлен.",
                )
                self._run_server_operation("stop", "Настройки сохранены.")
            else:
                self._run_server_operation("restart", "Настройки сохранены.")
            return
        self._update_status()
        QMessageBox.information(self, "Сохранение", "Настройки сохранены.")

    def _on_toggle(self):
        if self._server is None:
            QMessageBox.warning(
                self,
                "Веб-сервер",
                f"Модуль веб-сервера недоступен (не установлены зависимости).\n\n"
                f"Ошибка импорта:\n{self._import_error or '(нет деталей)'}",
            )
            return
        if self._server.is_running:
            self._run_server_operation("stop")
        else:
            # Проверить, что войти есть кому, перед запуском.
            if not self._has_accounts():
                QMessageBox.warning(
                    self,
                    "Веб-сервер",
                    "Невозможно запустить сервер: список пользователей пуст.\n"
                    "Добавьте пользователя в таблице ниже или включите вход через LDAP.",
                )
                return
            # Применяем текущие настройки перед запуском
            if (
                not self._validate_port()
                or not self._validate_tls()
                or self._validate_ldap() is None
            ):
                return
            if not self._save_settings():
                QMessageBox.critical(self, "Ошибка", "Не удалось сохранить настройки.")
                return
            self._run_server_operation("start")

    def _run_server_operation(self, action: str, success_message: str = "") -> None:
        """Запустить или остановить сервер вне GUI-потока; итог придёт сигналом."""
        from src.workers.gui_relay import post_to_gui
        from src.workers.network.worker_pool import get_service_worker_pool

        server = self._server
        if server is None:
            return
        titles = {"start": "Запуск…", "stop": "Остановка…", "restart": "Перезапуск…"}
        try:
            if action != "stop":
                server.prepare_on_gui_thread()
        except (RuntimeError, KeyError, ValueError, ImportError) as error:
            logger.error(f"WebServerDialog: подготовка сервера не удалась: {error}")
            QMessageBox.critical(
                self, "Веб-сервер", f"Не удалось подготовить сервер:\n{error}"
            )
            return
        self._server_pending = True
        self._toggle_btn.setEnabled(False)
        self._open_btn.setEnabled(False)
        self._status_label.setText(titles[action])

        def apply_server() -> bool:
            if action == "start":
                return server.start()
            if action == "restart":
                return server.restart()
            server.stop()
            return True

        dialog = self
        applied = self._on_server_applied

        def deliver(ok: bool, message: str) -> None:
            # Если диалог уже уничтожен, показывать итог некому.
            post_to_gui(dialog, applied, ok, success_message if ok else "", message)

        try:
            get_service_worker_pool().submit(
                apply_server,
                on_complete=lambda _task, ok: deliver(
                    bool(ok),
                    ""
                    if ok
                    else server.last_error or "Не удалось запустить сервер.",
                ),
                on_error=lambda _task, error: deliver(False, str(error)),
                metadata={"type": "web_server_settings"},
            )
        except RuntimeError as error:
            deliver(False, str(error))

    def _on_server_applied(self, ok: bool, success_message: str, error: str) -> None:
        """Показать итог после возврата в GUI, если окно ещё открыто."""
        self._server_pending = False
        self._toggle_btn.setEnabled(True)
        self._update_status()
        if not self.isVisible():
            return
        if not ok:
            QMessageBox.critical(self, "Веб-сервер", error)
        elif success_message:
            QMessageBox.information(self, "Сохранение", success_message)

    def has_pending_workers(self) -> bool:
        return self._server_pending or self._ldap_test_pending

    def _update_status(self):
        if self._server is None or not self._server.is_running:
            self._status_label.setText("Остановлен")
            self._status_label.setStyleSheet("font-weight: bold; color: #c0392b;")
            self._toggle_btn.setText("Запустить")
            self._open_btn.setEnabled(False)
        else:
            url = f"{web_base_url(config.app.web)}/"
            self._status_label.setText(f"Запущен — {url}")
            self._status_label.setStyleSheet("font-weight: bold; color: #27ae60;")
            self._toggle_btn.setText("Остановить")
            self._open_btn.setEnabled(True)

    def _open_browser(self):
        import webbrowser

        webbrowser.open(f"{web_base_url(config.app.web)}/")
