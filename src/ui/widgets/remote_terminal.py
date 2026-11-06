"""Встроенный полноценный терминал (bash) для SFTP-диалога.

Открывает интерактивный shell поверх существующего ``paramiko.SSHClient``
и рендерит его в ``QTextEdit`` как экран терминала (ввод напрямую
в область вывода, без строки отправки). Поддерживается базовый набор
VT100/xterm-последовательностей: курсор, стирание, прокрутка, scrollback,
альтернативный экран (vim/htop/top), размер PTY при изменении окна,
SGR-цвета (16-color, 256-color, truecolor).
"""

import codecs
import posixpath
import re
import shlex
import socket
import threading
import time
from collections import deque
from typing import Optional

from PySide6.QtCore import QEvent, QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QFontDatabase,
    QPainter,
    QPalette,
    QResizeEvent,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.logger import logger
from src.ui.icons import set_themed_icon
from src.ui.widgets.device_list_toolbar import FLAT_TOOLBUTTON_QSS
from src.utils.filesystem.sftp_files import OperationControl

_FKEYS = [
    "\x1bOP",  # F1
    "\x1bOQ",  # F2
    "\x1bOR",  # F3
    "\x1bOS",  # F4
    "\x1b[15~",  # F5
    "\x1b[17~",  # F6
    "\x1b[18~",  # F7
    "\x1b[19~",  # F8
    "\x1b[20~",  # F9
    "\x1b[21~",  # F10
    "\x1b[23~",  # F11
    "\x1b[24~",  # F12
]


# ---------------------------------------------------------------------------
# SGR-атрибуты (цвета/стиль ячейки)
# ---------------------------------------------------------------------------
# fg/bg: None = default, 0..15 = ANSI 16-color, (256, N) = 256-color,
#        (257, r, g, b) = truecolor. flags: bitmask 1=bold, 2=italic,
#        4=underline, 8=reverse.
_DEFAULT_ATTR = (None, None, 0)

# ANSI 16-color палитра (xterm). Индексы 0..7 — базовые, 8..15 — яркие.
_ANSI_16 = [
    "#000000",
    "#aa0000",
    "#00aa00",
    "#aa5500",
    "#0000aa",
    "#aa00aa",
    "#00aaaa",
    "#aaaaaa",
    "#555555",
    "#ff5555",
    "#55ff55",
    "#ffff55",
    "#5555ff",
    "#ff55ff",
    "#55ffff",
    "#ffffff",
]


def _color_to_css(c, ansi_palette: list[str] | None = None) -> str | None:
    if c is None:
        return None
    ansi_palette = ansi_palette or _ANSI_16
    if isinstance(c, int):
        return ansi_palette[c] if 0 <= c < 16 else None
    if isinstance(c, tuple):
        if c[0] == 256:
            n = c[1]
            # 256-color палитра: 0..15 = ANSI16, 16..231 = 6x6x6 cube, 232..255 = grayscale
            if n < 16:
                return ansi_palette[n]
            if n < 232:
                i = n - 16
                r, g, b = i // 36, (i // 6) % 6, i % 6
                vals = [0, 0x5F, 0x87, 0xAF, 0xD7, 0xFF]
                return f"#{vals[r]:02X}{vals[g]:02X}{vals[b]:02X}"
            v = 0x08 + (n - 232) * 10
            return f"#{v:02X}{v:02X}{v:02X}"
        if c[0] == 257:
            _, r, g, b = c
            return f"#{r:02X}{g:02X}{b:02X}"
    return None


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class TerminalScreen:
    """Экран терминала: буфер, курсор, парсер ANSI/VT100, scrollback.

    Чистая логика без Qt — ``feed()`` принимает строку вывода и обновляет
    состояние; ``response`` заполняется, если терминал запросил ответ
    (DSR — отчёт о позиции курсора).

    Каждая ячейка хранится как список ``[char, fg, bg, flags]``. SGR-цвета
    (16-color, 256-color, truecolor) применяются к текущему атрибуту и
    наследуются новыми символами. Scrollback хранит HTML-строки (с цветами).
    """

    def __init__(self, rows: int = 24, cols: int = 80):
        self.rows = rows
        self.cols = cols
        self.screen: list[list[list]] = [
            [[" ", *_DEFAULT_ATTR] for _ in range(cols)] for _ in range(rows)
        ]
        self.cursor_row = 0
        self.cursor_col = 0
        self.saved_cursor = (0, 0)
        self.scroll_top = 0
        self.scroll_bottom = rows - 1
        self.scrollback: deque = deque(maxlen=2000)
        self._state = "normal"  # normal|esc|csi|osc|osc_st|charset
        self._csi_buf = ""
        self._osc_buf = ""
        self._saved_primary = None
        self._alt_active = False
        self.response: Optional[str] = None
        self.beep_pending = False
        self._cur_attr = _DEFAULT_ATTR  # (fg, bg, flags)

    # ------------------------------------------------------------------
    # Ввод
    # ------------------------------------------------------------------

    def feed(self, data: str) -> None:
        for ch in data:
            self._process_char(ch)

    def resize(self, cols: int, rows: int) -> None:
        cols = max(cols, 1)
        rows = max(rows, 1)
        blank = [" ", *_DEFAULT_ATTR]
        new_screen = []
        for r in range(rows):
            if r < len(self.screen):
                row = list(self.screen[r])
            else:
                row = []
            if len(row) < cols:
                row.extend([list(blank) for _ in range(cols - len(row))])
            else:
                del row[cols:]
            new_screen.append(row)
        self.screen = new_screen
        self.rows = rows
        self.cols = cols
        self.cursor_row = min(self.cursor_row, rows - 1)
        self.cursor_col = min(self.cursor_col, cols - 1)
        self.scroll_top = 0
        self.scroll_bottom = rows - 1

    # ------------------------------------------------------------------
    # Рендер
    # ------------------------------------------------------------------

    def render_text(self) -> str:
        # Scrollback-строки хранятся уже без хвостовых пробелов. Строки
        # текущего экрана rstrip'ить нельзя — иначе не виден пробел,
        # набранный в конце командной строки (виден только после следующего
        # символа), и курсор в paintEvent окажется не на своей ячейке.
        lines = [
            "".join(cell[0] for cell in line) if isinstance(line, list) else line
            for line in self.scrollback
        ]
        for row in self.screen:
            lines.append("".join(cell[0] for cell in row))
        return "\n".join(lines)

    def row_text(self, row: int) -> str:
        """Текст строки экрана (только символы, без атрибутов).

        rstrip справа безопасен для парсинга команд: хвостовые пробелы
        приглашения не несут смысла, а последний набранный символ не
        пропадёт — он уже в ячейках.
        """
        if not (0 <= row < self.rows):
            return ""
        return "".join(cell[0] for cell in self.screen[row]).rstrip()

    def render_html(self, ansi_palette: list[str] | None = None) -> str:
        """Вернуть HTML-документ для QTextEdit.setHtml().

        Цвета применяются группами (runs) соседних ячеек с одинаковым
        атрибутом — по одному ``<span>`` на группу, а не по ячейке.

        Каждая строка оборачивается в ``<p>``, а не соединяется через
        ``<br>``: только блочные элементы создают отдельные блоки
        ``QTextDocument``, на которые опирается ``_place_cursor``
        (поиск по номеру блока = scrollback + cursor_row).
        """
        lines_html: list[str] = []
        # margin:0; padding:0 убирает межстрочные отступы <p> (~12px сверху/снизу),
        # иначе строки терминала выглядят двойными.
        para_style = "margin:0; padding:0; line-height:1; white-space:pre"
        for line in self.scrollback:
            if isinstance(line, list):
                line = self._row_to_html(line, ansi_palette)
            lines_html.append(
                f"<p style='{para_style}'>{line if line else '&nbsp;'}</p>"
            )
        for row in self.screen:
            lines_html.append(
                f"<p style='{para_style}'>"
                f"{self._row_to_html(row, ansi_palette) or '&nbsp;'}</p>"
            )
        body = "".join(lines_html)
        return f"<html><body>{body}</body></html>"

    def _row_to_html(
        self, row: list[list], ansi_palette: list[str] | None = None
    ) -> str:
        if not row:
            return ""
        parts: list[str] = []
        cur_attr = None
        cur_chars: list[str] = []
        for cell in row:
            ch, fg, bg, flags = cell
            attr = (fg, bg, flags)
            if attr != cur_attr:
                if cur_chars:
                    parts.append(self._span(cur_attr, "".join(cur_chars), ansi_palette))
                cur_chars = [ch]
                cur_attr = attr
            else:
                cur_chars.append(ch)
        if cur_chars:
            parts.append(self._span(cur_attr, "".join(cur_chars), ansi_palette))
        return "".join(parts) or "&nbsp;"

    @staticmethod
    def _span(attr: tuple, text: str, ansi_palette: list[str] | None = None) -> str:
        if attr == _DEFAULT_ATTR or not text:
            return _html_escape(text)
        fg, bg, flags = attr
        styles: list[str] = []
        if flags & 1:
            styles.append("font-weight: bold")
        if flags & 2:
            styles.append("font-style: italic")
        if flags & 4:
            styles.append("text-decoration: underline")
        # reverse: меняем fg/bg местами
        eff_fg, eff_bg = fg, bg
        if flags & 8:
            eff_fg, eff_bg = bg, fg
        fg_css = _color_to_css(eff_fg, ansi_palette)
        bg_css = _color_to_css(eff_bg, ansi_palette)
        if fg_css:
            styles.append(f"color: {fg_css}")
        if bg_css:
            styles.append(f"background-color: {bg_css}")
        if not styles:
            return _html_escape(text)
        return f'<span style="{"; ".join(styles)}">{_html_escape(text)}</span>'

    # ------------------------------------------------------------------
    # Примитивы экрана
    # ------------------------------------------------------------------

    def _blank_cell(self) -> list:
        return (
            [" ", *self._cur_attr]
            if self._cur_attr != _DEFAULT_ATTR
            else [" ", *_DEFAULT_ATTR]
        )

    def _put_char(self, ch: str) -> None:
        if self.cursor_col >= self.cols:
            self.cursor_col = 0
            if self.cursor_row == self.scroll_bottom:
                self._scroll_up()
            else:
                self.cursor_row += 1
        self.screen[self.cursor_row][self.cursor_col] = [ch, *self._cur_attr]
        self.cursor_col += 1

    def _linefeed(self) -> None:
        if self.cursor_row == self.scroll_bottom:
            self._scroll_up()
        else:
            self.cursor_row += 1

    def _scroll_up(self) -> None:
        row = self.screen.pop(self.scroll_top)
        if self.scroll_top == 0 and self.scroll_bottom == self.rows - 1:
            # Сохраняем строку в scrollback. rstrip хвостовые пробелы.
            plain = "".join(cell[0] for cell in row).rstrip()
            if not plain:
                self.scrollback.append("")
            else:
                trimmed = row[: len(plain)]
                self.scrollback.append([list(cell) for cell in trimmed])
        blank = [" ", *_DEFAULT_ATTR]
        self.screen.insert(self.scroll_bottom, [list(blank) for _ in range(self.cols)])

    def _scroll_down(self) -> None:
        self.screen.pop(self.scroll_bottom)
        blank = [" ", *_DEFAULT_ATTR]
        self.screen.insert(self.scroll_top, [list(blank) for _ in range(self.cols)])

    def _reverse_index(self) -> None:
        if self.cursor_row == self.scroll_top:
            self._scroll_down()
        else:
            self.cursor_row -= 1

    def _clear_screen(self) -> None:
        blank = [" ", *_DEFAULT_ATTR]
        self.screen = [
            [list(blank) for _ in range(self.cols)] for _ in range(self.rows)
        ]

    def _enter_alt(self) -> None:
        if self._alt_active:
            return
        self._saved_primary = {
            "screen": [[list(c) for c in r] for r in self.screen],
            "cursor": (self.cursor_row, self.cursor_col),
        }
        self._alt_active = True
        self._clear_screen()
        self.scrollback.clear()
        self.cursor_row = 0
        self.cursor_col = 0

    def _exit_alt(self) -> None:
        if not self._alt_active or self._saved_primary is None:
            return
        saved = self._saved_primary
        self.screen = saved["screen"]
        self.cursor_row, self.cursor_col = saved["cursor"]
        self._saved_primary = None
        self._alt_active = False

    # ------------------------------------------------------------------
    # SGR (Select Graphic Rounding) — обработка CSI ... m
    # ------------------------------------------------------------------

    def _apply_sgr(self, parts: list[int]) -> None:
        fg, bg, flags = self._cur_attr
        i = 0
        n = len(parts)
        if not parts:
            parts = [0]
        while i < n:
            code = parts[i]
            if code == 0:
                fg, bg, flags = None, None, 0
            elif code == 1:
                flags |= 1
            elif code == 3:
                flags |= 2
            elif code == 4:
                flags |= 4
            elif code == 5:
                pass  # blink — игнорируем
            elif code == 7:
                flags |= 8
            elif code == 22:
                flags &= ~1
            elif code == 23:
                flags &= ~2
            elif code == 24:
                flags &= ~4
            elif code == 25:
                pass  # blink off
            elif code == 27:
                flags &= ~8
            elif 30 <= code <= 37:
                fg = code - 30
            elif code == 38:
                # extended fg: 38;5;N или 38;2;R;G;B
                if i + 1 < n:
                    mode = parts[i + 1]
                    if mode == 5 and i + 2 < n:
                        fg = (256, parts[i + 2])
                        i += 2
                    elif mode == 2 and i + 4 < n:
                        fg = (257, parts[i + 2], parts[i + 3], parts[i + 4])
                        i += 4
            elif code == 39:
                fg = None
            elif 40 <= code <= 47:
                bg = code - 40
            elif code == 48:
                if i + 1 < n:
                    mode = parts[i + 1]
                    if mode == 5 and i + 2 < n:
                        bg = (256, parts[i + 2])
                        i += 2
                    elif mode == 2 and i + 4 < n:
                        bg = (257, parts[i + 2], parts[i + 3], parts[i + 4])
                        i += 4
            elif code == 49:
                bg = None
            elif 90 <= code <= 97:
                fg = code - 90 + 8
            elif 100 <= code <= 107:
                bg = code - 100 + 8
            i += 1
        self._cur_attr = (fg, bg, flags)

    # ------------------------------------------------------------------
    # Парсер
    # ------------------------------------------------------------------

    def _process_char(self, ch: str) -> None:
        st = self._state
        if st == "normal":
            if ch == "\x1b":
                self._state = "esc"
            elif ch == "\r":
                self.cursor_col = 0
            elif ch == "\n" or ch == "\x0b" or ch == "\x0c":
                self._linefeed()
            elif ch == "\b":
                if self.cursor_col > 0:
                    self.cursor_col -= 1
            elif ch == "\t":
                self.cursor_col = min(((self.cursor_col // 8) + 1) * 8, self.cols - 1)
            elif ch == "\a":
                self.beep_pending = True
            elif ch in ("\x00", "\x07"):
                pass
            else:
                self._put_char(ch)
        elif st == "esc":
            if ch == "[":
                self._state = "csi"
                self._csi_buf = ""
            elif ch == "]":
                self._state = "osc"
                self._osc_buf = ""
            elif ch == "M":
                self._reverse_index()
                self._state = "normal"
            elif ch == "D":
                self._linefeed()
                self._state = "normal"
            elif ch == "E":
                self.cursor_col = 0
                self._linefeed()
                self._state = "normal"
            elif ch == "7":
                self.saved_cursor = (self.cursor_row, self.cursor_col)
                self._state = "normal"
            elif ch == "8":
                self.cursor_row, self.cursor_col = self.saved_cursor
                self._state = "normal"
            elif ch == "c":
                self._clear_screen()
                self.scrollback.clear()
                self.cursor_row = 0
                self.cursor_col = 0
                self._cur_attr = _DEFAULT_ATTR
                self._state = "normal"
            elif ch == "[" or ch == "]":
                pass  # уже обработано выше
            elif ch == "(" or ch == ")":
                self._state = "charset"
            else:
                self._state = "normal"
        elif st == "csi":
            if ch.isdigit() or ch in ";?:<=>!":
                self._csi_buf += ch
            elif ch.isalpha() or ch in "@`":
                self._dispatch_csi(self._csi_buf, ch)
                self._state = "normal"
            else:
                self._state = "normal"
        elif st == "osc":
            if ch == "\x07":
                self._state = "normal"
            elif ch == "\x1b":
                self._state = "osc_st"
            else:
                self._osc_buf += ch
        elif st == "osc_st":
            self._state = "normal"
        elif st == "charset":
            self._state = "normal"

    def _dispatch_csi(self, buf: str, final: str) -> None:
        private = ""
        if buf and buf[0] in "?<=>":
            private = buf[0]
            buf = buf[1:]
        parts = [int(p) if p else 0 for p in buf.split(";")] if buf else []

        def p(idx: int, default: int = 1) -> int:
            if idx < len(parts) and parts[idx]:
                return parts[idx]
            return default

        if final == "A":
            self.cursor_row = max(0, self.cursor_row - p(0))
        elif final == "B":
            self.cursor_row = min(self.rows - 1, self.cursor_row + p(0))
        elif final == "C":
            self.cursor_col = min(self.cols - 1, self.cursor_col + p(0))
        elif final == "D":
            self.cursor_col = max(0, self.cursor_col - p(0))
        elif final in ("H", "f"):
            self.cursor_row = min(p(0) - 1, self.rows - 1) if p(0) > 0 else 0
            self.cursor_col = min(p(1) - 1, self.cols - 1) if p(1) > 0 else 0
        elif final == "G":
            self.cursor_col = min(p(0) - 1, self.cols - 1) if p(0) > 0 else 0
        elif final == "d":
            self.cursor_row = min(p(0) - 1, self.rows - 1) if p(0) > 0 else 0
        elif final == "J":
            # По умолчанию (без параметра) — CSI 0J: стереть от курсора до
            # конца экрана. p(0) вернул бы 1 (CSI 1J = до начала строки).
            n = parts[0] if parts and parts[0] else 0
            if n == 3:
                self.scrollback.clear()
            elif n == 2:
                self._clear_screen()
            elif n == 1:
                for r in range(self.cursor_row):
                    self._clear_row(r)
                self._clear_row_partial(self.cursor_row, 0, self.cursor_col + 1)
            else:
                self._clear_row_partial(self.cursor_row, self.cursor_col, self.cols)
                for r in range(self.cursor_row + 1, self.rows):
                    self._clear_row(r)
        elif final == "K":
            # CSI K без параметра = CSI 0K (стереть до конца строки),
            # а не CSI 1K (стереть от начала до курсора).
            n = parts[0] if parts and parts[0] else 0
            if n == 2:
                self._clear_row(self.cursor_row)
            elif n == 1:
                self._clear_row_partial(self.cursor_row, 0, self.cursor_col + 1)
            else:
                self._clear_row_partial(self.cursor_row, self.cursor_col, self.cols)
        elif final == "X":
            n = p(0)
            row = self.screen[self.cursor_row]
            blank = self._blank_cell()
            for i in range(min(n, self.cols - self.cursor_col)):
                row[self.cursor_col + i] = list(blank)
        elif final == "P":
            n = min(p(0), self.cols - self.cursor_col)
            row = self.screen[self.cursor_row]
            del row[self.cursor_col : self.cursor_col + n]
            blank = self._blank_cell()
            row.extend([list(blank) for _ in range(n)])
        elif final == "@":
            n = p(0)
            row = self.screen[self.cursor_row]
            blank = self._blank_cell()
            for _ in range(n):
                row.insert(self.cursor_col, list(blank))
            del row[self.cols :]
        elif final == "L":
            n = p(0)
            blank = [" ", *_DEFAULT_ATTR]
            for _ in range(n):
                self.screen.pop(self.scroll_bottom)
                self.screen.insert(
                    self.cursor_row, [list(blank) for _ in range(self.cols)]
                )
        elif final == "M":
            n = p(0)
            blank = [" ", *_DEFAULT_ATTR]
            for _ in range(n):
                self.screen.pop(self.cursor_row)
                self.screen.insert(
                    self.scroll_bottom, [list(blank) for _ in range(self.cols)]
                )
        elif final == "s":
            self.saved_cursor = (self.cursor_row, self.cursor_col)
        elif final == "u":
            self.cursor_row, self.cursor_col = self.saved_cursor
        elif final == "r":
            top = max(0, parts[0] - 1) if parts and parts[0] else 0
            if len(parts) >= 2 and parts[1]:
                bottom = min(self.rows - 1, parts[1] - 1)
            else:
                bottom = self.rows - 1
            if top < bottom:
                self.scroll_top, self.scroll_bottom = top, bottom
            else:
                self.scroll_top, self.scroll_bottom = 0, self.rows - 1
            self.cursor_row, self.cursor_col = 0, 0
        elif final == "n" and parts and parts[0] == 6:
            self.response = f"\x1b[{self.cursor_row + 1};{self.cursor_col + 1}R"
        elif final == "m":
            self._apply_sgr(parts)
        elif final in ("h", "l") and private == "?":
            mode = p(0)
            if mode in (47, 1047, 1049):
                if final == "h":
                    self._enter_alt()
                else:
                    self._exit_alt()
        elif final in ("h", "l"):
            pass  # остальные режимы игнорируем

    # ------------------------------------------------------------------
    # Очистка с сохранением атрибутов
    # ------------------------------------------------------------------

    def _clear_row(self, r: int) -> None:
        blank = self._blank_cell()
        self.screen[r] = [list(blank) for _ in range(self.cols)]

    def _clear_row_partial(self, r: int, start: int, end: int) -> None:
        row = self.screen[r]
        blank = self._blank_cell()
        for c in range(max(start, 0), min(end, self.cols)):
            row[c] = list(blank)


class RemoteShellWorker(QThread):
    """Фоновый поток: читает сырой вывод интерактивного SSH-шелла."""

    output_ready = Signal(str)
    closed = Signal(str)

    def __init__(self, ssh, parent=None):
        super().__init__(parent)
        self._ssh = ssh
        self._channel = None
        self._running = False
        self._stop_event = threading.Event()

    def run(self) -> None:
        try:
            transport = self._ssh.get_transport()
            if transport is None:
                raise ConnectionError("Нет SSH-соединения")
            control = OperationControl(self._stop_event.is_set, time.monotonic() + 10)
            control.check()
            self._channel = transport.open_session(timeout=10)
            self._channel.settimeout(0.5)
            with control.watch(self._channel):
                self._channel.get_pty(term="xterm-256color", width=80, height=24)
                self._channel.invoke_shell()
            self._running = not self._stop_event.is_set()
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while self._running and not self._stop_event.is_set():
                try:
                    data = self._channel.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                text = decoder.decode(data)
                if text:
                    self.output_ready.emit(text)
        except Exception as exc:
            if not self._stop_event.is_set():
                logger.error(f"Ошибка терминала: {exc}")
        finally:
            self._running = False
            if self._channel is not None:
                self._channel.close()
            self.closed.emit("Сессия закрыта")

    def send(self, text: str) -> None:
        if not self._channel or not self._running:
            return
        try:
            self._channel.send(text)
        except Exception as e:
            logger.error(f"Ошибка отправки в терминал: {e}")

    def resize_pty(self, cols: int, rows: int) -> None:
        if not self._channel or not self._running:
            return
        try:
            self._channel.resize_pty(width=cols, height=rows)
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()


class TerminalView(QTextEdit):
    """Экран терминала: ввод с клавиатуры напрямую в канал + рендер.

    Наследуется от ``QTextEdit`` (не ``QPlainTextEdit``), т.к. только
    ``QTextEdit`` поддерживает ``setHtml()`` для отрисовки SGR-цветов.
    """

    #: Пользователь нажал Enter на строке с командой ``cd``; аргумент —
    #: целевой каталог (относительные пути уже развёрнуты в абсолютные).
    commandEntered = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: Optional[RemoteShellWorker] = None
        self._pending_start = None
        self._screen = TerminalScreen(24, 80)
        self._last_size = (0, 0)
        self._follow = True
        self._placed_pos = -1
        # Отслеживание cwd шелла (для синхронизации с файловой панелью).
        # Подтверждается извне (set_shell_cwd) — команда в терминале может
        # упасть, поэтому cwd меняется только по факту успешного перехода.
        self._shell_cwd = "/"
        self._shell_oldpwd = "/"
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(40)
        self._render_timer.timeout.connect(self._render)
        self._pending_render = False
        self._last_html = ""

        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        # Встроенный каретка Qt отключён — рисуем свой блок-курсор (paintEvent)
        self.setCursorWidth(0)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # SGR-цвета терминала имеют приоритет над bash-подсветкой:
        # QSyntaxHighlighter перекрашивает текст независимо от HTML,
        # что ломает цвета, выставленные программой (ls --color и т.п.).
        # Поэтому хайлайтер не подключаем.

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
        ):
            self._last_html = ""
            self._schedule_render()

    def _ansi_palette(self) -> list[str]:
        palette = list(_ANSI_16)
        if self.palette().color(QPalette.ColorRole.Base).lightness() < 128:
            palette[4] = "#6ea8fe"
            palette[12] = "#9ecbff"
        return palette

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._worker is None:
            return
        # Блок-курсор на позиции терминального курсора (документный курсор
        # синхронизируется в _render / mousePressEvent — не трогаем его здесь).
        cr = self.cursorRect()
        if cr.isNull() or cr.height() <= 0:
            return
        r, c = self._screen.cursor_row, self._screen.cursor_col
        ch = " "
        if 0 <= r < self._screen.rows and 0 <= c < self._screen.cols:
            cell = self._screen.screen[r][c]
            ch = cell[0] if isinstance(cell, list) else cell
        fm = self.fontMetrics()
        cw = max(fm.horizontalAdvance(" "), 1)
        block_rect = QRect(cr.x(), cr.y(), cw, cr.height())
        pal = self.palette()
        p = QPainter(self.viewport())
        p.fillRect(block_rect, pal.color(QPalette.ColorRole.Text))
        if ch != " ":
            p.setPen(pal.color(QPalette.ColorRole.Base))
            p.setFont(self.font())
            p.drawText(block_rect, Qt.AlignmentFlag.AlignCenter, ch)
        p.end()

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------

    def start(self, ssh, host: str = "", initial_cwd: str = "/") -> None:
        if self._worker is not None:
            self.stop()
            self._pending_start = (ssh, host, initial_cwd)
            return
        self._screen = TerminalScreen(24, 80)
        self._last_size = (0, 0)
        self._follow = True
        self._placed_pos = -1
        self._last_html = ""
        self._shell_cwd = initial_cwd or "/"
        self._shell_oldpwd = self._shell_cwd
        self.setPlainText("")
        self._worker = RemoteShellWorker(ssh, self)
        self._worker.output_ready.connect(self._on_output)
        self._worker.closed.connect(self._on_closed)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()
        self._update_size()

    def stop(self) -> None:
        self._pending_start = None
        if self._worker is not None:
            self._worker.stop()

    def clear(self) -> None:
        self._screen = TerminalScreen(self._screen.rows, self._screen.cols)
        self._placed_pos = -1
        self._last_html = ""
        self._render()

    # ------------------------------------------------------------------
    # Вывод
    # ------------------------------------------------------------------

    def _on_output(self, data: str) -> None:
        self._screen.feed(data)
        if self._screen.response:
            if self._worker:
                self._worker.send(self._screen.response)
            self._screen.response = None
        if self._screen.beep_pending:
            self._screen.beep_pending = False
            QApplication.beep()
        self._schedule_render()

    def _schedule_render(self) -> None:
        if not self._pending_render:
            self._pending_render = True
            self._render_timer.start()

    def _render(self) -> None:
        self._pending_render = False
        html = self._screen.render_html(self._ansi_palette())
        if html != self._last_html:
            self._placed_pos = -1
            self._last_html = html
            self.setHtml(html)
            # setHtml сбрасывает курсор в начало — установим после.
        self._place_cursor()
        if self._follow:
            self.ensureCursorVisible()

    def _place_cursor(self) -> None:
        line_idx = len(self._screen.scrollback) + self._screen.cursor_row
        doc = self.document()
        block = doc.findBlockByNumber(line_idx)
        if not block.isValid():
            block = doc.lastBlock()
        # cursor_col может указывать за пределы rstrip-нутой строки (пробел в
        # конце приглашения) — ограничиваем позицию концом блока, иначе курсор
        # уедет на начало следующей строки.
        block_end = block.position() + max(block.length() - 1, 0)
        pos = min(block.position() + self._screen.cursor_col, block_end)
        pos = max(pos, 0)
        # Сравниваем и с закешированным значением, и с реальной позицией
        # документного курсора (его мог сдвинуть клик мыши).
        if pos == self._placed_pos and pos == self.textCursor().position():
            return
        self._placed_pos = pos
        tc = QTextCursor(doc)
        tc.setPosition(pos)
        self.setTextCursor(tc)

    def _on_closed(self, message: str) -> None:
        self._screen.feed(f"\n*** {message} ***\n")
        self._render()

    # ------------------------------------------------------------------
    # Ввод
    # ------------------------------------------------------------------

    def _send(self, text: str) -> None:
        if self._worker:
            self._worker.send(text)

    def send_command(self, text: str) -> None:
        """Отправить команду в шелл (публичный API для внешней синхронизации).

        Безопасен при незапущенном воркере — no-op.
        """
        if self._worker:
            self._worker.send(text)

    # ------------------------------------------------------------------
    # Отслеживание cwd шелла
    # ------------------------------------------------------------------

    def set_shell_cwd(self, path: str) -> None:
        """Подтвердить успешную смену каталога шелла (извне, после валидации).

        Вызывается диалогом, когда целевой каталог реально существует:
        oldpwd ← прежний cwd, cwd ← path. До подтверждения относительные
        пути в следующих командах разворачиваются от старого cwd — что
        корректно, т.к. несостоявшийся cd каталог не меняет.
        """
        if path and path != self._shell_cwd:
            self._shell_oldpwd = self._shell_cwd
            self._shell_cwd = path

    def _detect_cd_command(self) -> None:
        """Перехват Enter: распознать ``cd`` в текущей командной строке.

        Текст берётся со строки экрана, где стоит курсор, — в нём видно
        команду вместе с Tab-дополнением. Распознаётся последний ``cd``
        в строке как отдельное слово. Эмитит ``commandEntered(путь)``
        с развёрнутым абсолютным путём; подтверждение (set_shell_cwd) —
        на стороне диалога после проверки каталога.
        """
        if self._screen._alt_active:
            return  # альтернативный экран (vim/htop) — «cd» в тексте не команда
        line = self._screen.row_text(self._screen.cursor_row)
        if not line:
            return
        target = self._parse_cd(line)
        if target is None:
            return
        self.commandEntered.emit(target)

    # Приглашения bash: [user@host dir]#, user@host:dir#, /path #, bare $ / #
    _PROMPT_PATTERNS = (
        re.compile(r"^\s*\[[^\]]*@[\w.\-]+[^\]]*\]\s*[#$]\s"),
        re.compile(r"^\s*[\w.\-]+@[\w.\-]+:\S*\s*[#$]\s"),
        re.compile(r"^\s*[~/][\w./\-]*\s+[$#]\s"),
        re.compile(r"^\s*[$#]\s"),
    )

    @classmethod
    def _strip_prompt(cls, line: str) -> str:
        """Отрезать приглашение bash в начале строки.

        Консервативно: снимаются только узнаваемые формы PS1 (со скобками,
        с ``user@host``, с путём или голые ``$``/``#``). ``#`` внутри команды
        (комментарий) приглашением не считается.
        """
        for pat in cls._PROMPT_PATTERNS:
            m = pat.match(line)
            if m:
                return line[m.end() :]
        return line

    @classmethod
    def _parse_cd(cls, line: str) -> Optional[str]:
        """Извлечь целевой каталог из команды ``cd`` в строке.

        Поддерживаются: ``cd`` (→ home — путь неизвестен, возвращается
        None), ``cd -`` (→ oldpwd обрабатывает вызывающий), ``cd путь``,
        кавычки, ``; cd ...`` / ``&& cd ...`` в хвосте составной команды.
        Возвращает абсолютный путь или None (не cd / неоднозначно).
        """
        line = cls._strip_prompt(line)
        # Последнее вхождение "cd" как отдельного слова
        matches = list(re.finditer(r"(?<![\w./~-])cd(?![\w./~-])", line))
        if not matches:
            return None
        rest = line[matches[-1].end() :].strip()
        if not rest or rest.startswith((";", "&&", "|", "&")):
            return None  # голый cd → home; точный путь неизвестен
        # Аргумент — до конца строки, разделителя команд или комментария
        arg = re.split(r"\s*(?:;|&&|\|\||\|)\s*", rest)[0]
        arg = re.split(r"\s+#", arg)[0].strip()
        if not arg:
            return None
        if arg == "-":
            return "-"  # маркер oldpwd
        try:
            tokens = shlex.split(arg)
        except ValueError:
            return None  # битые кавычки — команда шеллу тоже не выполнится
        if len(tokens) != 1:
            return None  # лишние аргументы — bash вернёт ошибку, cwd не сменится
        path = tokens[0]
        if path.startswith("~"):
            return None  # ~user разрешать нечем; ~/x пропускаем осознанно
        if path == "/":
            return "/"
        # Экранированные пробелы уже развёрнуты shlex; нормализуем
        return posixpath.normpath(path)

    def resolve_cd_target(self, target: str) -> Optional[str]:
        """Разворачивает цель cd в абсолютный путь с учётом cwd шелла.

        target: абсолютный путь, относительный путь или "-" (oldpwd).
        Возвращает абсолютный путь или None (резолв невозможен).
        """
        if target == "/":
            return "/"
        if target == "-":
            return self._shell_oldpwd if self._shell_oldpwd else None
        if posixpath.isabs(target):
            return target
        return posixpath.normpath(posixpath.join(self._shell_cwd, target))

    def _paste(self) -> None:
        text = QApplication.clipboard().text()
        if text:
            self._send(text.replace("\n", "\r"))

    def event(self, e) -> bool:
        # Qt 6 выполняет Tab-фокус-навигацию в QWidget::event ДО вызова
        # keyPressEvent — Tab/Shift+Tab до терминала не доходят. Перехват
        # здесь, до механизма смены фокуса.
        if e.type() == QEvent.Type.KeyPress and self._worker is not None:
            k = e
            mods = k.modifiers()
            if not (mods & (Qt.ControlModifier | Qt.AltModifier)):
                if k.key() == Qt.Key_Tab:
                    self._send("\t")
                    k.accept()
                    return True
                if k.key() == Qt.Key_Backtab:
                    # Shift+Tab — обратная табуляция (xterm: ESC[Z)
                    self._send("\x1b[Z")
                    k.accept()
                    return True
        return super().event(e)

    def keyPressEvent(self, event) -> None:
        if self._worker is None:
            return
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)
        alt = bool(mods & Qt.AltModifier)

        if ctrl and shift:
            if key == Qt.Key_C:
                self.copy()
                return
            if key == Qt.Key_V:
                self._paste()
                return

        handled = True
        if key == Qt.Key_Return or key == Qt.Key_Enter:
            self._detect_cd_command()
            self._send("\r")
        elif alt and key == Qt.Key_Backspace:
            # Alt+Backspace = удалить слово назад (readline backward-kill-word)
            self._send("\x1b\x7f")
        elif alt and key == Qt.Key_Left:
            self._send("\x1bb")
        elif alt and key == Qt.Key_Right:
            self._send("\x1bf")
        elif key == Qt.Key_Backspace:
            self._send("\x7f")
        elif key == Qt.Key_Tab:
            self._send("\t")
        elif key == Qt.Key_Escape:
            self._send("\x1b")
        elif key == Qt.Key_Left:
            self._send("\x1b[D")
        elif key == Qt.Key_Right:
            self._send("\x1b[C")
        elif key == Qt.Key_Up:
            self._send("\x1b[A")
        elif key == Qt.Key_Down:
            self._send("\x1b[B")
        elif key == Qt.Key_Home:
            self._send("\x1b[H")
        elif key == Qt.Key_End:
            self._send("\x1b[F")
        elif key == Qt.Key_PageUp:
            self._send("\x1b[5~")
        elif key == Qt.Key_PageDown:
            self._send("\x1b[6~")
        elif key == Qt.Key_Delete:
            self._send("\x1b[3~")
        elif key == Qt.Key_Insert:
            self._send("\x1b[2~")
        elif Qt.Key_F1 <= key <= Qt.Key_F12:
            self._send(_FKEYS[key - Qt.Key_F1])
        elif ctrl and Qt.Key_A <= key <= Qt.Key_Z:
            self._send(chr(key - Qt.Key_A + 1))
        else:
            text = event.text()
            if not text:
                handled = False
            elif alt:
                self._send("\x1b" + text)
            else:
                self._send(text)
        if handled:
            event.accept()

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        self._place_cursor()

    def wheelEvent(self, event) -> None:
        sb = self.verticalScrollBar()
        was_at_bottom = sb.value() >= sb.maximum() - 1
        super().wheelEvent(event)
        if event.angleDelta().y() > 0:
            self._follow = False
        elif was_at_bottom:
            self._follow = True

    # ------------------------------------------------------------------
    # Размер
    # ------------------------------------------------------------------

    def _update_size(self) -> None:
        if not self.isVisible() or self._worker is None:
            return
        fm = self.fontMetrics()
        char_w = max(fm.horizontalAdvance(" "), 1)
        line_h = max(fm.lineSpacing(), 1)
        cols = max(self.viewport().width() // char_w - 1, 10)
        rows = max(self.viewport().height() // line_h - 1, 5)
        if (cols, rows) != self._last_size:
            self._last_size = (cols, rows)
            self._screen.resize(cols, rows)
            self._render()
            self._worker.resize_pty(cols, rows)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_size()

    def _on_worker_finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        pending = self._pending_start
        self._pending_start = None
        if pending is not None:
            self.start(*pending)


class RemoteTerminalWidget(QWidget):
    """Контейнер терминала: заголовок + экран (без строки отправки).

    В заголовке справа — кнопка синхронизации каталога с выпадающим меню
    (чекабельные режимы «Панель → Терминал» / «Терминал → Панель»).
    """

    #: Пользователь нажал Enter на команде cd в терминале (см. TerminalView).
    commandEntered = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        self.title_label = QLabel("Терминал")
        self.title_label.setStyleSheet("font-weight: bold;")
        header.addWidget(self.title_label)
        header.addStretch(1)

        # Кнопка синхронизации каталога (у правого края заголовка)
        self.sync_btn = QToolButton(self)
        set_themed_icon(self.sync_btn, "menu_sync")
        self.sync_btn.setToolTip("Синхронизация каталога панели и терминала")
        self.sync_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.sync_btn.setStyleSheet(
            FLAT_TOOLBUTTON_QSS + "QToolButton::menu-indicator { image: none; }"
        )
        sync_menu = QMenu(self.sync_btn)
        self.act_follow_panel = QAction("Панель → Терминал", sync_menu, checkable=True)
        self.act_follow_panel.setToolTip(
            "Навигация по файловой панели меняет каталог в терминале"
        )
        self.act_follow_panel.setChecked(True)
        self.act_follow_terminal = QAction(
            "Терминал → Панель", sync_menu, checkable=True
        )
        self.act_follow_terminal.setToolTip(
            "Команда cd в терминале открывает каталог в файловой панели"
        )
        self.act_follow_terminal.setChecked(True)
        sync_menu.addAction(self.act_follow_panel)
        sync_menu.addAction(self.act_follow_terminal)
        self.sync_btn.setMenu(sync_menu)
        header.addWidget(self.sync_btn)

        layout.addLayout(header)

        self.terminal_view = TerminalView(self)
        layout.addWidget(self.terminal_view, 1)
        self.terminal_view.commandEntered.connect(self.commandEntered)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def start(self, ssh, host: str = "", initial_cwd: str = "/") -> None:
        self.title_label.setText(f"Терминал — {host}" if host else "Терминал")
        self.terminal_view.start(ssh, host, initial_cwd=initial_cwd)

    def stop(self) -> None:
        self.terminal_view.stop()

    def clear(self) -> None:
        self.terminal_view.clear()

    def send_cd(self, path: str) -> None:
        """Сменить директорию шелла на ``path`` (синхронизация с файловой панелью).

        Отправляет ``cd <экранированный_путь>`` + Enter в активный шелл.
        Путь экранируется через ``shlex.quote`` для пробелов/спецсимволов.
        Ведущий пробел не дает команде попасть в history там, где
        ``HISTCONTROL`` содержит ``ignorespace``/``ignoreboth`` (дефолт
        Debian/Ubuntu); на выполнение не влияет, выглядит как обычный cd.
        No-op, если шелл не запущен.
        """
        if path:
            self.terminal_view.send_command(f" cd {shlex.quote(path)}\r")
            self.terminal_view.set_shell_cwd(path)

    def setFocus(self) -> None:
        super().setFocus()
        self.terminal_view.setFocus()

    def _update_size(self) -> None:
        self.terminal_view._update_size()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._update_size()
        self.terminal_view.setFocus()
