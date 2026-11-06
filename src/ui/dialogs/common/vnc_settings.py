"""Настройки VNC - единое окно для x11vnc, kmsvnc и krfb (KDE RFB).

Экспортирует:
- VncConfigWidget - композитный виджет для встраивания в мастер настройки
  и в диалоги. Селектор сервера сверху («Сервер: x11vnc | kmsvnc | krfb»,
  persist config.app.veyon.vnc_server), параметры подключения (общий порт
  и общий пароль на все серверы). Порт и пароль ОДИНАКОВЫ для всех
  серверов: ключи veyon.vnc_port / veyon.vnc_password. Окно задаёт ТОЛЬКО
  локальные параметры/пароль оператора; УСТАНОВКА/ОБНОВЛЕНИЕ сервера
  и пароля на хостах выполняется Мастером настройки
  (RemoteServiceInstallerWorker, повторный запуск установки применяет
  новый пароль к уже настроенным хостам) - в окне это указано явным
  блоком. lock_server - фиксация селектора (мастер задаёт выбор сервера
  на intro).
- VncSettingsDialog - тонкая обёртка-диалог над VncConfigWidget.

Режим VNC - альтернатива Veyon для браузерного удалённого рабочего стола:
на каждом хосте поднимается выбранный VNC-сервер, dno-tool подключается
напрямую к порту без Veyon-аутентификации.
"""

from PySide6.QtWidgets import (
    QMessageBox,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QSpinBox,
    QLabel,
    QPushButton,
    QSizePolicy,
    QDialog,
    QDialogButtonBox,
    QComboBox,
)

from PySide6.QtCore import Qt

from src.config import config
from src.config.settings import DEFAULT_VNC_SERVER
from src.logger import logger

# Подписи серверов VNC: порядок - порядок в списках выбора.
SERVER_LABELS = {
    "kmsvnc": "kmsvnc — X11 и Wayland, кроме NVIDIA (по умолчанию)",
    "x11vnc": "x11vnc — только сеансы X11",
    "krfb": "krfb — KDE, только сеанс пользователя",
}

# Что выбрать и почему: текст под списком выбора сервера.
SERVER_GUIDE = {
    "kmsvnc": (
        "<b>Когда выбирать:</b> вариант по умолчанию для МОС 12 и 13 — "
        "сеансы X11 и Wayland.<br>"
        "<b>Как работает:</b> системная служба читает изображение прямо из "
        "видеокарты: экран входа и сеанс пользователя, без запросов "
        "разрешения. Пакет поставляется вместе с dno-tool.<br>"
        "<b>Ограничения:</b> <b>видеокарты NVIDIA не поддерживаются</b> — "
        "работает с Intel и AMD. Мастер проверяет видеокарту заранее и на "
        "неподходящем хосте ничего не меняет, а пишет причину в журнал: "
        "для таких хостов выберите x11vnc (сеанс X11) или KRFB (Wayland). "
        "Также: только x86_64; один монитор; нет "
        "общего буфера обмена; пока экран погашен энергосбережением, "
        "показывает последний кадр. Нужен пароль."
    ),
    "x11vnc": (
        "<b>Когда выбирать:</b> хосты только с сеансом X11 (МОС 12, Plasma 5), "
        "в том числе с видеокартой NVIDIA; нужен общий буфер обмена. Пакет из "
        "репозитория МОС.<br>"
        "<b>Как работает:</b> системная служба от root показывает и экран "
        "входа, и сеанс пользователя, есть общий буфер обмена.<br>"
        "<b>Ограничения:</b> сеанс Wayland не видит: хосты с Wayland мастер "
        "пропустит с объяснением. Пароль необязателен."
    ),
    "krfb": (
        "<b>Когда выбирать:</b> Wayland KDE на хостах, где kmsvnc не работает "
        "(видеокарта NVIDIA).<br>"
        "<b>Как работает:</b> штатный сервер KDE в сеансе пользователя; в "
        "Wayland экран передаётся через портал KDE, разрешение мастер выдаёт "
        "заранее. Подключение — в браузере, на Linux также в krdc.<br>"
        "<b>Ограничения:</b> экран входа не показывает: до входа "
        "пользователя подключиться нельзя. Нужен пароль."
    ),
}

# Серверы, которые без пароля не настраиваются.
PASSWORD_REQUIRED = ("kmsvnc", "krfb")


def server_label(server: str) -> str:
    """Короткое имя сервера для журналов, меню и сводки мастера."""
    return server if server in SERVER_LABELS else DEFAULT_VNC_SERVER


# ================================================================
#  Композитный виджет
# ================================================================


class VncConfigWidget(QWidget):
    """Композитный виджет настройки VNC (x11vnc/kmsvnc/krfb) для мастера/диалога."""

    def __init__(self, parent=None, lock_server: bool = False):
        super().__init__(parent)
        self._lock_server = bool(lock_server)
        self._server_banner = None
        self._setup_ui()
        self._load_values()

    # ================================================================
    #  UI
    # ================================================================

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # --- Селектор сервера ---
        server_row = QHBoxLayout()
        self._server_row = server_row
        self._server_title = QLabel("Сервер:")
        server_row.addWidget(self._server_title)
        self._server_combo = QComboBox()
        for sid, label in SERVER_LABELS.items():
            self._server_combo.addItem(label, sid)
        self._server_combo.setToolTip(
            "VNC-сервер на хостах. Устанавливает его Мастер настройки; "
            "чем серверы отличаются, написано под настройками подключения."
        )
        self._server_combo.currentIndexChanged.connect(self._on_server_changed)
        if self._lock_server:
            self._server_combo.setEnabled(False)
        server_row.addWidget(self._server_combo)
        server_row.addStretch()
        layout.addLayout(server_row)

        layout.addWidget(self._build_params_tab())

        # Отсылка к мастеру: здесь только локальные настройки оператора,
        # установка/развертывание на хосты — через Мастер настройки.
        self._wizard_hint = QLabel(
            "Установка и обновление VNC-сервера на хостах выполняется "
            "Мастером настройки (Помощь - Мастер настройки)."
        )
        self._wizard_hint.setWordWrap(True)
        self._wizard_hint.setStyleSheet("color: gray;")
        layout.addWidget(self._wizard_hint)

    # --- Параметры подключения ---

    def _build_params_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setSpacing(10)

        conn_group = QGroupBox("Подключение")
        conn_form = QFormLayout(conn_group)
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(5900)
        self._port_spin.setToolTip(
            "Общий порт VNC-сервера (для всех серверов; можно переопределить "
            "per-host полем «Порт VNC» в редакторе хоста)."
        )
        conn_form.addRow("Порт по умолчанию:", self._port_spin)

        self._password_edit = QLineEdit()
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_edit.setPlaceholderText(
            "Пароль (общий для всех серверов; пусто - без пароля, только x11vnc)"
        )
        conn_form.addRow("Пароль:", self._password_edit)

        gen_row = QHBoxLayout()
        self._params_gen_btn = QPushButton("Сгенерировать")
        self._params_gen_btn.clicked.connect(self._generate_password)
        gen_row.addWidget(self._params_gen_btn)
        self._show_pwd_btn = QPushButton("Показать")
        self._show_pwd_btn.setCheckable(True)
        self._show_pwd_btn.toggled.connect(self._toggle_password_visible)
        gen_row.addWidget(self._show_pwd_btn)
        gen_row.addStretch()
        conn_form.addRow("", gen_row)

        tab_layout.addWidget(conn_group)

        # Инфо-подсказка по выбранному серверу
        self._server_hint = QLabel()
        self._server_hint.setWordWrap(True)
        self._server_hint.setTextFormat(Qt.TextFormat.RichText)
        tab_layout.addWidget(self._server_hint)

        tab_layout.addStretch()
        return tab

    # ================================================================
    #  Сервер
    # ================================================================

    def current_server(self) -> str:
        """Выбранный сервер: ключ из SERVER_LABELS."""
        return self._server_combo.currentData() or DEFAULT_VNC_SERVER

    @staticmethod
    def server_description(server: str) -> str:
        """Когда выбирать сервер, как он работает и чем ограничен (HTML)."""
        return SERVER_GUIDE.get(server, SERVER_GUIDE["x11vnc"])

    def show_server_banner(self, text: str) -> None:
        """Заменить селектор сервера информативным текстом (для мастера).

        Неактивный комбобокс скрывается, вместо него показывается метка
        на всю ширину. Внутренний выбор сервера (set_server/current_server)
        продолжает работать - порт, пароль и подсказка переключаются
        как раньше.
        """
        if self._server_banner is None:
            self._server_banner = QLabel()
            self._server_banner.setWordWrap(True)
            self._server_banner.setStyleSheet("color: gray;")
            # Expanding: иначе метка с переносами занимает только ширину
            # sizeHint (узкая полоса слева), а не всю ширину окна.
            self._server_banner.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )
            self._server_row.insertWidget(0, self._server_banner)
            # Весь лишний ряд - баннеру, а не spacer в конце.
            self._server_row.setStretchFactor(self._server_banner, 1)
        self._server_title.setVisible(False)
        self._server_combo.setVisible(False)
        self._server_banner.setText(text)
        self._server_banner.setVisible(True)

    def set_server(self, server: str) -> None:
        """Установить селектор сервера программно (мастер)."""
        idx = self._server_combo.findData(server)
        if idx >= 0:
            self._server_combo.setCurrentIndex(idx)

    def _on_server_changed(self, _idx: int):
        # Порт и пароль ОБЩИЕ для обоих серверов (ключи veyon.vnc_port
        # и veyon.vnc_password): при переключении селектора поле порта
        # не меняется, меняется только подсказка.
        server = self.current_server()
        self._server_hint.setText(self.server_description(server))

    # ================================================================
    #  Загрузка/сохранение
    # ================================================================

    def _load_values(self):
        server = getattr(config.app.veyon, "vnc_server", DEFAULT_VNC_SERVER) or DEFAULT_VNC_SERVER
        idx = self._server_combo.findData(server)
        # Сигнал currentIndexChanged сам вызовет _on_server_changed
        # (обновит подсказку); порт и пароль ставим напрямую из конфига.
        self._server_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._port_spin.setValue(
            int(getattr(config.app.veyon, "vnc_port", 5900) or 5900)
        )
        self._password_edit.setText(getattr(config.app.veyon, "vnc_password", "") or "")
        self._server_hint.setText(self.server_description(self.current_server()))

    def save(self):
        """Сохранить параметры VNC в конфиг.

        Порт и пароль общие для обоих серверов: ключи veyon.vnc_port
        и veyon.vnc_password.
        """
        server = self.current_server()
        port = int(self._port_spin.value())
        pwd = self._password_edit.text()
        from src.di import get_container
        from src.services.configuration.config_service import ConfigService

        service = get_container().resolve(ConfigService)
        if not service.update_vnc_config(port=port, password=pwd, server=server):
            raise ValueError("Некорректные параметры VNC")
        success, message = service.save_config()
        if not success:
            raise OSError(message)
        logger.info("VncConfigWidget: config saved (server=%s)", server)

    def is_configured(self) -> bool:
        """True, если порт задан (пароль опционален)."""
        return int(self._port_spin.value()) > 0

    def get_vnc_params(self) -> tuple:
        """Вернуть (server, port, password) без сохранения в config.

        Используется мастером первоначальной настройки для передачи
        значений в RemoteServiceInstallerWorker и в summary до Finish.
        """
        return (
            self.current_server(),
            int(self._port_spin.value()),
            self._password_edit.text(),
        )

    def cleanup(self):
        """Совместимость с прежним API (фоновых потоков больше нет)."""

    # ================================================================
    #  Генерация пароля
    # ================================================================

    def _generate_password(self):
        import secrets
        import string

        alphabet = string.ascii_letters + string.digits
        pwd = "".join(secrets.choice(alphabet) for _ in range(12))
        self._password_edit.setText(pwd)

    def _toggle_password_visible(self, checked: bool):
        """Показать/скрыть пароль в поле ввода."""
        self._password_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )


# ================================================================
#  Диалог-обёртка
# ================================================================


class VncSettingsDialog(QDialog):
    """Диалог настроек VNC - тонкая обёртка над VncConfigWidget."""

    def __init__(self, parent=None, lock_server: bool = False):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Настройки VNC")
        self.setMinimumWidth(560)
        self.setMinimumHeight(480)
        self.resize(600, 520)

        layout = QVBoxLayout(self)
        self.widget = VncConfigWidget(self, lock_server=lock_server)
        layout.addWidget(self.widget)

        buttons = QDialogButtonBox()
        self._save_btn = buttons.addButton("Сохранить", QDialogButtonBox.AcceptRole)
        self._cancel_btn = buttons.addButton("Отмена", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self._on_reject)
        layout.addWidget(buttons)

    def _on_save(self):
        try:
            self.widget.save()
        except Exception:
            QMessageBox.critical(self, "Сохранение VNC", "Не удалось сохранить настройки VNC.")
            return
        self.accept()

    def _on_reject(self):
        self.widget.cleanup()
        self.reject()

    def closeEvent(self, event):
        try:
            self.widget.cleanup()
        except Exception:
            pass
        event.accept()
