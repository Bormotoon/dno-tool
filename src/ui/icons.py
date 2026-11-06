"""Иконки с состоянием (toggle).

QAction/QToolButton с setCheckable(True) используют единый QIcon с двумя
вариантами (QIcon.State.On/Off): обычный SVG и его On-версия с изумрудным
акцентом «включено». Qt сам подставляет нужный вариант по checked —
никакой обвязки toggled→setIcon не требуется; программные сеттеры с
blockSignals тоже работают (иконка перерисовывается по checked).

On-версии иконок живут в теме (<имя>_on.svg рядом с базовыми -
в zip-архиве или папке assets/icons/<тема>/),
регистрируются в ICONS как "<ключ>_on" (src/config/settings.py).

Резолв иконок: _icon_from_key (по ключу ICONS) и qicon_from_path (по пути
device.icon) читают байты из zip темы через icon_theme_loader с fallback
на файл. Прямые QIcon(ICONS...)/QIcon(device.icon) запрещены - во
frozen-сборке папок тем нет.
"""

import re
import weakref

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QIconEngine, QImage, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from src.config import ICONS, get_asset_path
from src.logger import logger

# Кэш QIcon по ключу+теме: байты из zip уже декодированы в pixmap, повторный
# toggle_icon/refresh не ходит в архив. Ключ включает тему - при live-
# переключении старые записи просто не находятся и вытесняются очисткой.
_ICON_CACHE: dict = {}
_ICON_CACHE_THEME: str = ""

# Ключи, по которым байты из темы не найдены (warn один раз на ключ, не спамить).
# Пустая иконка из несуществующего пути - немая болезнь frozen-сборки: видно
# только пустое место, без лога причина не находится.
_MISSING_BYTES_LOGGED: set = set()


def _current_theme() -> str:
    try:
        from src.config import get_icon_theme

        return get_icon_theme()
    except Exception:  # noqa: BLE001
        return ""


def _pixmap_from_bytes(data: bytes) -> QPixmap:
    pm = QPixmap()
    try:
        pm.loadFromData(QByteArray(data))
    except Exception:  # noqa: BLE001
        return QPixmap()
    return pm


class _SvgBytesIconEngine(QIconEngine):
    """Векторный движок иконки: рендерит SVG-байты под любой размер.

    Замена QIcon(QPixmap) для тем из zip: растр фиксирует размер
    (SVG 128px не апскейлится - QIcon.pixmap(256) отдаёт 128, плитка
    списка растёт, а иконка нет). Движок хранит исходные байты и
    рисует через QSvgRenderer под запрошенный rect/size, как файловый
    SVG-движок Qt для QIcon(path.svg).
    """

    def __init__(self, data: bytes, on_data: bytes | None = None):
        super().__init__()
        self._data = bytes(data) if data else b""
        self._on_data = bytes(on_data) if on_data else None
        self._renderer_off = None
        self._renderer_on = None

    def clone(self):
        return _SvgBytesIconEngine(self._data, self._on_data)

    def _renderer(self, state: QIcon.State):
        use_on = state == QIcon.State.On and self._on_data
        cached = self._renderer_on if use_on else self._renderer_off
        if cached is None:
            blob = self._on_data if use_on else self._data
            if not blob:
                return None
            try:
                cached = QSvgRenderer(QByteArray(blob))
            except Exception:  # noqa: BLE001
                return None
            if use_on:
                self._renderer_on = cached
            else:
                self._renderer_off = cached
        try:
            return cached if cached.isValid() else None
        except Exception:  # noqa: BLE001
            return None

    def paint(
        self,
        painter: QPainter,
        rect: QRectF,
        mode: QIcon.Mode,
        state: QIcon.State,
    ) -> None:
        renderer = self._renderer(state)
        if renderer is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter, QRectF(rect))
        painter.restore()

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        try:
            w, h = size.width(), size.height()
        except Exception:  # noqa: BLE001
            return QPixmap()
        if w <= 0 or h <= 0:
            return QPixmap()
        pm = QPixmap(w, h)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        self.paint(painter, QRectF(0, 0, w, h), mode, state)
        painter.end()
        return pm

    def scaledPixmap(
        self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float
    ) -> QPixmap:
        try:
            w = max(1, int(round(size.width() * scale)))
            h = max(1, int(round(size.height() * scale)))
        except Exception:  # noqa: BLE001
            return self.pixmap(size, mode, state)
        pm = self.pixmap(QSize(w, h), mode, state)
        try:
            pm.setDevicePixelRatio(scale)
        except Exception:  # noqa: BLE001
            pass
        return pm


def _icon_from_bytes(data: bytes | None, on_data: bytes | None = None):
    """QIcon из байтов темы: SVG - векторный движок, растр - QPixmap.

    None = байты не иконка (пусто/битые). PNG-фолбэк сохраняет старое
    поведение QIcon(QPixmap) - растры масштабирует сам Qt.
    """
    if not data:
        return None
    try:
        probe = QSvgRenderer(QByteArray(bytes(data)))
        is_svg = bool(probe.isValid())
    except Exception:  # noqa: BLE001
        is_svg = False
    if not is_svg:
        pm = _pixmap_from_bytes(bytes(data))
        if pm.isNull():
            return None
        icon = QIcon(pm)
        if on_data:
            pm_on = _pixmap_from_bytes(bytes(on_data))
            if not pm_on.isNull():
                icon.addPixmap(pm_on, QIcon.Mode.Normal, QIcon.State.On)
        return icon
    if on_data:
        try:
            probe_on = QSvgRenderer(QByteArray(bytes(on_data)))
            if not probe_on.isValid():
                on_data = None
        except Exception:  # noqa: BLE001
            on_data = None
    try:
        return QIcon(
            _SvgBytesIconEngine(bytes(data), bytes(on_data) if on_data else None)
        )
    except Exception:  # noqa: BLE001
        return None


def _warn_missing_bytes(key_or_name: str) -> None:
    """Однократный warn: байты иконки не найдены ни в zip, ни в папках.

    Во frozen это означает пустой QIcon из несуществующего пути - без
    такой строки в логе симптом "пустое место вместо иконки" неопределим.
    """
    if key_or_name in _MISSING_BYTES_LOGGED:
        return
    _MISSING_BYTES_LOGGED.add(key_or_name)
    try:
        import sys

        frozen = bool(getattr(sys, "frozen", False))
    except Exception:  # noqa: BLE001
        frozen = False
    logger.warning(
        f"Иконка: байты {key_or_name} не найдены (zip и папки тем) -"
        f" пустая иконка{' (frozen)' if frozen else ''}"
    )


def _icon_from_key(icon_key: str) -> QIcon:
    """QIcon по ключу ICONS: сначала байты из zip, иначе путь с диска."""
    global _ICON_CACHE_THEME
    theme = _current_theme()
    if _ICON_CACHE_THEME != theme:
        _ICON_CACHE.clear()
        _ICON_CACHE_THEME = theme
    cached = _ICON_CACHE.get(icon_key)
    if cached is not None:
        return QIcon(cached) if isinstance(cached, QIcon) else QIcon()
    icon = None
    try:
        from src.config import get_themed_icon_bytes

        path = ICONS.get(icon_key, "")
        filename = path.replace("\\", "/").split("/")[-1] if path else ""
        data = get_themed_icon_bytes(filename) if filename else None
        if data:
            icon = _icon_from_bytes(data)
    except Exception:  # noqa: BLE001 - откат на путь
        icon = None
    if icon is None:
        if icon_key:
            _warn_missing_bytes(icon_key)
        icon = QIcon(ICONS.get(icon_key, ""))
    _ICON_CACHE[icon_key] = icon
    return QIcon(icon)


# Реестр виджетов с тематическими иконками для live-переключения темы
# иконок без перезапуска: (weakref на виджет, ключ в ICONS, toggle-флаг).
# Контекстные меню, строящиеся по показу, не регистрируются - они читают
# ICONS заново при каждом открытии.
_THEMED_WIDGETS: list = []

# Плейсхолдер цвета в SVG-шаблоне (toolbar_overflow.svg), заменяется на
# hex текущей палитры перед рендером - иконка читаема на любой теме.
_OVERFLOW_COLOR_PLACEHOLDER = "__COLOR__"
_OVERFLOW_SVG = "icons/toolbar_overflow.svg"
_OVERFLOW_FALLBACK_COLOR = "#888888"

# Токен цвета в SVG пака пользовательских панелей (Lucide):
# Qt SVG не знает currentColor и рисует черный контур - на темной теме
# иконку не видно. Перед рендером токен заменяется цветом палитры.
_CUSTOM_ICON_COLOR_TOKENS = (b"currentColor", b"currentcolor")
# Модульный кэш QSvgRenderer по цвету (перекраска SVG не дёшева).
_OVERFLOW_RENDERERS: dict = {}


def _overflow_color(mode: QIcon.Mode) -> str:
    """Hex цвета точек для режима иконки: палитровый ButtonText."""
    app = QApplication.instance()
    pal = app.palette() if app else QPalette()
    role = QPalette.ColorRole.ButtonText
    group = (
        QPalette.ColorGroup.Disabled
        if mode == QIcon.Mode.Disabled
        else QPalette.ColorGroup.Active
    )
    color = pal.color(group, role)
    if not color.isValid():
        return _OVERFLOW_FALLBACK_COLOR
    return color.name()


def _overflow_renderer(color: str) -> QSvgRenderer:
    """QSvgRenderer шаблона overflow-иконки, перекрашенного под color."""
    renderer = _OVERFLOW_RENDERERS.get(color)
    if renderer is None:
        try:
            with open(get_asset_path(_OVERFLOW_SVG), "rb") as f:
                data = f.read()
        except OSError:
            return None
        data = data.replace(_OVERFLOW_COLOR_PLACEHOLDER.encode(), color.encode())
        renderer = QSvgRenderer(QByteArray(data))
        _OVERFLOW_RENDERERS[color] = renderer
    return renderer


class _OverflowIconEngine(QIconEngine):
    """Движок: три точки, перекрашиваемые палитрой в момент отрисовки."""

    def paint(
        self,
        painter: QPainter,
        rect: QRectF,
        mode: QIcon.Mode,
        state: QIcon.State,
    ) -> None:
        renderer = _overflow_renderer(_overflow_color(mode))
        if renderer is None or not renderer.isValid():
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter, rect)
        painter.restore()

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        pm = QPixmap(size)
        pm.fill(Qt.GlobalColor.transparent)
        device = pm.devicePixelRatio()
        painter = QPainter(pm)
        painter.scale(device, device)
        self.paint(painter, QRectF(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return pm


def overflow_icon() -> QIcon:
    """Иконка overflow-кнопки тулбара: три точки цветом палитры (SVG)."""
    return QIcon(_OverflowIconEngine())


# Пак пользовательских панелей (Lucide) рисуется на холсте 24x24 штрихами
# от края до края, а иконки темы - на 128x128 с широкими полями. При одном
# боксе кнопки пак-значок выглядит заметно крупнее темового. Перед рендером
# холст 24 расширяется до 30 (-3 со всех сторон): чернила ужмутся до ~80%
# кадра и оптически сравняются с темовыми. Касается только корневого
# viewBox 0 0 24 24; темовые (128) и растры не трогаем.
_PACK_VIEWBOX_RE = re.compile(rb'viewBox\s*=\s*(["\'])0\s+0\s+24\s+24\1')
_PACK_VIEWBOX_PADDED = b'viewBox="-3 -3 30 30"'


def _pad_pack_viewbox(data: bytes | None) -> bytes | None:
    """Добавить поля пак-иконке 24x24 (см. _PACK_VIEWBOX_RE)."""
    if not data:
        return data
    try:
        return _PACK_VIEWBOX_RE.sub(_PACK_VIEWBOX_PADDED, data, count=1)
    except Exception:  # noqa: BLE001 - байты без токена возвращаем как есть
        return data


def tint_current_color(data: bytes | None) -> bytes | None:
    """Заменить currentColor в SVG-байтах цветом палитры (ButtonText).

    Пак пользовательских панелей (Lucide) рисуется через currentColor -
    без замены Qt дает черный контур, невидимый на темной теме. Байты
    без токена возвращаются как есть (темовые и цветные иконки не трогаем).
    """
    if not data:
        return data
    has_token = any(t in data for t in _CUSTOM_ICON_COLOR_TOKENS)
    if not has_token:
        return data
    color = _overflow_color(QIcon.Mode.Normal).encode("ascii", "ignore")
    for token in _CUSTOM_ICON_COLOR_TOKENS:
        data = data.replace(token, color)
    return data


def qicon_from_bytes(data: bytes | None, tint: bool = True) -> QIcon:
    """QIcon из байтов SVG/растра (custom-паки из zip, без диска).

    Монохром Lucide тинтится currentColor под палитру (иначе черный
    на темной теме); цветной пак уже без токена - возвращается как есть.
    """
    if not data:
        return QIcon()
    blob = _pad_pack_viewbox(tint_current_color(data)) if tint else data
    icon = _icon_from_bytes(blob)
    return icon if icon is not None else QIcon()


def qicon_from_file(path: str) -> QIcon:
    """QIcon из произвольного файла иконки (pack/file спеки кнопок).

    В отличие от qicon_from_path (пути внутри assets/icons через zip темы),
    читает байты с диска и тинтит currentColor под палитру - иначе пак
    Lucide рисуется черным на темной теме.
    """
    if not path:
        return QIcon()
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return QIcon(path)
    if not data:
        return QIcon()
    icon = _icon_from_bytes(_pad_pack_viewbox(tint_current_color(data)))
    return icon if icon is not None else QIcon(path)


def toggle_icon(icon_key: str) -> QIcon:
    """Иконка toggle-действия: базовый вариант + On-вариант (если есть).

    Args:
        icon_key: базовый ключ в ICONS (например, "toolbar_nodes");

    Returns:
        QIcon с добавленным addFile(..., QIcon.On) вариантом.
        При отсутствии On-файла возвращает обычную иконку.
    """
    icon = _icon_from_key(icon_key)
    on_path = ICONS.get(f"{icon_key}_on", "")
    if on_path:
        try:
            from src.config import get_themed_icon_bytes

            filename = on_path.replace("\\", "/").split("/")[-1]
            data = get_themed_icon_bytes(filename)
            if data:
                # On-вариант тем же векторным движком (состояние QIcon.On):
                # addPixmap растром снова зафиксировал бы размер.
                base_path = ICONS.get(icon_key, "")
                base_name = (
                    base_path.replace("\\", "/").split("/")[-1] if base_path else ""
                )
                base_data = get_themed_icon_bytes(base_name) if base_name else None
                engine_icon = _icon_from_bytes(base_data, data) if base_data else None
                if engine_icon is not None:
                    return engine_icon
                pm = _pixmap_from_bytes(data)
                if not pm.isNull():
                    icon.addPixmap(pm, QIcon.Mode.Normal, QIcon.State.On)
                else:
                    icon.addFile(on_path, QSize(), QIcon.Mode.Normal, QIcon.State.On)
            else:
                _warn_missing_bytes(f"{icon_key}_on")
                icon.addFile(on_path, QSize(), QIcon.Mode.Normal, QIcon.State.On)
        except Exception:  # noqa: BLE001
            icon.addFile(on_path, QSize(), QIcon.Mode.Normal, QIcon.State.On)
    return icon


# Кэш data-URI rich-text иконок: (ключ, размер) -> str. Тема - модульный
# флаг (как _ICON_CACHE_THEME): при live-переключении темы кэш сбрасывается.
_DATA_URI_CACHE: dict = {}
_DATA_URI_CACHE_THEME: str = ""


def icon_data_uri(icon_key: str, size: int = 16) -> str:
    """PNG data-URI иконки по ключу ICONS - для rich-text (<img src=...>).

    Qt rich-text не читает zip-темы и рисует placeholder «белый лист»,
    когда файла по пути ICONS нет на диске (папки тем в репо/frozen-сборке
    не хранятся). Здесь байты берутся через get_themed_icon_bytes (zip ->
    папка, как у _icon_from_key), SVG растеризуется QSvgRenderer в
    прозрачный QImage (потокобезопасно, в отличие от QPixmap), PNG -
    гладкое масштабирование. Итог - data:image/png;base64.

    Returns:
        URI или "" - иконку не найти (вызывающий должен опустить <img>).
    """
    global _DATA_URI_CACHE_THEME
    theme = _current_theme()
    if _DATA_URI_CACHE_THEME != theme:
        _DATA_URI_CACHE.clear()
        _DATA_URI_CACHE_THEME = theme
    cache_key = (icon_key, int(size))
    cached = _DATA_URI_CACHE.get(cache_key)
    if cached is not None:
        return cached
    uri = ""
    try:
        from src.config import get_themed_icon_bytes

        path = ICONS.get(icon_key, "")
        filename = path.replace("\\", "/").split("/")[-1] if path else ""
        data = get_themed_icon_bytes(filename) if filename else None
        if data:
            img = _raster_from_icon_bytes(bytes(data), int(size))
            if img is not None:
                ba = QByteArray()
                buf = QBuffer(ba)
                buf.open(QIODevice.OpenModeFlag.WriteOnly)
                if img.save(buf, "PNG"):
                    uri = "data:image/png;base64," + bytes(ba.toBase64()).decode(
                        "ascii"
                    )
    except Exception:  # noqa: BLE001 - rich-text без иконки лучше падения
        uri = ""
    _DATA_URI_CACHE[cache_key] = uri
    return uri


def _raster_from_icon_bytes(data: bytes, size: int):
    """QImage ARGB32 (size x size, прозрачный фон) из байтов иконки.

    SVG - векторный рендер под размер; растр - загрузка + smooth-скейл.
    None = байты пустые/битые.
    """
    if not data or size <= 0:
        return None
    try:
        probe = QSvgRenderer(QByteArray(data))
        if probe.isValid():
            img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            painter = QPainter(img)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            probe.render(painter, QRectF(0, 0, size, size))
            painter.end()
            if img.isNull():
                return None
            return img
    except Exception:  # noqa: BLE001 - попробуем как растр
        pass
    img = QImage()
    if not img.loadFromData(QByteArray(data)):
        return None
    if img.width() != size or img.height() != size:
        img = img.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    if img.isNull():
        return None
    if img.format() != QImage.Format.Format_ARGB32_Premultiplied:
        return img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    return img


def _icons_asset_member(path: str) -> bool:
    """Путь указывает внутрь assets/icons? (любой корень, любой диск).

    Нормализованный rel-хвост сравнивается посегментно: relpath тут не
    годится - во frozen путь может лежать на другом диске (_MEIPASS на
    C:, внешний assets проекта на D:), os.path.relpath тогда кидает
    ValueError и путь ошибочно считается чужим (источник пустых
    иконок-мониторов в сборке 0.10.0+).
    """
    if not path:
        return False
    parts = [p for p in re.split(r"[\\/]+", path) if p and p != "."]
    # Отрезаем букву диска (C:) и корень (/).
    if parts and len(parts[0]) == 2 and parts[0].endswith(":"):
        parts = parts[1:]
    try:
        i = parts.index("assets")
    except ValueError:
        return False
    tail = parts[i:]
    return len(tail) >= 2 and tail[1] == "icons"


def qicon_from_path(path: str) -> QIcon:
    """QIcon из пути к файлу иконки (для device.icon и прочих путей).

    Пути внутри assets/icons/ резолвятся по имени файла через zip темы
    (на лету) - так иконки работают и во frozen-сборке без папок тем;
    сам путь используется только как источник basename, как в
    _icon_from_key (та же цепочка, что у работающих иконок тулбара/меню).
    Чужие пути (пользовательские иконки вне assets) грузятся с диска
    как раньше.
    """
    if not path:
        return QIcon()
    if _icons_asset_member(path):
        try:
            from src.config import get_themed_icon_bytes

            filename = path.replace("\\", "/").split("/")[-1]
            if filename:
                data = get_themed_icon_bytes(filename)
                if data:
                    engine_icon = _icon_from_bytes(tint_current_color(data))
                    if engine_icon is not None:
                        return engine_icon
        except Exception:  # noqa: BLE001 - откат на пустую иконку
            pass
        _warn_missing_bytes(filename or path)
        return QIcon()
    return QIcon(path)


def set_themed_icon(widget, icon_key: str, toggle: bool = False):
    """Поставить виджету иконку из ICONS и зарегистрировать для refresh.

    Args:
        widget: QAction/QToolButton/QPushButton (любой с setIcon).
        icon_key: базовый ключ в ICONS.
        toggle: True - toggle-иконка (база + On-вариант).

    Returns:
        Тот же виджет (для цепочки вызовов).
    """
    widget.setIcon(toggle_icon(icon_key) if toggle else _icon_from_key(icon_key))
    for i, (ref, _key, _is_toggle) in enumerate(_THEMED_WIDGETS):
        if ref() is widget:
            _THEMED_WIDGETS[i] = (ref, icon_key, toggle)
            break
    else:
        try:
            _THEMED_WIDGETS.append((weakref.ref(widget), icon_key, toggle))
        except TypeError:
            pass  # не weakref-абельный - иконка поставлена, без refresh
    return widget


def make_action(icon_key: str, text: str, parent=None, toggle: bool = False):
    """Создать QAction с тематической иконкой (с авторегистрацией)."""
    from PySide6.QtGui import QAction

    action = QAction(text, parent)
    return set_themed_icon(action, icon_key, toggle)


def refresh_themed_icons() -> int:
    """Перечитать иконки всех зарегистрированных виджетов. Вернуть их число."""
    alive = []
    count = 0
    for ref, key, is_toggle in _THEMED_WIDGETS:
        widget = ref()
        if widget is None:
            continue
        try:
            widget.setIcon(toggle_icon(key) if is_toggle else _icon_from_key(key))
            count += 1
        except RuntimeError:
            continue  # C++ объект удалён - выкидываем из реестра
        alive.append((ref, key, is_toggle))
    _THEMED_WIDGETS[:] = alive
    return count
