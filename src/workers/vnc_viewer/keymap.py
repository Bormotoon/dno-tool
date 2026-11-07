"""Перевод нажатий Qt в keysym X11 для RFB KeyEvent.

Два режима ввода печатных клавиш:
- ``physical`` (по клавишам): отправляется keysym латинской раскладки US для
  физической клавиши, символ определяет текущая раскладка удалённого
  компьютера. Так работают kmsvnc (uinput) и переключение раскладки на хосте.
- ``symbol`` (по символам): отправляется keysym введённого символа с учётом
  раскладки оператора.

Служебные клавиши (стрелки, F1-F12, модификаторы) одинаковы в обоих режимах.
"""

from __future__ import annotations

from PySide6.QtCore import Qt

KEYBOARD_PHYSICAL = "physical"
KEYBOARD_SYMBOL = "symbol"

XK_SHIFT_L = 0xFFE1
XK_SHIFT_R = 0xFFE2
XK_CONTROL_L = 0xFFE3
XK_CONTROL_R = 0xFFE4
XK_ALT_L = 0xFFE9
XK_ALT_R = 0xFFEA
XK_SUPER_L = 0xFFEB
XK_SUPER_R = 0xFFEC
XK_DELETE = 0xFFFF
XK_TAB = 0xFF09
XK_ESCAPE = 0xFF1B
XK_F4 = 0xFFC1
XK_PRINT = 0xFF61
XK_BACKSPACE = 0xFF08

_K = Qt.Key
_SPECIAL: dict[int, int] = {
    _K.Key_Escape: XK_ESCAPE,
    _K.Key_Tab: XK_TAB,
    _K.Key_Backtab: XK_TAB,
    _K.Key_Backspace: XK_BACKSPACE,
    _K.Key_Return: 0xFF0D,
    _K.Key_Enter: 0xFF8D,
    _K.Key_Insert: 0xFF63,
    _K.Key_Delete: XK_DELETE,
    _K.Key_Pause: 0xFF13,
    _K.Key_Print: XK_PRINT,
    _K.Key_SysReq: 0xFF15,
    _K.Key_Clear: 0xFF0B,
    _K.Key_Home: 0xFF50,
    _K.Key_End: 0xFF57,
    _K.Key_Left: 0xFF51,
    _K.Key_Up: 0xFF52,
    _K.Key_Right: 0xFF53,
    _K.Key_Down: 0xFF54,
    _K.Key_PageUp: 0xFF55,
    _K.Key_PageDown: 0xFF56,
    _K.Key_Shift: XK_SHIFT_L,
    _K.Key_Control: XK_CONTROL_L,
    _K.Key_Meta: XK_SUPER_L,
    _K.Key_Alt: XK_ALT_L,
    _K.Key_AltGr: 0xFE03,
    _K.Key_CapsLock: 0xFFE5,
    _K.Key_NumLock: 0xFF7F,
    _K.Key_ScrollLock: 0xFF14,
    _K.Key_Super_L: XK_SUPER_L,
    _K.Key_Super_R: XK_SUPER_R,
    _K.Key_Menu: 0xFF67,
    _K.Key_Hyper_L: 0xFFED,
    _K.Key_Hyper_R: 0xFFEE,
    _K.Key_Help: 0xFF6A,
    _K.Key_Space: 0x20,
}
for _n in range(35):
    _SPECIAL[int(_K.Key_F1) + _n] = 0xFFBE + _n

_KEYPAD: dict[int, int] = {
    _K.Key_Asterisk: 0xFFAA,
    _K.Key_Plus: 0xFFAB,
    _K.Key_Minus: 0xFFAD,
    _K.Key_Period: 0xFFAE,
    _K.Key_Comma: 0xFFAE,
    _K.Key_Slash: 0xFFAF,
    _K.Key_Enter: 0xFF8D,
}
for _n in range(10):
    _KEYPAD[int(_K.Key_0) + _n] = 0xFFB0 + _n

# Ключи - целые: event.key() возвращает int, а не перечисление Qt.
_SPECIAL = {int(key): value for key, value in _SPECIAL.items()}
_KEYPAD = {int(key): value for key, value in _KEYPAD.items()}
_MODIFIER_KEYS = frozenset(int(key) for key in (_K.Key_Shift, _K.Key_Control, _K.Key_Alt, _K.Key_Meta))

# Скан-коды набора 1 = коды evdev для основного блока клавиатуры.
_PHYSICAL: dict[int, int] = {
    41: ord("`"),
    12: ord("-"),
    13: ord("="),
    26: ord("["),
    27: ord("]"),
    39: ord(";"),
    40: ord("'"),
    43: ord("\\"),
    51: ord(","),
    52: ord("."),
    53: ord("/"),
    86: ord("<"),
}
for _code, _char in zip(range(2, 12), "1234567890"):
    _PHYSICAL[_code] = ord(_char)
for _start, _row in ((16, "qwertyuiop"), (30, "asdfghjkl"), (44, "zxcvbnm")):
    for _offset, _char in enumerate(_row):
        _PHYSICAL[_start + _offset] = ord(_char)

# Правые модификаторы по скан-коду: Windows (бит 0x100 - расширенная клавиша)
# и evdev+8 (xcb, wayland).
_RIGHT_MODIFIERS_WINDOWS = {0x36: XK_SHIFT_R, 0x11D: XK_CONTROL_R, 0x138: XK_ALT_R, 0x15C: XK_SUPER_R}
_RIGHT_MODIFIERS_EVDEV = {54: XK_SHIFT_R, 97: XK_CONTROL_R, 100: XK_ALT_R, 126: XK_SUPER_R}


def physical_code(native_scan_code: int, platform_name: str) -> int | None:
    """Код физической клавиши (набор 1 / evdev) или None, если платформа его не даёт."""
    if not native_scan_code:
        return None
    if platform_name == "windows":
        return native_scan_code
    if platform_name in ("xcb", "wayland") or platform_name.startswith("wayland"):
        return native_scan_code - 8 if native_scan_code > 8 else None
    return None


def char_keysym(char: str) -> int | None:
    """keysym символа: Latin-1 напрямую, кириллица - keysym X11, прочее - Unicode."""
    if len(char) != 1:
        return None
    code = ord(char)
    if code < 0x20 or code == 0x7F:
        return None
    if code <= 0xFF:
        return code
    try:
        koi = char.encode("koi8_r")
    except UnicodeEncodeError:
        koi = b""
    if len(koi) == 1 and (koi[0] >= 0xC0 or koi[0] in (0xA3, 0xB3)):
        # Буквы кириллицы X11: 0x600 + код KOI8 (Cyrillic_io 0x6a3 ... Cyrillic_HARDSIGN 0x6ff).
        return 0x600 + koi[0]
    return 0x01000000 + code


def key_to_keysym(
    key: int,
    text: str,
    modifiers: Qt.KeyboardModifier,
    native_scan_code: int,
    platform_name: str,
    mode: str,
) -> int | None:
    """keysym для нажатия; None - клавишу отправлять не нужно."""
    key = int(key)
    code = physical_code(native_scan_code, platform_name)
    if key in _MODIFIER_KEYS and code is not None:
        table = _RIGHT_MODIFIERS_WINDOWS if platform_name == "windows" else _RIGHT_MODIFIERS_EVDEV
        right = table.get(code)
        if right is not None:
            return right
    if modifiers & Qt.KeyboardModifier.KeypadModifier and key in _KEYPAD:
        return _KEYPAD[key]
    if key in _SPECIAL:
        return _SPECIAL[key]
    physical = _PHYSICAL.get(code) if code is not None else None
    if mode == KEYBOARD_PHYSICAL and physical is not None:
        return physical
    symbol = char_keysym(text) if text else None
    if symbol is not None:
        return symbol
    if physical is not None:
        return physical
    # Ctrl+буква без текста: латинская буква по коду Qt.
    if int(_K.Key_A) <= key <= int(_K.Key_Z):
        return ord("a") + key - int(_K.Key_A)
    if 0x20 <= key <= 0x7E:
        return key
    if 0xA0 <= key <= 0xFF:
        return key
    return None


# Сочетания для меню окна: (подпись, последовательность keysym).
KEY_COMBOS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("Ctrl+Alt+Del", (XK_CONTROL_L, XK_ALT_L, XK_DELETE)),
    ("Alt+Tab", (XK_ALT_L, XK_TAB)),
    ("Alt+F4", (XK_ALT_L, XK_F4)),
    ("Ctrl+Esc", (XK_CONTROL_L, XK_ESCAPE)),
    ("Win (Super)", (XK_SUPER_L,)),
    ("Print Screen", (XK_PRINT,)),
    ("Ctrl+Alt+Backspace", (XK_CONTROL_L, XK_ALT_L, XK_BACKSPACE)),
)
