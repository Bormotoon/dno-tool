"""Компактное отображение независимых режимов выполнения SSH-шага."""

from PySide6.QtCore import QEvent, QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QHelpEvent, QIcon, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QToolTip,
)

from src.ui.icons import _icon_from_key


class CommandTypeDelegate(QStyledItemDelegate):
    """Центрировать значок типа и сохранять его контраст на выделенной строке."""

    ICON_SIZE = 24

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        """Размер ячейки определяется значком, а не подписью типа."""
        return QSize(self.ICON_SIZE + 16, self.ICON_SIZE + 14)

    @staticmethod
    def display_icon(icon: QIcon, option: QStyleOptionViewItem, size: QSize) -> QIcon:
        """На выделении использовать цвет текста, при блокировке — неактивный цвет."""
        if not option.state & QStyle.StateFlag.State_Enabled:
            color = option.palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        elif option.state & QStyle.StateFlag.State_Selected:
            color = option.palette.color(QPalette.ColorRole.HighlightedText)
        else:
            return icon
        ratio = option.widget.devicePixelRatioF() if option.widget else 1.0
        pixmap = icon.pixmap(size, ratio)
        if pixmap.isNull():
            return icon
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), color)
        painter.end()
        return QIcon(pixmap)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Рисовать один значок строго в центре ячейки поверх штатного выделения."""
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        icon = self.display_icon(QIcon(opt.icon), opt, QSize(self.ICON_SIZE, self.ICON_SIZE))
        opt.icon = QIcon()
        opt.text = ""
        opt.features &= ~QStyleOptionViewItem.ViewItemFeature.HasDecoration
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        rect = QRect(
            option.rect.x() + (option.rect.width() - self.ICON_SIZE) // 2,
            option.rect.y() + (option.rect.height() - self.ICON_SIZE) // 2,
            self.ICON_SIZE, self.ICON_SIZE,
        )
        painter.save()
        painter.setClipRect(option.rect)
        icon.paint(painter, rect, Qt.AlignmentFlag.AlignCenter)
        painter.restore()


class CommandModesDelegate(CommandTypeDelegate):
    """Центрированная группа включённых режимов с отдельными подсказками."""

    ICON_SIZE = 24
    GAP = 6
    MODES = (
        ("desktop_session", "command_desktop_session", "В окружении пользователя рабочего стола"),
        ("background", "command_background", "Запустить и не ждать"),
    )

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        """Оставить место для обоих режимов даже у обычного SSH-шага."""
        return QSize(2 * self.ICON_SIZE + self.GAP + 16, self.ICON_SIZE + 14)

    def _icon_rect(self, cell: QRect, position: int, count: int) -> QRect:
        """Одиночный значок по центру; пара симметрична относительно центра ячейки."""
        width = count * self.ICON_SIZE + (count - 1) * self.GAP
        return QRect(
            cell.x() + (cell.width() - width) // 2 + position * (self.ICON_SIZE + self.GAP),
            cell.y() + (cell.height() - self.ICON_SIZE) // 2,
            self.ICON_SIZE,
            self.ICON_SIZE,
        )

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Рисовать режимы поверх штатного выделения строки, без виджетов в ячейках."""
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        modes = index.data(Qt.ItemDataRole.UserRole) or {}
        painter.save()
        painter.setClipRect(option.rect)
        active = [entry for entry in self.MODES if modes.get(entry[0])]
        for position, (_, icon, _) in enumerate(active):
            themed = self.display_icon(_icon_from_key(icon), opt, QSize(self.ICON_SIZE, self.ICON_SIZE))
            themed.paint(painter, self._icon_rect(option.rect, position, len(active)), Qt.AlignmentFlag.AlignCenter)
        painter.restore()

    def helpEvent(
        self, event: QHelpEvent, view: QAbstractItemView,
        option: QStyleOptionViewItem, index: QModelIndex,
    ) -> bool:
        """При наведении объяснять именно тот режим, на который указывает мышь."""
        if event.type() == QEvent.Type.ToolTip:
            modes = index.data(Qt.ItemDataRole.UserRole) or {}
            active = [entry for entry in self.MODES if modes.get(entry[0])]
            for position, (_, _, label) in enumerate(active):
                if self._icon_rect(option.rect, position, len(active)).contains(event.pos()):
                    QToolTip.showText(event.globalPos(), label, view.viewport(), option.rect)
                    return True
        return super().helpEvent(event, view, option, index)
