"""Общий редактор кода с нумерацией строк, действиями и поиском."""

import re

from PySide6.QtCore import QEvent, QRect, QRegularExpression, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontDatabase, QKeySequence, QPainter, QPalette, QTextCursor, QTextDocument, QTextFormat, QAction, QActionGroup, QTextOption
from PySide6.QtWidgets import QApplication, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTextEdit, QVBoxLayout, QWidget, QMenuBar, QInputDialog

from .shell_highlight import BashSyntaxHighlighter
from src.ui.icons import set_themed_icon


def script_font():
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setFamilies(['Cascadia Mono', 'Consolas', 'DejaVu Sans Mono', 'Liberation Mono', font.family()])
    font.setPointSize(max(10, QApplication.font().pointSize()))
    return font


class _LineNumbers(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_line_numbers(event)


class ShellCodeEditor(QPlainTextEdit):
    read_only_changed = Signal()

    def setReadOnly(self, enabled):
        super().setReadOnly(enabled)
        self.read_only_changed.emit()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.indent_width = 4
        self.use_tabs = False
        self.language = 'Bash'
        self.setFont(script_font())
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(' ') * self.indent_width)
        self.gutter = _LineNumbers(self)
        self.highlighter = BashSyntaxHighlighter(self.document())
        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self.selectionChanged.connect(self._highlight_current_line)
        self.textChanged.connect(self._highlight_current_line)
        self._update_margin()
        self._highlight_current_line()

    def gutter_width(self):
        return 16 + self.fontMetrics().horizontalAdvance('9') * max(2, len(str(self.blockCount())))

    def _update_margin(self, *_):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)
        rect = self.contentsRect()
        self.gutter.setGeometry(QRect(rect.left(), rect.top(), self.gutter_width(), rect.height()))

    def _update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_margin()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_margin()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, 'highlighter') and event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            if self.highlighter and hasattr(self.highlighter, 'refresh_theme'):
                self.highlighter.refresh_theme(self.palette())
            self._highlight_current_line()
            self.gutter.update()
        if hasattr(self, 'gutter') and event.type() == QEvent.Type.FontChange:
            self.setTabStopDistance(self.fontMetrics().horizontalAdvance(' ') * self.indent_width)
            self._update_margin()

    def paint_line_numbers(self, event):
        painter = QPainter(self.gutter)
        painter.fillRect(event.rect(), self.palette().color(QPalette.ColorRole.AlternateBase))
        block = self.firstVisibleBlock()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        current = self.textCursor().blockNumber()
        while block.isValid() and top <= event.rect().bottom():
            height = self.blockBoundingRect(block).height()
            if block.isVisible() and top + height >= event.rect().top():
                painter.setPen(self.palette().color(QPalette.ColorRole.Text if block.blockNumber() == current else QPalette.ColorRole.PlaceholderText))
                painter.drawText(0, round(top), self.gutter.width() - 8, self.fontMetrics().height(),
                                 Qt.AlignmentFlag.AlignRight, str(block.blockNumber() + 1))
            top += height
            block = block.next()

    def _highlight_current_line(self) -> None:
        selection = QTextEdit.ExtraSelection()
        color = QColor(self.palette().color(QPalette.ColorRole.Highlight))
        color.setAlpha(22)
        selection.format.setBackground(color)
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        selections = [selection]
        cursor = self.textCursor()
        word = cursor.selectedText()
        if word and re.fullmatch(r'\w+', word):
            pattern = QRegularExpression(
                r'(?<!\w)' + QRegularExpression.escape(word) + r'(?!\w)',
                QRegularExpression.PatternOption.UseUnicodePropertiesOption,
            )
            flags = QTextDocument.FindFlag.FindCaseSensitively
            match = self.document().find(pattern, cursor.selectionStart(), flags)
            # Частично выделенный идентификатор не считается целым словом.
            if not match.isNull() and match.selectionStart() == cursor.selectionStart():
                color.setAlpha(70)
                match = self.document().find(pattern, 0, flags)
                while not match.isNull():
                    occurrence = QTextEdit.ExtraSelection()
                    occurrence.format.setBackground(color)
                    occurrence.cursor = match
                    selections.append(occurrence)
                    match = self.document().find(pattern, match, flags)
        self.setExtraSelections(selections)
        self.gutter.update()

    def keyPressEvent(self, event):
        if not self.isReadOnly() and event.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self._indent(event.key() == Qt.Key.Key_Backtab or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                cursor = self.textCursor()
                before = cursor.block().text()[:cursor.positionInBlock()]
                indentation = re.match(r'[ \t]*', before)[0]
                cursor.beginEditBlock()
                cursor.insertText('\n' + indentation)
                cursor.endEditBlock()
                self.setTextCursor(cursor)
                return
        super().keyPressEvent(event)

    def _indent(self, backwards):
        if self.isReadOnly():
            return
        cursor = self.textCursor()
        if not cursor.hasSelection() and not backwards:
            cursor.insertText('\t' if self.use_tabs else ' ' * (self.indent_width - cursor.positionInBlock() % self.indent_width))
            return
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        first = self.document().findBlock(start)
        last = self.document().findBlock(end)
        if end > start and end == last.position():
            last = last.previous()
        changes = []
        block = first
        while block.isValid() and block.blockNumber() <= last.blockNumber():
            text = block.text()
            remove = (1 if text.startswith('\t') else min(self.indent_width, len(text) - len(text.lstrip(' ')))) if backwards else 0
            changes.append((block.position(), remove, '' if backwards else ('\t' if self.use_tabs else ' ' * self.indent_width)))
            block = block.next()
        cursor.beginEditBlock()
        for position, remove, replacement in reversed(changes):
            cursor.setPosition(position)
            cursor.setPosition(position + remove, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(replacement)
        cursor.endEditBlock()
        if end > start:
            first_change = changes[0]
            start = max(first.position(), start + len(first_change[2]) - min(first_change[1], start - first.position()))
            end += sum(len(replacement) - remove for _, remove, replacement in changes)
            cursor.setPosition(start)
            cursor.setPosition(max(start, end), QTextCursor.MoveMode.KeepAnchor)
        else:
            position, remove, replacement = changes[0]
            cursor.setPosition(max(position, start + len(replacement) - min(remove, start - position)))
        self.setTextCursor(cursor)

    def set_language(self, language):
        from . import syntax_highlight as syntax
        if self.highlighter:
            self.highlighter.setDocument(None)
            self.highlighter.deleteLater()
        self.language = language
        cls = syntax.HIGHLIGHTERS.get(language)
        self.highlighter = cls(self.document()) if cls else None
        if self.highlighter:
            self.highlighter.refresh_theme(self.palette())

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoomIn(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
        else:
            super().wheelEvent(event)

    def insertFromMimeData(self, source):
        if not self.isReadOnly():
            self.insertPlainText(source.text())

    def line_action(self, operation):
        if self.isReadOnly():
            return
        cursor = self.textCursor()
        first = self.document().findBlock(cursor.selectionStart())
        last = self.document().findBlock(cursor.selectionEnd())
        if cursor.hasSelection() and cursor.selectionEnd() == last.position():
            last = last.previous()
        start, end = first.position(), last.position() + last.length() - 1
        edit = QTextCursor(self.document())
        edit.setPosition(start)
        edit.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        text = edit.selectedText().replace('\u2029', '\n')
        if operation == 'duplicate':
            edit.beginEditBlock()
            edit.setPosition(end)
            edit.insertText('\n' + text)
            edit.endEditBlock()
        elif operation == 'delete':
            edit.beginEditBlock()
            if last.next().isValid():
                edit.setPosition(end + 1, QTextCursor.MoveMode.KeepAnchor)
            elif start:
                edit.setPosition(start - 1)
                edit.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            edit.insertText('')
            edit.endEditBlock()
        elif operation in ('up', 'down'):
            neighbor = first.previous() if operation == 'up' else last.next()
            if not neighbor.isValid():
                return
            if operation == 'up':
                edit.setPosition(neighbor.position())
                edit.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                replacement = text + '\n' + neighbor.text()
                start = neighbor.position()
            else:
                edit.setPosition(start)
                edit.setPosition(neighbor.position() + neighbor.length() - 1, QTextCursor.MoveMode.KeepAnchor)
                replacement = neighbor.text() + '\n' + text
                start += len((neighbor.text() + '\n').encode('utf-16-le')) // 2
            edit.beginEditBlock()
            edit.insertText(replacement)
            edit.endEditBlock()
            edit.setPosition(start)
            edit.setPosition(start + len(text.encode('utf-16-le')) // 2, QTextCursor.MoveMode.KeepAnchor)
        elif operation == 'comment':
            markers = {'Bash': '#', 'Python': '#', 'YAML': '#', 'INI': ';', 'Conf': '#',
                       'Env': '#', 'PHP': '//', 'JavaScript': '//', 'SQL': '--',
                       'HTML': '<!--', 'CSS': '/*'}
            marker = markers.get(self.language)
            if marker is None:
                return
            closing = {'HTML': ' -->', 'CSS': ' */'}.get(self.language, '')
            lines = text.split('\n')
            uncomment = all(not line.strip() or (line.lstrip().startswith(marker) and
                             (not closing or line.rstrip().endswith(closing.strip()))) for line in lines)
            result = []
            for line in lines:
                indent = line[:len(line) - len(line.lstrip())]
                body = line[len(indent):]
                if body:
                    if uncomment:
                        body = body[len(marker):]
                        if body.startswith(' '):
                            body = body[1:]
                        if closing:
                            body = body.rstrip()[:-len(closing.strip())].rstrip()
                    else:
                        body = marker + ' ' + body + closing
                result.append(indent + body)
            edit.beginEditBlock()
            edit.insertText('\n'.join(result))
            edit.endEditBlock()
            end = edit.position()
            edit.setPosition(start)
            edit.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(edit)


class ScriptEditor(QWidget):
    """Область кода и панели управления без файловых и сетевых операций."""

    save_requested = Signal()

    def __init__(self, parent=None, read_only=False, *, editor_class=ShellCodeEditor, show_menu=True):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.menu_bar = QMenuBar(self)
        self.menu_bar.setNativeMenuBar(False)
        layout.setMenuBar(self.menu_bar)
        self.menu_bar.setVisible(show_menu)
        self.actions_menu = self.menu_bar.addMenu('✏️ Правка')
        self.search_menu = self.menu_bar.addMenu('🔎 Поиск')
        self.view_menu = self.menu_bar.addMenu('👁️ Вид')
        self.settings_menu = self.menu_bar.addMenu('⚙️ Настройки')
        self.wrap = self.view_menu.addAction('Перенос строк')
        self.wrap.setCheckable(True)
        self.whitespace = self.view_menu.addAction('Показывать пробелы и табуляцию')
        self.whitespace.setCheckable(True)
        self.search_panel = QWidget()
        search_layout = QVBoxLayout(self.search_panel)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Поиск (Ctrl+F)')
        self.search.setClearButtonEnabled(True)
        search_row.addWidget(self.search, 1)
        self.case = QCheckBox('Aa')
        self.case.setToolTip('Учитывать регистр')
        self.whole = QCheckBox('Слово')
        self.regex = QCheckBox('Regex')
        for control in (self.case, self.whole, self.regex):
            search_row.addWidget(control)
        for label, back in [('↑', True), ('↓', False)]:
            button = QPushButton(label)
            button.setAutoDefault(False)
            button.setMaximumWidth(28)
            button.clicked.connect(lambda checked=False, backwards=back: self.find(backwards))
            search_row.addWidget(button)
        search_layout.addLayout(search_row)
        self.replace_panel = QWidget()
        replace_row = QHBoxLayout(self.replace_panel)
        replace_row.setContentsMargins(0, 0, 0, 0)
        self.replacement = QLineEdit()
        self.replacement.setPlaceholderText('Замена (группы regex: \\1, \\g<имя>)')
        replace_row.addWidget(self.replacement, 1)
        self.replace_buttons = []
        for label, callback in [('Заменить', self.replace_one), ('Заменить всё', self.replace_all)]:
            button = QPushButton(label)
            button.setAutoDefault(False)
            button.clicked.connect(callback)
            replace_row.addWidget(button)
            self.replace_buttons.append(button)
        search_layout.addWidget(self.replace_panel)
        self.search_error = QLabel()
        self.search_error.setWordWrap(True)
        search_layout.addWidget(self.search_error)
        layout.addWidget(self.search_panel)
        self.search_panel.hide()
        self.replace_panel.hide()
        self.editor = editor_class(self)
        layout.addWidget(self.editor, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._mutating_actions = []
        self._last_match = None
        self._action('Найти…', 'Ctrl+F', self._focus_search, menu=self.search_menu)
        self._action('Заменить…', 'Ctrl+H', lambda: self._focus_search(True), menu=self.search_menu)
        self.search_menu.addSeparator()
        self._action('Следующее совпадение', 'F3', self.find, menu=self.search_menu)
        self._action('Предыдущее совпадение', 'Shift+F3', lambda: self.find(True), menu=self.search_menu)
        self.search_menu.addSeparator()
        self._action('Перейти к строке…', 'Ctrl+G', self.goto_line, menu=self.search_menu)
        self.undo_action = self._action('Отменить', 'Ctrl+Z', self.editor.undo, True)
        self.redo_action = self._action('Повторить', 'Ctrl+Y', self.editor.redo, True)
        self.actions_menu.addSeparator()
        self._action('Вырезать', 'Ctrl+X', self.editor.cut, True)
        self._action('Копировать', 'Ctrl+C', self.editor.copy)
        self._action('Вставить', 'Ctrl+V', self.editor.paste, True)
        self._action('Выделить всё', 'Ctrl+A', self.editor.selectAll)
        self.actions_menu.addSeparator()
        for label, key, op in [('Дублировать строки', 'Ctrl+D', 'duplicate'),
                               ('Удалить строки', 'Ctrl+Shift+K', 'delete'),
                               ('Переместить вверх', 'Alt+Up', 'up'),
                               ('Переместить вниз', 'Alt+Down', 'down'),
                               ('Комментарий', 'Ctrl+/', 'comment')]:
            self._action(label, key, lambda operation=op: self.editor.line_action(operation), True)
        self._action('Увеличить отступ', '', lambda: self.editor._indent(False), True)
        self._action('Уменьшить отступ', '', lambda: self.editor._indent(True), True)
        self.view_menu.addSeparator()
        self._action('Увеличить масштаб', 'Ctrl++', lambda: self.editor.zoomIn(1), menu=self.view_menu)
        self._action('Уменьшить масштаб', 'Ctrl+-', lambda: self.editor.zoomOut(1), menu=self.view_menu)
        self.save_action = self._action('Сохранить', 'Ctrl+S', self.save_requested.emit, True, menu=False)
        self.save_action.setEnabled(False)
        self._save_enabled = False
        self.wrap.toggled.connect(lambda enabled: self.editor.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if enabled else QPlainTextEdit.LineWrapMode.NoWrap))
        self.whitespace.toggled.connect(self._show_whitespace)
        self.language_menu = self.settings_menu.addMenu('Язык подсветки')
        set_themed_icon(self.language_menu.menuAction(), 'command')
        self.language_group = QActionGroup(self)
        self.language_actions = {}
        for language in ('Bash', 'Python', 'YAML', 'JSON', 'PHP', 'HTML', 'CSS',
                         'JavaScript', 'SQL', 'INI', 'Conf', 'Env', 'Обычный текст'):
            action = self.language_menu.addAction(language)
            action.setCheckable(True)
            self.language_group.addAction(action)
            action.setChecked(language == 'Bash')
            action.triggered.connect(lambda checked=False, value=language: self.set_language(value))
            self.language_actions[language] = action
        self.indentation_menu = self.settings_menu.addMenu('Отступы')
        set_themed_icon(self.indentation_menu.menuAction(), 'menu_settings')
        self.indentation_group = QActionGroup(self)
        self.indentation_actions = []
        for index, label in enumerate(('2 пробела', '4 пробела', '8 пробелов', 'Табуляция')):
            action = self.indentation_menu.addAction(label)
            action.setCheckable(True)
            self.indentation_group.addAction(action)
            action.setChecked(index == 1)
            action.triggered.connect(lambda checked=False, value=index: self._indentation_changed(value))
            self.indentation_actions.append(action)
        self.search.textChanged.connect(self._search_changed)
        for control in (self.case, self.whole, self.regex):
            control.toggled.connect(self._search_changed)
        self.editor.cursorPositionChanged.connect(self._update_status)
        self.editor.selectionChanged.connect(self._update_status)
        self.editor.textChanged.connect(self._update_status)
        self.editor.read_only_changed.connect(self._update_actions)
        self.editor.undoAvailable.connect(self._update_actions)
        self.editor.redoAvailable.connect(self._update_actions)
        for control in (self.editor, self.search, self.replacement):
            control.installEventFilter(self)
        self.setReadOnly(read_only)
        self._update_status()

    def _action(self, label, shortcut, callback, mutating=False, *, menu=None):
        action = QAction(label, self)
        icon_key = {
            'Ctrl+Z': 've_undo', 'Ctrl+Y': 've_redo', 'Ctrl+C': 'menu_copy_name',
            'Ctrl+F': 'menu_scanner', 'Ctrl+G': 'menu_forward', 'Ctrl+S': 'menu_save',
            'Ctrl+D': 'menu_copy_name', 'Ctrl+Shift+K': 'menu_delete',
            'Alt+Up': 'menu_up', 'Alt+Down': 'menu_down',
        }.get(shortcut)
        if icon_key:
            set_themed_icon(action, icon_key)
        action.setShortcut(QKeySequence(shortcut))
        action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        action.triggered.connect(lambda checked=False: callback())
        if shortcut in ('Ctrl+Z', 'Ctrl+Y', 'Ctrl+X', 'Ctrl+C', 'Ctrl+V', 'Ctrl+A'):
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            self.editor.addAction(action)
        else:
            self.addAction(action)
        if menu is not False:
            (self.actions_menu if menu is None else menu).addAction(action)
        if mutating:
            self._mutating_actions.append(action)
        return action

    def attach_menu_bar(self, menu_bar: QMenuBar) -> None:
        """Добавить меню редактора в главное меню содержащего окна."""
        for action in self.menu_bar.actions():
            menu_bar.addAction(action)
        self.menu_bar.hide()

    def set_save_enabled(self, enabled):
        self._save_enabled = enabled
        self._update_actions()

    def setReadOnly(self, enabled):
        self.editor.setReadOnly(enabled)
        self._update_actions()

    def _update_actions(self, *_):
        writable = not self.editor.isReadOnly()
        for action in self._mutating_actions:
            action.setEnabled(writable)
        self.undo_action.setEnabled(writable and self.editor.document().isUndoAvailable())
        self.redo_action.setEnabled(writable and self.editor.document().isRedoAvailable())
        self.save_action.setEnabled(writable and self._save_enabled)
        for button in self.replace_buttons:
            button.setEnabled(writable)
        self.replacement.setReadOnly(not writable)

    def _indentation_changed(self, index):
        self.indentation_actions[index].setChecked(True)
        self.editor.use_tabs = index == 3
        self.editor.indent_width = [2, 4, 8, 4][index]
        self.editor.setTabStopDistance(self.editor.fontMetrics().horizontalAdvance(' ') * self.editor.indent_width)

    def set_language(self, language):
        if language in self.language_actions:
            self.language_actions[language].setChecked(True)
            self.editor.set_language(language)
            self._update_status()

    def _show_whitespace(self, enabled):
        option = self.editor.document().defaultTextOption()
        flags = option.flags()
        flag = QTextOption.Flag.ShowTabsAndSpaces
        option.setFlags(flags | flag if enabled else flags & ~flag)
        self.editor.document().setDefaultTextOption(option)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape and self.search_panel.isVisible():
                self.search_panel.hide()
                self.editor.setFocus()
                return True
            if watched is self.search and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.find(bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
                return True
            if watched is self.replacement and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.replace_one()
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.search_panel.isVisible():
            self.search_panel.hide()
            self.editor.setFocus()
            event.accept()
        else:
            super().keyPressEvent(event)

    def _focus_search(self, replace=False):
        self.search_panel.show()
        self.replace_panel.setVisible(replace)
        self.search.setFocus()
        self.search.selectAll()

    def _search_changed(self, *_):
        self._last_match = None
        self.search_error.clear()
        if self.search.text():
            self.find(from_start=True)

    def _pattern(self):
        query = self.search.text()
        if not query:
            return None
        try:
            pattern = query if self.regex.isChecked() else re.escape(query)
            if self.whole.isChecked():
                pattern = r'(?<!\w)(?:' + pattern + r')(?!\w)'
            result = re.compile(pattern, 0 if self.case.isChecked() else re.IGNORECASE)
            self.search_error.clear()
            return result
        except re.error as exc:
            self.search_error.setText(f'Ошибка regex: {exc}')
            return None

    @staticmethod
    def _qt_position(text, index):
        return len(text[:index].encode('utf-16-le')) // 2

    @staticmethod
    def _python_position(text, index):
        return len(text.encode('utf-16-le')[:index * 2].decode('utf-16-le'))

    def find(self, backwards=False, from_start=False):
        pattern = self._pattern()
        if pattern is None:
            if not self.search.text():
                self._focus_search()
            return
        text = self.editor.toPlainText()
        cursor = self.editor.textCursor()
        position = self._python_position(text, cursor.selectionStart() if backwards else cursor.selectionEnd())
        matches = pattern.finditer(text)
        chosen = fallback = None
        if backwards:
            for match in matches:
                fallback = match
                if from_start or match.end() <= position:
                    if not (self._last_match == match.span() and match.start() == match.end() == position):
                        chosen = match
            chosen = chosen or fallback
        else:
            for match in matches:
                if fallback is None:
                    fallback = match
                if from_start or match.start() >= position:
                    if self._last_match == match.span() and match.start() == match.end() == position and not from_start:
                        continue
                    chosen = match
                    break
            chosen = chosen or fallback
        if chosen is None:
            self.search_error.setText('Совпадений нет')
            self._last_match = None
            return
        self._last_match = chosen.span()
        cursor.setPosition(self._qt_position(text, chosen.start()))
        cursor.setPosition(self._qt_position(text, chosen.end()), QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()

    def replace_one(self):
        if self.editor.isReadOnly():
            return
        pattern = self._pattern()
        if pattern is None:
            return
        text = self.editor.toPlainText()
        cursor = self.editor.textCursor()
        start = self._python_position(text, cursor.selectionStart())
        end = self._python_position(text, cursor.selectionEnd())
        match = pattern.match(text, start)
        if match is None or match.end() != end or (start == end and self._last_match != (start, end)):
            self.find()
            return
        try:
            replacement = match.expand(self.replacement.text()) if self.regex.isChecked() else self.replacement.text()
        except (re.error, IndexError) as exc:
            self.search_error.setText(f'Ошибка замены: {exc}')
            return
        cursor.insertText(replacement)
        self.editor.setTextCursor(cursor)
        self._last_match = (start + len(replacement), start + len(replacement)) if start == end else None
        self.find()

    def replace_all(self):
        if self.editor.isReadOnly():
            return
        pattern = self._pattern()
        if pattern is None:
            return
        text = self.editor.toPlainText()
        try:
            changes = [(match.start(), match.end(), match.expand(self.replacement.text())
                        if self.regex.isChecked() else self.replacement.text()) for match in pattern.finditer(text)]
        except (re.error, IndexError) as exc:
            self.search_error.setText(f'Ошибка замены: {exc}')
            return
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        # Переводим индексы один раз: emoji занимает две позиции QTextCursor.
        offsets = [0]
        for char in text:
            offsets.append(offsets[-1] + (2 if ord(char) > 0xffff else 1))
        for start, end, replacement in reversed(changes):
            cursor.setPosition(offsets[start])
            cursor.setPosition(offsets[end], QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(replacement)
        cursor.endEditBlock()
        self._last_match = None
        self.search_error.setText(f'Заменено: {len(changes)}')

    def goto_line(self):
        line, ok = QInputDialog.getInt(self, 'Переход к строке', 'Номер строки:',
                                       self.editor.textCursor().blockNumber() + 1, 1, self.editor.blockCount())
        if ok:
            self.editor.setTextCursor(QTextCursor(self.editor.document().findBlockByNumber(line - 1)))
            self.editor.setFocus()

    def _update_status(self):
        cursor = self.editor.textCursor()
        count = len(cursor.selectedText())
        self.status.setText(f'Строка {cursor.blockNumber() + 1}, столбец {cursor.positionInBlock() + 1}'
                            f'  ·  Выделено: {count}  ·  Строк: {self.editor.blockCount()}'
                            f'  ·  {self.editor.language}')
