"""GroupedHeaderView — QHeaderView с двухуровневыми группированными заголовками.

Поддерживает верхний ряд «групп» (например «SSH», «Veyon», «VNC»), каждая из
которых охватывает несколько колонок, и нижний ряд обычных подзаголовков.

Использование::

    table = QTableWidget(...)
    header = GroupedHeaderView(Qt.Orientation.Horizontal, table)
    table.setHorizontalHeader(header)
    header.setGroupedHeaders([
        {"label": "SSH",   "columns": [1, 2, 3, 4]},
        {"label": "Veyon", "columns": [5, 6, 7]},
        {"label": "VNC",   "columns": [8, 9, 10, 11]},
    ])
    # Колонка 0 (хост) не входит ни в одну группу — рисуется на полную высоту.

Группы рисуются в верхней полосе высотой `groupHeight()`; подзаголовки — в
нижней полосе стандартной высоты. `sizeHint()` учитывает обе полосы.

Группа с ключом ``"collapsed"`` (True/False) рисуется со стрелкой свёрнутости;
щелчок по групповой ячейке отправляет ``groupClicked(index)``. Скрытые колонки
группы не учитываются: ячейка группы охватывает только видимые.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QRect, QPoint, QSize, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QFontMetrics
from PySide6.QtWidgets import QHeaderView, QStyle, QStyleOption, QStyleOptionHeader, QToolTip


class GroupedHeaderView(QHeaderView):
    """QHeaderView с поддержкой двухуровневых групповых заголовков.

    Группы задаются через setGroupedHeaders. Колонки, не входящие ни в одну
    группу, рисуются на полную высоту (без разделения на верх/низ).
    """

    _GROUP_HEIGHT = 22  # высота верхней полосы групп
    _BOTTOM_MARGIN = 2  # отступ под подзаголовком
    _ARROW_SIZE = 9  # сторона стрелки свёрнутости

    groupClicked = Signal(int)

    def __init__(self, orientation: Qt.Orientation, parent=None) -> None:
        super().__init__(orientation, parent)
        self._groups: List[Dict] = []
        self._col_to_group: Dict[int, Dict] = {}
        # Принудительно включаем двухуровневое рисование
        self.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSectionsClickable(True)
        self.setSectionsMovable(False)

    # ------------------------------------------------------------ public API

    def setGroupedHeaders(self, groups: List[Dict]) -> None:
        """Задать групповые заголовки.

        Args:
            groups: список вида [{"label": str, "columns": [int, ...]}, ...].
                    columns — индексы колонок, охватываемых группой.
                    Колонки, не указанные ни в одной группе, рисуются на полную
                    высоту (без верхнего уровня).
        """
        self._groups = list(groups or [])
        self._col_to_group = {}
        for g in self._groups:
            for col in g.get("columns", []):
                self._col_to_group[col] = g
        self.viewport().update()
        # Пересчитать геометрию
        self.updateGeometry()
        self.setGeometry(self.x(), self.y(), self.width(), self.height())

    def groupHeight(self) -> int:
        """Высота верхней полосы групп."""
        return self._GROUP_HEIGHT if self._groups else 0

    def groupRect(self, index: int) -> QRect:
        """Прямоугольник группы во viewport по видимым колонкам; пустой, если их нет."""
        cols = [c for c in self._groups[index].get("columns", [])
                if not self.isSectionHidden(c)]
        if not cols:
            return QRect()
        left = min(self.sectionViewportPosition(c) for c in cols)
        right = max(self.sectionViewportPosition(c) + self.sectionSize(c) for c in cols)
        return QRect(left, 0, right - left, self.groupHeight())

    def groupAt(self, pos: QPoint) -> int:
        """Индекс группы под точкой верхней полосы или -1."""
        if not self._groups or pos.y() >= self.groupHeight():
            return -1
        for index in range(len(self._groups)):
            if self.groupRect(index).contains(pos):
                return index
        return -1

    def groupMinimumWidth(self, label: str, collapsible: bool) -> int:
        """Ширина, при которой подпись группы не обрезается."""
        font = QFont(self.font())
        font.setBold(True)
        width = QFontMetrics(font).horizontalAdvance(label) + 12
        return width + (self._ARROW_SIZE + 6 if collapsible else 0)

    # ------------------------------------------------------------ events

    def mousePressEvent(self, event) -> None:
        index = self.groupAt(event.position().toPoint())
        if index >= 0 and event.button() == Qt.MouseButton.LeftButton:
            # Щелчок по группе не выделяет колонки таблицы.
            event.accept()
            self.groupClicked.emit(index)
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        # Второй щелчок двойного клика тоже переключает группу.
        if self.groupAt(event.position().toPoint()) >= 0:
            self.mousePressEvent(event)
            return
        super().mouseDoubleClickEvent(event)

    def viewportEvent(self, event) -> bool:
        if event.type() == event.Type.ToolTip:
            index = self.groupAt(event.pos())
            if index >= 0:
                tip = self._groups[index].get("tooltip")
                if tip:
                    QToolTip.showText(event.globalPos(), tip, self)
                    return True
        return super().viewportEvent(event)

    # ------------------------------------------------------------ geometry

    def sizeHint(self) -> QSize:
        base = super().sizeHint()
        h = base.height() + self.groupHeight() + self._BOTTOM_MARGIN
        return QSize(base.width(), h)

    def sectionSizeFromContents(self, logical_index: int) -> QSize:
        base = super().sectionSizeFromContents(logical_index)
        if logical_index in self._col_to_group:
            # Групповая колонка: подзаголовок рисуется только в нижней полосе
            h = self.groupHeight() + base.height() + self._BOTTOM_MARGIN
        else:
            # Негрупповая колонка: на полную высоту
            h = self.groupHeight() + base.height() + self._BOTTOM_MARGIN if self._groups else base.height()
        return QSize(base.width(), h)

    # ------------------------------------------------------------ painting

    def paintEvent(self, event) -> None:
        """Двухуровневая отрисовка: верх — группы, низ — подзаголовки."""
        if not self._groups:
            super().paintEvent(event)
            return

        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        opt = QStyleOptionHeader()
        self.initStyleOption(opt)

        gh = self.groupHeight()
        bottom_h = self.height() - gh - self._BOTTOM_MARGIN

        # 1. Верхняя полоса: рисуем группы
        for index, g in enumerate(self._groups):
            rect = self.groupRect(index)
            if rect.isEmpty():
                continue
            self._draw_group_cell(painter, rect, g.get("label", ""), opt,
                                  g.get("collapsed"))

        # 2. Негрупповые колонки: рисуем на полную высоту (от 0 до height)
        for logical in range(self.count()):
            if logical in self._col_to_group or self.isSectionHidden(logical):
                continue
            x = self.sectionViewportPosition(logical)
            w = self.sectionSize(logical)
            rect = QRect(x, 0, w, self.height() - self._BOTTOM_MARGIN)
            self._draw_section_cell(painter, rect, logical, opt, full_height=True)

        # 3. Групповые колонки: подзаголовки в нижней полосе
        for logical in range(self.count()):
            if logical not in self._col_to_group or self.isSectionHidden(logical):
                continue
            x = self.sectionViewportPosition(logical)
            w = self.sectionSize(logical)
            rect = QRect(x, gh, w, bottom_h)
            self._draw_section_cell(painter, rect, logical, opt, full_height=False)

        painter.end()

    # ------------------------------------------------------------ drawing helpers

    def _draw_group_cell(self, painter: QPainter, rect: QRect, label: str,
                         opt: QStyleOptionHeader,
                         collapsed: Optional[bool] = None) -> None:
        """Нарисовать ячейку группы в верхней полосе."""
        # Фон
        pal = opt.palette
        bg = pal.button().color() if pal is not None else QColor(240, 240, 240)
        painter.fillRect(rect, bg)
        # Рамка
        pen = QPen(QColor(200, 200, 200))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        # Текст (жирный, по центру)
        font = QFont(self.font())
        font.setBold(True)
        painter.setFont(font)
        text_color = pal.buttonText().color() if pal is not None else QColor(0, 0, 0)
        painter.setPen(QPen(text_color))
        if collapsed is None:
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
            return
        # Стрелка слева от подписи: вправо — свёрнута, вниз — развёрнута.
        size = self._ARROW_SIZE
        width = painter.fontMetrics().horizontalAdvance(label) + size + 4
        left = rect.left() + max(2, (rect.width() - width) // 2)
        arrow = QStyleOption()
        arrow.initFrom(self)
        arrow.rect = QRect(left, rect.top() + (rect.height() - size) // 2, size, size)
        arrow.palette = pal
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_IndicatorArrowRight if collapsed
            else QStyle.PrimitiveElement.PE_IndicatorArrowDown,
            arrow, painter, self,
        )
        painter.setPen(QPen(text_color))
        painter.drawText(
            QRect(left + size + 4, rect.top(), rect.right() - left - size - 3, rect.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label,
        )

    def _draw_section_cell(self, painter: QPainter, rect: QRect, logical: int,
                           opt: QStyleOptionHeader, full_height: bool) -> None:
        """Нарисовать ячейку подзаголовка (или полную ячейку для негрупповых)."""
        pal = opt.palette
        # Фон
        bg = pal.window().color() if pal is not None else QColor(255, 255, 255)
        painter.fillRect(rect, bg)
        # Рамка
        pen = QPen(QColor(220, 220, 220))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawRect(rect.adjusted(0, 0, -1, -1))
        # Текст
        text = self.model().headerData(logical, self.orientation(),
                                       Qt.ItemDataRole.DisplayRole) if self.model() else ""
        text_color = pal.windowText().color() if pal is not None else QColor(0, 0, 0)
        painter.setPen(QPen(text_color))
        font = QFont(self.font())
        if not full_height:
            font.setBold(False)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(text) if text is not None else "")