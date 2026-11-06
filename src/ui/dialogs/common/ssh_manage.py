"""Управление SSH-ключами: генерация, импорт/экспорт, проверка соответствия пар ключей.

Экспортирует:
- SSHKeyTableWidget — виджет таблицы ключей (~/.ssh) с кнопками
  создать/удалить/экспорт/импорт/извлечь public/просмотр known_hosts.
  Без деплоя на хосты — используется как в мастере настройки, так и в диалоге меню.
- SSHManageDialog — обёртка-диалог над SSHKeyTableWidget для пункта меню «Удалённое управление».
"""

from PySide6.QtWidgets import (
    QDialog,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QMessageBox,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QComboBox,
    QDialogButtonBox,
    QFileDialog,
    QAbstractItemView,
    QFormLayout,
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon
import os
import base64
import struct
import shutil
import zipfile
import io
from src.config import ICONS
from src.ui.icons import _icon_from_key, qicon_from_path  # zip-темы: иконки из памяти
from src.logger import logger
from src.utils.filesystem.fs_utils import ensure_user_owned
from src.ui.dialogs.tools.known_hosts import KnownHostsDialog
from src.workers.command.executor_base import (
    load_private_key,
    get_default_key_path,
    _detect_key_type,
    _KEY_TYPE_NAMES,
)

_SSH_DIR = os.path.join(os.path.expanduser("~"), ".ssh")


class SSHKeyTableWidget(QWidget):
    """Виджет таблицы SSH-ключей без установки на хосты.

    Содержит метку статуса ключа, таблицу ключей (~/.ssh) и кнопки
    управления (создать/удалить/экспорт/импорт/извлечь public/просмотр known_hosts).
    Используется в мастере настройки и в диалоге меню «Управление SSH ключами».
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()
        self.check_key_status()
        self.reload_keys_table()


    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Таблица ключей
        self._keys_table = QTableWidget(0, 5)
        self._keys_table.setHorizontalHeaderLabels(
            ["Имя", "Тип", "Закрытый", "Открытый", "По умолчанию"]
        )
        self._keys_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._keys_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self._keys_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self._keys_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self._keys_table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )
        self._keys_table.verticalHeader().setVisible(False)
        self._keys_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._keys_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._keys_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._keys_table.setIconSize(QSize(16, 16))
        layout.addWidget(self._keys_table, 1)

        # Кнопки
        btn_row = QHBoxLayout()
        self._create_btn = QPushButton("Создать пару")
        self._create_btn.clicked.connect(self._on_create_key_pair)
        btn_row.addWidget(self._create_btn)

        self._delete_btn = QPushButton("Удалить")
        self._delete_btn.clicked.connect(self._on_delete_key_pair)
        btn_row.addWidget(self._delete_btn)

        self._export_btn = QPushButton("Экспорт")
        self._export_btn.clicked.connect(self._on_export_key_pair)
        btn_row.addWidget(self._export_btn)

        self._import_btn = QPushButton("Импорт")
        self._import_btn.clicked.connect(self._on_import_key_pair)
        btn_row.addWidget(self._import_btn)

        self._extract_btn = QPushButton("Извлечь public")
        self._extract_btn.setToolTip(
            "Извлечь открытый ключ из закрытого для выбранной строки"
        )
        self._extract_btn.clicked.connect(self._on_extract_public)
        btn_row.addWidget(self._extract_btn)

        self._known_hosts_btn = QPushButton("Просмотр known_hosts")
        self._known_hosts_btn.setToolTip("Открыть диалог просмотра known_hosts")
        self._known_hosts_btn.clicked.connect(self._on_show_known_hosts)
        btn_row.addWidget(self._known_hosts_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)

    # --- Статус ключа ---

    def check_key_status(self):
        """Запомнить пути ключей по умолчанию."""
        privkey_path = get_default_key_path()
        pubkey_basename = os.path.basename(privkey_path) + ".pub"
        self.pubkey_path = os.path.join(os.path.dirname(privkey_path), pubkey_basename)
        self.privkey_path = privkey_path

    def check_key_pair_match(self, privkey_path, pubkey_path):
        if not os.path.exists(privkey_path) or not os.path.exists(pubkey_path):
            return False
        try:
            key = load_private_key(privkey_path)
            if key is None:
                return False
            derived_pub = f"{key.get_name()} {key.get_base64()} {key.get_name()}"
            with open(pubkey_path, "r", encoding="utf-8") as f:
                pubkey = f.read().strip()
            derived_key = " ".join(derived_pub.split()[:2])
            pubkey_key = " ".join(pubkey.split()[:2])
            return derived_key == pubkey_key
        except Exception as e:
            logger.debug("ssh_manage keys_match: %s", e)
            return False

    # --- Таблица ключей ---

    def scan_ssh_keys(self):
        """Сканировать ~/.ssh и вернуть список найденных пар ключей."""
        keys = []
        try:
            entries = os.listdir(_SSH_DIR)
        except OSError:
            return keys
        default_priv = os.path.normpath(get_default_key_path())
        seen = set()
        for name in sorted(entries):
            if name.endswith(".pub"):
                name = name[:-4]
            type_name = _detect_key_type(name)
            if type_name is None:
                continue
            pub_name = name + ".pub"
            if name in seen:
                continue
            seen.add(name)
            priv_path = os.path.join(_SSH_DIR, name)
            pub_path = os.path.join(_SSH_DIR, pub_name)
            has_priv = os.path.isfile(priv_path)
            has_pub = os.path.isfile(pub_path)
            pair_match = False
            if has_priv and has_pub:
                pair_match = self.check_key_pair_match(priv_path, pub_path)
            keys.append(
                {
                    "name": name,
                    "type": type_name,
                    "priv_path": priv_path,
                    "pub_path": pub_path,
                    "has_priv": has_priv,
                    "has_pub": has_pub,
                    "pair_match": pair_match,
                    "is_default": has_priv
                    and os.path.normpath(priv_path) == default_priv,
                }
            )
        return keys

    def reload_keys_table(self):
        """Перезаполнить таблицу ключей."""
        table = self._keys_table
        table.setRowCount(0)
        icon_ok = _icon_from_key("result_success")
        icon_err = _icon_from_key("result_failure")
        for k in self.scan_ssh_keys():
            row = table.rowCount()
            table.insertRow(row)
            name_item = QTableWidgetItem(k["name"])
            name_item.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
            )
            table.setItem(row, 0, name_item)

            type_item = QTableWidgetItem(k["type"])
            type_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row, 1, type_item)

            priv_item = QTableWidgetItem()
            priv_item.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
            )
            if k["has_priv"]:
                priv_item.setIcon(icon_ok)
                priv_item.setText("есть")
            else:
                priv_item.setIcon(icon_err)
                priv_item.setText("нет")
            table.setItem(row, 2, priv_item)

            pub_item = QTableWidgetItem()
            pub_item.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
            )
            if k["has_pub"] and k["has_priv"]:
                pub_item.setIcon(icon_ok if k["pair_match"] else icon_err)
                pub_item.setText("совпадает" if k["pair_match"] else "НЕ совпадает")
            elif k["has_pub"]:
                pub_item.setIcon(icon_ok)
                pub_item.setText("есть")
            else:
                pub_item.setIcon(icon_err)
                pub_item.setText("нет")
            table.setItem(row, 3, pub_item)

            default_item = QTableWidgetItem()
            default_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if k["is_default"]:
                default_item.setText("✔")
            table.setItem(row, 4, default_item)

    def _get_selected_key(self):
        """Вернуть словарь с данными выбранной в таблице строки или None."""
        row = self._keys_table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "Управление ключами", "Выберите ключ в таблице."
            )
            return None
        keys = self.scan_ssh_keys()
        if row < len(keys):
            return keys[row]
        return None

    # --- Кнопки ---

    def _on_create_key_pair(self):
        sub = QDialog(self)
        sub.setWindowTitle("Создание пары ключей")
        sub_layout = QVBoxLayout(sub)
        sub_layout.setContentsMargins(10, 10, 10, 10)
        sub_layout.setSpacing(8)

        form = QFormLayout()
        type_combo = QComboBox()
        type_combo.addItem("Ed25519 (рекомендуется)", "ed25519")
        type_combo.addItem("RSA 4096", "rsa")
        type_combo.addItem("ECDSA", "ecdsa")
        form.addRow("Тип ключа:", type_combo)
        sub_layout.addLayout(form)

        btn_row = QHBoxLayout()
        gen_btn = QPushButton("Сгенерировать")
        cancel_btn = QPushButton("Отмена")
        btn_row.addStretch()
        btn_row.addWidget(gen_btn)
        btn_row.addWidget(cancel_btn)
        sub_layout.addLayout(btn_row)

        def _do_generate():
            self.generate_key(key_type=type_combo.currentData())
            self.reload_keys_table()
            sub.accept()

        gen_btn.clicked.connect(_do_generate)
        cancel_btn.clicked.connect(sub.reject)
        sub.exec()

    def _on_delete_key_pair(self):
        sel = self._get_selected_key()
        if not sel:
            return
        if not (sel["has_priv"] or sel["has_pub"]):
            QMessageBox.information(self, "Удаление ключа", "Нет файлов для удаления.")
            return
        warn = f"Удалить ключ '{sel['name']}'?"
        if sel["is_default"]:
            warn += (
                "\n\nЭто ключ, используемый приложением по умолчанию. "
                "После удаления SSH-подключения перестанут работать "
                "до создания нового ключа."
            )
        reply = QMessageBox.question(
            self,
            "Удаление ключа",
            warn,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for path in (sel["priv_path"], sel["pub_path"]):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError as e:
                    QMessageBox.critical(
                        self, "Ошибка", f"Не удалось удалить {path}:\n{e}"
                    )
                    return
        self.reload_keys_table()
        self.check_key_status()

    def _on_export_key_pair(self):
        sel = self._get_selected_key()
        if not sel:
            return
        if not (sel["has_priv"] or sel["has_pub"]):
            QMessageBox.information(self, "Экспорт", "Нет файлов ключей для экспорта.")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Экспорт пары ключей",
            f"{sel['name']}_keys.zip",
            "ZIP-архивы (*.zip);;Все файлы (*)",
        )
        if not file_path:
            return
        try:
            with zipfile.ZipFile(file_path, "w", zipfile.ZIP_DEFLATED) as zf:
                if sel["has_priv"]:
                    zf.write(
                        sel["priv_path"], arcname=os.path.basename(sel["priv_path"])
                    )
                if sel["has_pub"]:
                    zf.write(sel["pub_path"], arcname=os.path.basename(sel["pub_path"]))
        except Exception as e:
            QMessageBox.critical(self, "Экспорт", f"Ошибка экспорта ключей:\n{e}")
            return
        QMessageBox.information(
            self, "Экспорт", f"Ключ '{sel['name']}' экспортирован в:\n{file_path}"
        )

    def _on_import_key_pair(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Импорт ключа", "", "Все файлы (*)"
        )
        if not file_path:
            return
        try:
            if not os.path.exists(_SSH_DIR):
                os.makedirs(_SSH_DIR, mode=0o700)
        except OSError as e:
            QMessageBox.critical(
                self, "Импорт", f"Не удалось создать каталог ~/.ssh:\n{e}"
            )
            return

        key_base = None

        # Импорт ZIP-архива
        if zipfile.is_zipfile(file_path):
            try:
                with zipfile.ZipFile(file_path, "r") as zf:
                    names = [n for n in zf.namelist() if not n.endswith("/")]
                    priv_entries = [
                        n
                        for n in names
                        if not n.endswith(".pub") and n in _KEY_TYPE_NAMES
                    ]
                    pub_entries = [n for n in names if n.endswith(".pub")]
                    if not priv_entries and not pub_entries:
                        QMessageBox.warning(
                            self,
                            "Импорт",
                            "В архиве не найдено файлов известных SSH-ключей "
                            "(id_ed25519, id_rsa, id_ecdsa).",
                        )
                        return
                    for entry in priv_entries + pub_entries:
                        target = os.path.join(_SSH_DIR, os.path.basename(entry))
                        with zf.open(entry) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        ensure_user_owned(target)
                    key_base = (
                        os.path.basename(priv_entries[0])
                        if priv_entries
                        else os.path.basename(pub_entries[0])[:-4]
                    )
            except Exception as e:
                QMessageBox.critical(self, "Импорт", f"Ошибка чтения архива:\n{e}")
                return
        else:
            # Импорт одиночного файла
            filename = os.path.basename(file_path)
            if filename.endswith(".pub"):
                base = filename[:-4]
                if base not in _KEY_TYPE_NAMES:
                    base, ok = self._prompt_key_name(filename)
                    if not ok:
                        return
                target_name = base + ".pub"
                key_base = base
            else:
                key_base = filename if filename in _KEY_TYPE_NAMES else None
                if key_base is None:
                    detected = _detect_key_type(file_path)
                    if detected is None:
                        key_base, ok = self._prompt_key_name(filename)
                        if not ok:
                            return
                    else:
                        key_base, ok = self._prompt_key_name(
                            filename, default=os.path.basename(file_path)
                        )
                        if not ok:
                            return
                target_name = key_base

            target = os.path.join(_SSH_DIR, target_name)
            try:
                shutil.copy2(file_path, target)
                ensure_user_owned(target)
            except OSError as e:
                QMessageBox.critical(
                    self, "Импорт", f"Не удалось скопировать ключ:\n{e}"
                )
                return

            # Если импортирован закрытый ключ без открытого — вывести .pub
            if not target_name.endswith(".pub"):
                pub_path = target + ".pub"
                if not os.path.exists(pub_path):
                    try:
                        pub_key_str = self._derive_public_key_from_private(target)
                        with open(pub_path, "w", encoding="utf-8") as f:
                            f.write(pub_key_str)
                        ensure_user_owned(pub_path)
                    except Exception as e:
                        logger.warning(
                            "Импорт: не удалось вывести открытый ключ: %s", e
                        )

        self.reload_keys_table()
        self.check_key_status()
        if key_base:
            QMessageBox.information(
                self, "Импорт", f"Ключ «{key_base}» импортирован в ~/.ssh."
            )

    def _prompt_key_name(self, filename, default="imported"):
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(
            self,
            "Имя ключа",
            f"Не удалось определить тип ключа «{filename}».\n"
            "Введите базовое имя (например, id_ed25519):",
            text=default,
        )
        if not ok or not text.strip():
            return None, False
        return text.strip(), True

    def _on_extract_public(self):
        sel = self._get_selected_key()
        if not sel:
            return
        if not sel["has_priv"]:
            QMessageBox.information(
                self, "Извлечение public", "У выбранного ключа нет закрытого файла."
            )
            return
        try:
            pub_key_str = self._derive_public_key_from_private(sel["priv_path"])
            with open(sel["pub_path"], "w", encoding="utf-8") as f:
                f.write(pub_key_str)
            ensure_user_owned(sel["pub_path"])
        except Exception as e:
            QMessageBox.critical(
                self, "Извлечение public", f"Не удалось извлечь открытый ключ:\n{e}"
            )
            return
        self.reload_keys_table()
        self.check_key_status()
        QMessageBox.information(
            self, "Извлечение public", f"Открытый ключ извлечён в:\n{sel['pub_path']}"
        )

    def _on_show_known_hosts(self):
        """Открыть диалог просмотра ~/.ssh/known_hosts."""
        known_hosts_path = os.path.expanduser("~/.ssh/known_hosts")
        dlg = KnownHostsDialog(known_hosts_path, self)
        dlg.exec()

    # --- Генерация ---

    def generate_key(self, key_type: str = "ed25519"):
        import paramiko

        ssh_dir = os.path.dirname(self.privkey_path) or os.path.expanduser("~/.ssh")
        key_names = {"ed25519": "id_ed25519", "rsa": "id_rsa", "ecdsa": "id_ecdsa"}
        priv_name = key_names.get(key_type, "id_ed25519")
        created = []
        try:
            os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
            if key_type == "ed25519":
                from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                    Ed25519PrivateKey,
                )
                from cryptography.hazmat.primitives.serialization import (
                    Encoding,
                    PublicFormat,
                    NoEncryption,
                    PrivateFormat,
                )

                priv = Ed25519PrivateKey.generate()
                priv_bytes = priv.private_bytes(
                    encoding=Encoding.PEM,
                    format=PrivateFormat.OpenSSH,
                    encryption_algorithm=NoEncryption(),
                )
                pub_bytes = priv.public_key().public_bytes(
                    Encoding.Raw, PublicFormat.Raw
                )
                type_name = b"ssh-ed25519"
                blob = (
                    struct.pack(">I", len(type_name))
                    + type_name
                    + struct.pack(">I", len(pub_bytes))
                    + pub_bytes
                )
                pub_key_str = (
                    f"ssh-ed25519 "
                    f"{base64.b64encode(blob).decode()} "
                    f"dno-tool-generated-key\n"
                )
            elif key_type == "ecdsa":
                key = paramiko.ECDSAKey.generate()
                buffer = io.StringIO()
                key.write_private_key(buffer)
                priv_bytes = buffer.getvalue().encode("utf-8")
                pub_key_str = (
                    f"{key.get_name()} {key.get_base64()} dno-tool-generated-key\n"
                )
            else:
                key = paramiko.RSAKey.generate(4096)
                buffer = io.StringIO()
                key.write_private_key(buffer)
                priv_bytes = buffer.getvalue().encode("utf-8")
                pub_key_str = f"ssh-rsa {key.get_base64()} dno-tool-generated-key\n"

            # Никогда не заменяем существующую пару. O_EXCL защищает также
            # от появления файла между проверкой пути и его открытием.
            suffix = 0
            while True:
                name = priv_name if suffix == 0 else f"{priv_name}.dno_{suffix}"
                private_path = os.path.join(ssh_dir, name)
                public_path = private_path + ".pub"
                if os.path.lexists(private_path) or os.path.lexists(public_path):
                    suffix += 1
                    continue
                try:
                    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    suffix += 1
                    continue
                created.append(private_path)
                with os.fdopen(fd, "wb") as f:
                    f.write(priv_bytes)
                    f.flush()
                    os.fsync(f.fileno())
                fd = os.open(public_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                created.append(public_path)
                with os.fdopen(fd, "wb") as f:
                    f.write(pub_key_str.encode("utf-8"))
                    f.flush()
                    os.fsync(f.fileno())
                break
            ensure_user_owned(private_path)
            ensure_user_owned(public_path)
            self.privkey_path = private_path
            self.pubkey_path = public_path
            type_label = {
                "ed25519": "Ed25519",
                "ecdsa": "ECDSA",
                "rsa": "RSA 4096",
            }.get(key_type, key_type.upper())
            QMessageBox.information(
                self, "SSH ключ", f"{type_label} ключи созданы:\n{private_path}\n{public_path}"
            )
        except Exception as e:
            for path in reversed(created):
                try:
                    os.unlink(path)
                except OSError:
                    logger.error("Не удалось удалить неполную SSH-пару: %s", path)
            QMessageBox.critical(self, "Ошибка", f"Ошибка генерации ключей: {e}")
        self.check_key_status()
        self.reload_keys_table()

        table = getattr(self, "_keys_table", None)
        if created and table is not None:
            for row in range(table.rowCount()):
                if table.item(row, 0).text() == os.path.basename(private_path):
                    table.selectRow(row)
                    break

    def _derive_public_key_from_private(self, privkey_path):
        key = load_private_key(privkey_path)
        if key is None:
            raise ValueError(f"Не удалось загрузить закрытый ключ: {privkey_path}")
        return f"{key.get_name()} {key.get_base64()} dno-tool-generated-key\n"

    def key_pair_exists(self) -> bool:
        return os.path.exists(getattr(self, "privkey_path", "")) and os.path.exists(
            getattr(self, "pubkey_path", "")
        )


class SSHManageDialog(QDialog):
    """Диалог управления SSH-ключами — обёртка над SSHKeyTableWidget.

    Показывает таблицу ключей (~/.ssh) с кнопками создать/удалить/экспорт/
    импорт/извлечь public/просмотр known_hosts. Без деплоя на хосты —
    деплой ключей выполняется только в мастере настройки.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Управление SSH ключами")
        self.resize(540, 400)

        layout = QVBoxLayout(self)
        self.widget = SSHKeyTableWidget(self)
        layout.addWidget(self.widget)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
