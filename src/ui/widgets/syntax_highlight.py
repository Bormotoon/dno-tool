"""Контекстная подсветка форматов общего редактора кода."""

import os
import re
from collections.abc import Callable, Iterator
from itertools import groupby
from threading import Event

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Slot
from PySide6.QtGui import QPalette, QSyntaxHighlighter, QTextDocument
from pygments.lexer import DelegatingLexer, RegexLexer, bygroups, include
from pygments.lexers.css import CssLexer
from pygments.lexers.data import JsonLexer, YamlLexer
from pygments.lexers.html import HtmlLexer
from pygments.lexers.jsx import JsxLexer
from pygments.lexers.php import PhpLexer
from pygments.lexers.python import PythonLexer
from pygments.lexers.sql import SqlLexer
from pygments.token import Comment, Keyword, Literal, Name, Number, Operator, String, Text

from src.workers.gui_relay import post_to_gui

from .shell_highlight import BashSyntaxHighlighter, syntax_formats

_LineSpans = list[list[tuple[int, int, str]]]


class _SettingsLexer(RegexLexer):
    """Значения INI и dotenv с независимыми состояниями кавычек."""

    tokens = {
        'root': [
            (r'\s+', Text), (r'[;#][^\n]*', Comment.Single),
            (r'\[[^\]\n]+\]', Keyword),
            (r'(export)([ \t]+)', bygroups(Keyword, Text)),
            (r'([\w.-]+)([ \t]*)([=:])', bygroups(Name.Attribute, Text, Operator), 'value'),
            (r'[^\n]+', Text),
        ],
        'value': [
            (r'\n', Text, '#pop'),
            (r'[ \t]+[;#][^\n]*', Comment.Single),
            (r'"', String.Double, 'double'), (r"'", String.Single, 'single'),
            include('variable'),
            (r'[^\n\s\x22\x27$]+|[ \t]+|\$', String),
        ],
        'double': [
            (r'\\[^\n]', String.Escape), (r'"', String.Double, '#pop'),
            include('variable'), (r'[^\x22\\$]+|.', String.Double),
        ],
        'single': [(r"'", String.Single, '#pop'), (r"[^']+", String.Single)],
        'variable': [(r'\$(?:\{[^}\n]*\}|[A-Za-z_]\w*)', Name.Variable)],
    }


class _EnvLexer(_SettingsLexer):
    """В dotenv точка с запятой остаётся частью значения."""

    tokens = {
        **_SettingsLexer.tokens,
        'root': [
            (r'\s+', Text), (r'#[^\n]*', Comment.Single),
            (r'(export)([ \t]+)', bygroups(Keyword, Text)),
            (r'([\w.-]+)([ \t]*)(=)', bygroups(Name.Attribute, Text, Operator), 'value'),
            (r'[^\n]+', Text),
        ],
        'value': [
            (r'\n', Text, '#pop'), (r'[ \t]+#[^\n]*', Comment.Single),
            (r'"', String.Double, 'double'), (r"'", String.Single, 'single'),
            include('variable'), (r'[^\n\s\x22\x27$]+|[ \t]+|\$', String),
        ],
    }


class _PhpLexer(DelegatingLexer):
    """PHP-фрагменты и документы с HTML между PHP-блоками."""

    def __init__(self, **options: object) -> None:
        super().__init__(HtmlLexer, PhpLexer, **options)

    def get_tokens_unprocessed(self, text: str) -> Iterator[tuple[int, object, str]]:
        self.language_lexer.startinline = not re.search(r'<\?', text)
        yield from super().get_tokens_unprocessed(text)


class _TokenScanner:
    """Лексический разбор без доступа к Qt и документу редактора."""

    def __init__(self, lexer_class: type, options: dict) -> None:
        self._lexer = lexer_class(**options)

    def _role(self, token: object, value: str) -> str | None:
        if token in Comment:
            return 'comment'
        if token in Keyword:
            return 'keyword'
        if token in Name.Tag:
            return 'operator'
        if token in Name.Attribute or token in Name.Variable:
            return 'variable'
        if token in Name.Function or token in Name.Class or token in Name.Builtin:
            return 'command'
        if token in String:
            return 'string'
        if token in Number:
            return 'number'
        if token in Operator:
            return 'operator'
        return None

    def _tokens(self, text: str) -> Iterator[tuple[int, object, str]]:
        # Виртуальный перевод строки завершает однострочные комментарии PHP.
        for start, token, value in self._lexer.get_tokens_unprocessed(text + '\n'):
            if self._cancelled.is_set():
                return
            if start < len(text):
                yield start, token, value[:len(text) - start]

    def scan(self, text: str, block_count: int, cancelled: Event) -> _LineSpans | None:
        self._cancelled = cancelled
        lines = [[] for _ in range(block_count)]
        line, column, position = 0, 0, 0
        for start, token, value in self._tokens(text):
            if cancelled.is_set():
                return None
            # Лексеры возвращают исходные позиции без нормализации пробелов.
            if start > position:
                gap = text[position:start].split('\n')
                line += len(gap) - 1
                column = (column if len(gap) == 1 else 0) + len(gap[-1].encode('utf-16-le')) // 2
            role = self._role(token, value)
            parts = value.split('\n')
            for index, part in enumerate(parts):
                length = len(part.encode('utf-16-le')) // 2
                if role and length and line < len(lines):
                    lines[line].append((column, length, role))
                column += length
                if index < len(parts) - 1:
                    line += 1
                    column = 0
            position = start + len(value)
        return lines


class _ScanTask(QRunnable):
    """Разбор больших файлов вне GUI с отменой устаревшего снимка."""

    def __init__(self, scanner: _TokenScanner, text: str, blocks: int, revision: int,
                 owner: QObject, accept: Callable[[int, _LineSpans], None],
                 cancelled: Event) -> None:
        super().__init__()
        self.scanner, self.text, self.blocks = scanner, text, blocks
        self.revision, self.owner, self.accept = revision, owner, accept
        self.cancelled = cancelled

    def run(self) -> None:
        lines = self.scanner.scan(self.text, self.blocks, self.cancelled)
        if lines is not None and not self.cancelled.is_set():
            # Редактор мог закрыться: ретранслятор проверит это уже в GUI-потоке.
            post_to_gui(self.owner, self.accept, self.revision, lines)


class _CodeHighlighter(QSyntaxHighlighter):
    """Лексический снимок на правку; большие файлы разбираются в рабочем потоке."""

    lexer_class = PythonLexer
    lexer_options: dict = {}
    scanner_class = _TokenScanner
    _async_threshold = 64 * 1024

    def __init__(self, document: QTextDocument) -> None:
        super().__init__(None)
        self.setParent(document)
        self._revision = None
        self._text_revision = 0
        self._generation = 0
        self._lines: list = []
        self._cancelled = Event()
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.timeout.connect(self.rehighlight)
        self._cancel_on_destroy = lambda *_: self._cancelled.set()
        self.destroyed.connect(self._cancel_on_destroy)
        self.formats = syntax_formats()
        # Инвалидация должна предшествовать внутреннему обработчику Qt.
        self.setDocument(document)

    def setDocument(self, document: QTextDocument | None) -> None:
        previous = self.document()
        if previous is document:
            return
        self._refresh.stop()
        self._cancelled.set()
        if previous is not None:
            previous.contentsChange.disconnect(self._contents_changed)
        self._text_revision += 1
        self._revision = None
        self._lines = []
        if document is not None:
            document.contentsChange.connect(self._contents_changed)
        super().setDocument(document)

    def refresh_theme(self, palette: QPalette | None = None) -> None:
        self.formats = syntax_formats(palette)
        self.rehighlight()

    @Slot(int, int, int)
    def _contents_changed(self, position: int, removed: int, added: int) -> None:
        if not removed and not added:
            return
        self._text_revision += 1
        self._revision = None
        self._cancelled.set()
        self._lines = []
        # Завершающий разделитель может изменить токены предыдущих строк.
        self._refresh.start(0)

    @Slot(int, object)
    def _accept_scan(self, revision: int, lines: _LineSpans) -> None:
        if self.document() is None or revision != self._text_revision:
            return
        self._lines = lines
        self._generation = (self._generation + 1) % 2147483647
        self.rehighlight()

    def _prepare(self) -> None:
        document = self.document()
        if self._revision == self._text_revision:
            return
        self._revision = self._text_revision
        text = document.toPlainText()
        self._cancelled = Event()
        scanner = self.scanner_class(self.lexer_class, self.lexer_options)
        if len(text) > self._async_threshold:
            QThreadPool.globalInstance().start(_ScanTask(
                scanner, text, document.blockCount(), self._text_revision,
                self, self._accept_scan, self._cancelled))
        else:
            self._lines = scanner.scan(text, document.blockCount(), self._cancelled) or []
            self._generation = (self._generation + 1) % 2147483647

    def highlightBlock(self, text: str) -> None:
        self._prepare()
        line = self.currentBlock().blockNumber()
        if line < len(self._lines):
            for start, length, role in self._lines[line]:
                self.setFormat(start, length, self.formats[role])
        # Изменение открывающей конструкции обновляет также последующие блоки.
        self.setCurrentBlockState(self._generation)


class JsonHighlighter(_CodeHighlighter):
    lexer_class = JsonLexer


class PythonHighlighter(_CodeHighlighter):
    lexer_class = PythonLexer


class PhpHighlighter(_CodeHighlighter):
    lexer_class = _PhpLexer


class HtmlHighlighter(_CodeHighlighter):
    lexer_class = HtmlLexer


class CssHighlighter(_CodeHighlighter):
    lexer_class = CssLexer


class JavaScriptHighlighter(_CodeHighlighter):
    lexer_class = JsxLexer


class SqlHighlighter(_CodeHighlighter):
    lexer_class = SqlLexer


class IniHighlighter(_CodeHighlighter):
    lexer_class = _SettingsLexer


class ConfHighlighter(IniHighlighter):
    pass


class EnvHighlighter(IniHighlighter):
    lexer_class = _EnvLexer


class _YamlScanner(_TokenScanner):
    _ansible_keywords = frozenset(
        'hosts tasks name become become_user become_method when loop with_items with_dict '
        'with_list register notify tags vars args handlers pre_tasks post_tasks block rescue '
        'always delegate_to run_once ignore_errors changed_when failed_when environment'.split()
    )
    _jinja = re.compile(r'\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}', re.DOTALL)

    def _role(self, token: object, value: str) -> str | None:
        if token in Name.Tag:
            return 'keyword' if value in self._ansible_keywords else 'command'
        if token in String.Interpol:
            return 'variable'
        if token in Name.Constant or (token in Name.Variable and value.isspace()):
            return 'string'
        if token in Literal.Scalar.Plain:
            if value.lower() in ('true', 'false', 'yes', 'no', 'on', 'off', 'null', '~'):
                return 'keyword'
            if re.fullmatch(r'[-+]?(?:0[xX][\da-fA-F_]+|\d[\d_]*(?:\.\d*)?(?:[eE][-+]?\d+)?)', value):
                return 'number'
            return 'string'
        return super()._role(token, value)

    def _tokens(self, text: str) -> Iterator[tuple[int, object, str]]:
        # Jinja может пересекать несколько токенов YAML, в том числе кавычки.
        tokens = list(super()._tokens(text))
        template_parts, position = [], 0
        for start, token, value in tokens:
            if token in Comment:
                template_parts.extend((text[position:start], ' ' * len(value)))
                position = start + len(value)
        template_parts.append(text[position:])
        templates = iter(self._jinja.finditer(''.join(template_parts)))
        template = next(templates, None)
        # Обычный скаляр «true 123» является одной строкой, а не bool и числом.
        merged = []
        for plain, group in groupby(tokens, lambda item: item[1] in Literal.Scalar.Plain):
            if plain:
                items = list(group)
                merged.append((items[0][0], Literal.Scalar.Plain, ''.join(item[2] for item in items)))
            else:
                merged.extend(group)
        for start, token, value in merged:
            end = start + len(value)
            position = start
            while template and template.end() <= start:
                template = next(templates, None)
            while template and template.start() < end:
                left, right = max(position, template.start()), min(end, template.end())
                if left > position:
                    yield position, token, text[position:left]
                yield left, (token if token in Comment else String.Interpol), text[left:right]
                position = right
                if template.end() > end:
                    break
                template = next(templates, None)
            if position < end:
                yield position, token, text[position:end]


class YamlHighlighter(_CodeHighlighter):
    lexer_class = YamlLexer
    scanner_class = _YamlScanner


HIGHLIGHTERS = {
    'Bash': BashSyntaxHighlighter, 'Python': PythonHighlighter, 'JSON': JsonHighlighter,
    'YAML': YamlHighlighter, 'PHP': PhpHighlighter, 'HTML': HtmlHighlighter,
    'CSS': CssHighlighter, 'JavaScript': JavaScriptHighlighter, 'SQL': SqlHighlighter,
    'INI': IniHighlighter, 'Conf': ConfHighlighter, 'Env': EnvHighlighter,
}


def syntax_language(filename: str) -> str:
    """Определить язык одинаково для фабрики и редактора удалённых файлов."""
    name = os.fspath(filename).replace('\\', '/').rsplit('/', 1)[-1].lower()
    if name == '.env' or name.startswith('.env.') or name.endswith('.env'):
        return 'Env'
    if name in ('.bashrc', '.bash_profile', '.bash_login', '.profile', '.bash_logout'):
        return 'Bash'
    return {
        '.sh': 'Bash', '.bash': 'Bash', '.py': 'Python', '.pyw': 'Python',
        '.json': 'JSON', '.yaml': 'YAML', '.yml': 'YAML', '.php': 'PHP',
        '.html': 'HTML', '.htm': 'HTML', '.css': 'CSS', '.js': 'JavaScript',
        '.jsx': 'JavaScript', '.mjs': 'JavaScript', '.cjs': 'JavaScript',
        '.sql': 'SQL', '.ini': 'INI', '.cfg': 'INI', '.conf': 'Conf',
    }.get(os.path.splitext(name)[1], 'Обычный текст')


def get_syntax_highlighter(document: QTextDocument, filename: str) -> QSyntaxHighlighter | None:
    highlighter = HIGHLIGHTERS.get(syntax_language(filename))
    return highlighter(document) if highlighter else None
