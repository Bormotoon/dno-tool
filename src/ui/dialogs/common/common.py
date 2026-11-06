"""Общие диалоговые окна приложения: редактирование устройства, настройки, экспорт/импорт команд, просмотр скриптов."""

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QDialogButtonBox,
    QFormLayout,
    QSpinBox,
    QDoubleSpinBox,
    QCheckBox,
    QTextEdit,
    QHBoxLayout,
    QGroupBox,
    QPushButton,
    QSizePolicy,
    QWidget,
    QComboBox,
    QToolButton,
    QScrollArea,
    QFrame,
)
from PySide6.QtCore import Qt, QTimer
from src.config import config
from src.ui.icons import _icon_from_key, qicon_from_path  # zip-темы: иконки из памяти
from src.workers import CommandWorker
from src.workers.command.locale_utils import STANDARD_LOCALES
from src.domain.models.device import DeviceModel
from typing import List, Dict, Optional, Any, Tuple
from PySide6.QtGui import QIcon, QFont
from base64 import b64encode, b64decode
import json
from src.logger import logger
from src.ui.widgets.syntax_highlight import BashSyntaxHighlighter
from src.ui.widgets.thread_count_slider import ThreadCountSlider


class DeviceEditDialog(QDialog):
    """Диалог редактирования свойств устройства"""

    def __init__(self, device=None, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.device = device or DeviceModel({"name": "", "host": ""})
        self.setWindowTitle("Редактирование устройства")
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        self.setMinimumWidth(320)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content = QWidget()
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        content_layout.addLayout(form)

        self.name_label = QLabel("Имя:")
        self.name_edit = QLineEdit(self.device.name)
        form.addRow(self.name_label, self.name_edit)

        self.host_label = QLabel("Адрес хоста:")
        self.host_edit = QLineEdit(self.device.host)
        self.host_edit.setPlaceholderText("IP-адрес или имя хоста")
        form.addRow(self.host_label, self.host_edit)

        self.user_label = QLabel("Связка учётных данных:")
        from src.ui.widgets.bundle_selector import BundleSelector

        self.bundle_selector = BundleSelector(
            allow_none=True, none_label="(наследовать глобальную)"
        )
        self.bundle_selector.set_current_iid(self.device.credential_bundle_iid)
        node_iid = getattr(self.device, "node_iid", None)
        if node_iid and node_iid != "local":
            bundle_iid = self.device.credential_bundle_iid
            if bundle_iid and self.bundle_selector.current_iid() != bundle_iid:
                self.bundle_selector._combo.addItem("Связка ноды", bundle_iid)
                self.bundle_selector.set_current_iid(bundle_iid)
            self.bundle_selector.setEnabled(False)
            self.bundle_selector.setToolTip(
                "Связки учетных данных изменяются на ноде-владельце."
            )
        form.addRow(self.user_label, self.bundle_selector)

        self._advanced_toggle = QToolButton()
        self._advanced_toggle.setText("Дополнительно")
        self._advanced_toggle.setAccessibleName("Дополнительные параметры хоста")
        self._advanced_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._advanced_toggle.setCheckable(True)
        self._advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        content_layout.addWidget(self._advanced_toggle)
        self._advanced = QWidget()
        advanced_layout = QVBoxLayout(self._advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        ssh_group = QGroupBox("SSH")
        ssh_form = QFormLayout(ssh_group)
        ssh_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        ssh_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        advanced_layout.addWidget(ssh_group)

        self.sudo_label = QLabel("Использовать sudo:")
        self.sudo_combo = QComboBox()
        self.sudo_combo.addItem("По умолчанию (наследовать)")
        self.sudo_combo.addItem("Да")
        self.sudo_combo.addItem("Нет")
        # device.use_sudo: None → 0, True → 1, False → 2
        idx = 0 if self.device.use_sudo is None else (1 if self.device.use_sudo else 2)
        self.sudo_combo.setCurrentIndex(idx)
        self.sudo_combo.setToolTip(
            "Выполнять весь обычный SSH-скрипт с правами root через sudo и Bash.\n"
            "Команды рабочего стола выполняются от пользователя сеанса.\n"
            "По умолчанию — наследует глобальную настройку SSH.\n"
            "Да/Нет — явный per-device override.\n\n"
            "Пароль берётся из выбранной связки учётных данных."
        )
        ssh_form.addRow(self.sudo_label, self.sudo_combo)

        # Локаль: editable QComboBox с пунктами «По умолчанию (наследовать)»,
        # «(отключить)» и стандартными локалями. None = наследовать SSHConfig.locale;
        # строка (в т.ч. пустая "" = «(отключить)») = per-device override.
        # Пункт «По умолчанию» — непустой sentinel, в accept/get_updated_device
        # маппится обратно на None.
        self.locale_combo = QComboBox()
        self.locale_combo.setEditable(True)
        self._LOCALE_DEFAULT = "По умолчанию (наследовать)"
        self._LOCALE_DISABLED = "(отключить)"
        self.locale_combo.addItem(self._LOCALE_DEFAULT)
        self.locale_combo.addItem(self._LOCALE_DISABLED)
        for loc in STANDARD_LOCALES:
            self.locale_combo.addItem(loc)
        # device.locale: None → «По умолчанию», "" → «(отключить)», строка → она
        cur_locale = self.device.locale
        if cur_locale is None:
            self.locale_combo.setCurrentIndex(0)
        elif cur_locale == "":
            self.locale_combo.setCurrentIndex(1)
        else:
            idx = self.locale_combo.findText(cur_locale)
            if idx < 0:
                self.locale_combo.insertItem(2, cur_locale)
                idx = 2
            self.locale_combo.setCurrentIndex(idx)
        self.locale_combo.setPlaceholderText("ru_RU.UTF-8")
        self.locale_combo.setToolTip(
            "Локаль, экспортируемая перед выполнением SSH-команд на этом хосте\n"
            "(LANG/LC_ALL/LC_CTYPE).\n\n"
            "«По умолчанию» — наследует глобальную настройку SSH.\n"
            "«(отключить)» — не экспортировать локаль (если на хосте\n"
            "ru_RU.UTF-8 не сгенерирована, устраняет предупреждение setlocale).\n\n"
            "Можно выбрать из списка или ввести свою."
        )
        self.locale_label = QLabel("Локаль:")
        ssh_form.addRow(self.locale_label, self.locale_combo)

        self.port_label = QLabel("Порт SSH:")
        self.port_edit = QSpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(self.device.port or 22)
        ssh_form.insertRow(0, self.port_label, self.port_edit)

        remote_group = QGroupBox("Экран и пробуждение")
        remote_form = QFormLayout(remote_group)
        remote_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        remote_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        advanced_layout.addWidget(remote_group)

        # VNC-порт per-host override (0 = глобальный config vnc_port)
        self.vnc_port_label = QLabel("Порт VNC:")
        self.vnc_port_edit = QSpinBox()
        self.vnc_port_edit.setRange(0, 65535)
        self.vnc_port_edit.setSpecialValueText("Глобальный")
        self.vnc_port_edit.setValue(int(self.device.vnc_port or 0))
        self.vnc_port_edit.setToolTip(
            "Порт VNC-сервера на этом хосте (x11vnc и krfb).\n"
            "«Глобальный» (0) - использовать порт из настроек VNC."
        )
        remote_form.addRow(self.vnc_port_label, self.vnc_port_edit)

        self.thumbnail_backend_combo = QComboBox()
        self.thumbnail_backend_combo.addItem("По умолчанию", None)
        self.thumbnail_backend_combo.addItem("Veyon", "veyon")
        self.thumbnail_backend_combo.addItem("VNC", "vnc")
        idx = self.thumbnail_backend_combo.findData(self.device.thumbnail_backend)
        self.thumbnail_backend_combo.setCurrentIndex(max(0, idx))
        node_iid = getattr(self.device, "node_iid", None)
        self.thumbnail_backend_combo.setToolTip(
            "По умолчанию - настройка ноды-владельца. Изменения сохраняются на ноде."
            if node_iid and node_iid != "local"
            else "По умолчанию - общая настройка миниатюр этого экземпляра."
        )
        remote_form.addRow("Источник миниатюр:", self.thumbnail_backend_combo)
        self._thumbnail_backends_supported = True

        self.mac_label = QLabel("MAC-адрес:")
        self.mac_edit = QLineEdit(self.device.mac_address or "")
        self.mac_edit.setPlaceholderText("00:11:22:33:44:55")
        self.mac_edit.setToolTip("MAC-адрес для включения хоста через Wake-on-LAN.")
        remote_form.addRow(self.mac_label, self.mac_edit)
        content_layout.addWidget(self._advanced)
        self._advanced.hide()
        self._advanced_toggle.toggled.connect(self._set_advanced_expanded)
        self._scroll.setWidget(self._content)
        layout.addWidget(self._scroll, 1)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self.resize(340, 300)

    def _set_advanced_expanded(self, expanded: bool) -> None:
        """Показывает дополнительные поля и подбирает высоту в пределах экрана."""
        self._advanced.setVisible(expanded)
        self._advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._content.layout().activate()
        margins = self.layout().contentsMargins()
        height = (
            self._content.sizeHint().height()
            + self.button_box.sizeHint().height()
            + margins.top() + margins.bottom()
            + self.layout().spacing()
        )
        screen = self.screen()
        if screen is not None:
            height = min(height, screen.availableGeometry().height() - 80)
        self.resize(self.width(), height)

    def accept(self):
        """Валидация перед сохранением: если sudo включено, но пароль не указан — предупреждение."""
        from src.workers.command.sudo_utils import should_use_sudo

        sudo_idx = self.sudo_combo.currentIndex()
        use_sudo = {0: None, 1: True, 2: False}[sudo_idx]
        # Локаль: «По умолчанию» → None (наследовать), «(отключить)» → "" (явно
        # отключить), иначе — введённая/выбранная строка.
        loc_text = self.locale_combo.currentText().strip()
        if loc_text == self._LOCALE_DEFAULT:
            locale = None
        elif loc_text == self._LOCALE_DISABLED or not loc_text:
            locale = ""
        else:
            locale = loc_text
        tmp = DeviceModel(
            {
                "iid": getattr(self.device, "iid", None),
                "name": self.name_edit.text(),
                "host": self.host_edit.text(),
                "credential_bundle_iid": self.bundle_selector.current_iid(),
                "port": self.port_edit.value(),
                "mac_address": self.mac_edit.text() or None,
                "use_sudo": use_sudo,
                "locale": locale,
                "vnc_port": self.vnc_port_edit.value() or None,
                "thumbnail_backend": self.thumbnail_backend_combo.currentData(),
            }
        )
        node_iid = getattr(self.device, "node_iid", None)
        if not (node_iid and node_iid != "local") and should_use_sudo(tmp):
            # Проверяем пароль: связка хоста, затем глобальная связка по умолчанию
            has_pwd = False
            bundle_iid = self.bundle_selector.current_iid()
            if bundle_iid:
                try:
                    from src.di import get_container
                    from src.services.configuration.credential_service import CredentialService

                    svc = get_container().resolve_optional(CredentialService)
                    if svc is not None:
                        b = svc.get_bundle_by_iid(bundle_iid)
                        if b is not None:
                            has_pwd = bool(b.password)
                except Exception:
                    pass
            if not has_pwd:
                # Проверяем глобальную связку по умолчанию
                try:
                    from src.di import get_container
                    from src.services.configuration.credential_service import CredentialService

                    svc = get_container().resolve_optional(CredentialService)
                    if svc is not None:
                        b = svc.get_default_bundle()
                        if b is not None:
                            has_pwd = bool(b.password)
                except Exception:
                    pass
            if not has_pwd:
                btn = QMessageBox.warning(
                    self,
                    "Пароль для sudo",
                    "Включено использование sudo, но пароль не указан ни в связке, "
                    "ни в глобальной связке по умолчанию.\n\n"
                    "Команды с sudo потребуют пароль на хосте. Без пароля "
                    "они не смогут выполниться.\n\n"
                    "• «Указать связку» — вернуться в диалог.\n"
                    "• «Сохранить как есть» — если на хосте sudo без пароля "
                    "или пароль укажете позже.",
                    QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if btn != QMessageBox.StandardButton.Save:
                    self.bundle_selector.setFocus()
                    return
        super().accept()

    def set_thumbnail_backends_supported(self, supported: bool) -> None:
        """Disable unsupported VNC edits on a legacy owner node."""
        self._thumbnail_backends_supported = supported
        idx = self.thumbnail_backend_combo.findData("vnc")
        self.thumbnail_backend_combo.model().item(idx).setEnabled(supported)
        if not supported:
            self.thumbnail_backend_combo.setToolTip(
                "По умолчанию - настройка ноды-владельца. "
                "Для выбора VNC обновите программу на ноде."
            )

    def get_updated_device(self) -> DeviceModel:
        """Получить обновленные данные устройства"""
        node_iid = getattr(self.device, "node_iid", None)
        bundle_iid = (
            self.device.credential_bundle_iid
            if node_iid and node_iid != "local"
            else self.bundle_selector.current_iid()
        )
        device = DeviceModel(
            {
                "iid": getattr(self.device, "iid", None),
                "name": self.name_edit.text(),
                "host": self.host_edit.text(),
                "credential_bundle_iid": bundle_iid,
                "port": self.port_edit.value()
                if self.port_edit.value() != config.app.ssh.port
                else None,
                "mac_address": self.mac_edit.text() if self.mac_edit.text() else None,
                "use_sudo": {0: None, 1: True, 2: False}[
                    self.sudo_combo.currentIndex()
                ],
                "vnc_port": self.vnc_port_edit.value() or None,
                "thumbnail_backend": self.thumbnail_backend_combo.currentData(),
                "locale": (
                    None
                    if self.locale_combo.currentText().strip() == self._LOCALE_DEFAULT
                    else (
                        ""
                        if self.locale_combo.currentText().strip()
                        in (self._LOCALE_DISABLED, "")
                        else self.locale_combo.currentText().strip()
                    )
                ),
            }
        )
        node_iid = getattr(self.device, "node_iid", None)
        if node_iid:
            device.node_iid = node_iid
        return device

    @staticmethod
    def edit_device(device, parent=None):
        """
        Статический метод для редактирования устройства
        Args:
            device: Словарь с данными устройства
            parent: Родительское окно
        Returns:
            tuple: (bool, dict) - Успешно ли выполнено редактирование и новые данные
        """
        dialog = DeviceEditDialog(device, parent)
        if dialog.exec():
            return True, dialog.get_updated_device()
        return False, None

    @staticmethod
    def add_device(parent=None):
        """
        Статический метод для добавления нового устройства
        Args:
            parent: Родительское окно
        Returns:
            tuple: (bool, dict) - Успешно ли выполнено добавление и новые данные
        """
        dialog = DeviceEditDialog(None, parent)
        if dialog.exec():
            return True, dialog.get_updated_device()
        return False, None


class ConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Настройки")
        self.setup_ui()

    def setup_ui(self):
        main_layout = QVBoxLayout()

        # Create SSH settings group
        ssh_group = QGroupBox("Настройки SSH по умолчанию")
        ssh_layout = QFormLayout()

        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(config.app.ssh.port)
        self.strict_host_checking = QCheckBox()
        self.strict_host_checking.setChecked(config.app.ssh.strict_host_checking)

        self.command_timeout = QSpinBox()
        self.command_timeout.setRange(5, 300)
        self.command_timeout.setValue(config.app.ssh.command_timeout)
        self.command_timeout.setSuffix(" сек")

        self.connect_timeout = QSpinBox()
        self.connect_timeout.setRange(1, 60)
        self.connect_timeout.setValue(config.app.ssh.connect_timeout)
        self.connect_timeout.setSuffix(" сек")

        self.use_sudo = QCheckBox("Использовать sudo для non-root логина")
        self.use_sudo.setChecked(config.app.ssh.use_sudo)
        self.use_sudo.setToolTip(
            "При non-root логине выполнять весь обычный SSH-скрипт с правами root.\n"
            "Требуется разрешение sudo на Bash. Команды рабочего стола\n"
            "выполняются от пользователя сеанса.\n\n"
            "Поддерживаются любые команды: однострочные, многострочные, пайпы,\n"
            "heredoc, sudo -u, sudo bash -c.\n\n"
            "Пароль берётся из связки учётных данных (device или глобальной).\n"
            "Одна попытка пароля; повторный запрос завершает задачу ошибкой.\n"
            "Перекрывается per-device в свойствах устройства."
        )

        # Связка учётных данных по умолчанию (глобальная).
        from src.ui.widgets.bundle_selector import BundleSelector

        self.bundle_selector = BundleSelector(
            allow_none=True, none_label="(нет связки)"
        )
        try:
            from src.di import get_container
            from src.services.configuration.credential_service import CredentialService

            svc = get_container().resolve_optional(CredentialService)
            if svc is not None:
                self.bundle_selector.set_current_iid(svc.get_default_bundle_iid())
        except Exception:
            pass
        self.bundle_selector.setToolTip(
            "Связка учётных данных (логин/пароль) по умолчанию для всех хостов.\n"
            "Хосты с явным выбором связки перекрывают это значение.\n\n"
            "Кнопка справа открывает окно управления связками."
        )

        self.locale_combo = QComboBox()
        self.locale_combo.setEditable(True)
        self.locale_combo.addItems(list(STANDARD_LOCALES))
        cur_locale = (config.app.ssh.locale or "").strip()
        if cur_locale:
            idx = self.locale_combo.findText(cur_locale)
            if idx < 0:
                self.locale_combo.insertItem(0, cur_locale)
                idx = 0
            self.locale_combo.setCurrentIndex(idx)
        else:
            self.locale_combo.setEditText("")
        self.locale_combo.setPlaceholderText("ru_RU.UTF-8")
        self.locale_combo.setToolTip(
            "Локаль, экспортируемая перед выполнением SSH-команд\n"
            "(LANG/LC_ALL/LC_CTYPE) для корректного вывода кириллицы.\n\n"
            "По умолчанию «ru_RU.UTF-8» (ROSA Linux).\n\n"
            "Оставьте поле пустым, если на хостах локаль ru_RU.UTF-8\n"
            "не сгенерирована (нет в `locale -a`) — это устранит\n"
            "предупреждение:\n"
            "  bash: warning: setlocale: LC_ALL: cannot change locale (ru_RU.UTF-8)\n\n"
            "Перекрывается per-device в свойствах устройства."
        )

        # Add fields to SSH group
        ssh_layout.addRow("Связка по умолчанию:", self.bundle_selector)
        ssh_layout.addRow("Порт:", self.port)
        ssh_layout.addRow("Строгая проверка ключей:", self.strict_host_checking)
        ssh_layout.addRow("Таймаут команды:", self.command_timeout)
        ssh_layout.addRow("Таймаут подключения:", self.connect_timeout)
        ssh_layout.addRow(self.use_sudo)
        ssh_layout.addRow("Локаль:", self.locale_combo)

        ssh_group.setLayout(ssh_layout)
        main_layout.addWidget(ssh_group)

        # Create General settings group
        general_group = QGroupBox("Общие настройки")
        general_layout = QFormLayout()

        self.thread_count = ThreadCountSlider(value=config.app.network.thread_count)
        self.thread_count.setToolTip(
            "Размер каждого из двух независимых общих пулов: статусы и "
            "служебные операции. Например, 8 означает до 8 потоков на пул. "
            "Пулы общие для приложения, веб-интерфейса и нод. "
            "Применяется после перезапуска программы."
        )
        self.command_thread_count = ThreadCountSlider(
            value=config.app.execution.thread_count
        )
        self.command_thread_count.setToolTip(
            "Общий лимит одновременно выполняемых команд из приложения, "
            "веб-интерфейса и нод. Применяется после перезапуска программы."
        )

        self.ping_interval = QSpinBox()
        self.ping_interval.setRange(5, 300)
        self.ping_interval.setValue(config.app.network.ping_interval)
        self.ping_interval.setSuffix(" сек")

        self.ping_timeout = QDoubleSpinBox()
        self.ping_timeout.setRange(0.5, 10.0)
        self.ping_timeout.setValue(config.app.network.ping_timeout)
        self.ping_timeout.setSingleStep(0.5)
        self.ping_timeout.setSuffix(" сек")

        self.expand_trees = QCheckBox()
        self.expand_trees.setChecked(config.app.expand)

        self.kaomoji_enabled = QCheckBox()
        self.kaomoji_enabled.setChecked(getattr(config.app, "kaomoji_enabled", True))
        self.kaomoji_enabled.setToolTip(
            "Показывать каомодзи-эмоции в статусной строке\n"
            "(меняются в зависимости от результата операции)"
        )

        # Add fields to General group
        general_layout.addRow(
            "Потоки статусов и служебных операций:", self.thread_count
        )
        general_layout.addRow("Потоки выполнения команд:", self.command_thread_count)
        restart_hint = QLabel(
            "Изменения числа потоков применяются после перезапуска программы."
        )
        restart_hint.setWordWrap(True)
        restart_hint.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        general_layout.addRow(restart_hint)
        general_layout.addRow("Интервал пинга:", self.ping_interval)
        general_layout.addRow("Таймаут пинга:", self.ping_timeout)
        general_layout.addRow("Разворачивать деревья:", self.expand_trees)
        general_layout.addRow("Каомодзи в статус-строке:", self.kaomoji_enabled)

        general_group.setLayout(general_layout)
        main_layout.addWidget(general_group)

        # Внешний вид: тема оформления (QPalette). Выбор применяется сразу,
        # сохраняется в конфиг только по «Сохранить», «Отмена» возвращает назад.
        appearance_group = QGroupBox("Внешний вид")
        appearance_layout = QFormLayout()
        from src.ui.theme_manager import (
            FALLBACK_THEME_ID,
            SYSTEM_THEME_ID,
            apply_theme,
            list_themes,
        )

        self._initial_theme_id = getattr(config.app, "theme", FALLBACK_THEME_ID)
        self.theme_combo = QComboBox()
        for theme in list_themes():
            tid = theme.get("id", SYSTEM_THEME_ID)
            label = theme.get("name", tid)
            if theme.get("dark") is True:
                label = f"{label} (тёмная)"
            elif theme.get("dark") is False:
                label = f"{label} (светлая)"
            self.theme_combo.addItem(label, tid)
        idx = self.theme_combo.findData(self._initial_theme_id)
        self.theme_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.theme_combo.setToolTip(
            "Тема оформления интерфейса.\n"
            "«Системная» - нативный вид Windows/Linux.\n"
            "Остальные - палитра поверх стиля Fusion: цвета окон,\n"
            "меню, тултипов и элементов едины на всех системах.\n\n"
            "Применяется сразу, без перезапуска."
        )
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        appearance_layout.addRow("Тема:", self.theme_combo)
        from src.ui.icon_theme_manager import list_icon_themes

        self._initial_icon_theme_id = getattr(config.app, "icon_theme", "flat")
        self.icon_theme_combo = QComboBox()
        for icon_theme in list_icon_themes():
            iid = icon_theme.get("id", "flat")
            self.icon_theme_combo.addItem(icon_theme.get("name", iid), iid)
        idx = self.icon_theme_combo.findData(self._initial_icon_theme_id)
        self.icon_theme_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.icon_theme_combo.setToolTip(
            "Набор иконок интерфейса.\n"
            "Flat - плоские, Classic - объёмные.\n\n"
            "Применяется сразу, без перезапуска."
        )
        self.icon_theme_combo.currentIndexChanged.connect(self._on_icon_theme_changed)
        appearance_layout.addRow("Иконки:", self.icon_theme_combo)
        appearance_group.setLayout(appearance_layout)
        main_layout.addWidget(appearance_group)

        # Дебаунс live-применения тем: каждое переключение комбобокса
        # (стрелки/колесо по списку) иначе синхронно делает полный refresh
        # иконок всех виджетов (при большом дереве - фриз на каждый шаг).
        # Пачка переключений схлопывается в одно применение по таймауту.
        self._theme_debounce_timer = QTimer(self)
        self._theme_debounce_timer.setSingleShot(True)
        self._theme_debounce_timer.setInterval(200)
        self._theme_debounce_timer.timeout.connect(self._apply_pending_themes)
        self._pending_theme_id = None
        self._pending_icon_theme_id = None
        # Фактически применённая visual-тема (apply_theme может откатить
        # неизвестный id на fallback): база для «изменилась ли» в reject.
        self._applied_theme_id = self._initial_theme_id

        # Add buttons
        buttons = QDialogButtonBox()
        self.save_button = buttons.addButton("Сохранить", QDialogButtonBox.AcceptRole)
        self.cancel_button = buttons.addButton("Отмена", QDialogButtonBox.RejectRole)

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        main_layout.addWidget(buttons)
        self.setLayout(main_layout)

    def _on_theme_changed(self, index: int) -> None:
        """Live-применение темы при выборе в комбобоксе (без сохранения).

        Откладывается через дебаунс-таймер: перелистывание комбобокса
        стрелками/колесом не должно фризить GUI на каждый шаг.
        """
        self._pending_theme_id = self.theme_combo.itemData(index)
        self._theme_debounce_timer.start()

    def _on_icon_theme_changed(self, index: int) -> None:
        """Live-применение темы иконок (без сохранения), через дебаунс."""
        self._pending_icon_theme_id = self.icon_theme_combo.itemData(index)
        self._theme_debounce_timer.start()

    def _apply_pending_themes(self) -> None:
        """Разово применить накопленные за дебаунс выборы тем."""
        theme_id = self._pending_theme_id
        icon_theme_id = self._pending_icon_theme_id
        self._pending_theme_id = None
        self._pending_icon_theme_id = None
        if theme_id is not None:
            from PySide6.QtWidgets import QApplication

            from src.ui.theme_manager import FALLBACK_THEME_ID, apply_theme

            app = QApplication.instance()
            if app is not None:
                self._applied_theme_id = apply_theme(app, theme_id or FALLBACK_THEME_ID)
        if icon_theme_id is not None:
            from src.ui.icon_theme_manager import apply_icon_theme

            apply_icon_theme(icon_theme_id or "flat")
        if theme_id is not None or icon_theme_id is not None:
            self._refresh_parent_icons()

    def _icon_theme_effectively_changed(self) -> bool:
        """Текущая икон-тема отличается от исходной на момент открытия."""
        try:
            from src.ui.icon_theme_manager import get_icon_theme

            return get_icon_theme() != self._initial_icon_theme_id
        except Exception:
            return False

    def _visual_theme_effectively_changed(self) -> bool:
        """Визуальная тема реально применялась и отличается от исходной.

        Трекаем фактический id применённой темы (apply_theme возвращает
        эффективный id): сравнение по палитре ненадёжно для system-темы.
        """
        return self._applied_theme_id != self._initial_theme_id

    def _refresh_parent_icons(self) -> None:
        """Попросить главное окно перечитать иконки (live-превью темы)."""
        seen = set()
        widget = self.parent()
        while widget is not None and id(widget) not in seen:
            seen.add(id(widget))
            refresh = getattr(widget, "refresh_icons", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception as e:
                    logger.debug(f"ConfigDialog: parent refresh_icons: {e}")
                return
            try:
                widget = widget.parent()
            except Exception:
                return

    def reject(self) -> None:
        """Отмена: вернуть темы, которые были до открытия настроек.

        Быстрый выход: если темы реально не менялись (окно закрыли без
        переключения комбобоксов), не трогаем apply_theme/apply_icon_theme
        и не дёргаем полный refresh - setStyle+setPalette иначе даёт
        re-polish всех окон (заметный фриз на закрытие).
        """
        self._theme_debounce_timer.stop()
        self._pending_theme_id = None
        self._pending_icon_theme_id = None
        theme_changed = self._visual_theme_effectively_changed()
        icon_changed = self._icon_theme_effectively_changed()
        if theme_changed or icon_changed:
            if theme_changed:
                from PySide6.QtWidgets import QApplication

                from src.ui.theme_manager import apply_theme

                app = QApplication.instance()
                if app is not None:
                    self._applied_theme_id = apply_theme(app, self._initial_theme_id)
            if icon_changed:
                from src.ui.icon_theme_manager import apply_icon_theme

                apply_icon_theme(self._initial_icon_theme_id)
            self._refresh_parent_icons()
        super().reject()

    def accept(self):
        """Save configuration when OK is clicked"""
        thread_count_new = self.thread_count.value()
        thread_count_changed = thread_count_new != config.app.network.thread_count
        command_thread_count_new = self.command_thread_count.value()
        thread_count_changed |= (
            command_thread_count_new != config.app.execution.thread_count
        )

        # Сохраняем связку по умолчанию (глобальная)
        try:
            from src.di import get_container
            from src.services.configuration.credential_service import CredentialService

            svc = get_container().resolve_optional(CredentialService)
            if svc is not None:
                svc.set_default_bundle_iid(self.bundle_selector.current_iid() or "")
        except Exception as e:
            logger.error(f"ConfigDialog: cannot set default bundle: {e}")

        config.app.ssh.port = self.port.value()
        config.app.ssh.strict_host_checking = self.strict_host_checking.isChecked()
        config.app.ssh.command_timeout = self.command_timeout.value()
        config.app.ssh.connect_timeout = self.connect_timeout.value()
        config.app.ssh.use_sudo = self.use_sudo.isChecked()
        config.app.ssh.locale = self.locale_combo.currentText().strip()
        config.app.network.thread_count = thread_count_new
        config.app.execution.thread_count = command_thread_count_new

        # Валидация: sudo без пароля — предупреждение (пароль опционален,
        # если у хостов есть per-device связки или на хостах sudo без пароля).
        if config.app.ssh.use_sudo:
            # Проверяем, есть ли пароль в выбранной связке по умолчанию
            default_iid = self.bundle_selector.current_iid()
            has_default_pwd = False
            if default_iid:
                try:
                    from src.di import get_container
                    from src.services.configuration.credential_service import CredentialService

                    svc = get_container().resolve_optional(CredentialService)
                    if svc is not None:
                        b = svc.get_bundle_by_iid(default_iid)
                        if b is not None:
                            has_default_pwd = bool(b.password)
                except Exception:
                    pass
            if not has_default_pwd:
                btn = QMessageBox.warning(
                    self,
                    "Пароль для sudo",
                    "Глобальная настройка sudo включена, но в выбранной связке "
                    "нет пароля (или связка не выбрана).\n\n"
                    "Команды с sudo потребуют пароль на каждом хосте. Без пароля "
                    "они не смогут выполниться.\n\n"
                    "• «Указать связку» — вернуться в настройки.\n"
                    "• «Сохранить как есть» — если на хостах sudo без пароля "
                    "или для каждого хоста задана отдельная связка с паролем.",
                    QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if btn != QMessageBox.StandardButton.Save:
                    self.bundle_selector.setFocus()
                    return

        # Сохраняем новые настройки пинга
        config.app.network.ping_interval = self.ping_interval.value()
        config.app.network.ping_timeout = self.ping_timeout.value()

        config.app.expand = self.expand_trees.isChecked()
        config.app.kaomoji_enabled = self.kaomoji_enabled.isChecked()
        from src.ui.theme_manager import FALLBACK_THEME_ID as _FALLBACK_THEME_ID

        selected_theme = self.theme_combo.currentData() or _FALLBACK_THEME_ID
        selected_icon_theme = self.icon_theme_combo.currentData() or "flat"
        # Если дебаунс ещё не успел применить выбор live - применяем сейчас.
        # apply_theme/apply_icon_theme возвращают эффективный id (unknown
        # откатывается на fallback) - сохраняем в конфиг именно его.
        self._theme_debounce_timer.stop()
        self._pending_theme_id = None
        self._pending_icon_theme_id = None
        from PySide6.QtWidgets import QApplication

        from src.ui.icon_theme_manager import apply_icon_theme
        from src.ui.theme_manager import apply_theme

        app = QApplication.instance()
        if app is not None:
            config.app.theme = apply_theme(app, selected_theme)
        else:
            config.app.theme = selected_theme
        config.app.icon_theme = apply_icon_theme(selected_icon_theme)
        config.save()

        # Обновляем настройки для всех активных таймеров пинга
        from src.workers.network import get_host_ping_timer_manager

        manager = get_host_ping_timer_manager()
        manager.update_all_timers_settings(
            ping_interval=config.app.network.ping_interval,
            ping_timeout=config.app.network.ping_timeout,
            ping_port=config.app.ssh.port,
        )

        if thread_count_changed:
            QMessageBox.information(
                self,
                "Перезапуск",
                "Количество потоков изменено. Для применения перезапустите программу.",
            )

        super().accept()


class _DeviceInfoDialogLegacy(QDialog):
    def __init__(self, device: DeviceModel, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.device = device
        self.setup_ui()

    def setup_ui(self):
        self.setWindowTitle("Информация об устройстве")
        self.setMinimumWidth(300)

        layout = QVBoxLayout(self)

        # Add icon label at the top
        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Get current icon from list item
        if self.device.icon is not None:
            icon = qicon_from_path(self.device.icon)
            if isinstance(icon, QIcon):
                pixmap = icon.pixmap(48, 48)
                self.icon_label.setPixmap(pixmap)

        layout.addWidget(self.icon_label)

        # Create form layout for device info with alignments
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignRight)

        # Create and style name fields
        name_label = QLabel("Имя:")
        name_value = QLabel(self.device.name)
        name_value.setAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow(name_label, name_value)

        # Create and style host fields
        host_label = QLabel("Хост:")
        host_value = QLabel(self.device.host)
        host_value.setAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow(host_label, host_value)

        layout.addLayout(form)

        # Add OK button
        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class CommandInfoDialog(QDialog):
    def __init__(self, command_data: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.command_data = command_data
        self.setup_ui()

    def setup_ui(self):
        self.setWindowTitle("Информация о команде")
        # self.setGeometry(300, 300, 450, 250)
        layout = QVBoxLayout()

        self.detailsLabel = QLabel("Выберите команду из списка", self)
        self.detailsLabel.setWordWrap(True)
        layout.addWidget(QLabel("Детали команды:"))
        layout.addWidget(self.detailsLabel)
        layout.addStretch(1)
        self.setLayout(layout)

        if self.command_data:
            name = self.command_data.get("name", "N/A")
            commands = self.command_data.get("command", [])
            description = self.command_data.get("description", "Нет описания")
            details_text = f"<b>Имя:</b> {name} <br/>"
            details_text += f"<b>Описание:</b> {description}<br/>"
            details_text += "<b>Команды для выполнения:</b>"
            if commands:
                details_text += "<ul>"
                for cmd in commands:
                    details_text += f"<li><code>{cmd}</code></li>"
                details_text += "</ul>"
            else:
                details_text += "<i>(нет команд)</i>"
            self.detailsLabel.setText(details_text)
        else:
            self.detailsLabel.setText("Нет данных для отображения.")


class ExportCommandDlg(QDialog):
    """
    Диалог экспорта команд.
    Поддерживает два режима:
    1. Экспорт списка команд (список словарей с type/text)
    2. Экспорт полной команды (словарь с name, description, timeout, params, commands)
    """

    def __init__(self, parent=None, commands=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        # Определяем тип данных и кодируем
        encoded_text = b64encode(
            json.dumps(commands, ensure_ascii=False).encode("utf-8")
        ).decode("utf-8")
        self.encoded_text = encoded_text
        self.is_full_command = isinstance(commands, dict)
        self.setup_ui()

    def setup_ui(self):
        self.setWindowTitle(
            "Экспорт полной команды" if self.is_full_command else "Экспорт команд"
        )
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)

        if self.is_full_command:
            self.info_label = QLabel(
                "Скопируйте текст для экспорта команды (включая имя, описание, таймаут, параметры):"
            )
        else:
            self.info_label = QLabel("Скопируйте текст для экспорта команд:")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        self.command_text_edit = QTextEdit()
        self.command_text_edit.setReadOnly(True)
        self.command_text_edit.setText(self.encoded_text)
        layout.addWidget(self.command_text_edit, stretch=1)

        # Кнопки копирования и закрытия
        buttons_layout = QHBoxLayout()

        self.copy_button = QPushButton("Копировать")
        self.copy_button.clicked.connect(self.command_text_edit.selectAll)
        self.copy_button.clicked.connect(self.command_text_edit.copy)
        buttons_layout.addWidget(self.copy_button)

        buttons_layout.addStretch()

        ok_button = QPushButton("OK")
        ok_button.clicked.connect(self.accept)
        buttons_layout.addWidget(ok_button)

        layout.addLayout(buttons_layout)
        self.setLayout(layout)

        # Автоматически выделяем весь текст
        self.command_text_edit.selectAll()
        self.command_text_edit.copy()


class ImportCommandDlg(QDialog):
    """
    Диалог импорта команд.
    Автоматически определяет формат:
    1. Список команд (список словарей с type/text)
    2. Полная команда (словарь с name, description, timeout, params, commands)

    Returns:
        dict с полями:
        - is_full_command: bool (True если полная команда)
        - data: сами данные (dict или list)
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        self.setWindowTitle("Импорт команды")
        self.setMinimumWidth(400)
        self.setSizeGripEnabled(True)

        layout = QVBoxLayout(self)

        self.info_label = QLabel(
            "Вставьте закодированный текст команды:\nПоддерживается импорт как отдельных команд, так и полных команд со всей информацией."
        )
        self.info_label.setWordWrap(True)
        self.info_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.addWidget(self.info_label)

        self.command_text_edit = QTextEdit()
        self.command_text_edit.setPlaceholderText("Вставьте данные команды...")
        self.command_text_edit.setSizePolicy(
            QSizePolicy.Preferred, QSizePolicy.Expanding
        )
        layout.addWidget(self.command_text_edit, stretch=1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_decoded_command(self):
        """
        Декодируем base64 строку и определяем тип данных.

        Returns:
            dict с полями:
            - is_full_command: bool
            - data: dict (полная команда) или list (список команд)
            - error: str (если ошибка)
        """
        try:
            encoded_text = self.command_text_edit.toPlainText().strip()
            if not encoded_text:
                return {
                    "is_full_command": False,
                    "data": None,
                    "error": "Текст команды пуст",
                }

            decoded_json = b64decode(encoded_text.encode("utf-8")).decode("utf-8")
            data = json.loads(decoded_json)

            # Определяем тип данных
            if isinstance(data, dict):
                # Это полная команда - проверяем обязательные поля
                # name не требуется - при импорте используется имя текущей команды
                if "commands" not in data:
                    return {
                        "is_full_command": False,
                        "data": None,
                        "error": "Отсутствует поле 'commands'",
                    }

                # Проверяем формат команд внутри полной команды
                if not isinstance(data["commands"], list):
                    return {
                        "is_full_command": False,
                        "data": None,
                        "error": "Поле 'commands' должно быть списком",
                    }

                for cmd in data["commands"]:
                    if not isinstance(cmd, dict):
                        return {
                            "is_full_command": False,
                            "data": None,
                            "error": "Каждая команда должна быть словарём",
                        }
                    if "type" not in cmd or "text" not in cmd:
                        return {
                            "is_full_command": False,
                            "data": None,
                            "error": "Каждая команда должна иметь 'type' и 'text'",
                        }

                return {"is_full_command": True, "data": data, "error": None}

            elif isinstance(data, list):
                # Это список команд - проверяем формат
                for cmd in data:
                    if not isinstance(cmd, dict):
                        return {
                            "is_full_command": False,
                            "data": None,
                            "error": "Каждая команда должна быть словарём",
                        }
                    if "type" not in cmd or "text" not in cmd:
                        return {
                            "is_full_command": False,
                            "data": None,
                            "error": "Каждая команда должна иметь 'type' и 'text'",
                        }

                return {"is_full_command": False, "data": data, "error": None}

            else:
                return {
                    "is_full_command": False,
                    "data": None,
                    "error": "Неверный формат данных",
                }

        except json.JSONDecodeError as e:
            return {
                "is_full_command": False,
                "data": None,
                "error": f"Ошибка декодирования JSON: {e}",
            }
        except Exception as e:
            return {"is_full_command": False, "data": None, "error": f"Ошибка: {e}"}


class BashViewerDialog(QDialog):
    """Read-only plain-text script preview with search and line numbers."""

    def __init__(self, parent=None, initial_text: str = None):
        super().__init__(parent)
        from src.ui.widgets.script_editor import ScriptEditor

        self.setSizeGripEnabled(True)
        self.setWindowTitle("Просмотр скрипта")
        self.setMinimumSize(700, 440)
        self.resize(960, 640)
        layout = QVBoxLayout(self)
        self.script_editor = ScriptEditor(self, read_only=True, show_menu=False)
        self.editor = self.script_editor.editor
        self.highlighter = self.editor.highlighter
        self.editor.setPlainText(initial_text or "")
        layout.addWidget(self.script_editor)
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.button_box.rejected.connect(self.accept)
        layout.addWidget(self.button_box)
