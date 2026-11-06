"""Граф топологии сети: узлы, ребра, сцена и вью.

Портирован из netmap/__main__.py. Отличия:
- цвета из палитры приложения (темная/светлая тема), а не хардкод;
- ребра обновляются через itemChange узла (не только mouseMove);
- метод fit_view() для показа всего графа.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QTimer, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
)


# Акцентные цвета типов узлов (одинаковы в обеих темах, фон - из палитры).
_EDGE_COLORS = {
    "network": "#4f9cff",
    "router": "#ffb74d",
    "host": "#55d98b",
    "local": "#b388ff",
    "unknown": "#8a93a3",
}

# Иконки типов узлов (ключи ICONS, файлы topo_*.svg без подложки).
# "host" - динамический (по hostname), остальные фиксированы.
NODE_ICON_KEYS = {
    "network": "topo_network",
    "router": "topo_router",
    "host": "topo_host",
    "local": "topo_local",
    "unknown": "topo_unknown",
}

# Кэш SVG-рендереров иконок узлов: (тема, ключ) -> QSvgRenderer.
# Ключ включает тему - при live-переключении старые записи просто
# не находятся; тема читается в момент запроса.
_ICON_RENDERERS: dict = {}


def node_icon_renderer(icon_key: str):
    """QSvgRenderer иконки узла из zip темы. None - иконку не найти."""
    if not icon_key:
        return None
    try:
        from src.config import get_icon_theme, get_themed_icon_bytes
        from PySide6.QtCore import QByteArray
        from PySide6.QtSvg import QSvgRenderer
    except Exception:
        return None
    try:
        theme = get_icon_theme()
        cache_key = (theme, icon_key)
        renderer = _ICON_RENDERERS.get(cache_key)
        if renderer is not None:
            return renderer
        data = get_themed_icon_bytes(f"{icon_key}.svg")
        if not data:
            return None
        renderer = QSvgRenderer(QByteArray(bytes(data)))
        if not renderer.isValid():
            return None
        _ICON_RENDERERS[cache_key] = renderer
        return renderer
    except Exception:
        return None


def highlight_color() -> QColor:
    """Цвет подсветки пути: Highlight палитры, fallback - янтарный."""
    try:
        app = QApplication.instance()
        if app is not None:
            return app.palette().color(QPalette.ColorRole.Highlight)
    except Exception:
        pass
    return QColor("#ffcf40")


def head_tri(cx: float, cy: float, s: float, angle: float, spread: float) -> QPolygonF:
    """Треугольная стрелка в точке (cx, cy) вдоль угла angle."""
    return QPolygonF(
        [
            QPointF(
                cx + math.cos(angle) * s * 0.9,
                cy + math.sin(angle) * s * 0.9,
            ),
            QPointF(
                cx - math.cos(angle - spread) * s * 0.7,
                cy - math.sin(angle - spread) * s * 0.7,
            ),
            QPointF(
                cx - math.cos(angle + spread) * s * 0.7,
                cy - math.sin(angle + spread) * s * 0.7,
            ),
        ]
    )


def _node_brush(node_type: str) -> QColor:
    """Фон узла: базовый цвет палитры, тонированный под тип."""
    try:
        app = QApplication.instance()
        if app is not None:
            base = app.palette().color(QPalette.ColorRole.Base)
        else:
            base = QColor("#2b2b2b")
    except Exception:
        base = QColor("#2b2b2b")
    tint = QColor(_EDGE_COLORS.get(node_type, "#aaaaaa"))
    # Смешать базу с оттенком 4:1, чтобы узел читался в любой теме.
    r = (base.red() * 4 + tint.red()) // 5
    g = (base.green() * 4 + tint.green()) // 5
    b = (base.blue() * 4 + tint.blue()) // 5
    return QColor(r, g, b)


class NetworkNode(QGraphicsRectItem):
    """Узел графа: имя + IP.

    Два стиля отображения: "scheme" (цветной прямоугольник, как раньше)
    и "icons" (тематическая SVG-иконка topo_* сверху, подписи снизу
    по центру, без прямоугольника). Режим задается сценой
    (set_node_style) и переживает смену палитры/темы иконок
    через refresh_theme. В режиме "icons" рамка рисуется только
    для подсвеченных/выделенных узлов.

    Выделяется кликом и Ctrl+клик (мультивыделение), перетаскивается
    мышью; при групповом драге двигаются все выделенные узлы.
    """

    WIDTH = 160
    HEIGHT = 54
    # Геометрия иконки в режиме "icons": квадрат сверху по центру,
    # подписи снизу по центру. Прямоугольника нет.
    ICON_SIZE = 48.0
    ICON_PAD = 8.0
    ICON_GAP = 4.0
    ICON_MAX_WIDTH = 240.0

    def __init__(
        self,
        node_id: str,
        name: str,
        ip: str,
        node_type: str,
        display_mode: str = "scheme",
        icon_key: str = "",
    ) -> None:
        super().__init__(-self.WIDTH / 2, -self.HEIGHT / 2, self.WIDTH, self.HEIGHT)
        self.node_id = node_id
        self.name = name
        self.ip = ip
        self.node_type = node_type
        self._display_mode = "icons" if display_mode == "icons" else "scheme"
        self._icon_key = icon_key or NODE_ICON_KEYS.get(node_type, "")
        self._icon_renderer = None
        self._icon_rect = None
        self._highlighted = False
        # Group drag: стартовая позиция узла на начало перетаскивания
        # (восстанавливается, если драг отменился клавишей Esc).
        self._drag_start: tuple | None = None
        # Alt+драг подсети: [(узел-хост, стартовый x, стартовый y)].
        self._alt_followers: list = []
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        # Прямоугольник больше отрисовки - клик мимо текста тоже ловится,
        # но узлы соседние перехватывать не должен.
        self.setAcceptHoverEvents(True)
        self.name_item = QGraphicsTextItem(self)
        self.name_item.setPlainText(name)
        name_font = QFont()
        name_font.setPointSize(9)
        name_font.setBold(True)
        self.name_item.setFont(name_font)
        self.name_item.setZValue(100)
        self.ip_item = QGraphicsTextItem(self)
        self.ip_item.setPlainText(ip)
        ip_font = QFont()
        ip_font.setPointSize(8)
        self.ip_item.setFont(ip_font)
        self.ip_item.setZValue(100)
        # Заливка/рамка/иконка - через refresh_theme (учитывает display_mode).
        self.refresh_theme()

    def _refresh_text_colors(self) -> None:
        try:
            app = QApplication.instance()
            if app is not None:
                pal = app.palette()
                self.name_item.setDefaultTextColor(pal.color(QPalette.ColorRole.Text))
                self.ip_item.setDefaultTextColor(
                    pal.color(QPalette.ColorRole.PlaceholderText)
                )
                return
        except Exception:
            pass
        self.name_item.setDefaultTextColor(QColor("#ffffff"))
        self.ip_item.setDefaultTextColor(QColor("#b8c0cc"))

    def _is_icons(self) -> bool:
        return self._display_mode == "icons"

    def effective_half(self) -> tuple:
        """Полуразмеры узла под ребра: scheme - фикс, icons - бокс иконка+текст."""
        try:
            rect = self.rect()
            return (rect.width() / 2, rect.height() / 2)
        except Exception:
            return (self.WIDTH / 2, self.HEIGHT / 2)

    def _update_text_position(self) -> None:
        if not self._is_icons():
            try:
                self.setRect(-self.WIDTH / 2, -self.HEIGHT / 2, self.WIDTH, self.HEIGHT)
            except Exception:
                pass
            name_rect = self.name_item.boundingRect()
            self.name_item.setPos(-name_rect.width() / 2, -23)
            ip_rect = self.ip_item.boundingRect()
            self.ip_item.setPos(-ip_rect.width() / 2, 2)
            self._icon_rect = None
            return
        # Режим иконки: иконка сверху по центру, подписи снизу по центру.
        # Прямоугольник-рамка отсутствует - rect покрывает иконку+тексты,
        # чтобы клики/выделение работали и стрелки упирались в край подписи.
        try:
            name_rect = self.name_item.boundingRect()
            ip_rect = self.ip_item.boundingRect()
            name_w = name_rect.width()
            ip_w = ip_rect.width()
            name_h = name_rect.height()
            ip_h = ip_rect.height()
            total_w = max(self.ICON_SIZE, name_w, ip_w) + self.ICON_PAD * 2
            total_w = min(total_w, self.ICON_MAX_WIDTH)
            total_h = self.ICON_SIZE + self.ICON_GAP + name_h + ip_h + self.ICON_PAD * 2
            self.setRect(-total_w / 2, -total_h / 2, total_w, total_h)
            top = -total_h / 2 + self.ICON_PAD
            icon_x = -self.ICON_SIZE / 2
            self._icon_rect = (icon_x, top, self.ICON_SIZE, self.ICON_SIZE)
            name_y = top + self.ICON_SIZE + self.ICON_GAP
            self.name_item.setPos(-name_w / 2, name_y)
            self.ip_item.setPos(-ip_w / 2, name_y + name_h)
        except Exception:
            pass

    def update_label(self, name: str | None = None, ip: str | None = None) -> None:
        if name is not None:
            self.name = name
        if ip is not None:
            self.ip = ip
        self.name_item.setPlainText(self.name)
        self.ip_item.setPlainText(self.ip)
        self._update_text_position()
        if self._is_icons():
            # Rect мог вырасти под длинную подпись - переподрезать ребра.
            try:
                scene = self.scene()
                if scene is not None and hasattr(scene, "update_edges"):
                    if not getattr(scene, "_edge_updates_suspended", False):
                        scene.update_node_edges(self.node_id)
            except Exception:
                pass

    def set_display_mode(self, mode: str, icon_key: str = "") -> None:
        """Стиль отображения узла: "scheme" (прямоугольник) | "icons".

        icon_key - ключ ICONS иконки topo_* (пусто = по node_type).
        """
        self._display_mode = "icons" if mode == "icons" else "scheme"
        if icon_key:
            self._icon_key = icon_key
        self.refresh_theme()
        try:
            self.update()
        except Exception:
            pass

    def set_icon_key(self, icon_key: str) -> None:
        """Сменить иконку узла (тип оборудования уточнен сканом)."""
        self._icon_key = icon_key or self._icon_key
        if self._display_mode == "icons":
            self._icon_renderer = node_icon_renderer(self._icon_key)
            try:
                self.update()
            except Exception:
                pass

    def refresh_theme(self) -> None:
        """Перечитать цвета под текущую палитру (при смене темы).

        В режиме "icons" - еще и байты иконки под текущую тему иконок.
        Прямоугольника нет: перо/заливка прозрачные, рамка рисуется
        в paint только для подсвеченных/выделенных узлов.
        """
        accent = QColor(_EDGE_COLORS.get(self.node_type, "#aaaaaa"))
        if self._display_mode == "icons":
            self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            self.setPen(QPen(Qt.PenStyle.NoPen))
            self._icon_renderer = node_icon_renderer(self._icon_key)
        else:
            self.setBrush(QBrush(_node_brush(self.node_type)))
            self.setPen(QPen(accent, 4 if self._highlighted else 2))
        self._refresh_text_colors()
        self._update_text_position()

    def paint(self, painter, option, widget=None) -> None:  # noqa: N802
        if self._display_mode != "icons":
            super().paint(painter, option, widget)
            return
        # Режим иконки: прямоугольника нет. Подсветка/выделение -
        # скругленная рамка вокруг бокса иконка+подписи.
        try:
            from PySide6.QtCore import QRectF

            if self._highlighted or self.isSelected():
                if self._highlighted:
                    color = QColor(highlight_color())
                    width = 3.0
                else:
                    color = QColor(_EDGE_COLORS.get(self.node_type, "#aaaaaa"))
                    width = 2.0
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                painter.setPen(QPen(color, width))
                painter.drawRoundedRect(self.rect(), 8.0, 8.0)
                painter.restore()
        except Exception:
            pass
        renderer = self._icon_renderer
        if renderer is None:
            return
        try:
            from PySide6.QtCore import QRectF

            icon_rect = getattr(self, "_icon_rect", None)
            if icon_rect is None:
                # Геометрия еще не пересчитана (первый paint): по центру.
                icon_rect = (
                    -self.ICON_SIZE / 2,
                    -self.ICON_SIZE / 2,
                    self.ICON_SIZE,
                    self.ICON_SIZE,
                )
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            renderer.render(painter, QRectF(*icon_rect))
            painter.restore()
        except Exception:
            pass

    def set_highlighted(self, on: bool) -> None:
        """Подсветить/снять подсветку узла (путь к выделенному)."""
        self._highlighted = bool(on)
        self.refresh_theme()
        try:
            self.update()
        except Exception:
            pass

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            try:
                scene = self.scene()
                if scene is not None and hasattr(scene, "update_edges"):
                    if getattr(scene, "_edge_updates_suspended", False):
                        return super().itemChange(change, value)
                    scene.update_node_edges(self.node_id)
            except Exception:
                pass
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            # В режиме иконок выделение рисуется вручную в paint.
            try:
                self.update()
            except Exception:
                pass
        return super().itemChange(change, value)

    # ------------------------------------------------------------------
    # Групповое перетаскивание: mousePressEvent запоминает стартовые
    # позиции ВСЕХ выделенных узлов, mouseMoveEvent двигает их вместе
    # с данным узлом, mouseReleaseEvent фиксирует результат.
    # Alt+драг по подсети: ее хосты (соседи по ребрам) двигаются вместе
    # с ней, пока Alt зажат; при отпускании Alt хосты остаются на месте.
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            scene = self.scene()
            if scene is not None and not self.isSelected():
                # Ctrl+клик по невыделенному: Qt сам добавит к выделению
                # (ItemIsSelectable), ничего дополнительно не делаем.
                pass
            self._drag_start = (self.x(), self.y())
            try:
                scene = self.scene()
                if scene is not None:
                    for node in scene.nodes.values():
                        if node is not self and node.isSelected():
                            node._drag_start = (node.x(), node.y())
            except Exception:
                pass
            # Alt+драг подсети: свита собирается лениво в mouseMoveEvent
            # (первый тик с AltModifier), здесь только сброс.
            self._alt_followers = []
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (
            self._drag_start is not None
            and self.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable
        ):
            dx = self.x() - self._drag_start[0]
            dy = self.y() - self._drag_start[1]
            if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                try:
                    scene = self.scene()
                    if scene is not None:
                        moved = False
                        for node in scene.nodes.values():
                            if node is self or node._drag_start is None:
                                continue
                            if not node.isSelected():
                                continue
                            node.setPos(
                                node._drag_start[0] + dx,
                                node._drag_start[1] + dy,
                            )
                            moved = True
                except Exception:
                    pass
            # Alt+драг подсети: хосты следуют только пока Alt зажат.
            # Дельта инкрементальная (от прошлого события): после
            # отпускания и повторного зажатия Alt продолжается без
            # скачка. Свита собирается лениво - при первом Alt-тике.
            if self.node_type == "network" and self.scene() is not None:
                alt_down = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
                if alt_down and not self._alt_followers:
                    try:
                        for edge in self.scene().edges:
                            if edge.source is self:
                                if edge.target.node_type == "host":
                                    self._alt_followers.append(
                                        (edge.target, edge.target.pos())
                                    )
                            elif (
                                edge.target is self and edge.source.node_type == "host"
                            ):
                                self._alt_followers.append(
                                    (edge.source, edge.source.pos())
                                )
                    except Exception:
                        self._alt_followers = []
                if self._alt_followers:
                    if alt_down:
                        try:
                            fdx = event.scenePos().x() - event.lastScenePos().x()
                            fdy = event.scenePos().y() - event.lastScenePos().y()
                        except Exception:
                            fdx, fdy = 0.0, 0.0
                        if fdx or fdy:
                            for node, _start in self._alt_followers:
                                try:
                                    node.setPos(node.x() + fdx, node.y() + fdy)
                                except Exception:
                                    continue
                    # Alt отпущен: свита замирает до повторного зажатия
                    # (позиции остаются, продолжение - инкрементально).
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        try:
            scene = self.scene()
            if scene is not None:
                for node in scene.nodes.values():
                    node._drag_start = None
        except Exception:
            pass
        self._alt_followers = []
        super().mouseReleaseEvent(event)


class NetworkEdge(QGraphicsPathItem):
    """Ребро между двумя узлами: линия + стрелка у цели (source -> target).

    Стили: plain - сплошная линия с фиксированной стрелкой у цели;
    ants - бегущий пунктир с стрелкой на конце у цели. Анимация муравьев
    идет ТОЛЬКО по выделенному маршруту (highlight): невыделенные ребра
    в стиле ants рисуются как статичный пунктир.
    """

    ARROW_SIZE = 12.0
    # Пунктир: штрих/зазор и скорость анимации (пикс/тик, мс).
    DASH = 8.0
    GAP = 10.0
    ANTS_INTERVAL_MS = 70
    ANTS_STEP = 4.0

    def __init__(self, source: NetworkNode, target: NetworkNode) -> None:
        super().__init__()
        self.source = source
        self.target = target
        try:
            app = QApplication.instance()
            if app is not None:
                mid = app.palette().color(QPalette.ColorRole.Mid)
            else:
                mid = QColor("#68717d")
        except Exception:
            mid = QColor("#68717d")
        self._mid = QColor(mid)
        self._highlight_color = QColor(highlight_color())
        self._style = "ants"
        self._phase = 0.0
        # Наконечник - отдельный полигональный айтем: линия может быть
        # пунктирной, стрелка всегда сплошная. Дочерний к линии - двигается
        # и удаляется вместе с ней.
        self._head_item = QGraphicsPolygonItem(self)
        self._head_item.setZValue(0)
        self.setPen(QPen(self._mid, 1.2))
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self.setZValue(-10)
        self._highlighted = False
        self.update_position()

    def set_style(self, style: str) -> None:
        """Переключить стиль ребра: plain | ants. Перерисовать сразу."""
        style = str(style) if str(style) in ("plain", "ants") else "ants"
        if self._style == style:
            return
        self._style = style
        self.update_position()

    def advance_ants(self) -> None:
        """Тик анимации: сдвинуть фазу пунктира (только выделенные ребра)."""
        if not self._highlighted:
            return
        period = self.DASH + self.GAP
        self._phase = (self._phase + self.ANTS_STEP) % period
        try:
            self.update()
        except Exception:
            pass

    def set_highlighted(self, on: bool) -> None:
        """Подсветить/снять подсветку ребра (путь к выделенному)."""
        self._highlighted = bool(on)
        self._apply_pen()
        self.setZValue(-5 if self._highlighted else -10)
        self.update_position()

    def _apply_pen(self) -> None:
        """Перо ребра: цвет/толщина/пунктир по стилю и подсветке."""
        color = self._highlight_color if self._highlighted else self._mid
        width = 2.5 if self._highlighted else 1.2
        pen = QPen(color, width)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        kind = getattr(self, "kind", "observed")
        if kind == "group":
            pen.setStyle(Qt.PenStyle.DotLine)
        elif kind == "unknown":
            pen.setStyle(Qt.PenStyle.DashLine)
        elif kind == "hypothesis":
            pen.setStyle(Qt.PenStyle.DashDotLine)
        elif self._style == "ants" and self._highlighted:
            # Пунктир: фаза двигает штрихи к цели (отрицательный офсет).
            pen.setDashPattern([self.DASH, self.GAP])
            pen.setDashOffset(-(self._phase % (self.DASH + self.GAP)))
        self.setPen(pen)
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))

    @staticmethod
    def _half_sizes(node) -> tuple:
        """Полуразмеры узла: из rect (иконки - бокс иконка+подпись)."""
        try:
            if hasattr(node, "effective_half"):
                hw, hh = node.effective_half()
                return (float(hw) + 3, float(hh) + 3)
        except Exception:
            pass
        try:
            rect = node.rect()
            return (rect.width() / 2 + 3, rect.height() / 2 + 3)
        except Exception:
            return (80.0, 27.0)

    def update_position(self) -> None:
        """Линия + стрелка, подрезанные к границам узлов.

        В режиме иконок граница - бокс иконка+подпись, стрелка упирается
        в его край. Без подрезки наконечник у цели рисовался в центре
        узла и прятался под его боксом (z ребра -10 ниже узлов).
        Стрелка - отдельный полигональный айтем: линия может быть
        пунктирной, наконечник всегда сплошная заливка.
        """
        p1 = self.source.scenePos()
        p2 = self.target.scenePos()
        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
        length = math.hypot(dx, dy)
        path = QPainterPath()
        if length < 1e-6:
            self.setPath(path)
            self._head_item.setPolygon(QPolygonF())
            return
        ux, uy = dx / length, dy / length
        size = self.ARROW_SIZE
        angle = math.atan2(dy, dx)
        spread = math.pi / 7.0

        def boundary_point(center: QPointF, sign: float, node) -> QPointF:
            """Точка пересечения луча с границей бокса узла (полуразмеры)."""
            half_w, half_h = self._half_sizes(node)
            ax = abs(ux)
            ay = abs(uy)
            if ax < 1e-9:
                t = half_h
            elif ay < 1e-9:
                t = half_w
            else:
                t = min(half_w / ax, half_h / ay)
            return QPointF(center.x() + ux * t * sign, center.y() + uy * t * sign)

        start = boundary_point(p1, 1.0, self.source)
        # Граница цели: наконечник целиком СНАРУЖИ бокса (вершина упирается
        # в границу, основание сзади). Иначе голова прячется под узлом.
        color = self._highlight_color if self._highlighted else self._mid
        tip = boundary_point(p2, -1.0, self.target)
        back_l = QPointF(
            tip.x() - math.cos(angle - spread) * size,
            tip.y() - math.sin(angle - spread) * size,
        )
        back_r = QPointF(
            tip.x() - math.cos(angle + spread) * size,
            tip.y() - math.sin(angle + spread) * size,
        )
        # Линия (сплошная либо пунктир по стилю) до середины основания
        # наконечника: срез на боковой угол (back_l/back_r) смещал
        # стрелку вбок от линии.
        base_mid = QPointF(
            tip.x() - math.cos(angle) * size * math.cos(spread),
            tip.y() - math.sin(angle) * size * math.cos(spread),
        )
        path.moveTo(start.x(), start.y())
        path.lineTo(base_mid.x(), base_mid.y())
        self._apply_pen()
        self.setPath(path)

        # Наконечник у цели - отдельный айтем: сплошная заливка даже при
        # пунктирной линии (в ants-стиле штриховка не должна рвать стрелку).
        head = QPolygonF([tip, back_l, back_r])
        self._head_item.setPolygon(head)
        self._head_item.setBrush(QBrush(color))
        self._head_item.setPen(QPen(color, 1.0))


class NetworkScene(QGraphicsScene):
    """Сцена графа с реестром узлов и ребер."""

    def __init__(self) -> None:
        super().__init__()
        self.nodes: dict = {}
        self.edges: list = []
        self.edge_index: dict = {}
        self.incident: dict = {}
        self.owner_edges: dict = {}
        # Пока True - itemChange узлов не тянет update_edges.
        # Используется в layout_graph для пакетного позиционирования.
        self._edge_updates_suspended = False
        # Стиль ребер (plain | ants) и общий таймер анимации муравьев.
        self._edge_style = "ants"
        self._ants_timer: QTimer | None = None
        # Стиль узлов: scheme (цветные прямоугольники) | icons (topo_*).
        self._node_style = "scheme"

    def set_node_style(self, style: str) -> None:
        """Стиль отображения узлов: scheme | icons. Применить ко всем + будущим."""
        style = "icons" if str(style) == "icons" else "scheme"
        self._node_style = style
        for node in self.nodes.values():
            try:
                node.set_display_mode(style)
            except Exception:
                continue
        # Геометрия границ изменилась (прямоугольник <-> иконка+подпись):
        # переподрезать ребра к новым боксам.
        try:
            self.update_edges()
        except Exception:
            pass

    def set_edge_updates_suspended(self, suspended: bool) -> None:
        """Вкл/выкл пакетный режим: в конце - одно общее обновление ребер."""
        self._edge_updates_suspended = suspended
        if not suspended:
            self.update_edges()

    def set_edge_style(self, style: str) -> None:
        """Применить стиль ко всем ребрам (и будущим): plain | ants.

        Муравьи (бегущий пунктир) анимируются одним общим таймером сцены,
        двигаются ТОЛЬКО выделенные ребра - невыделенные статичны.
        """
        style = str(style) if str(style) in ("plain", "ants") else "ants"
        self._edge_style = style
        if self._ants_timer is not None:
            self._ants_timer.stop()
            self._ants_timer = None
        for edge in self.edges:
            try:
                edge.set_style(style)
            except Exception:
                continue

    def _ensure_ants_timer(self) -> None:
        """Запустить общий таймер муравьев (если стиль ants и он еще не идет)."""
        if self._edge_style_value() != "ants":
            return
        if self._ants_timer is not None:
            return
        if not any(getattr(e, "_highlighted", False) for e in self.edges):
            return
        self._ants_timer = QTimer(self)
        self._ants_timer.setInterval(NetworkEdge.ANTS_INTERVAL_MS)
        self._ants_timer.timeout.connect(self._advance_ants)
        self._ants_timer.start()

    def _advance_ants(self) -> None:
        """Тик общего таймера муравьев: двигаются только выделенные ребра."""
        animated = False
        for edge in self.edges:
            try:
                if edge._highlighted:
                    edge.advance_ants()
                    animated = True
            except Exception:
                continue
        if not animated and self._ants_timer is not None:
            # Нечего анимировать: таймер не нужен до следующей подсветки.
            self._ants_timer.stop()
            self._ants_timer = None

    def _edge_style_value(self) -> str:
        return getattr(self, "_edge_style", "ants")

    def _node_style_value(self) -> str:
        return getattr(self, "_node_style", "scheme")

    def clear_graph(self) -> None:
        self.clear()
        self.nodes.clear()
        self.edges.clear()
        self.edge_index.clear()
        self.incident.clear()
        self.owner_edges.clear()

    def add_node(
        self,
        node_id: str,
        name: str,
        ip: str,
        node_type: str,
        position: tuple = (0, 0),
        icon_key: str = "",
    ) -> NetworkNode:
        existing = self.nodes.get(node_id)
        if existing is not None:
            existing.update_label(name=name, ip=ip)
            if icon_key:
                existing.set_icon_key(icon_key)
            return existing
        node = NetworkNode(
            node_id,
            name,
            ip,
            node_type,
            display_mode=self._node_style_value(),
            icon_key=icon_key,
        )
        self.addItem(node)
        node.setPos(position[0], position[1])
        self.nodes[node_id] = node
        return node

    def add_edge(self, source_id: str, target_id: str, kind: str = "group", owner: str = "") -> None:
        source = self.nodes.get(source_id)
        target = self.nodes.get(target_id)
        if source is None or target is None or source is target:
            return
        key = (source_id, target_id, kind)
        owner = owner or f"group:{source_id}:{target_id}"
        self.owner_edges.setdefault(owner, set()).add(key)
        if key in self.edge_index:
            self.edge_index[key].owners.add(owner)
            return self.edge_index[key]
        edge = NetworkEdge(source, target)
        edge.kind = kind
        edge.owners = {owner}
        edge.key = key
        titles = {"observed": "Наблюдаемый переход", "unknown": "Неизвестное продолжение: число переходов не установлено",
                  "hypothesis": "Гипотеза общего префикса", "group": "Принадлежность CIDR-группе, не сетевое соединение"}
        edge.setToolTip(f"{titles.get(kind, kind)}\n{source_id} → {target_id}")
        edge._style = self._edge_style_value()
        edge._apply_pen()
        self.addItem(edge)
        self.edges.append(edge)
        self.edge_index[key] = edge
        self.incident.setdefault(source_id, set()).add(edge)
        self.incident.setdefault(target_id, set()).add(edge)
        return edge

    def remove_owners(self, owners: list[str]) -> None:
        """Удалить только связи без оставшихся владельцев, включая QGraphicsItem."""
        removed = set()
        for owner in owners:
            for key in self.owner_edges.pop(owner, set()):
                edge = self.edge_index.get(key)
                if edge is None:
                    continue
                edge.owners.discard(owner)
                if edge.owners:
                    continue
                self.edge_index.pop(key, None)
                for node_id in key[:2]:
                    self.incident.get(node_id, set()).discard(edge)
                self.removeItem(edge)
                removed.add(edge)
        if removed:
            self.edges = [edge for edge in self.edges if edge not in removed]

    def remove_nodes(self, node_ids: set[str]) -> None:
        """Согласованно удалить узлы и инцидентные связи из всех индексов."""
        affected = {edge for nid in node_ids for edge in self.incident.get(nid, ())}
        for edge in affected:
            self.edge_index.pop(edge.key, None)
            for owner in edge.owners:
                self.owner_edges.get(owner, set()).discard(edge.key)
            for nid in edge.key[:2]:
                self.incident.get(nid, set()).discard(edge)
            self.removeItem(edge)
        self.edges = [edge for edge in self.edges if edge not in affected]
        for nid in node_ids:
            self.incident.pop(nid, None)
            node = self.nodes.pop(nid, None)
            if node is not None:
                self.removeItem(node)

    def rename_node(self, old_id: str, new_id: str) -> None:
        """Изменить id группы с согласованным переносом индексов связей."""
        if new_id in self.nodes:
            self.remove_nodes({old_id})
            return
        node = self.nodes.pop(old_id)
        node.node_id = new_id
        self.nodes[new_id] = node
        edges = self.incident.pop(old_id, set())
        self.incident.setdefault(new_id, set()).update(edges)
        for edge in edges:
            old_key = edge.key
            key = tuple(new_id if value == old_id else value for value in old_key[:2]) + (edge.kind,)
            self.edge_index.pop(old_key, None)
            self.edge_index[key] = edge
            edge.key = key
            for owner in edge.owners:
                self.owner_edges[owner].discard(old_key)
                self.owner_edges[owner].add(key)

    def update_node_edges(self, node_id: str) -> None:
        """Перемещение одного узла обновляет только его инцидентные связи."""
        for edge in self.incident.get(node_id, ()):
            edge.update_position()

    def update_edges(self) -> None:
        for edge in self.edges:
            try:
                edge.update_position()
            except Exception:
                continue

    def clear_highlights(self) -> None:
        """Снять подсветку пути со всех узлов и ребер."""
        for edge in self.edges:
            try:
                edge.set_highlighted(False)
            except Exception:
                continue
        for node in self.nodes.values():
            try:
                node.set_highlighted(False)
            except Exception:
                continue

    def highlight_path(self, node_ids: list, edge_pairs: list) -> int:
        """Подсветить путь: узлы по id и ребра по парам id. Вернуть число ребер."""
        wanted_nodes = set(node_ids)
        for node_id in wanted_nodes:
            node = self.nodes.get(node_id)
            if node is not None:
                try:
                    node.set_highlighted(True)
                except Exception:
                    continue
        lit = 0
        for first, second in edge_pairs:
            src = self.nodes.get(first)
            dst = self.nodes.get(second)
            if src is None or dst is None or src is dst:
                continue
            for kind in ("observed", "unknown", "hypothesis", "group"):
                edge = self.edge_index.get((first, second, kind))
                if edge is not None:
                    edge.set_highlighted(True)
                    lit += 1
                    break
        # Муравьи: подсветка появилась - нужен общий таймер анимации.
        if lit:
            self._ensure_ants_timer()
        return lit

    def refresh_theme(self) -> None:
        for node in self.nodes.values():
            try:
                node.refresh_theme()
            except Exception:
                continue


class NetworkView(QGraphicsView):
    """Вью графа: зум колесом, панорама drag, fit по кнопке.

    Выделение: клик по узлу, Ctrl+клик - добавить/убрать из выделения,
    клик по пустому месту - снять выделение, Alt+драг по пустому месту -
    рамка выделения (выделяются узлы, попавшие в область). Рамка
    включается временно на время жеста, обычный драг - панорама.
    """

    # Delete на выделенном: диалог решает, что удалять (с подтверждением).
    delete_requested = Signal()

    def __init__(self) -> None:
        self.network_scene = NetworkScene()
        super().__init__(self.network_scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        # Alt+драг по пустому месту: на время жеста включается
        # RubberBandDrag (рамка выделения), на release возвращается панорама.
        self._alt_band = False

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.AltModifier
        ):
            # Alt зажат: рамка выделения вместо панорамы. Если нажатие
            # попало на узел - Qt сам отдаст событие узлу (рамка не
            # стартует на айтеме), режим вернется на release.
            self._alt_band = True
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        if self._alt_band:
            self._alt_band = False
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if (
            event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
            and not event.isAutoRepeat()
        ):
            if any(item.isSelected() for item in self.network_scene.items()):
                self.delete_requested.emit()
                return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        factor = 1.15
        if event.angleDelta().y() < 0:
            factor = 1 / factor
        self.scale(factor, factor)

    def fit_view(self) -> None:
        rect = self.network_scene.itemsBoundingRect()
        if rect.isNull():
            return
        self.fitInView(
            rect.adjusted(-60, -60, 60, 60), Qt.AspectRatioMode.KeepAspectRatio
        )
