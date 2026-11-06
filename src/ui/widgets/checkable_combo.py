"""Выпадающий список с поддержкой множественного выбора (чекбоксы)."""

from PySide6.QtWidgets import QComboBox, QStyledItemDelegate, QListView
from PySide6.QtCore import Qt, Signal, QModelIndex, QEvent


class CheckableComboBox(QComboBox):
    item_check_state_changed = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)

        # Настройка отображения
        self.setView(QListView(self))
        self.view().viewport().installEventFilter(self)
        self.setModel(self.model())
        self.setItemDelegate(QStyledItemDelegate())

        # Делаем редактируемым и настраиваем lineEdit
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setAlignment(Qt.AlignLeft)

        # Блокируем стандартное поведение при закрытии
        self.model().dataChanged.connect(self._update_display_text)
        self._update_display_text()

    def _handle_item_pressed(self, index: QModelIndex):
        item = self.model().itemFromIndex(index)
        if item is None or not item.isEnabled():
            return
        new_state = Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
        item.setCheckState(new_state)
        self.item_check_state_changed.emit(item.text(), new_state == Qt.Checked)

    def eventFilter(self, watched, event) -> bool:
        """Переключать всю строку одинаково, не передавая клик обработчику закрытия."""
        if watched == self.view().viewport() and event.type() in (
            QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
        ) and event.button() == Qt.MouseButton.LeftButton:
            index = self.view().indexAt(event.position().toPoint())
            if index.isValid():
                if event.type() == QEvent.Type.MouseButtonPress:
                    self.view().setCurrentIndex(index)
                    self._handle_item_pressed(index)
                return True
        return super().eventFilter(watched, event)

    def _update_display_text(self):
        """Обновляет текст, игнорируя стандартное поведение QComboBox"""
        checked = self.checked_items()
        display_text = ", ".join(checked) if checked else "Выберите элементы"
        self.lineEdit().setText(display_text)
        self.setToolTip(display_text)

    def hidePopup(self):
        super().hidePopup()
        self._update_display_text()

    def showPopup(self):
        super().showPopup()
        # Сохраняем текущий текст перед открытием
        self._saved_text = self.lineEdit().text()

    def addItem(self, text: str, checked=False, userData=None):
        super().addItem(text, userData)
        item = self.model().item(self.count() - 1)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )
        self._update_display_text()

    def addItems(self, texts: list, userDataList=None):
        super().addItems(texts)
        for i in range(self.count()):
            item = self.model().item(i)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
        self._update_display_text()

    def checked_items(self) -> list:
        return [
            self.itemText(i)
            for i in range(self.count())
            if self.model().item(i).checkState() == Qt.Checked
        ]

    def all_items(self) -> list:
        """Все элементы списка (отмеченные и неотмеченные)."""
        return [self.itemText(i) for i in range(self.count())]

    def set_checked(self, index_or_text, checked: bool) -> bool:
        """Программно отметить/снять элемент по индексу или тексту.

        Возвращает True если элемент найден.
        """
        idx = -1
        if isinstance(index_or_text, int):
            idx = index_or_text
        else:
            idx = self.findText(str(index_or_text))
        if idx < 0 or idx >= self.count():
            return False
        item = self.model().item(idx)
        if item is None:
            return False
        item.setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )
        self._update_display_text()
        # Программное изменение тоже эмитит сигнал: подписчики (пингер)
        # реагируют так же, как на клик пользователя.
        self.item_check_state_changed.emit(self.itemText(idx), checked)
        return True

    def set_checked_items(self, texts: list) -> None:
        """Отметить только перечисленные элементы, остальные снять."""
        wanted = set(str(t) for t in (texts or []))
        try:
            self.model().blockSignals(True)
            for i in range(self.count()):
                item = self.model().item(i)
                if item is None:
                    continue
                item.setCheckState(
                    Qt.CheckState.Checked
                    if self.itemText(i) in wanted
                    else Qt.CheckState.Unchecked
                )
        finally:
            self.model().blockSignals(False)
        self._update_display_text()
        # Массовое восстановление состояния: сигналы не эмитим
        # (подписчикам достаточно вызвать checked_items() при необходимости).

    def remove_item(self, text: str) -> bool:
        """Удалить элемент по тексту. Возвращает True если удален."""
        idx = self.findText(str(text))
        if idx < 0:
            return False
        self.removeItem(idx)
        self._update_display_text()
        return True

    def clear_all(self) -> None:
        """Удалить все элементы."""
        super().clear()
        self._update_display_text()
