"""Панель проекта: список импортированных видео и аудио."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal, QMimeData, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QAbstractItemView,
    QComboBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.domain.video.models import MediaAsset
from src.ui.icons import set_themed_icon
from src.ui.widgets.device_list_toolbar import FLAT_TOOLBUTTON_QSS

ASSET_MIME = "application/x-dno-asset"


def _mk_tool_btn(key: str, tip: str, parent=None) -> QToolButton:
    """Плоская кнопка с иконкой как в главных тулбарах (device_list_toolbar).

    Без рамки/фона, иконка 32x32 - однородно с тулбаром списка устройств.
    """
    btn = QToolButton(parent)
    set_themed_icon(btn, key)
    btn.setIconSize(QSize(32, 32))
    btn.setFixedSize(32, 32)
    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    btn.setStyleSheet(FLAT_TOOLBUTTON_QSS)
    btn.setToolTip(tip)
    btn.setAccessibleName(tip)
    return btn


def _fmt_dur(s: float) -> str:
    s = max(0, int(s))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _row_text(a: MediaAsset) -> str:
    """Строка файла: имя + длительность/статус анализа."""
    if a.duration > 0:
        meta = _fmt_dur(a.duration)
    elif getattr(a, "probe_error", ""):
        meta = f"ошибка анализа: {a.probe_error[:60]}"
    else:
        meta = "анализ..."
    if a.kind == "video" and a.width:
        meta += f" {a.width}x{a.height}"
    if a.duration > 0 and a.probe_error:
        meta += f" · {a.probe_error[:60]}"
    if getattr(a, "wave_error", ""):
        meta += " (волна: ошибка)"
    return f"{a.name}\n{meta}"


class AssetList(QListWidget):
    """Список с корректным DnD: mime с iid ассета для дропа на таймлайн."""

    def mimeTypes(self) -> list[str]:  # noqa: N802
        return [ASSET_MIME]

    def mimeData(self, items) -> QMimeData | None:  # noqa: N802
        if not items:
            return None
        mime = QMimeData()
        mime.setData(ASSET_MIME, items[0].data(Qt.ItemDataRole.UserRole).encode())
        return mime


class ProjectPanel(QWidget):
    import_requested = Signal()
    delete_file_requested = Signal()
    clear_list_requested = Signal()
    remove_requested = Signal(str)  # iid, контекст-меню - убрать из проекта
    repair_requested = Signal(str)  # iid, контекст-меню - ремонт файла
    reprobe_requested = Signal(str)  # iid, контекст-меню - повторный анализ
    add_to_timeline_requested = Signal(str)  # iid, контекст-меню - на таймлайн
    add_all_to_timeline_requested = Signal()  # контекст-меню - все на таймлайн
    show_in_folder_requested = Signal(str)  # iid, контекст-меню - открыть папку
    file_info_requested = Signal(str)  # iid, контекст-меню - свойства файла
    rename_requested = Signal(str)  # iid, контекст-меню - переименовать в списке
    add_batch_requested = Signal(list)
    remove_batch_requested = Signal(list)
    reprobe_batch_requested = Signal(list)
    title_requested = Signal(str)
    source_preview_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Поиск по имени и пути")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Поиск медиа")
        layout.addWidget(self.search)
        filters = QHBoxLayout()
        self.filter = QComboBox(self)
        for title, key in (
            ("Все файлы", "all"),
            ("Видео", "video"),
            ("Аудио", "audio"),
            ("Ошибки", "error"),
            ("На таймлайне", "used"),
            ("Не используются", "unused"),
        ):
            self.filter.addItem(title, key)
        self.sort = QComboBox(self)
        self.sort.addItems(["Порядок импорта", "По имени", "По длительности"])
        for combo in (self.filter, self.sort):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(5)
            combo.setMinimumWidth(70)
        self.filter.setAccessibleName("Фильтр медиа")
        self.sort.setAccessibleName("Сортировка медиа")
        filters.addWidget(self.filter)
        filters.addWidget(self.sort)
        layout.addLayout(filters)
        self.list = AssetList()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setDragEnabled(True)
        self.list.setDragDropMode(AssetList.DragDropMode.DragOnly)
        # крупные миниатюры первого кадра слева от строки
        self.list.setIconSize(QSize(96, 72))
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.itemDoubleClicked.connect(
            lambda item: self.source_preview_requested.emit(
                item.data(Qt.ItemDataRole.UserRole)
            )
        )
        layout.addWidget(self.list, 1)
        # импорт/удаление/очистка в нижнем ряду (экспорт - под текстами)
        row = QHBoxLayout()
        self.btn_import = _mk_tool_btn("ve_import", "Импорт медиа", self)
        self.btn_import.clicked.connect(self.import_requested.emit)
        self.btn_delete_file = _mk_tool_btn(
            "ve_remove_file", "Удалить файл из списка", self
        )
        self.btn_delete_file.clicked.connect(self.delete_file_requested.emit)
        self.btn_clear_list = _mk_tool_btn("ve_clear_list", "Очистить список", self)
        self.btn_clear_list.clicked.connect(self.clear_list_requested.emit)
        row.addWidget(self.btn_import)
        row.addStretch(1)
        row.addWidget(self.btn_delete_file)
        row.addWidget(self.btn_clear_list)
        layout.addLayout(row)
        self.btn_add = _mk_tool_btn("ve_add_timeline", "Добавить на таймлайн", self)
        self.btn_add.setToolTip(
            "Выбранные файлы; без выделения — все видимые в порядке списка"
        )
        self.btn_add.clicked.connect(
            lambda: self.add_batch_requested.emit(
                self.selected_iids() or self.ordered_iids()
            )
        )
        row.insertWidget(1, self.btn_add)
        self.summary = QLabel(self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self._assets: dict[str, MediaAsset] = {}
        self._used: set[str] = set()
        self._thumbs: dict[str, QIcon] = {}
        self._audio_icon: QIcon | None = None
        self.search.textChanged.connect(self._filter_items)
        self.filter.currentIndexChanged.connect(self._filter_items)
        self.sort.currentIndexChanged.connect(self._sort_items)

    def set_export_enabled(self, on: bool) -> None:
        # кнопка экспорта переехала в диалог (под панель текстов)
        dlg = self.parent()
        if dlg is not None and hasattr(dlg, "btn_export"):
            dlg.btn_export.setEnabled(on)

    def _context_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is not None and not item.isSelected():
            self.list.clearSelection()
            self.list.setCurrentItem(item)
        iid = item.data(Qt.ItemDataRole.UserRole) if item else self.current_iid()
        menu = QMenu(self)
        act_add = act_add_all = act_info = act_folder = None
        act_rename = act_reprobe = None
        act_title = None
        act_source = None
        if iid:
            # меню элемента: полный набор команд
            a = self._assets.get(iid)
            act_add = menu.addAction("Добавить на таймлайн")
            act_source = menu.addAction("Просмотр исходника / In/Out…")
            if self._assets:
                act_add_all = menu.addAction("Добавить все на таймлайн")
            act_info = menu.addAction("Свойства файла")
            act_folder = menu.addAction("Открыть папку с файлом")
            act_rename = menu.addAction("Переименовать")
            act_title = menu.addAction("Титр из названия файла")
            menu.addSeparator()
            if a is not None and a.duration <= 0:
                # анализ не удался/не завершен: дать выход без перезапуска
                act_reprobe = menu.addAction("Повторить анализ")
        elif self._assets:
            # меню пустого места при непустом списке: общая выкладка
            act_add_all = menu.addAction("Добавить все на таймлайн")
            menu.addSeparator()
        act_import = menu.addAction("Импортировать медиа")
        menu.addSeparator()
        act_remove = None
        act_repair = None
        act_clear = menu.addAction("Очистить список")
        if iid:
            menu.addSeparator()
            act_remove = menu.addAction("Удалить из проекта")
            a = self._assets.get(iid)
            if a is not None and a.duration > 0:
                act_repair = menu.addAction("Починить контейнер (remux)")
        chosen = menu.exec(self.list.mapToGlobal(pos))
        if chosen is None:
            return
        if act_add is not None and chosen is act_add:
            self.add_batch_requested.emit(self.selected_iids() or [iid])
        elif act_add_all is not None and chosen is act_add_all:
            self.add_all_to_timeline_requested.emit()
        elif act_info is not None and chosen is act_info:
            self.file_info_requested.emit(iid)
        elif act_folder is not None and chosen is act_folder:
            self.show_in_folder_requested.emit(iid)
        elif act_rename is not None and chosen is act_rename:
            self.rename_requested.emit(iid)
        elif act_title is not None and chosen is act_title:
            self.title_requested.emit(iid)
        elif act_source is not None and chosen is act_source:
            self.source_preview_requested.emit(iid)
        elif act_reprobe is not None and chosen is act_reprobe:
            self.reprobe_batch_requested.emit(self.selected_iids() or [iid])
        elif act_remove is not None and chosen is act_remove:
            self.remove_batch_requested.emit(self.selected_iids() or [iid])
        elif act_repair is not None and chosen is act_repair:
            self.repair_requested.emit(iid)
        elif chosen is act_import:
            self.import_requested.emit()
        elif chosen is act_clear:
            self.clear_list_requested.emit()

    def _audio_fallback_icon(self) -> QIcon:
        if self._audio_icon is None:
            self._audio_icon = self.style().standardIcon(
                QStyle.StandardPixmap.SP_MediaVolume
            )
        return self._audio_icon

    def set_thumb(self, iid: str, icon: QIcon) -> None:
        """Поставить готовую миниатюру на item (сигнал из пула миниатюр)."""
        self._thumbs[iid] = icon
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == iid:
                item.setIcon(icon)
                return

    def visible_iids(self) -> set[str]:
        viewport = self.list.viewport().rect()
        return {
            item.data(Qt.ItemDataRole.UserRole)
            for index in range(self.list.count())
            if not (item := self.list.item(index)).isHidden()
            and self.list.visualItemRect(item).intersects(viewport)
        }

    def ordered_iids(self) -> list[str]:
        return [
            item.data(Qt.ItemDataRole.UserRole)
            for index in range(self.list.count())
            if not (item := self.list.item(index)).isHidden()
        ]

    def selected_iids(self) -> list[str]:
        selected = {
            item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems()
        }
        return [iid for iid in self.ordered_iids() if iid in selected]

    def _sort_items(self) -> None:
        selected = {
            item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems()
        }
        items = [self.list.takeItem(0) for _ in range(self.list.count())]
        order = {iid: index for index, iid in enumerate(self._assets)}

        def key(item: QListWidgetItem) -> tuple:
            asset = self._assets[item.data(Qt.ItemDataRole.UserRole)]
            if self.sort.currentIndex() == 1:
                return tuple(
                    (1, int(part)) if part.isdigit() else (0, part.casefold())
                    for part in re.split(r"(\d+)", asset.name)
                )
            return (
                (asset.duration, asset.name.casefold())
                if self.sort.currentIndex() == 2
                else (order[asset.iid],)
            )

        for item in sorted(items, key=key):
            self.list.addItem(item)
            item.setSelected(item.data(Qt.ItemDataRole.UserRole) in selected)
        self._filter_items()

    def _filter_items(self) -> None:
        query = self.search.text().casefold().strip()
        kind = self.filter.currentData()
        for index in range(self.list.count()):
            item = self.list.item(index)
            asset = self._assets[item.data(Qt.ItemDataRole.UserRole)]
            matches = (
                kind == "all"
                or kind == asset.kind
                or (kind == "error" and bool(asset.probe_error))
                or (kind == "used" and asset.iid in self._used)
                or (kind == "unused" and asset.iid not in self._used)
            )
            item.setHidden(
                not matches or query not in f"{asset.name} {asset.path}".casefold()
            )
        pending = sum(
            a.duration <= 0 and not a.probe_error for a in self._assets.values()
        )
        errors = sum(bool(a.probe_error) for a in self._assets.values())
        self.summary.setText(
            f"Файлов: {len(self._assets)} · анализ: {pending} · ошибки: {errors}"
            if self._assets
            else ""
        )
        self.btn_add.setEnabled(any(a.duration > 0 for a in self._assets.values()))

    def remove_thumb(self, iid: str) -> None:
        self._thumbs.pop(iid, None)
        for index in range(self.list.count()):
            item = self.list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == iid:
                asset = self._assets.get(iid)
                item.setIcon(
                    self._audio_fallback_icon()
                    if asset and asset.kind == "audio"
                    else QIcon()
                )
                return

    def update_asset(self, iid: str) -> None:
        """Обновить строку файла на месте (анализ готов): без rebuild
        списка - не прыгают скролл и выделение."""
        a = self._assets.get(iid)
        if a is None:
            return
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == iid:
                item.setText(_row_text(a))
                item.setToolTip(
                    a.path + ("\n" + a.probe_error if a.probe_error else "")
                )
                icon = self._thumbs.get(iid)
                if icon is not None:
                    item.setIcon(icon)
                self._filter_items()
                return

    def set_assets(
        self,
        assets: list[MediaAsset],
        thumbs: dict[str, QIcon] | None = None,
        used: set[str] | None = None,
    ) -> None:
        self._assets = {a.iid: a for a in assets}
        self._used = set(used or ())
        if thumbs is not None:
            self._thumbs = dict(thumbs)
        vertical = self.list.verticalScrollBar().value()
        items = {
            self.list.item(index).data(Qt.ItemDataRole.UserRole): self.list.item(index)
            for index in range(self.list.count())
        }
        for iid, item in items.items():
            if iid not in self._assets:
                self.list.takeItem(self.list.row(item))
        for a in assets:
            item = items.get(a.iid)
            if item is None:
                item = QListWidgetItem()
                self.list.addItem(item)
            item.setText(_row_text(a))
            item.setData(Qt.ItemDataRole.UserRole, a.iid)
            item.setToolTip(a.path + ("\n" + a.probe_error if a.probe_error else ""))
            icon = self._thumbs.get(a.iid)
            if icon is not None:
                item.setIcon(icon)
            elif a.kind == "audio":
                # у аудио нет кадров: статичная иконка
                item.setIcon(self._audio_fallback_icon())
            else:
                item.setIcon(QIcon())
        if [
            self.list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.list.count())
        ] != list(self._assets) or self.sort.currentIndex():
            self._sort_items()
        self._filter_items()
        self.list.verticalScrollBar().setValue(vertical)

    def current_iid(self) -> str | None:
        items = self.list.selectedItems()
        if items:
            return items[0].data(Qt.ItemDataRole.UserRole)
        return None
