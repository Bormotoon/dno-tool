"""BundleSelector — виджет выбора связки учётных данных.

QComboBox (наполнение — description) + QToolButton (иконка) для открытия
диалога управления связками (CredentialsDialog).

Используется везде, где раньше был QLineEdit для логина/пароля:
  - ConfigDialog (глобальная связка по умолчанию)
  - DeviceEditDialog (per-host override)
  - SetupWizard (шаг SSH)
  - IpRangeDialog (массовое добавление хостов)
  - DeviceTreeView (inline-колонка, через BundleDelegate)
"""

from typing import Optional

from PySide6.QtWidgets import QWidget, QHBoxLayout, QComboBox, QToolButton
from PySide6.QtCore import Signal


def credential_service_for(widget):
    while widget is not None:
        service = getattr(widget, "wizard_credential_service", None)
        if service is not None:
            return service
        widget = widget.parent()
    from src.di import get_container
    from src.services.configuration.credential_service import CredentialService

    return get_container().resolve_optional(CredentialService)


class BundleSelector(QWidget):
    """Композитный виджет: QComboBox выбора связки + кнопка-иконка управления.

    Сигнал ``current_iid`` испускается при изменении выбора.
    """

    # Срабатывает при изменении выбора (iid). None = «(наследовать)» / «(нет)».
    changed = Signal(object)

    def __init__(
        self,
        parent=None,
        allow_none: bool = True,
        none_label: str = "(наследовать)",
    ) -> None:
        """
        Args:
            parent: Родительский виджет.
            allow_none: Разрешить выбор «нет связки» (первая запись).
            none_label: Текст записи «нет связки» (например «(наследовать)»).
        """
        super().__init__(parent)
        self._allow_none = allow_none
        self._none_label = none_label
        self._none_iid = None  # iid для записи «нет связки»
        self._suppress_signal = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._combo = QComboBox(self)
        self._combo.setMinimumWidth(140)
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        layout.addWidget(self._combo, 1)

        self._manage_btn = QToolButton(self)
        from src.ui.icons import set_themed_icon

        set_themed_icon(self._manage_btn, "menu_credentials")
        self._manage_btn.setToolTip("Управление связками учётных данных")
        self._manage_btn.clicked.connect(self._on_manage_clicked)
        layout.addWidget(self._manage_btn)

        # Первичное наполнение
        self.refresh()

    # ========== Публичный API ==========

    def current_iid(self) -> Optional[str]:
        """Текущий выбранный iid связки. None = «нет связки»."""
        iid = self._combo.currentData()
        if iid is None or iid == "":
            return None
        return iid

    def _get_service(self):
        return credential_service_for(self)

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def set_current_iid(self, iid: Optional[str]) -> None:
        """Установить выбор по iid. None / пусто → запись «нет связки»."""
        self._desired_iid = iid
        self._suppress_signal = True
        try:
            if not iid:
                if self._allow_none:
                    self._combo.setCurrentIndex(0)
                else:
                    # Нет записи «нет связки» — выбираем первую связку если есть
                    if self._combo.count() > 0:
                        self._combo.setCurrentIndex(0)
                return
            # Ищем iid среди данных combo
            for i in range(self._combo.count()):
                if self._combo.itemData(i) == iid:
                    self._combo.setCurrentIndex(i)
                    return
            # iid не найден — возможно связка удалена. Выбираем «нет».
            if self._allow_none:
                self._combo.setCurrentIndex(0)
        finally:
            self._suppress_signal = False

    def refresh(self) -> None:
        """Перечитать связки из CredentialService и обновить combo.

        Сохраняет текущий выбор по iid (если ещё существует).
        """
        current = getattr(self, "_desired_iid", self.current_iid())
        self._suppress_signal = True
        try:
            self._combo.clear()
            # Запись «нет связки»
            if self._allow_none:
                self._combo.addItem(self._none_label, None)
            # Связки из сервиса
            try:
                svc = self._get_service()
                bundles = svc.get_bundles() if svc else []
            except Exception:
                bundles = []
            for b in bundles:
                desc = b.get("description") or b.get("login") or "(без описания)"
                self._combo.addItem(desc, b.get("iid", ""))
            # Восстанавливаем выбор
            self.set_current_iid(current)
        finally:
            self._suppress_signal = False

    # ========== Внутренние ==========

    def _on_combo_changed(self, _index: int) -> None:
        if not self._suppress_signal:
            self._desired_iid = self.current_iid()
            self.changed.emit(self.current_iid())

    def _on_manage_clicked(self) -> None:
        """Открыть диалог управления связками, затем обновить combo."""
        try:
            from src.ui.dialogs.tools.credentials import CredentialsDialog

            dlg = CredentialsDialog(self)
            dlg.exec()
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(
                self,
                "Связки учётных данных",
                f"Не удалось открыть диалог управления связками:\n{e}",
            )
        # Обновляем список (связки могли измениться), сохраняем выбор
        self.refresh()
        # Сигнал об изменении (если выбранная связка удалилась — iid станет None)
        self.changed.emit(self.current_iid())
