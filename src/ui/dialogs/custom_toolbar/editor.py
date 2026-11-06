"""Редактор пользовательских панелей: панели слева, кнопки справа."""

import copy
import logging
import uuid

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.config import config
from src.data import datastore
from src.ui.icons import _icon_from_key
from src.ui.widgets.device_list_toolbar import FLAT_TOOLBUTTON_QSS
from src.ui.widgets.custom_toolbar import (
    PANEL_ICON_SIZE_DEFAULT,
    PANEL_ICON_SIZE_MAX,
    PANEL_ICON_SIZE_MIN,
    sanitize_panel_icon_size,
)

logger = logging.getLogger(__name__)


class CustomToolbarsDialog(QDialog):
    """Менеджер панелей. Сохраняет в config.app.custom_toolbar по OK."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Пользовательские панели")
        self.resize(760, 520)

        try:
            panels = getattr(config.app.custom_toolbar, "panels", []) or []
            self._panels = copy.deepcopy(panels)
        except Exception:
            self._panels = []
        self._cur_panel = 0
        self._cur_button = 0
        self._applied_any = False
        # Перестройка панелей главного окна: parent (MainWindow). Вызывается
        # после Apply и при любом закрытии после применения (Apply не закрывает
        # диалог, Ok/Cancel - закрывают).
        self._on_applied = None
        if parent is not None and hasattr(parent, "_rebuild_custom_toolbars"):
            self._on_applied = parent._rebuild_custom_toolbars

        root = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal, self)
        root.addWidget(split, 1)

        # Слева: панели
        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.panel_list = QListWidget(left)
        self.panel_list.itemSelectionChanged.connect(self._on_panel_selected)
        left_layout.addWidget(self.panel_list, 1)
        prow = QHBoxLayout()
        prow.setSpacing(2)
        self.panel_add = QToolButton(left)
        self.panel_add.setIcon(_icon_from_key("menu_add_panel"))
        self.panel_add.setToolTip("Добавить панель")
        self.panel_add.setAutoRaise(False)
        self.panel_add.clicked.connect(self._add_panel)
        self.panel_rename = QToolButton(left)
        self.panel_rename.setIcon(_icon_from_key("menu_rename"))
        self.panel_rename.setToolTip("Переименовать панель")
        self.panel_rename.setAutoRaise(False)
        self.panel_rename.clicked.connect(self._rename_panel)
        self.panel_del = QToolButton(left)
        self.panel_del.setIcon(_icon_from_key("menu_delete"))
        self.panel_del.setToolTip("Удалить панель")
        self.panel_del.setAutoRaise(False)
        self.panel_del.clicked.connect(self._del_panel)
        for b in (self.panel_add, self.panel_rename, self.panel_del):
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setIconSize(QSize(32, 32))
            b.setFixedSize(QSize(32, 32))
            b.setStyleSheet(FLAT_TOOLBUTTON_QSS)
            prow.addWidget(b)
        prow.addStretch(1)
        left_layout.addLayout(prow)
        split.addWidget(left)

        # Справа: настройки панели + кнопки + форма кнопки
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        # Секция «Панель»: свойства выбранной панели (размер иконок -
        # один на всю панель; отображение названия в тулбаре)
        self.icon_size_spin = QSpinBox(right)
        self.icon_size_spin.setRange(PANEL_ICON_SIZE_MIN, PANEL_ICON_SIZE_MAX)
        self.icon_size_spin.setSingleStep(4)
        self.icon_size_spin.setSuffix(" px")
        self.icon_size_spin.setToolTip(
            "Размер иконок кнопок этой панели "
            f"({PANEL_ICON_SIZE_MIN}-{PANEL_ICON_SIZE_MAX} px, "
            f"{PANEL_ICON_SIZE_DEFAULT} - как у обычных панелей)"
        )
        self.icon_size_spin.valueChanged.connect(self._on_panel_icon_size)
        self.show_name_check = QCheckBox("Показывать название", right)
        self.show_name_check.setToolTip(
            "Рисовать название панели в начале тулбара\n(перед разделителем и кнопками)"
        )
        self.show_name_check.toggled.connect(self._on_panel_show_name)
        # Группа «Панель»: свойства выбранной панели отделены рамкой
        panel_box = QGroupBox("Настройки панели", right)
        panel_box_lay = QVBoxLayout(panel_box)
        panel_box_lay.setSpacing(4)
        panel_box_lay.addWidget(self.show_name_check)
        size_row = QHBoxLayout()
        size_row.setSpacing(6)
        size_row.addWidget(QLabel("Иконки:", panel_box))
        size_row.addWidget(self.icon_size_spin)
        size_row.addStretch(1)
        panel_box_lay.addLayout(size_row)
        right_layout.addWidget(panel_box)
        # Группа «Список кнопок»: список + tool-ряд добавления/порядка
        list_box = QGroupBox("Список кнопок", right)
        list_lay = QVBoxLayout(list_box)
        list_lay.setSpacing(4)
        self.button_list = QListWidget(list_box)
        self.button_list.itemSelectionChanged.connect(self._on_button_selected)
        self.button_list.itemDoubleClicked.connect(lambda *_: self._pick_command())
        list_lay.addWidget(self.button_list, 1)

        brow = QHBoxLayout()
        brow.setSpacing(2)
        self.btn_add = QToolButton(list_box)
        self.btn_add.setIcon(_icon_from_key("menu_add_command"))
        self.btn_add.setToolTip("Добавить кнопку")
        self.btn_add.setAutoRaise(False)
        self.btn_add.clicked.connect(self._add_button)
        self.btn_del = QToolButton(list_box)
        self.btn_del.setIcon(_icon_from_key("menu_delete"))
        self.btn_del.setToolTip("Удалить кнопку")
        self.btn_del.setAutoRaise(False)
        self.btn_del.clicked.connect(self._del_button)
        self.btn_up = QToolButton(list_box)
        self.btn_up.setIcon(_icon_from_key("menu_up"))
        self.btn_up.setToolTip("Переместить вверх")
        self.btn_up.setAutoRaise(False)
        self.btn_up.clicked.connect(lambda: self._move_button(-1))
        self.btn_down = QToolButton(list_box)
        self.btn_down.setIcon(_icon_from_key("menu_down"))
        self.btn_down.setToolTip("Переместить вниз")
        self.btn_down.setAutoRaise(False)
        self.btn_down.clicked.connect(lambda: self._move_button(1))
        for b in (
            self.btn_add,
            self.btn_del,
            self.btn_up,
            self.btn_down,
        ):
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            b.setIconSize(QSize(32, 32))
            b.setFixedSize(QSize(32, 32))
            b.setStyleSheet(FLAT_TOOLBUTTON_QSS)
        # Добавить кнопку слева, удаление/порядок - справа
        brow.addWidget(self.btn_add)
        brow.addStretch(1)
        brow.addWidget(self.btn_del)
        brow.addWidget(self.btn_up)
        brow.addWidget(self.btn_down)
        list_lay.addLayout(brow)
        # Внутри группы списка: настройки выбранной кнопки
        btn_box = QGroupBox("Настройки кнопки", list_box)
        form = QFormLayout(btn_box)
        self.name_edit = QLineEdit(btn_box)
        self.name_edit.setPlaceholderText("Название кнопки")
        self.name_edit.textEdited.connect(self._on_name_edited)
        form.addRow("Название:", self.name_edit)
        cmd_row = QHBoxLayout()
        self.cmd_label = QLabel("не выбрана", btn_box)
        self.cmd_label.setWordWrap(True)
        cmd_row.addWidget(self.cmd_label, 1)
        self.cmd_btn = QPushButton("Выбрать...", btn_box)
        self.cmd_btn.clicked.connect(self._pick_command)
        cmd_row.addWidget(self.cmd_btn)
        form.addRow("Команда:", cmd_row)
        # Иконка: предпросмотр + спек + выбор
        icon_row = QHBoxLayout()
        icon_row.setSpacing(8)
        self.icon_preview = QLabel("?", btn_box)
        self.icon_preview.setFixedSize(40, 40)
        self.icon_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_preview.setStyleSheet("border: 1px solid palette(mid);")
        icon_row.addWidget(self.icon_preview)
        self.icon_label = QLabel("не выбрана", btn_box)
        self.icon_label.setWordWrap(True)
        icon_row.addWidget(self.icon_label, 1)
        self.icon_btn = QPushButton("Выбрать...", btn_box)
        self.icon_btn.clicked.connect(self._pick_icon)
        icon_row.addWidget(self.icon_btn)
        form.addRow("Иконка:", icon_row)
        list_lay.addWidget(btn_box)
        right_layout.addWidget(list_box, 1)
        split.addWidget(right)
        split.setSizes([220, 540])

        # Нижний ряд: обычные кнопки с текстом, Применить слева, ОК/Отмена
        # справа (QDialogButtonBox не даёт русского текста и контроля порядка).
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.apply_btn = QPushButton("Применить", self)
        self.apply_btn.clicked.connect(self._apply)
        self.ok_btn = QPushButton("ОК", self)
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self._on_accept)
        self.cancel_btn = QPushButton("Отмена", self)
        self.cancel_btn.clicked.connect(self.reject)
        # Единая ширина троих: по самому широкому natural size (мин. 88).
        foot_btns = (self.apply_btn, self.ok_btn, self.cancel_btn)
        w = max(b.minimumSizeHint().width() for b in foot_btns)
        for b in foot_btns:
            b.setFixedWidth(max(88, w))
        foot.addStretch(1)
        foot.addWidget(self.ok_btn)
        foot.addWidget(self.apply_btn)
        foot.addWidget(self.cancel_btn)
        root.addLayout(foot)

        self._refresh_panels()
        self._refresh_buttons()

    # ---- Панели ----

    def _refresh_panels(self):
        self.panel_list.clear()
        for p in self._panels:
            name = getattr(p, "name", "") or "Панель"
            n = len(getattr(p, "buttons", []) or [])
            QListWidgetItem(f"{name} ({n})", self.panel_list)
        if self._panels:
            self._cur_panel = max(0, min(self._cur_panel, len(self._panels) - 1))
            self.panel_list.setCurrentRow(self._cur_panel)

    def _on_panel_selected(self):
        self._cur_panel = self.panel_list.currentRow()
        self._cur_button = 0
        self._refresh_buttons()

    def _sync_panel_size_box(self):
        """Спинбокс размера иконок - значение текущей панели."""
        size = PANEL_ICON_SIZE_DEFAULT
        enabled = False
        if self._panels and 0 <= self._cur_panel < len(self._panels):
            size = sanitize_panel_icon_size(
                getattr(self._panels[self._cur_panel], "icon_size", None)
            )
            enabled = True
        self.icon_size_spin.blockSignals(True)
        self.icon_size_spin.setValue(size)
        self.icon_size_spin.setEnabled(enabled)
        self.icon_size_spin.blockSignals(False)

    def _on_panel_show_name(self, checked: bool):
        """Чекбокс отображения названия текущей панели."""
        if not self._panels or not 0 <= self._cur_panel < len(self._panels):
            return
        self._panels[self._cur_panel].show_name = bool(checked)

    def _sync_panel_show_name(self):
        """Чекбокс названия - значение текущей панели."""
        show = True
        enabled = False
        if self._panels and 0 <= self._cur_panel < len(self._panels):
            show = bool(getattr(self._panels[self._cur_panel], "show_name", True))
            enabled = True
        self.show_name_check.blockSignals(True)
        self.show_name_check.setChecked(show)
        self.show_name_check.setEnabled(enabled)
        self.show_name_check.blockSignals(False)

    def _on_panel_icon_size(self, value: int):
        """Размер иконок текущей панели (один на всю панель)."""
        if not self._panels or not 0 <= self._cur_panel < len(self._panels):
            return
        panel = self._panels[self._cur_panel]
        size = sanitize_panel_icon_size(value)
        try:
            panel.icon_size = size
        except Exception:
            pass

    def _add_panel(self):
        name, ok = QInputDialog.getText(self, "Панель", "Название панели:")
        if not ok or not (name or "").strip():
            return
        from src.config import CustomToolbarPanel

        self._panels.append(
            CustomToolbarPanel(iid=uuid.uuid4().hex, name=name.strip(), buttons=[])
        )
        self._cur_panel = len(self._panels) - 1
        self._refresh_panels()
        self._refresh_buttons()

    def _rename_panel(self):
        if not self._panels or self._cur_panel < 0:
            return
        cur = self._panels[self._cur_panel]
        name, ok = QInputDialog.getText(
            self, "Панель", "Название панели:", text=getattr(cur, "name", "")
        )
        if not ok or not (name or "").strip():
            return
        cur.name = name.strip()
        self._refresh_panels()

    def _del_panel(self):
        if not self._panels or self._cur_panel < 0:
            return
        cur = self._panels[self._cur_panel]
        ret = QMessageBox.question(
            self, "Панель", f"Удалить панель «{getattr(cur, 'name', '')}»?"
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._panels.pop(self._cur_panel)
        self._cur_panel = max(0, self._cur_panel - 1)
        self._refresh_panels()
        self._refresh_buttons()

    # ---- Кнопки ----

    def _cur_buttons(self):
        if not self._panels or self._cur_panel < 0:
            return []
        try:
            panel = self._panels[self._cur_panel]
            btns = getattr(panel, "buttons", None)
            if btns is None:
                btns = []
                try:
                    panel.buttons = btns
                except Exception:
                    pass
            return btns
        except Exception:
            return []

    def _refresh_buttons(self):
        self.button_list.clear()
        for b in self._cur_buttons():
            name = getattr(b, "name", "") or "?"
            task = None
            try:
                task = datastore.find_task_by_iid(getattr(b, "task_iid", "") or "")
            except Exception:
                task = None
            suffix = "" if task else " [!]"
            item = QListWidgetItem(f"{name}{suffix}", self.button_list)
            try:
                from src.ui.widgets.custom_toolbar import custom_qicon

                item.setIcon(custom_qicon(getattr(b, "icon", "") or ""))
            except Exception:
                pass
        btns = self._cur_buttons()
        if btns:
            self._cur_button = max(0, min(self._cur_button, len(btns) - 1))
            self.button_list.setCurrentRow(self._cur_button)
        self._refresh_form()
        self._sync_panel_size_box()
        self._sync_panel_show_name()

    def _on_button_selected(self):
        self._cur_button = self.button_list.currentRow()
        self._refresh_form()

    def _refresh_form(self):
        btns = self._cur_buttons()
        if not btns or self._cur_button < 0 or self._cur_button >= len(btns):
            self.name_edit.setText("")
            self.cmd_label.setText("не выбрана")
            self.icon_label.setText("не выбрана")
            self.icon_preview.setText("?")
            for w in (self.name_edit, self.cmd_btn, self.icon_btn):
                w.setEnabled(False)
            return
        for w in (self.name_edit, self.cmd_btn, self.icon_btn):
            w.setEnabled(True)
        b = btns[self._cur_button]
        if self.name_edit.text() != getattr(b, "name", ""):
            self.name_edit.setText(getattr(b, "name", "") or "")
        try:
            task = datastore.find_task_by_iid(getattr(b, "task_iid", "") or "")
        except Exception:
            task = None
        if task:
            self.cmd_label.setText(str(task.get("name", "?")))
        elif getattr(b, "task_iid", ""):
            self.cmd_label.setText("команда удалена")
        else:
            self.cmd_label.setText("не выбрана")
        spec = getattr(b, "icon", "") or ""
        self.icon_label.setText(spec or "не выбрана")
        try:
            from src.ui.widgets.custom_toolbar import custom_qicon

            pm = custom_qicon(spec).pixmap(QSize(32, 32))
            if pm and not pm.isNull():
                self.icon_preview.setPixmap(pm)
            else:
                self.icon_preview.setText("?")
        except Exception:
            self.icon_preview.setText("?")

    def _on_name_edited(self, text: str):
        btns = self._cur_buttons()
        if btns and 0 <= self._cur_button < len(btns):
            btns[self._cur_button].name = text
            # Обновить строку списка без полного ребилда
            item = self.button_list.item(self._cur_button)
            if item is not None:
                item.setText(text or "?")

    def _add_button(self):
        if not self._panels:
            QMessageBox.information(self, "Панели", "Сначала создайте панель.")
            return
        from src.ui.dialogs.custom_toolbar.command_picker import CommandPickerDialog

        try:
            commands_data = datastore.get_commands_data()
        except Exception:
            commands_data = []
        picker = CommandPickerDialog(commands_data, self)
        if picker.exec() != QDialog.DialogCode.Accepted or not picker.selected:
            return
        task = picker.selected
        from src.config import CustomToolbarButton

        btn = CustomToolbarButton(
            iid=uuid.uuid4().hex,
            name=str(task.get("name", "")),
            task_iid=str(task.get("iid", "")),
            icon="",
        )
        self._cur_buttons().append(btn)
        self._cur_button = len(self._cur_buttons()) - 1
        self._refresh_panels()
        self._refresh_buttons()

    def _edit_button(self):
        # Быстрое редактирование: выбор команды и иконки через форму ниже
        self._pick_command()

    def _del_button(self):
        btns = self._cur_buttons()
        if not btns or self._cur_button < 0:
            return
        btns.pop(self._cur_button)
        self._cur_button = max(0, self._cur_button - 1)
        self._refresh_panels()
        self._refresh_buttons()

    def _move_button(self, delta: int):
        btns = self._cur_buttons()
        i = self._cur_button
        j = i + delta
        if not btns or i < 0 or j < 0 or j >= len(btns):
            return
        btns[i], btns[j] = btns[j], btns[i]
        self._cur_button = j
        self._refresh_buttons()

    def _pick_command(self):
        btns = self._cur_buttons()
        if not btns or self._cur_button < 0:
            return
        from src.ui.dialogs.custom_toolbar.command_picker import CommandPickerDialog

        try:
            commands_data = datastore.get_commands_data()
        except Exception:
            commands_data = []
        picker = CommandPickerDialog(commands_data, self)
        if picker.exec() != QDialog.DialogCode.Accepted or not picker.selected:
            return
        task = picker.selected
        b = btns[self._cur_button]
        b.task_iid = str(task.get("iid", ""))
        if not (getattr(b, "name", "") or "").strip():
            b.name = str(task.get("name", ""))
        self._refresh_buttons()

    def _pick_icon(self):
        btns = self._cur_buttons()
        if not btns or self._cur_button < 0:
            return
        from src.ui.dialogs.custom_toolbar.icon_picker import IconPickerDialog

        b = btns[self._cur_button]
        picker = IconPickerDialog(getattr(b, "icon", "") or "", self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return
        b.icon = picker.result_spec or ""
        self._refresh_buttons()

    def _apply(self):
        """Применить изменения к конфигу и файлу, не закрывая диалог."""
        # Валидация: у кнопки должна быть команда
        for p in self._panels:
            for b in getattr(p, "buttons", []) or []:
                if not getattr(b, "task_iid", ""):
                    QMessageBox.warning(
                        self,
                        "Панели",
                        f"Кнопка «{getattr(b, 'name', '?')}» без команды.",
                    )
                    return
        try:
            config.app.custom_toolbar.panels = self._panels
        except Exception as e:
            QMessageBox.warning(self, "Панели", f"Не удалось сохранить:\n{e}")
            return
        try:
            config.save()
        except Exception as e:
            QMessageBox.warning(self, "Панели", f"Не удалось записать файл:\n{e}")
            return
        # Обновить панели главного окна не закрывая диалог
        self._applied_any = True
        if self._on_applied is not None:
            try:
                self._on_applied()
            except Exception as e:
                logger.error(f"CustomToolbarsDialog: rebuild after apply: {e}")

    def _changed_since_open(self) -> bool:
        """Было ли Применить в этой сессии (для перестройки на Reject)."""
        return self._applied_any

    def _on_accept(self):
        self._apply()
        super().accept()
