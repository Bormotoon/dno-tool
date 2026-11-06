"""Выпадающий список задач с иерархическим деревом и параметрами команд."""

from PySide6.QtWidgets import *
from PySide6.QtCore import *
from PySide6.QtGui import *
from src.config import ICONS
from src.ui.icons import _icon_from_key, qicon_from_path  # zip-темы: иконки из памяти
from src.logger import logger

# Роль для хранения полного словаря задачи (Task)
TASK_DATA_ROLE = Qt.UserRole + 1

# Разделитель между сегментами пути в строке комбобокса
_PATH_SEPARATOR = " \u2192 "  # " → "
# Символ обрезки начала пути при переполнении ширины
_ELLIPSIS = "\u2026"  # "…"


class TaskPathLineEdit(QLineEdit):
    """ReadOnly QLineEdit, отрисовывающий путь задачи сегментами с иконками.

    Каждый сегмент — кортеж (QIcon | None, str). Иконка рисуется слева от текста
    сегмента, между сегментами — текстовый разделитель ``" → "``.

    При переполнении ширины виджета обрезается **начало** пути: первые сегменты
    заменяются на один символ ``…`` + разделитель, листовой сегмент (последний)
    всегда остаётся видим. Если даже один листовой сегмент шире доступной
    ширины, его текст обрезается хвостом через ``QFontMetrics.elidedText``.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._segments: list[tuple[QIcon | None, str]] = []
        # QLineEdit с readOnly сам рисует фон/рамку через QStyle; мы переопределяем
        # только отрисовку контента, поэтому setFrame сохраняем.

    def setPathSegments(self, segments: list[tuple[QIcon | None, str]]) -> None:
        """Установить сегменты пути и перерисовать виджет."""
        self._segments = list(segments)
        # Очищаем текст line edit, чтобы placeholder не перекрывал нашу отрисовку
        # и чтобы super().paintEvent() не выводил дубликат.
        super().setText("")
        self.update()

    def clearSegments(self) -> None:
        """Сбросить сегменты — снова виден placeholder."""
        self._segments = []
        super().setText("")
        self.update()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API
        """Перехват: пустая строка сбрасывает сегменты, непустая делегируется.

        На практике мы зовём только ``setPathSegments``/``clearSegments``, но
        ``setText("")`` безопасен и используется при сбросе.
        """
        if not text:
            self.clearSegments()
        else:
            self._segments = []
            super().setText(text)

    def _content_rect(self) -> QRect:
        """Прямоугольник для отрисовки контента с учётом рамки и margins."""
        m = self.textMargins()
        opt = QStyleOptionFrame()
        self.initStyleOption(opt)
        fw = self.style().pixelMetric(
            QStyle.PixelMetric.PM_DefaultFrameWidth, opt, self
        )
        r = self.rect().adjusted(
            fw + m.left(), fw + m.top(), -fw - m.right(), -fw - m.bottom()
        )
        return r

    def _icon_size(self, fm: QFontMetrics) -> QSize:
        """Размер иконки под кегль шрифта (с небольшим запасом по высоте)."""
        h = fm.height() + 2
        return QSize(h, h)

    def _segment_width(
        self,
        icon: QIcon | None,
        text: str,
        fm: QFontMetrics,
        icon_size: QSize,
    ) -> int:
        """Ширина одного сегмента (иконка + небольшой gap + текст)."""
        w = 0
        if icon is not None and not icon.isNull():
            w += icon_size.width() + 2  # 2px gap между иконкой и текстом
        w += fm.horizontalAdvance(text)
        return w

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        if not self._segments:
            # Нет сегментов — пусть QLineEdit сам рисует placeholder.
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        opt = QStyleOptionFrame()
        self.initStyleOption(opt)
        # Рисуем рамку/фон через QStyle, как обычный QLineEdit.
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_FrameLineEdit, opt, painter, self
        )

        content = self._content_rect()
        if content.width() <= 0 or content.height() <= 0:
            return

        fm = self.fontMetrics()
        icon_size = self._icon_size(fm)
        sep_w = fm.horizontalAdvance(_PATH_SEPARATOR)
        avail_w = content.width()

        # Полная ширина всех сегментов с разделителями.
        total_w = 0
        seg_widths: list[int] = []
        for icon, text in self._segments:
            sw = self._segment_width(icon, text, fm, icon_size)
            seg_widths.append(sw)
            total_w += sw
        total_w += sep_w * (len(self._segments) - 1)

        # Определяем, какие сегменты показывать.
        # Порядок в self._segments: корень -> ... -> лист (лист последний).
        visible: list[tuple[QIcon | None, str, int]] = []  # (icon, text, width)
        prefix_ellipsis = False

        if total_w <= avail_w:
            visible = [
                (icon, text, seg_widths[i])
                for i, (icon, text) in enumerate(self._segments)
            ]
        else:
            # Обрезаем начало: накапливаем сегменты справа налево, начиная с листа.
            n = len(self._segments)
            kept: list[int] = []
            running = 0
            for i in range(n - 1, -1, -1):
                w = seg_widths[i]
                add = w if not kept else (sep_w + w)
                if running + add > avail_w:
                    break
                kept.append(i)
                running += add
            kept.reverse()

            if not kept:
                # Не влезает даже лист — рисуем только лист с elidedText хвоста.
                icon, text = self._segments[-1]
                icon_w = (
                    icon_size.width() + 2
                    if (icon is not None and not icon.isNull())
                    else 0
                )
                elided = fm.elidedText(
                    text, Qt.TextElideMode.ElideRight, avail_w - icon_w
                )
                visible = [(icon, elided, icon_w + fm.horizontalAdvance(elided))]
            else:
                visible = [
                    (self._segments[i][0], self._segments[i][1], seg_widths[i])
                    for i in kept
                ]
                # Если отброшены сегменты слева от kept[0], добавляем префикс «… ».
                if kept[0] > 0:
                    ellipsis_w = fm.horizontalAdvance(_ELLIPSIS)
                    need = ellipsis_w + sep_w
                    # Проверяем, влезает ли эллипсис перед первым оставленным сегментом.
                    if running + need <= avail_w:
                        prefix_ellipsis = True
                        running += need
                    else:
                        # Не влезает — отбрасываем первый оставленный, добавляем эллипсис.
                        if len(visible) > 1:
                            visible = visible[1:]
                            prefix_ellipsis = True

        # Отрисовка.
        pal = self.palette()
        text_color = pal.color(QPalette.ColorRole.Text)
        painter.setPen(text_color)

        x = content.left()
        text_flags = int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        def _draw_text_at(px: int, s: str) -> int:
            """Рисует строку s вертикально центрированно по content, возвращает.advance"""
            r = QRect(px, content.top(), fm.horizontalAdvance(s), content.height())
            painter.drawText(r, text_flags, s)
            return r.width()

        if prefix_ellipsis:
            x += _draw_text_at(x, _ELLIPSIS)
            x += _draw_text_at(x, _PATH_SEPARATOR)

        last_idx = len(visible) - 1
        for idx, (icon, text, _w) in enumerate(visible):
            if icon is not None and not icon.isNull():
                pix = icon.pixmap(icon_size)
                if not pix.isNull():
                    slot_w = icon_size.width()
                    slot_h = icon_size.height()
                    slot_rect = QRectF(
                        x,
                        content.center().y() - slot_h / 2,
                        slot_w,
                        slot_h,
                    )
                    painter.drawPixmap(
                        slot_rect, pix, QRectF(0, 0, pix.width(), pix.height())
                    )
                    x += slot_w + 2
            x += _draw_text_at(x, text)
            if idx < last_idx:
                x += _draw_text_at(x, _PATH_SEPARATOR)

        painter.end()


class TaskTreeView(QTreeView):
    """Кастомный QTreeView для TaskComboBox, перехватывающий выбор элементов."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._task_combo = parent

    def mousePressEvent(self, event: QMouseEvent):
        index = self.indexAt(event.pos())
        if index.isValid():
            task_data = self.model().data(index, TASK_DATA_ROLE)
            if task_data:
                # Это задача - закрываем popup и обновляем текст
                self._task_combo.hidePopup()
                self._task_combo._on_task_selected(index, task_data)
                event.accept()
                return
        # Для категорий или невалидных индексов - стандартное поведение
        super().mousePressEvent(event)


class TaskTreeModel(QStandardItemModel):
    """Модель данных для QTreeView, отображающая иерархическую структуру Tasks (пакетов шагов)."""

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)

    def load_data(self, data_structure: list):
        self.clear()
        root_item = self.invisibleRootItem()
        self._populate_recursive(root_item, data_structure)
        # root_item.sortChildren(0)
        self.collapse_all_items()

    def collapse_all_items(self):
        parent = self.parent()
        if isinstance(parent, TaskComboBox):
            tree_view = parent.view()
            if isinstance(tree_view, QTreeView):
                tree_view.collapseAll()

    def refresh_icons(self) -> None:
        """Перечитать иконки задач/категорий под текущую тему иконок."""
        command_icon = _icon_from_key("command")
        folder_icon = _icon_from_key("folder")

        def walk(parent_item) -> None:
            for row in range(parent_item.rowCount()):
                child = parent_item.child(row)
                if child is None:
                    continue
                try:
                    has_task = child.data(TASK_DATA_ROLE) is not None
                except Exception:
                    has_task = False
                try:
                    child.setIcon(command_icon if has_task else folder_icon)
                except Exception:
                    pass
                if child.hasChildren():
                    walk(child)

        walk(self.invisibleRootItem())

    def _populate_recursive(self, parent_item: QStandardItem, data_list: list):
        if not isinstance(data_list, list):
            return
        for item_data in data_list:
            if not isinstance(item_data, dict):
                continue
            # Это Task (имя + список commands)
            if "name" in item_data and "commands" in item_data:
                task_name = item_data.get("name", "Без имени")
                task_item = QStandardItem(task_name)
                task_item.setIcon(_icon_from_key("command"))
                task_item.setEditable(False)
                task_item.setSelectable(True)

                # Convert legacy command format if needed
                if "commands" in item_data:
                    commands = item_data["commands"]
                    if commands and isinstance(commands, list):
                        if not isinstance(commands[0], dict):
                            item_data["commands"] = [
                                {"type": "ssh", "text": cmd} for cmd in commands
                            ]

                task_item.setData(item_data, TASK_DATA_ROLE)
                desc = item_data.get("description", "")
                if "params" in item_data and item_data["params"]:
                    desc += f"\nПараметры: {', '.join(item_data['params'])}"
                task_item.setToolTip(desc)
                parent_item.appendRow(task_item)

            # Категория (папка) содержит список дочерних элементов
            elif len(item_data) == 1:
                category_name = list(item_data.keys())[0]
                child_data_list = item_data[category_name]
                if isinstance(child_data_list, list):
                    category_item = QStandardItem(category_name)
                    category_item.setEditable(False)
                    category_item.setSelectable(False)
                    category_item.setIcon(_icon_from_key("folder"))
                    parent_item.appendRow(category_item)
                    self._populate_recursive(category_item, child_data_list)

    def find_first_task_item(
        self, parent_index: QModelIndex = QModelIndex()
    ) -> QModelIndex:
        """Находит первую Task в дереве, начиная с индекса parent_index"""
        rows = self.rowCount(parent_index)
        for row in range(rows):
            index = self.index(row, 0, parent_index)
            if self.data(index, TASK_DATA_ROLE):
                return index
            if self.hasChildren(index):
                found_index = self.find_first_task_item(index)
                if found_index.isValid():
                    return found_index
        return QModelIndex()

    def find_task_by_iid(
        self, iid: str, parent_index: QModelIndex = QModelIndex()
    ) -> QModelIndex:
        """Найти Task по iid в дереве. Возвращает невалидный индекс, если не найден."""
        if not iid:
            return QModelIndex()
        rows = self.rowCount(parent_index)
        for row in range(rows):
            index = self.index(row, 0, parent_index)
            data = self.data(index, TASK_DATA_ROLE)
            if data and data.get("iid") == iid:
                return index
            if self.hasChildren(index):
                found_index = self.find_task_by_iid(iid, index)
                if found_index.isValid():
                    return found_index
        return QModelIndex()

    def export_data(self, parent_index: QModelIndex = QModelIndex()) -> list:
        data = []
        rows = self.rowCount(parent_index)
        for row in range(rows):
            index = self.index(row, 0, parent_index)
            item = self.itemFromIndex(index)
            if self.data(index, TASK_DATA_ROLE):
                # Это Task (пакет команд)
                data.append(self.data(index, TASK_DATA_ROLE))
            elif self.hasChildren(index):
                # Это категория
                category_name = item.text()
                children = self.export_data(index)
                if children:
                    data.append({category_name: children})
        return data


class TaskFilterProxyModel(QSortFilterProxyModel):
    """Proxy-фильтр дерева задач: совпадение в имени или полном пути.

    Паритет с веб-фильтром (``applyTaskSearch`` в ``app.js``): запрос
    сравнивается без учёта регистра с именем задачи и с полным путём
    ``Папка → ... → Задача``. Категории сами не матчатся — при включённом
    ``recursiveFilteringEnabled`` они показываются автоматически, если виден
    хотя бы один потомок. Пустой запрос — видно всё.
    """

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._query = ""
        self.setRecursiveFilteringEnabled(True)
        # Сортировку не включаем — порядок задаёт исходная модель.
        self.setDynamicSortFilter(True)

    def set_query(self, text: str | None) -> None:
        """Установить текст фильтра (casefold, без regex)."""
        query = (text or "").casefold()
        if query == self._query:
            return
        self._query = query
        self.invalidateFilter()

    def query(self) -> str:
        """Текущий запрос фильтра (уже casefold)."""
        return self._query

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802 - Qt API
        if not self._query:
            return True
        src = self.sourceModel()
        if src is None:
            return False
        index = src.index(source_row, 0, source_parent)
        if not index.isValid():
            return False
        # Только задачи-листья проверяем; папки пропускает recursive-механизм.
        if src.data(index, TASK_DATA_ROLE) is None:
            return False
        name = str(src.data(index, Qt.ItemDataRole.DisplayRole) or "")
        if self._query in name.casefold():
            return True
        # Полный путь: поднимаемся к корню, сравниваем склеенную строку.
        parts = [name]
        parent = index.parent()
        while parent.isValid():
            parts.append(str(src.data(parent, Qt.ItemDataRole.DisplayRole) or ""))
            parent = parent.parent()
        path = " ".join(reversed(parts))
        return self._query in path.casefold()


class TaskComboBox(QComboBox):
    taskSelected = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._model = TaskTreeModel(self)
        self._filter = TaskFilterProxyModel(self)
        self._filter.setSourceModel(self._model)
        self._tree_view = TaskTreeView(self)
        self.view().setFixedHeight(400)
        self._tree_view.setHeaderHidden(True)
        # Комбобокс и дерево смотрят через proxy — порядок исходной модели
        # сохраняется, фильтрация не трогает данные.
        self.setModel(self._filter)
        self.setView(self._tree_view)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setEditable(True)
        # Кастомный line edit для отрисовки пути с иконками сегментов.
        self._path_edit = TaskPathLineEdit(self)
        self.setLineEdit(self._path_edit)
        self._path_edit.setReadOnly(True)
        self._path_edit.installEventFilter(self)
        self.setPlaceholderText("Выберите задачу...")
        # Храним данные последней выбранной задачи (не зависит от наведения курсора)
        self._selected_task_data = None
        # Строка поиска над деревом в popup (создаётся лениво в showPopup,
        # т.к. контейнер popup пересоздаётся самим QComboBox).
        self._filter_edit: QLineEdit | None = None
        self._filter_empty: QLabel | None = None

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        # Открываем popup при клике на lineEdit
        if obj == self.lineEdit() and event.type() == QEvent.Type.MouseButtonPress:
            self.showPopup()
            return True
        # Поиск в popup: Esc очищает запрос (второй Esc закрывает),
        # Enter выбирает первую видимую задачу.
        if (
            obj is self._filter_edit
            and self._filter_edit is not None
            and event.type() == QEvent.Type.KeyPress
        ):
            key = event.key()
            if key == Qt.Key.Key_Escape:
                if self._filter_edit.text():
                    self._filter_edit.clear()
                else:
                    self.hidePopup()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._select_first_visible()
                return True
        return super().eventFilter(obj, event)

    # ---- Фильтр дерева задач ----

    def set_filter_text(self, text: str | None) -> None:
        """Установить текст фильтра и применить его к дереву."""
        self._filter.set_query(text)
        if self._filter_edit_alive():
            with QSignalBlocker(self._filter_edit):
                self._filter_edit.setText(text or "")
        self._apply_filter_visibility()

    def filter_text(self) -> str:
        """Текущий текст фильтра (как введён, без casefold)."""
        if self._filter_edit_alive():
            return self._filter_edit.text()
        return ""

    def clear_filter(self) -> None:
        """Сбросить фильтр и свернуть дерево к исходному виду."""
        if self._filter_edit_alive():
            with QSignalBlocker(self._filter_edit):
                self._filter_edit.clear()
        self._filter.set_query("")
        self._apply_filter_visibility()

    def _on_filter_text_changed(self, text: str) -> None:
        self._filter.set_query(text)
        self._apply_filter_visibility()

    def _apply_filter_visibility(self) -> None:
        """Авто-раскрытие при фильтре / collapse при очистке + пустое состояние."""
        if self._filter.query():
            self._tree_view.expandAll()
        else:
            self._tree_view.collapseAll()
        self._update_empty_state()

    def _update_empty_state(self) -> None:
        if not self._filter_edit_alive():
            return
        is_empty = self._filter.rowCount() == 0
        self._filter_empty.setVisible(is_empty)
        self._tree_view.setVisible(not is_empty)

    def _filter_edit_alive(self) -> bool:
        """Проверка, что виджеты поиска живы (контейнер popup пересоздаётся Qt)."""
        if self._filter_edit is None:
            return False
        try:
            self._filter_edit.parent()
        except RuntimeError:
            self._filter_edit = None
            self._filter_empty = None
            return False
        return True

    def _ensure_filter_widgets(self) -> None:
        """Создать строку поиска и плейсхолдер, встроить в контейнер popup."""
        if not self._filter_edit_alive():
            edit = QLineEdit()
            edit.setPlaceholderText("Поиск задач...")
            edit.setClearButtonEnabled(True)
            edit.textChanged.connect(self._on_filter_text_changed)
            edit.installEventFilter(self)
            self._filter_edit = edit
            empty = QLabel("Ничего не найдено")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setEnabled(False)
            empty.setVisible(False)
            self._filter_empty = empty
        container = self._tree_view.parentWidget()
        if container is None:
            return
        layout = container.layout()
        if layout is None:
            return
        # Встраиваем один раз: повторный showPopup не дублирует виджеты.
        if self._filter_edit.parent() is not container and hasattr(
            layout, "insertWidget"
        ):
            layout.insertWidget(0, self._filter_edit)
            layout.insertWidget(1, self._filter_empty)
            with QSignalBlocker(self._filter_edit):
                self._filter_edit.setText(self._filter.query())
        self._update_empty_state()

    def showPopup(self) -> None:  # noqa: N802 - Qt API
        super().showPopup()
        self._ensure_filter_widgets()
        if self._filter_edit is not None:
            self._filter_edit.setFocus()

    def hidePopup(self) -> None:  # noqa: N802 - Qt API
        super().hidePopup()
        # Фокус обратно на комбобокс, чтобы колёсико/клавиатура работали как раньше.
        self.setFocus()

    # ---- Выбор задач (индексы дерева — proxy, путь — через source) ----

    def _source_index(self, proxy_index: QModelIndex) -> QModelIndex:
        if not proxy_index.isValid():
            return QModelIndex()
        return self._filter.mapToSource(proxy_index)

    def _first_task_proxy_index(self) -> QModelIndex:
        """Первая видимая (с учётом фильтра) задача в дереве."""
        return self._find_first_task_proxy(QModelIndex())

    def _find_first_task_proxy(self, parent: QModelIndex) -> QModelIndex:
        proxy = self._filter
        for row in range(proxy.rowCount(parent)):
            index = proxy.index(row, 0, parent)
            if proxy.data(index, TASK_DATA_ROLE) is not None:
                return index
            if proxy.hasChildren(index):
                found = self._find_first_task_proxy(index)
                if found.isValid():
                    return found
        return QModelIndex()

    def _select_first_visible(self) -> None:
        """Выбор первой видимой задачи (для Enter в строке поиска)."""
        index = self._first_task_proxy_index()
        if index.isValid():
            task_data = self._filter.data(index, TASK_DATA_ROLE)
            if task_data is not None:
                self.hidePopup()
                self._on_task_selected(index, task_data)

    def _on_task_selected(self, index: QModelIndex, task_data: dict):
        """Вызывается при выборе задачи из дерева."""
        segments = self._get_task_path_segments(self._source_index(index))
        self._path_edit.setPathSegments(segments)
        self._selected_task_data = task_data
        self.taskSelected.emit(task_data)

    def model(self) -> TaskTreeModel:
        # Исходная модель с данными (дерево показывает proxy поверх неё).
        return self._model

    def view(self) -> QTreeView:
        return self._tree_view

    def load_tasks(self, data_structure: list):
        previous_iid = self.get_selected_task_iid()
        self.model().load_data(data_structure)
        self._update_empty_state()
        if self._filter.query():
            # Фильтр пережил перезагрузку данных — раскрываем совпадения.
            self._tree_view.expandAll()
        if previous_iid:
            source_index = self._model.find_task_by_iid(previous_iid)
            if source_index.isValid():
                task_data = self._model.data(source_index, TASK_DATA_ROLE)
                segments = self._get_task_path_segments(source_index)
                self._path_edit.setPathSegments(segments)
                self._selected_task_data = task_data
                # Текущий индекс — только если задача видна через фильтр.
                # Данные выбора восстанавливаем всегда, чтобы активный фильтр
                # не терял выбор при перезагрузке команд.
                proxy_index = self._filter.mapFromSource(source_index)
                if proxy_index.isValid():
                    self.view().setCurrentIndex(proxy_index)
                    # Раскрываем ветку к восстановленной задаче.
                    parent = proxy_index.parent()
                    while parent.isValid():
                        self.view().expand(parent)
                        parent = parent.parent()
                self.taskSelected.emit(task_data)
                return
        self._selected_task_data = None

    def clear(self):
        """Очищает комбобокс и сбрасывает выбранную задачу."""
        super().clear()
        self.clear_filter()
        self._selected_task_data = None

    def expand_all_items(self, index: QModelIndex = QModelIndex()):
        proxy = self._filter
        if not index.isValid():
            for row in range(proxy.rowCount()):
                root_index = proxy.index(row, 0)
                self.expand_all_items(root_index)
            return
        if proxy.hasChildren(index):
            self.view().expand(index)
            for row in range(proxy.rowCount(index)):
                child_index = proxy.index(row, 0, index)
                self.expand_all_items(child_index)

    def select_first_task(self):
        first_task_index = self._first_task_proxy_index()
        task_data = None
        if first_task_index.isValid():
            self.view().setCurrentIndex(first_task_index)
            task_data = self._filter.data(first_task_index, TASK_DATA_ROLE)
            if task_data is not None:
                segments = self._get_task_path_segments(
                    self._source_index(first_task_index)
                )
                self._path_edit.setPathSegments(segments)
                self._selected_task_data = task_data
        else:
            self._path_edit.clearSegments()
            self._selected_task_data = None

        self.taskSelected.emit(task_data)

    def _get_task_path(self, index: QModelIndex) -> str:
        """Строит путь к задаче в виде 'Папка -> Подпапка -> Задача'"""
        names = []
        model = self.model()
        while index.isValid():
            item = model.itemFromIndex(index)
            if item is not None:
                text = item.text()
                names.append(text)
            index = index.parent()
        return " -> ".join(reversed(names))

    def _get_task_path_segments(
        self, index: QModelIndex
    ) -> list[tuple[QIcon | None, str]]:
        """Строит список сегментов пути от корня к листу.

        Каждый сегмент — кортеж ``(QIcon | None, str)``. Иконка берётся из
        ``QStandardItem.icon()`` (модель уже установила ``ICONS['folder']``
        для папок и ``ICONS['command']`` для задач). Порядок: корень → ... → лист.
        """
        segments: list[tuple[QIcon | None, str]] = []
        model = self.model()
        while index.isValid():
            item = model.itemFromIndex(index)
            if item is not None:
                icon = item.icon() if not item.icon().isNull() else None
                segments.append((icon, item.text()))
            index = index.parent()
        segments.reverse()
        return segments

    def get_selected_task_data(self) -> dict | None:
        """Возвращает данные последней выбранной задачи."""
        return self._selected_task_data

    def get_selected_task_iid(self) -> str | None:
        """Возвращает iid последней выбранной задачи или None."""
        data = self._selected_task_data
        if data is None:
            return None
        return data.get("iid")

    def export_data(self) -> list:
        return self.model().export_data()
