"""Диалог управления связками учётных данных (login/password).

CRUD-таблица: 3 колонки [Описание, Логин, Пароль]. Inline-редактирование
(double-click на ячейку). Кнопки «Добавить» / «Удалить».
Пароль маскируется через PasswordDelegate (как в WebServerDialog).

Сохранение — через CredentialService (шифрует пароли при config.save()).
"""

import uuid

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
)
from PySide6.QtCore import Qt

from src.logger import logger
from src.ui.widgets.device_tree import PasswordDelegate, PASSWORD_DATAROLE
from src.services.configuration.credential_service import CredentialService, PASSWORD_UNCHANGED


class CredentialsDialog(QDialog):
    """Диалог управления связками учётных данных."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Связки учётных данных")
        self.setMinimumWidth(560)
        self.setMinimumHeight(320)
        self._svc = None
        widget = parent
        while widget is not None:
            self._svc = getattr(widget, "wizard_credential_service", None)
            if self._svc is not None:
                break
            widget = widget.parentWidget()
        if self._svc is None and parent is not None and hasattr(parent, "_get_service"):
            self._svc = parent._get_service()
        if self._svc is None:
            self._svc = self._get_service()
        self._setup_ui()
        self._load_bundles()

    # ========== UI ==========

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        hint = QLabel(
            "Связки учётных данных (логин/пароль) для SSH-подключений.\n"
            "Дважды кликните по ячейке для редактирования. Пароль хранится "

        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)

        # Таблица: Описание | Логин | Пароль
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Описание", "Логин", "Пароль"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setItemDelegateForColumn(2, PasswordDelegate(self))
        layout.addWidget(self._table, 1)

        # Кнопки Добавить/Удалить
        self._add_btn = QPushButton("Добавить")
        self._add_btn.clicked.connect(self._on_add)
        self._del_btn = QPushButton("Удалить")
        self._del_btn.clicked.connect(self._on_delete)


        # Кнопки Сохранить/Закрыть
        bottom = QHBoxLayout()
        self._save_btn = QPushButton("Сохранить")
        self._save_btn.clicked.connect(self._on_save)
        self._close_btn = QPushButton("Закрыть")
        self._close_btn.clicked.connect(self.accept)
        bottom.addWidget(self._add_btn)
        bottom.addWidget(self._del_btn)
        bottom.addStretch(1)
        bottom.addWidget(self._save_btn)
        bottom.addWidget(self._close_btn)
        layout.addLayout(bottom)

    def _get_service(self) -> CredentialService:
        from src.di import get_container

        return get_container().resolve(CredentialService)

    # ========== Загрузка ==========

    def _load_bundles(self) -> None:
        """Заполнить таблицу из CredentialService (plaintext)."""
        self._table.setRowCount(0)
        if self._svc is None:
            return
        for b in self._svc.get_bundles():
            self._add_row(
                iid=b.get("iid", ""),
                description=b.get("description", ""),
                login=b.get("login", ""),
                password=b.get("password", ""),
            )

    def _add_row(
        self,
        iid: str = "",
        description: str = "",
        login: str = "",
        password: str = "",
    ) -> None:
        """Добавить строку в таблицу.

        Пароль хранится в PASSWORD_DATAROLE (реальное значение) и в EditRole
        (тоже реальный текст — делегат маскирует при отображении).
        iid хранится в UserRole+5 (чтобы не путать с PASSWORD_DATAROLE).
        """
        row = self._table.rowCount()
        self._table.insertRow(row)

        d_item = QTableWidgetItem(description)
        d_item.setData(Qt.ItemDataRole.UserRole + 5, iid)
        l_item = QTableWidgetItem(login)
        p_item = QTableWidgetItem(password)
        p_item.setData(PASSWORD_DATAROLE, password or "")

        self._table.setItem(row, 0, d_item)
        self._table.setItem(row, 1, l_item)
        self._table.setItem(row, 2, p_item)

    # ========== CRUD ==========

    def _on_add(self) -> None:
        self._add_row(iid=uuid.uuid4().hex, description="", login="", password="")
        # Прокручиваем к новой строке и начинаем редактирование описания
        last = self._table.rowCount() - 1
        self._table.selectRow(last)
        self._table.editItem(self._table.item(last, 0))

    def _on_delete(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        # Удаляем снизу вверх
        for idx in sorted(rows, key=lambda i: i.row(), reverse=True):
            self._table.removeRow(idx.row())

    # ========== Сохранение ==========

    def _collect_bundles(self) -> list:
        """Собрать список связок из таблицы для CredentialService.update_bundles.

        Реальный пароль берётся из PASSWORD_DATAROLE (PasswordDelegate
        записывает туда при редактировании). В text() может быть маска.
        Если пароль не редактировался — используется sentinel PASSWORD_UNCHANGED,
        и CredentialService сохранит старое значение.
        """
        out = []
        for row in range(self._table.rowCount()):
            d_item = self._table.item(row, 0)
            l_item = self._table.item(row, 1)
            p_item = self._table.item(row, 2)
            if not d_item:
                continue
            description = d_item.text().strip()
            login = l_item.text().strip() if l_item else ""
            # Пароль: из PASSWORD_DATAROLE (реальное) или sentinel
            real_pw = p_item.data(PASSWORD_DATAROLE) if p_item else ""
            iid = d_item.data(Qt.ItemDataRole.UserRole + 5) or ""
            out.append(
                {
                    "iid": iid,
                    "description": description,
                    "login": login,
                    "password": real_pw if real_pw else PASSWORD_UNCHANGED,
                }
            )
        return out

    def _on_save(self) -> bool:
        """Сохранить связки через CredentialService."""
        if self._svc is None:
            QMessageBox.critical(self, "Ошибка", "Сервис учётных данных недоступен.")
            return False
        bundles = self._collect_bundles()
        ok = self._svc.update_bundles(bundles)
        if not ok:
            QMessageBox.critical(self, "Ошибка", "Не удалось сохранить связки.")
            return False
        ok = self._svc.save()
        if not ok:
            QMessageBox.critical(self, "Ошибка", "Не удалось сохранить конфигурацию.")
            return False
        message = "Связки добавлены в черновик мастера. Они запишутся после «Готово»." if getattr(self._svc._config, "wizard_draft", False) else "Связки учётных данных сохранены."
        QMessageBox.information(self, "Сохранение", message)
        return True
