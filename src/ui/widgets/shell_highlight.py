"""Stateful shell highlighting shared by command editors and previews."""

import re

from PySide6.QtGui import QColor, QFont, QPalette, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import QApplication


_WORDS = re.compile(r'[\w./:+-]+')
_VARIABLE = re.compile(r'\$(?:\{[^}]*\}|[A-Za-z_]\w*|[0-9]+|[@*#?$!_-])')
_PARAM = re.compile(r'%[\w]+%')
_HEREDOC = re.compile(r"<<(-)?\s*(?:'([^']+)'|\"([^\"]+)\"|([\w.-]+)|\\([\w.-]+))")
_KEYWORDS = set('if then else elif fi case esac for select while until do done in function time coproc'.split())
_COMMANDS = set(('echo printf read cd pwd pushd popd dirs let eval exec exit export readonly set shift '
                 'source trap umask unset alias unalias bg fg jobs kill wait break continue return test '
                 'true false type which grep sed awk ls cat mv cp rm mkdir chmod chown sudo apt yum dnf '
                 'ssh scp find xargs tar gzip systemctl journalctl curl wget python python3 bash sh '
                 'sleep touch tee cut sort head tail wc mount getent ip hostname').split())


def shell_spans(text, state=('', ())):
    """Return Python-indexed tokens and the quote/heredoc state for the next line.

    This is a display lexer, never a parser or an execution/validation mechanism.
    """
    quote, pending = state
    spans = []
    pending = list(pending)
    if pending:
        delimiter, strip_tabs, interpolate = pending[0]
        if (text.lstrip('\t') if strip_tabs else text) == delimiter:
            return [(0, len(text), 'operator')], ('', tuple(pending[1:]))
        spans.append((0, len(text), 'string'))
        if interpolate:
            for match in _VARIABLE.finditer(text):
                if not _escaped(text, match.start()):
                    spans.append((match.start(), match.end(), 'variable'))
        for match in _PARAM.finditer(text):
            spans.append((match.start(), match.end(), 'parameter'))
        return spans, ('', tuple(pending))

    i = 0
    while i < len(text):
        start = i
        if quote or text[i] in "'\"" or text.startswith("$'", i):
            if not quote:
                quote = "$'" if text.startswith("$'", i) else text[i]
                i += len(quote)
            delimiter = "'" if quote == "$'" else quote
            content_start = i
            while i < len(text):
                if text[i] == delimiter:
                    break
                if text[i] == '\\' and quote in ('"', "$'"):
                    i += 2
                else:
                    i += 1
            end = min(i + 1, len(text))
            spans.append((start, end, 'string'))
            if quote == '"':
                for match in _VARIABLE.finditer(text, content_start, i):
                    if not _escaped(text, match.start()):
                        spans.append((match.start(), match.end(), 'variable'))
            for match in _PARAM.finditer(text, content_start, i):
                spans.append((match.start(), match.end(), 'parameter'))
            if i < len(text):
                quote = ''
            i = end
            continue
        if text[i] == '\\':
            i += 2
            continue
        if text[i] == '#' and (i == 0 or text[i - 1].isspace() or text[i - 1] in ';|&()'):
            spans.append((i, len(text), 'shebang' if i == 0 and text.startswith('#!') else 'comment'))
            break
        match = _PARAM.match(text, i) or _VARIABLE.match(text, i)
        if match:
            spans.append((i, match.end(), 'parameter' if text[i] == '%' else 'variable'))
            i = match.end()
            continue
        match = _HEREDOC.match(text, i) if text.startswith('<<', i) and not text.startswith('<<<', i) and (i == 0 or text[i - 1] != '<') else None
        if match:
            pending.append((match[2] or match[3] or match[4] or match[5], bool(match[1]), bool(match[4])))
            spans.append((i, match.end(), 'operator'))
            i = match.end()
            continue
        match = _WORDS.match(text, i)
        if match:
            word = match[0]
            role = ('keyword' if word in _KEYWORDS else 'command' if word in _COMMANDS
                    else 'variable' if match.end() < len(text) and text[match.end()] == '='
                    else 'option' if word.startswith('-') else 'number' if word.isdigit() else None)
            if role:
                spans.append((i, match.end(), role))
            i = match.end()
            continue
        if text[i] in '|&;()<>=!{}':
            spans.append((i, i + 1, 'operator'))
        i += 1
    return spans, (quote, tuple(pending))


def _escaped(text, pos):
    start = pos
    while start > 0 and text[start - 1] == '\\':
        start -= 1
    return (pos - start) % 2 == 1


def syntax_formats(palette: QPalette | None = None) -> dict[str, QTextCharFormat]:
    """Создать общую палитру подсветки для светлой и тёмной темы."""
    palette = palette or (QApplication.palette() if QApplication.instance() else QPalette())
    dark = palette.color(QPalette.ColorRole.Base).lightness() < 128
    colors = ({'keyword': '#82aaff', 'command': '#c792ea', 'comment': '#91a4ae',
               'string': '#b5d68a', 'variable': '#f6ad82', 'parameter': '#80deea',
               'operator': '#e5c07b', 'option': '#89c5e8', 'number': '#f6ad82', 'shebang': '#91a4ae'}
              if dark else
              {'keyword': '#174ea6', 'command': '#7b279e', 'comment': '#607060',
               'string': '#27632a', 'variable': '#a54015', 'parameter': '#006978',
               'operator': '#805600', 'option': '#195e82', 'number': '#a54015', 'shebang': '#607060'})
    formats = {}
    for role, color in colors.items():
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if role in ('keyword', 'parameter', 'shebang'):
            fmt.setFontWeight(QFont.Weight.Bold)
        if role == 'comment':
            fmt.setFontItalic(True)
        formats[role] = fmt
    return formats


class BashSyntaxHighlighter(QSyntaxHighlighter):
    """Quote-aware Bash highlighter with Qt block state and UTF-16 offsets."""

    def __init__(self, document):
        super().__init__(document)
        self._states = {('', ()): 0}
        self._state_values = [('', ())]
        self.refresh_theme()

    def refresh_theme(self, palette=None):
        self.formats = syntax_formats(palette)
        self.rehighlight()

    def highlightBlock(self, text):
        previous = self.previousBlockState()
        state = self._state_values[previous] if 0 <= previous < len(self._state_values) else ('', ())
        spans, next_state = shell_spans(text, state)
        # QTextDocument indexes UTF-16 code units, Python indexes Unicode points.
        offsets = [0]
        for char in text:
            offsets.append(offsets[-1] + (2 if ord(char) > 0xffff else 1))
        for start, end, role in spans:
            self.setFormat(offsets[start], offsets[end] - offsets[start], self.formats[role])
        if next_state not in self._states:
            self._states[next_state] = len(self._state_values)
            self._state_values.append(next_state)
        self.setCurrentBlockState(self._states[next_state])
