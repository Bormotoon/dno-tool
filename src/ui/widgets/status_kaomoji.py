"""Каомодзи статус-строки (по образцу Hermes).

Реестры «настроений» и выбор случайного каомодзи для состояний
``idle``/``busy``/``ok``/``error`` и mood-ситуаций (``lock``/``power``/
``wake``/``bulk``). Используется MainWindow в статусной строке; виджет
метки живёт в главном окне, здесь только данные и выбор.

Включение/отключение — настройка ``config.app.kaomoji_enabled``.
"""

import random

# Каомодзи по базовым состояниям статус-строки. Выбирается при обновлении
# статуса, повтор подряд исключается (см. pick_kaomoji).
#
# ВАЖНО (Windows): символы подбирались под покрытие шрифтами — канадские
# слоговые (ᕙ ᕗ ᕦ ᐛ) и иные экзотические глифы НЕ покрыты ни одним шрифтом
# на типовой Windows → Qt fallback перебирает шрифты (включая растровый
# Fixedsys) и DirectWrite сыплет предупреждениями
# «CreateFontFaceFromHDC() failed» (qt.qpa.fonts). Пул ниже даёт 0
# предупреждений. При добавлении новых каомодзи проверять глифы через
# GetGlyphIndicesW (Segoe UI + MS Gothic как минимум).
STATUS_KAOMOJI = {
    "idle": ["・ω・", "( ･ω･ )", "(￣ω￣)", "╮(╯▽╰)╭", "¬‿¬", "¯\\_(ツ)_/¯"],
    "busy": [
        "(⌐■_■)",
        "┌(・。・)┘♪",
        "Ψ(⇀‸↼‶)Ψ",
        "(ง'̀-'́)ง",
        "(⊙_⊙)",
        "(・_・;)",
    ],
    "ok": [
        "(◕‿◕)",
        "Ψ( ▽‿)Ψ",
        "(￣‿￣)ノ",
        "(•̀ᴗ•́)و",
        "(≧▽≦)",
        "(๑˃ᴗ˂)ﻭ",
    ],
    "error": [
        "(╯°□°)╯︵ ┻━┻",
        "╥﹏╥",
        "(T_T)",
        "(×﹏×)",
        "(￢_￢;)",
        "(;´д`)",
    ],
}

# Mood-каомодзи: перекрывают базовый пул состояния для особых ситуаций.
# Ключ — mood-токен, передаваемый в _set_status(..., mood=...).
STATUS_KAOMOJI_MOOD = {
    "lock": ["(￢_￢)", "(－‿－)", "(￣へ￣)", "(¬_¬)", "(ｏ・_・)ノ"],
    "power": ["(－.－) zzZ", "(x_x)", "(‐^‐)bZz", "∑(ﾟ＊ﾟ;)", "(-.-) Zzz..."],
    "wake": ["( ＾▽＾)／", "┗(＾0＾)┓", "(⊙_◎)"],
    "bulk": ["Ψ(ò_óˇ)Ψ", "(ง •̀_•́)ง", "ヽ(｀Д´)ﾉ", "(•̀ᴗ•́)و ̑̑"],
}

# Подписи состояний для tooltip метки каомодзи.
KAOMOJI_STATE_LABELS = {
    "idle": "Готово",
    "busy": "Выполняется…",
    "ok": "Успех",
    "error": "Ошибка",
}

# Mood-токен фичи: какие фичи получают особый набор каомодзи.
FEATURE_MOOD = {
    "lock": "lock",
    "lock_input": "lock",
    "ssh_lock": "lock",
    "ssh_lock_input": "lock",
    "powerdown": "power",
    "reboot": "power",
    "logoff": "power",
    "logoff_webapi": "power",
    "power_on": "wake",
    "krfb_password": "lock",
    "krfb_restart": "wake",
}

# Шрифт метки каомодзи: широкое покрытие CJK/box-drawing глифов,
# минимизирует fallback-перебор шрифтов Qt (см. комментарий у STATUS_KAOMOJI).
KAOMOJI_FONT_FAMILY = "MS Gothic"


def pick_kaomoji(state: str, mood: str = "", prev: str = "") -> str:
    """Выбрать каомодзи для состояния/mood (случайно, без повтора подряд).

    Пул берётся из ``STATUS_KAOMOJI_MOOD[mood]`` (если mood задан и
    известен), иначе из ``STATUS_KAOMOJI[state]``. При отсутствии пула
    возвращается пустая строка.

    Args:
        state: состояние статус-строки (``idle``/``busy``/``ok``/``error``).
        mood: опциональный mood-токен (``lock``/``power``/``wake``/``bulk``).
        prev: предыдущий показанный каомодзи (исключает повтор подряд).

    Returns:
        Случайный каомодзи из пула или пустая строка.
    """
    pool = None
    if mood:
        pool = STATUS_KAOMOJI_MOOD.get(mood)
    if not pool:
        pool = STATUS_KAOMOJI.get(state)
    if not pool:
        return ""
    choice = prev
    for _ in range(8):
        choice = random.choice(pool)
        if choice != prev or len(pool) == 1:
            break
    return choice
