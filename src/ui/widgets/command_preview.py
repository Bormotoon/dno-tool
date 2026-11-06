"""Bounded syntax-highlighted previews for command table cells."""

from collections import OrderedDict

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAbstractTextDocumentLayout, QPalette, QTextCursor, QTextDocument, QTextOption
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem

from .shell_highlight import BashSyntaxHighlighter


class CommandPreviewDelegate(QStyledItemDelegate):
    MAX_LINES = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._documents = OrderedDict()

    def sizeHint(self, option, index):
        lines = min(self.MAX_LINES, str(index.data() or '').count('\n') + 1)
        return QSize(160, lines * option.fontMetrics.lineSpacing() + 14)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # initStyleOption replaces newlines with U+2028; retain real blocks for
        # shell comment/quote state and for the bounded three-line preview.
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or '')
        # Bound layout work even for large imported playbooks.
        lines = text.split('\n', self.MAX_LINES)
        preview = '\n'.join(line[:500] + (' …' if len(line) > 500 else '') for line in lines[:self.MAX_LINES])
        if len(lines) > self.MAX_LINES:
            preview += ' …'
        type_index = index.siblingAtColumn(0)
        command_type = type_index.data(Qt.ItemDataRole.UserRole) or str(type_index.data() or '').lower()
        key = (preview, opt.font.toString(), opt.palette.cacheKey(), command_type)
        if key not in self._documents:
            doc = QTextDocument()
            doc.setDocumentMargin(0)
            doc.setDefaultFont(opt.font)
            text_option = QTextOption()
            text_option.setWrapMode(QTextOption.WrapMode.NoWrap)
            doc.setDefaultTextOption(text_option)
            doc.setPlainText(preview)
            highlighter = None
            if command_type == 'ssh':
                highlighter = BashSyntaxHighlighter(doc)
                highlighter.refresh_theme(opt.palette)
            self._documents[key] = (doc, highlighter)
            if len(self._documents) > 128:
                self._documents.popitem(last=False)
        self._documents.move_to_end(key)
        doc, _ = self._documents[key]
        opt.text = ''
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        rect = option.rect.adjusted(7, 7, -7, -7)
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette = opt.palette
        if opt.state & QStyle.StateFlag.State_Selected:
            selection = QAbstractTextDocumentLayout.Selection()
            selection.cursor = QTextCursor(doc)
            selection.cursor.select(QTextCursor.SelectionType.Document)
            selection.format.setForeground(opt.palette.color(QPalette.ColorRole.HighlightedText))
            context.selections = [selection]
        painter.save()
        painter.setClipRect(rect)
        # Центрируем текст по высоте ячейки, как значки типа и режимов шага.
        offset_y = max(0.0, (rect.height() - doc.size().height()) / 2)
        painter.translate(rect.left(), rect.top() + offset_y)
        doc.documentLayout().draw(painter, context)
        painter.restore()
