"""Страница мастера настройки: SSH-ключи локальной машины (local_ssh)."""

import os

from PySide6.QtWidgets import QCheckBox, QFormLayout, QFrame, QVBoxLayout

from src.config import config
from src.ui.dialogs.common.ssh_manage import SSHKeyTableWidget
from src.ui.dialogs.wizard.setups.base import SetupPage, hint_label
from src.ui.widgets.bundle_selector import BundleSelector


class SshSetupPage(SetupPage):
    """SSH-ключи и связка по умолчанию."""

    sid = "local_ssh"

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        layout.addWidget(
            hint_label(
                "Используется существующий SSH-ключ по умолчанию.\n"
                "Если открытого ключа нет, он извлекается из закрытого.\n"
                "Установка ключей на удалённые хосты выполняется на шаге «Удалённые».\n"
                "Связка по умолчанию используется для хостов, у которых не задана "
                "своя связка на шаге «Хосты»."
            )
        )

        creds_box = QFrame()
        creds_box.setFrameShape(QFrame.Shape.NoFrame)
        creds_form = QFormLayout(creds_box)
        creds_form.setContentsMargins(0, 4, 0, 4)
        creds_form.setSpacing(6)

        # Связка учётных данных по умолчанию (глобальная)
        self._ssh_bundle_selector = BundleSelector(
            allow_none=True, none_label="(нет связки)"
        )
        try:
            from src.di import get_container
            from src.services.configuration.credential_service import CredentialService

            svc = get_container().resolve_optional(CredentialService)
            if svc is not None:
                self._ssh_bundle_selector.set_current_iid(svc.get_default_bundle_iid())
        except Exception:
            pass
        self._ssh_bundle_selector.setToolTip(
            "Связка учётных данных (логин/пароль) по умолчанию для SSH-подключений.\n"
            "Хосты с явным выбором связки перекрывают это значение.\n\n"
            "Кнопка справа открывает окно управления связками."
        )
        creds_form.addRow("Связка по умолчанию:", self._ssh_bundle_selector)

        self._ssh_use_sudo_cb = QCheckBox(
            "Использовать sudo для non-root логина (например, wheel)"
        )
        self._ssh_use_sudo_cb.setChecked(config.app.ssh.use_sudo)
        self._ssh_use_sudo_cb.setToolTip(
            "Включите, если SSH-логин - не root, а пользователь с sudo.\n"
            "Пароль sudo берётся из пароля выбранной связки (по умолчанию\n"
            "или per-host).\n"
            "Команды установки пакетов на удалённых хостах выполняются через sudo."
        )
        creds_form.addRow(self._ssh_use_sudo_cb)
        layout.addWidget(creds_box)

        self._ssh_widget = SSHKeyTableWidget(self)
        layout.addWidget(self._ssh_widget, 1)

    # --- контракт SetupPage ---

    def on_enter(self, ctx) -> None:
        w = self._ssh_widget
        if w is None:
            return
        if hasattr(w, "check_key_status"):
            try:
                w.check_key_status()
            except Exception:
                pass
        if hasattr(w, "reload_keys_table"):
            try:
                w.reload_keys_table()
            except Exception:
                pass

    def collect(self, ctx) -> None:
        ctx.ssh.bundle_iid = self.bundle_iid()
        ctx.ssh.use_sudo = self.sudo_checked()
        ctx.ssh.private_key_path = self.selected_private_key_path()
        self._key_error = ""
        try:
            ctx.ssh.pubkey = self.prepare_ssh_public_key()
        except Exception as error:
            ctx.ssh.pubkey = ""
            self._key_error = str(error)

    def validate(self, ctx) -> tuple:
        if ctx.services.get("ssh", True) and not ctx.ssh.pubkey:
            return False, getattr(self, "_key_error", "") or "Нет доступного закрытого SSH-ключа."
        return True, ""

    def summary_html(self, ctx) -> str:
        services = getattr(ctx, "services", None) or {}
        if not services.get("ssh", True):
            return "SSH: не выбран"
        if self._ssh_widget is not None:
            if self.key_pair_exists():
                return "SSH: ключи сгенерированы"
            return "SSH: ключи отсутствуют"
        return "SSH: не настроен"

    # --- хелперы (логика старого монолита) ---

    def prepare_ssh_public_key(self) -> str:
        """Use an existing private key; derive only its missing public companion."""
        from src.workers.command.executor_base import load_private_key

        private = self.selected_private_key_path()
        if not private or not os.path.isfile(private):
            raise ValueError("Закрытый SSH-ключ отсутствует. Добавьте существующий ключ перед продолжением.")
        key = load_private_key(private)
        if key is None:
            raise ValueError("Не удалось прочитать закрытый SSH-ключ.")
        public = private + ".pub"
        derived = f"{key.get_name()} {key.get_base64()}"
        if not os.path.lexists(public):
            try:
                with open(public, "x", encoding="utf-8") as stream:
                    stream.write(derived + "\n")
            except FileExistsError:
                pass
        with open(public, encoding="utf-8") as stream:
            value = stream.read().strip()
        if " ".join(value.split()[:2]) != derived:
            raise ValueError("Открытый SSH-ключ не соответствует закрытому. Исправьте пару ключей.")
        return value

    def selected_private_key_path(self):
        from src.workers.command.executor_base import get_default_key_path

        row = self._ssh_widget._keys_table.currentRow()
        if row >= 0:
            keys = self._ssh_widget.scan_ssh_keys()
            if row < len(keys):
                return keys[row]["priv_path"]
        return get_default_key_path()

    def sudo_checked(self) -> bool:
        if self._ssh_use_sudo_cb is None:
            return False
        return bool(self._ssh_use_sudo_cb.isChecked())

    def bundle_iid(self) -> str:
        if self._ssh_bundle_selector is None:
            return ""
        try:
            return self._ssh_bundle_selector.current_iid() or ""
        except Exception:
            return ""

    def key_pair_exists(self) -> bool:
        w = self._ssh_widget
        if w is None:
            return False
        try:
            private = self.selected_private_key_path()
            return w.check_key_pair_match(private, private + ".pub")
        except Exception:
            return False

    def sudo_warning(self) -> str | None:
        """Текст предупреждения для shell: sudo включен, а пароля в связке нет."""
        if not self.sudo_checked():
            return None
        default_iid = self.bundle_iid()
        has_pwd = False
        if default_iid:
            try:
                svc = self._ssh_bundle_selector._get_service()
                if svc is not None:
                    b = svc.get_bundle_by_iid(default_iid)
                    if b is not None:
                        has_pwd = bool(b.password)
            except Exception:
                pass
        if has_pwd:
            return None
        return (
            "Включено использование sudo, но в выбранной связке "
            "нет пароля.\n\n"
            "Установка пакетов на хостах с sudo потребует пароль. "
            "Без пароля пакеты не смогут установиться."
        )
