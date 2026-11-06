"""Диалог управления нодами (федерация инстансов dno-tool).

Список нод: таблица [Имя, Хост, Порт, Отпечаток ключа] с inline-CRUD.
Секция «Эта нода»: ключ доступа (показ, копирование), отпечаток, порт
прослушивания, флаг «Принимать подключения», перегенерация ключа доступа.
Доступ односторонний: чтобы подключить ноду, достаточно её ключа доступа.

Служебное меню (Ctrl+Alt+Shift): очистка/блокировка/разблокировка выбранной
ноды, загрузка служебного мастер-ключа и снятие блокировки этой копии.
Разблокировка требует мастер-ключа. Ключ загружается из файла или вставкой,
живёт в памяти до выхода и нигде не показывается. Без открытой части ключа
в сборке пункты мастер-ключа скрыты.

Сохранение: через NodeService (config.app.nodes), криптография в
src/utils/security/node_crypto.py и node_master.py.
"""

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QVBoxLayout,
)
from PySide6.QtCore import Qt, QThread, Signal

from src.config import config
from src.logger import logger
from src.services.remote.node_service import NodeService
from src.utils.security.ban import is_banned
from src.utils.security.node_crypto import fingerprint_from_pubkey, parse_access_key

# Файл служебного ключа — одна короткая строка.
_MASTER_FILE_LIMIT = 4096


class NodeCheckWorker(QThread):
    """Проверка связи с нодами: handshake для каждой выбранной ноды.

    Блокирующий HTTP (hello → challenge → verify, до ~15 с на ноду),
    выполняется в отдельном потоке, результат: сигналами в GUI.
    """

    checked = Signal(str, bool, str)  # (node_iid, ok, message)
    all_done = Signal()

    def __init__(self, svc, iids, parent=None):
        super().__init__(parent)
        self._svc = svc
        self._iids = list(iids)

    def run(self) -> None:
        for iid in self._iids:
            if self.isInterruptionRequested():
                break
            try:
                client = self._svc.get_client(iid)
                if client is not None:
                    ok, msg = client.check_connection()
                else:
                    ok, msg, _info = self._svc.start_handshake(iid)
            except Exception as e:
                logger.debug(f"NodeCheckWorker: {iid}: {e}")
                ok, msg = False, f"Ошибка: {e}"
            self.checked.emit(iid, ok, msg)
        self.all_done.emit()

    def stop(self) -> None:
        """Прервать проверку перед следующим сетевым обращением."""
        self.requestInterruption()


class _NodeServiceWorker(QThread):
    """Служебная команда выбранной ноде по HTTP (очистка/блокировка)."""

    done_signal = Signal(bool, str)

    def __init__(self, svc, iid: str, action: str, parent=None):
        super().__init__(parent)
        self._svc = svc
        self._iid = iid
        self._action = action

    def run(self) -> None:
        method = {
            "wipe": self._svc.wipe_remote_node,
            "block": self._svc.block_remote_node,
            "unblock": self._svc.unblock_remote_node,
        }[self._action]
        try:
            ok, message = method(self._iid)
        except Exception as error:
            ok, message = False, f"Ошибка: {type(error).__name__}"
        self.done_signal.emit(ok, message)


class _MasterProbeWorker(QThread):
    """Первое подключение служебным ключом: получить и проверить ключ TLS."""

    done_signal = Signal(bool, str, str)  # (ok, message, pubkey)

    def __init__(self, svc, host: str, port: int, parent=None):
        super().__init__(parent)
        self._svc = svc
        self._host = host
        self._port = port

    def run(self) -> None:
        try:
            ok, message, pubkey = self._svc.probe_master_node(self._host, self._port)
        except Exception as error:
            ok, message, pubkey = False, f"Ошибка: {type(error).__name__}", ""
        self.done_signal.emit(ok, message, pubkey)


class NodeSettingsDialog(QDialog):
    """Диалог управления нодами (федерация)."""


    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Ноды (федерация)")
        self.setMinimumWidth(760)
        self.setMinimumHeight(520)
        self._svc = self._get_service()
        self._check_worker = None
        self._wipe_worker = None
        self._probe_worker = None
        self._pending_add = None
        self._server_pending = False
        self._setup_ui()
        self._load_nodes()
        self._load_own_info()

    def done(self, result: int) -> None:
        """Закрыть диалог, передав незавершённые worker-ы сервису."""
        for attr in ("_check_worker", "_probe_worker"):
            worker = getattr(self, attr)
            if worker is not None:
                setattr(self, attr, None)
                try:
                    worker.done_signal.disconnect()
                except (AttributeError, RuntimeError, TypeError):
                    pass
                self._svc.retire_background_worker(worker)
        super().done(result)

    # ========== сервис ==========

    def _get_service(self):
        try:
            from src.di import get_container

            return get_container().resolve(NodeService)
        except Exception as e:
            logger.error(f"NodeSettingsDialog: NodeService недоступен: {e}")
            return None

    # ========== UI ==========

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        hint = QLabel(
            "Ноды - другие копии программы, объединённые в единую сеть: "
            "хосты каждой ноды видны в общем дереве, со статусами, "
            "миниатюрами и возможностью выполнять команды. Чтобы подключить "
            "ноду, получите у её оператора ключ доступа и укажите адрес и порт; "
            "свой ключ передавать не нужно. Владелец ключа доступа получает "
            "полное управление нодой; новый ключ отзывает доступ у всех. "
            "Node API использует HTTPS с проверкой ключа."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)

        self._ban_label = QLabel(
            "Эта копия программы заблокирована после очистки ноды: выполнение "
            "команд и подключение к нодам запрещены. Блокировка снимается только "
            "служебным ключом."
        )
        self._ban_label.setWordWrap(True)
        self._ban_label.setStyleSheet("color: white; background-color: #b00020; padding: 4px;")
        self._ban_label.setVisible(is_banned())
        layout.addWidget(self._ban_label)

        # --- Секция «Эта нода» ---
        own = QLabel("<b>Эта нода</b>")
        layout.addWidget(own)

        own_grid = QHBoxLayout()
        own_grid.addWidget(QLabel("Ключ доступа:"))
        self._access_key_edit = QLineEdit()
        self._access_key_edit.setReadOnly(True)
        self._access_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._access_key_edit.setPlaceholderText("<ключ отсутствует>")
        self._access_key_edit.setToolTip(
            "Передайте ключ операторам, которым нужен доступ к этой ноде. "
            "Кто знает ключ, тот управляет нодой."
        )
        own_grid.addWidget(self._access_key_edit, 1)
        self._show_key_btn = QPushButton("Показать")
        self._show_key_btn.setCheckable(True)
        self._show_key_btn.toggled.connect(self._on_toggle_key)
        own_grid.addWidget(self._show_key_btn)
        self._copy_btn = QPushButton("Копировать")
        self._copy_btn.clicked.connect(self._on_copy_access_key)
        own_grid.addWidget(self._copy_btn)
        layout.addLayout(own_grid)

        own_grid2 = QHBoxLayout()
        own_grid2.addWidget(QLabel("Отпечаток ключа:"))
        self._fp_label = QLabel("")
        self._fp_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        own_grid2.addWidget(self._fp_label, 1)

        own_grid2.addWidget(QLabel("Порт прослушивания:"))
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(8801)
        own_grid2.addWidget(self._port_spin)

        self._listen_cb = QCheckBox("Принимать подключения других нод")
        own_grid2.addWidget(self._listen_cb)
        layout.addLayout(own_grid2)

        own_grid3 = QHBoxLayout()
        self._regen_btn = QPushButton("Новый ключ доступа…")
        self._regen_btn.setToolTip(
            "Создать новый ключ доступа: все, кому был передан старый, "
            "потеряют доступ к этой ноде"
        )
        self._regen_btn.clicked.connect(self._on_regenerate)
        self._apply_listen_btn = QPushButton("Применить настройки сервера")
        self._apply_listen_btn.clicked.connect(self._on_apply_listen)
        own_grid3.addStretch(1)
        own_grid3.addWidget(self._apply_listen_btn)
        own_grid3.addWidget(self._regen_btn)
        layout.addLayout(own_grid3)

        # --- Таблица нод ---
        peers = QLabel("<b>Подключённые ноды</b>")
        layout.addWidget(peers)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            [
                "Имя",
                "Хост",
                "Порт",
                "Отпечаток ключа",
                "Отображение",
                "Статус",
            ]
        )
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self._table, 1)

        btn_layout = QHBoxLayout()
        self._add_btn = QPushButton("Добавить…")
        self._add_btn.clicked.connect(self._on_add)
        self._del_btn = QPushButton("Удалить")
        self._del_btn.clicked.connect(self._on_delete)
        self._change_key_btn = QPushButton("Сменить ключ…")
        self._change_key_btn.setToolTip(
            "Указать новый ключ доступа выбранной ноды (после его смены на ноде)"
        )
        self._change_key_btn.clicked.connect(self._on_change_key)
        self._check_btn = QPushButton("Проверить связь")
        self._check_btn.setToolTip(
            "Проверить связь с выбранными нодами (или со всеми, "
            "если ничего не выделено) и показать результат"
        )
        self._check_btn.clicked.connect(self._on_check)
        self._check_status = QLabel("")
        btn_layout.addWidget(self._check_btn)
        btn_layout.addWidget(self._check_status, 1)
        btn_layout.addStretch(0)
        btn_layout.addWidget(self._add_btn)
        btn_layout.addWidget(self._change_key_btn)
        btn_layout.addWidget(self._del_btn)
        # Служебное меню скрыто, пока не нажато Ctrl+Alt+Shift.
        self._service_btn = QPushButton("Служебное")
        self._service_btn.setStyleSheet("color: white; background-color: #b00020;")
        service_menu = QMenu(self._service_btn)
        service_menu.aboutToShow.connect(lambda: self._fill_service_menu(service_menu))
        self._service_btn.setMenu(service_menu)
        self._service_btn.setVisible(False)
        btn_layout.addWidget(self._service_btn)
        layout.addLayout(btn_layout)

        # --- Кнопки Сохранить/Закрыть ---
        bottom = QHBoxLayout()
        self._save_btn = QPushButton("Сохранить")
        self._save_btn.clicked.connect(self._on_save)
        self._close_btn = QPushButton("Закрыть")
        self._close_btn.clicked.connect(self.accept)
        bottom.addStretch(1)
        bottom.addWidget(self._save_btn)
        bottom.addWidget(self._close_btn)
        layout.addLayout(bottom)

    # ========== загрузка ==========

    def _load_own_info(self) -> None:
        if self._svc is None:
            return
        try:
            self._access_key_edit.setText(self._svc.get_access_key())
            self._fp_label.setText(self._svc.get_fingerprint() or "")
        except (ValueError, OSError) as error:
            self._access_key_edit.clear()
            self._fp_label.setText(str(error))
        self._port_spin.setValue(self._svc.get_listen_port())
        self._listen_cb.setChecked(self._svc.get_listen_enabled())

    def _load_nodes(self) -> None:
        self._table.setRowCount(0)
        if self._svc is None:
            return
        for rec in self._svc.get_nodes():
            self._add_row(rec)

    def _add_row(self, rec=None) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        name = getattr(rec, "name", "") if rec else ""
        host = getattr(rec, "host", "") if rec else ""
        port = getattr(rec, "port", 8801) if rec else 8801
        pubkey = getattr(rec, "pubkey", "") if rec else ""
        iid = getattr(rec, "iid", "") if rec else ""
        # «Отображение» = autoconnect (видимость/опрос у себя).
        visible = bool(getattr(rec, "autoconnect", True)) if rec else True

        name_item = QTableWidgetItem(name)
        name_item.setData(Qt.ItemDataRole.UserRole, iid)  # iid ноды

        host_item = QTableWidgetItem(host)
        port_item = QTableWidgetItem(str(port))
        port_item.setTextAlignment(Qt.AlignCenter)
        fp_item = QTableWidgetItem(fingerprint_from_pubkey(pubkey) if pubkey else "")
        fp_item.setFlags(fp_item.flags() & ~Qt.ItemIsEditable)

        # Колонка «Отображение»: чекбокс (скрытые ноды: unchecked; галка
        # вернёт ноду)
        enable_item = QTableWidgetItem()
        enable_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        enable_item.setCheckState(
            Qt.CheckState.Checked if visible else Qt.CheckState.Unchecked
        )
        enable_item.setTextAlignment(Qt.AlignCenter)
        enable_item.setToolTip(
            "Показывать ноду у себя: секция в дереве и опрос статусов. "
            "Снято: секция скрыта, опрос остановлен, запись и ключ сохраняются. "
            "Возвращает ноду, скрытую из контекстного меню дерева."
        )

        self._table.setItem(row, 0, name_item)
        self._table.setItem(row, 1, host_item)
        self._table.setItem(row, 2, port_item)
        self._table.setItem(row, 3, fp_item)
        self._table.setItem(row, 4, enable_item)
        check_item = QTableWidgetItem("—")
        check_item.setFlags(check_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        check_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        check_item.setToolTip(self._diagnostic_text(iid))
        self._table.setItem(row, 5, check_item)

    # ========== CRUD ==========

    def _on_add(self) -> None:
        """Добавить ноду (диалог ввода хоста/порта/ключа)."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Добавить ноду")
        form = QVBoxLayout(dlg)

        form.addWidget(QLabel("Имя ноды:"))
        name_edit = QLineEdit()
        form.addWidget(name_edit)

        form.addWidget(QLabel("Хост (IP или hostname):"))
        host_edit = QLineEdit()
        host_edit.setPlaceholderText("например, 192.168.1.10")
        form.addWidget(host_edit)

        form.addWidget(QLabel("Порт:"))
        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setValue(8801)
        form.addWidget(port_spin)

        form.addWidget(QLabel("Ключ доступа ноды:"))
        key_edit = QLineEdit()
        key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText("вставьте ключ доступа из настроек той ноды")
        form.addWidget(key_edit)
        fp_hint = QLabel("")
        fp_hint.setWordWrap(True)
        fp_hint.setStyleSheet("color: gray;")
        form.addWidget(fp_hint)

        def _upd_fp():
            text = key_edit.text().strip()
            if not text:
                fp_hint.setText("")
                return
            try:
                pubkey, _secret = parse_access_key(text)
            except ValueError as error:
                fp_hint.setText(f"{error}. Проверьте вставку")
                return
            fp = fingerprint_from_pubkey(pubkey)
            fp_hint.setText(f"Отпечаток ключа: {fp}. Сверьте его с оператором ноды")

        key_edit.textChanged.connect(_upd_fp)

        btns = QHBoxLayout()
        ok_btn = QPushButton("Добавить")
        cancel_btn = QPushButton("Отмена")
        btns.addStretch(1)
        btns.addWidget(ok_btn)
        btns.addWidget(cancel_btn)
        form.addLayout(btns)
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)

        if dlg.exec() != QDialog.Accepted:
            return
        host = host_edit.text().strip()
        name = name_edit.text().strip() or host
        port = port_spin.value()
        access_key = key_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "Ноды", "Укажите хост ноды.")
            return
        if not access_key and self._svc.has_master_key():
            self._start_master_probe(name, host, port)
            return
        if not access_key:
            QMessageBox.warning(self, "Ноды", "Укажите ключ доступа ноды.")
            return
        ok, msg, rec = self._svc.add_node(name, host, port, access_key)
        if not ok:
            QMessageBox.warning(self, "Ноды", msg)
            return
        self._add_row(rec)
        self._save_silent()

    def _start_master_probe(self, name: str, host: str, port: int) -> None:
        """Добавить ноду без ключа доступа: подключение служебным ключом."""
        if self._probe_worker is not None or not host:
            return
        self._pending_add = (name, host, port)
        self._add_btn.setEnabled(False)
        self._check_status.setText(f"Подключение к {host} …")
        worker = _MasterProbeWorker(self._svc, host, port)
        worker.done_signal.connect(self._on_master_probe_done)
        worker.finished.connect(worker.deleteLater)
        self._probe_worker = worker
        worker.start()

    def _on_master_probe_done(self, ok: bool, message: str, pubkey: str) -> None:
        self._probe_worker = None
        self._add_btn.setEnabled(True)
        pending, self._pending_add = self._pending_add, None
        self._check_status.setText(message)
        if not ok or pending is None:
            QMessageBox.warning(self, "Ноды", f"Нода не добавлена: {message}")
            return
        name, host, port = pending
        ok, msg, rec = self._svc.add_node(name, host, port, pubkey=pubkey)
        if not ok:
            QMessageBox.warning(self, "Ноды", msg)
            return
        self._add_row(rec)
        self._save_silent()

    def _on_change_key(self) -> None:
        """Указать новый ключ доступа выбранной ноды."""
        if self._svc is None:
            return
        rows = self._table.selectionModel().selectedRows()
        if len(rows) != 1:
            QMessageBox.information(self, "Ноды", "Выберите ровно одну ноду.")
            return
        item = self._table.item(rows[0].row(), 0)
        iid = item.data(Qt.ItemDataRole.UserRole) if item else ""
        if not iid:
            return
        text, ok = QInputDialog.getText(
            self,
            "Ключ доступа",
            "Новый ключ доступа ноды:",
            QLineEdit.EchoMode.Password,
        )
        if not ok or not text.strip():
            return
        ok, message = self._svc.update_node(iid, access_key=text.strip())
        if not ok:
            QMessageBox.warning(self, "Ноды", message)
            return
        rec = self._svc.get_node_by_iid(iid)
        fp_item = self._table.item(rows[0].row(), 3)
        if rec is not None and fp_item is not None:
            fp_item.setText(fingerprint_from_pubkey(rec.pubkey))
        self._check_status.setText(message)

    def _on_delete(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        if (
            QMessageBox.question(
                self,
                "Удаление ноды",
                f"Удалить {len(rows)} ноду(ы)? Клиент будет остановлен, секция "
                "исчезнет из дерева.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.No
        ):
            return
        for idx in sorted(rows, key=lambda i: i.row(), reverse=True):
            item = self._table.item(idx.row(), 0)
            iid = item.data(Qt.ItemDataRole.UserRole) if item else ""
            if iid and self._svc is not None:
                ok, message = self._svc.delete_node(iid)
                if not ok:
                    QMessageBox.warning(self, "Ноды", message)
                    continue
            self._table.removeRow(idx.row())

    # ========== служебные действия ==========

    def keyPressEvent(self, event) -> None:
        """Ctrl+Alt+Shift показывает служебное меню."""
        needed = (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.ShiftModifier
        )
        if (event.modifiers() & needed) == needed:
            self._service_btn.setVisible(True)
        super().keyPressEvent(event)

    def _selected_node(self) -> "tuple[str, str]":
        """iid и хост единственной выделенной ноды, иначе ('', '')."""
        rows = self._table.selectionModel().selectedRows()
        if len(rows) != 1:
            return "", ""
        item = self._table.item(rows[0].row(), 0)
        host_item = self._table.item(rows[0].row(), 1)
        return (
            item.data(Qt.ItemDataRole.UserRole) if item else "",
            host_item.text().strip() if host_item else "",
        )

    def _confirm_wipe(self, host: str) -> bool:
        """Компактный диалог очистки: подтверждение вводом адреса ноды."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Очистка ноды")
        layout = QVBoxLayout(dlg)

        warning = QLabel(
            f"На ноде <b>{host}</b> будут НЕОБРАТИМО удалены все ключи, "
            "настройки, хосты, команды и учётные данные, а также весь каталог "
            "~/.ssh (включая authorized_keys), программа на ней завершится. "
            "Копия программы на ноде будет заблокирована, пока блокировка не "
            "снята служебным ключом."
        )
        warning.setWordWrap(True)
        warning.setFixedWidth(420)
        layout.addWidget(warning)

        layout.addWidget(QLabel(f"Для подтверждения введите адрес ноды — <b>{host}</b>:"))
        field = QLineEdit()
        field.setPlaceholderText(host)
        layout.addWidget(field)

        btns = QHBoxLayout()
        ok_btn = QPushButton("Очистить")
        ok_btn.setEnabled(False)
        ok_btn.setStyleSheet("color: white; background-color: #b00020;")
        cancel_btn = QPushButton("Отмена")
        btns.addStretch(1)
        btns.addWidget(ok_btn)
        btns.addWidget(cancel_btn)
        layout.addLayout(btns)

        field.textChanged.connect(lambda text: ok_btn.setEnabled(text.strip() == host))
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)
        return dlg.exec() == QDialog.Accepted

    def _on_remote_action(self, action: str) -> None:
        """Очистить / заблокировать / разблокировать выбранную ноду."""
        if self._svc is None or self._wipe_worker is not None:
            return
        iid, host = self._selected_node()
        if not iid:
            QMessageBox.information(self, "Ноды", "Выберите ровно одну ноду.")
            return
        if action == "wipe":
            if not self._confirm_wipe(host):
                return
            status = f"Очистка ноды {host} …"
        elif action == "block":
            if (
                QMessageBox.question(
                    self,
                    "Блокировка ноды",
                    f"Заблокировать ноду {host}? Данные сохранятся, но выполнять "
                    "команды и подключаться к нодам она не сможет, пока "
                    "блокировка не снята служебным ключом.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                == QMessageBox.StandardButton.No
            ):
                return
            status = f"Блокировка ноды {host} …"
        else:  # unblock
            if not self._svc.has_master_key():
                QMessageBox.warning(
                    self, "Ноды",
                    "Снять блокировку можно только служебным ключом. "
                    "Сначала загрузите его.",
                )
                return
            if (
                QMessageBox.question(
                    self, "Разблокировка ноды",
                    f"Снять блокировку с ноды {host}?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                == QMessageBox.StandardButton.No
            ):
                return
            status = f"Разблокировка ноды {host} …"
        self._service_btn.setEnabled(False)
        self._check_status.setText(status)
        worker = _NodeServiceWorker(self._svc, iid, action)
        worker.done_signal.connect(self._on_remote_action_done)
        worker.finished.connect(worker.deleteLater)
        self._wipe_worker = worker
        worker.start()

    def _on_remote_action_done(self, ok: bool, message: str) -> None:
        self._wipe_worker = None
        self._service_btn.setEnabled(True)
        self._check_status.setText(message)
        if ok:
            QMessageBox.information(self, "Ноды", message)
        else:
            QMessageBox.warning(self, "Ноды", f"Не выполнено: {message}")

    # ========== служебное меню ==========

    def _fill_service_menu(self, menu: QMenu) -> None:
        """Пункты меню по текущему состоянию (сам ключ не показывается)."""
        menu.clear()
        if self._svc is None:
            return
        iid, _host = self._selected_node()
        wipe = menu.addAction("Очистить ноду…", lambda: self._on_remote_action("wipe"))
        block = menu.addAction("Заблокировать ноду…", lambda: self._on_remote_action("block"))
        for action in (wipe, block):
            action.setEnabled(bool(iid))
        if not self._svc.master_key_supported():
            return
        unblock = menu.addAction("Разблокировать ноду…", lambda: self._on_remote_action("unblock"))
        unblock.setEnabled(bool(iid))
        unblock.setToolTip("Требуется служебный ключ")
        menu.addSeparator()
        if self._svc.has_master_key():
            menu.addAction("Забыть служебный ключ", self._on_remove_master)
        else:
            menu.addAction("Загрузить служебный ключ…", self._on_import_master)
        if is_banned():
            menu.addAction("Снять блокировку этой копии…", self._on_lift_ban)

    def _prompt_master_key(self) -> str:
        """Запросить служебный ключ: вставкой в поле или из файла.

        Пустая строка — отмена или отказ чтения файла.
        """
        dlg = QDialog(self)
        dlg.setWindowTitle("Служебный ключ")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Вставьте служебный ключ или выберите файл:"))
        field = QLineEdit()
        field.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(field)

        def _from_file() -> None:
            path, _ = QFileDialog.getOpenFileName(dlg, "Служебный ключ")
            if not path:
                return
            try:
                with open(path, "r", encoding="ascii") as handle:
                    field.setText(handle.read(_MASTER_FILE_LIMIT).strip())
            except (OSError, UnicodeDecodeError):
                QMessageBox.warning(dlg, "Ноды", "Не удалось прочитать файл.")

        btns = QHBoxLayout()
        file_btn = QPushButton("Из файла…")
        file_btn.clicked.connect(_from_file)
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn = QPushButton("Отмена")
        cancel_btn.clicked.connect(dlg.reject)
        btns.addWidget(file_btn)
        btns.addStretch(1)
        btns.addWidget(ok_btn)
        btns.addWidget(cancel_btn)
        layout.addLayout(btns)
        if dlg.exec() != QDialog.Accepted:
            return ""
        return field.text().strip()

    def _on_import_master(self) -> None:
        text = self._prompt_master_key()
        if not text:
            return
        ok, message = self._svc.import_master_key(text)
        if ok:
            QMessageBox.information(self, "Ноды", message)
        else:
            QMessageBox.warning(self, "Ноды", message)

    def _on_remove_master(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Служебный ключ",
                "Забыть загруженный служебный ключ? Ноды, добавленные без "
                "ключа доступа, перестанут подключаться до новой загрузки.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.No
        ):
            return
        self._svc.remove_master_key()

    def _on_lift_ban(self) -> None:
        """Снять блокировку загруженным служебным ключом или из файла."""
        text = "" if self._svc.has_master_key() else self._prompt_master_key()
        if not text and not self._svc.has_master_key():
            return
        ok, message = self._svc.lift_ban(text)
        self._ban_label.setVisible(is_banned())
        if ok:
            QMessageBox.information(self, "Ноды", message)
        else:
            QMessageBox.warning(self, "Ноды", message)

    # ========== проверка связи ==========

    def _on_check(self) -> None:
        """Проверить связь: handshake с выделенными нодами (или со всеми).

        Сетевой I/O: в QThread (до ~15 с на ноду), GUI не блокируется.
        Результат: текст в статус-метке + цвет строки таблицы.
        """
        if self._svc is None:
            QMessageBox.critical(self, "Ноды", "Сервис нод недоступен.")
            return
        if self._check_worker is not None and self._check_worker.isRunning():
            return  # проверка уже идёт
        # Выделенные строки, иначе: все строки таблицы
        rows = [idx.row() for idx in self._table.selectionModel().selectedRows()]
        if not rows:
            rows = list(range(self._table.rowCount()))
        targets = []
        for row in rows:
            item = self._table.item(row, 0)
            if item is None:
                continue
            iid = item.data(Qt.ItemDataRole.UserRole)
            host = self._table.item(row, 1).text() if self._table.item(row, 1) else ""
            if iid:
                targets.append((row, iid, host))
        if not targets:
            self._check_status.setText("Нет нод для проверки")
            return

        self._check_btn.setEnabled(False)
        names = ", ".join(host for _r, _i, host in targets)
        self._check_status.setText(f"Проверка: {names} …")

        self._check_worker = NodeCheckWorker(self._svc, [i for _r, i, _h in targets])
        self._check_worker.checked.connect(self._on_check_result)
        self._check_worker.finished.connect(self._on_check_done)
        self._check_worker.start()

    def _on_check_result(self, iid: str, ok: bool, msg: str) -> None:
        """Показать результат handshake в отдельной ячейке без заливки строки."""
        if self.sender() is not self._check_worker:
            return
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is None or item.data(Qt.ItemDataRole.UserRole) != iid:
                continue
            status = self._table.item(row, 5)
            if status is not None:
                status.setText("✓ Доступна" if ok else "! Ошибка")
                status.setForeground(Qt.GlobalColor.darkGreen if ok else Qt.GlobalColor.darkRed)
                status.setToolTip(msg + "\n" + self._diagnostic_text(iid))
            host = self._table.item(row, 1).text() if self._table.item(row, 1) else iid
            item.setToolTip(f"{host}: {msg}")
            break
        self._check_status.setText(msg)

    def _on_check_done(self) -> None:
        """Все проверки завершены: разблокировать кнопку."""
        if self.sender() is not self._check_worker:
            return
        self._check_btn.setEnabled(True)
        if self._check_worker is not None:
            self._check_worker.deleteLater()
            self._check_worker = None

    def _diagnostic_text(self, iid: str) -> str:
        """Сведения о последнем обмене без токенов и содержимого запросов."""
        from datetime import datetime

        state = self._svc.get_diagnostics(iid) if self._svc else {}
        success = state.get("last_success")
        last = datetime.fromtimestamp(success).strftime("%d.%m %H:%M:%S") if success else "нет"
        return (
            f"Последний успешный обмен: {last}\n"
            f"Этап: {state.get('stage', 'не подключалась')}\n"
            f"Версия: {state.get('version', 'неизвестна')}\n"
            f"Ошибка API: {state.get('error') or 'нет'}"
        )

    # ========== собственная нода ==========

    def _on_toggle_key(self, shown: bool) -> None:
        self._access_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        )
        self._show_key_btn.setText("Скрыть" if shown else "Показать")

    def _on_copy_access_key(self) -> None:
        text = self._access_key_edit.text()
        if text:
            QApplication.clipboard().setText(text)
            self._copy_btn.setText("Скопировано ✓")
        else:
            self._copy_btn.setText("Ключ отсутствует")

    def _on_regenerate(self) -> None:
        if self._svc is None:
            return
        if (
            QMessageBox.question(
                self,
                "Новый ключ доступа",
                "Создать новый ключ доступа к этой ноде?\n\n"
                "Все, кому был передан текущий ключ, сразу потеряют доступ. "
                "Новый ключ нужно передать тем, кому доступ по-прежнему нужен.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.No
        ):
            return
        if not self._svc.regenerate_access_key():
            QMessageBox.warning(self, "Ноды", "Не удалось сохранить новый ключ доступа.")
            return
        self._load_own_info()
        self._copy_btn.setText("Копировать")
        QMessageBox.information(
            self,
            "Ноды",
            "Новый ключ доступа создан. Прежний ключ больше не действует.",
        )

    def _on_apply_listen(self) -> None:
        """Применить порт/флаг прослушивания и перезапустить сервер нод."""
        if self._svc is None or self._server_pending:
            return
        web_port = int(config.app.web.port or 8765)
        if self._listen_cb.isChecked() and self._port_spin.value() == web_port:
            # Один порт может занять только один из серверов.
            QMessageBox.warning(
                self,
                "Ноды",
                f"Порт {web_port} указан в настройках веб-сервера управления. "
                "Выберите другой порт.",
            )
            return
        if not self._svc.set_listen_settings(
            self._port_spin.value(), self._listen_cb.isChecked()
        ):
            QMessageBox.warning(self, "Ноды", "Не удалось сохранить параметры сервера.")
            return
        from src.workers.gui_relay import post_to_gui
        from src.workers.network.worker_pool import get_service_worker_pool

        port, enabled = self._port_spin.value(), self._listen_cb.isChecked()
        self._server_pending = True
        self._apply_listen_btn.setEnabled(False)

        def apply_server() -> tuple[bool, str]:
            from src.web.node_server import get_node_server

            srv = get_node_server()
            fallback = "Не удалось применить настройки сервера нод."
            if enabled:
                if srv.restart(port=port):
                    return True, ""
                return False, srv.last_error or fallback
            srv.stop()
            ok = not srv.has_pending_workers()
            return ok, "" if ok else fallback

        dialog = self
        applied = self._on_server_applied

        def complete(task_id: str, result: tuple[bool, str]) -> None:
            post_to_gui(dialog, applied, *result)

        def failed(task_id: str, error: str) -> None:
            post_to_gui(dialog, applied, False, error)

        try:
            get_service_worker_pool().submit(
                apply_server, on_complete=complete, on_error=failed,
                metadata={"type": "node_server_settings"},
            )
        except RuntimeError as error:
            failed("", str(error))

    def _on_server_applied(self, ok: bool, error: str) -> None:
        """Показать результат применения настроек после возврата в GUI."""
        self._server_pending = False
        self._apply_listen_btn.setEnabled(True)
        if not self.isVisible():
            return
        if not ok:
            QMessageBox.warning(self, "Ноды", error)
            return
        QMessageBox.information(self, "Ноды", "Настройки сервера применены.")

    # ========== сохранение ==========

    def _save_silent(self) -> bool:
        """Сохранить таблицу нод без модальных сообщений."""
        if self._svc is None:
            return False
        ok = True
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if not item:
                continue
            iid = item.data(Qt.ItemDataRole.UserRole)
            if not iid:
                continue
            name = item.text().strip()
            host = (
                self._table.item(row, 1).text().strip()
                if self._table.item(row, 1)
                else ""
            )
            try:
                port = (
                    int(self._table.item(row, 2).text())
                    if self._table.item(row, 2)
                    else 8801
                )
            except ValueError:
                return False
            en_item = self._table.item(row, 4)
            visible = en_item.checkState() == Qt.CheckState.Checked if en_item else True
            ok2, _ = self._svc.update_node(
                iid,
                name=name,
                host=host,
                port=port,
                autoconnect=visible,
            )
            ok = ok and ok2
        return ok

    def _on_save(self) -> None:
        if self._svc is None:
            QMessageBox.critical(self, "Ноды", "Сервис нод недоступен.")
            return
        if not self._save_silent():
            QMessageBox.critical(self, "Ноды", "Не удалось сохранить часть нод.")
            return
        QMessageBox.information(self, "Ноды", "Настройки нод сохранены.")
