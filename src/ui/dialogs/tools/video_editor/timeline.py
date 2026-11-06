"""Таймлайн: QGraphicsView с дорожками, блоками, playhead, zoom/scroll."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSceneHoverEvent,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from src.domain.video.models import Clip, Project, TextOverlay, Track
from src.domain.video.text_timing import TextTiming

from .preview_widget import _mk_tool_btn

TRACK_H = 52
RULER_H = 26
HANDLE = 8


def _app_palette() -> QPalette:
    """Палитра приложения для темизации сцены (темные темы - темный фон)."""
    try:
        app = QApplication.instance()
        if app is not None:
            return app.palette()
    except Exception:
        pass
    return QPalette()


def _is_dark_palette(pal: QPalette) -> bool:
    """Темная ли тема (та же эвристика, что в theme_manager)."""
    try:
        return (
            pal.color(QPalette.ColorRole.Text).value()
            > pal.color(QPalette.ColorRole.Base).value()
        )
    except Exception:
        return False


def _contrast_text(bg: QColor) -> QColor:
    """Черный/белый текст по яркости фона клипа (читается в любой теме)."""
    try:
        lum = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
        return QColor("#000000") if lum > 128 else QColor("#ffffff")
    except Exception:
        return QColor("#000000")


def _clip_color(kind: str, pal: QPalette) -> QColor:
    """Цвет клипа: пастель в светлой теме, приглушенный в темной.

    В темной теме смешиваем пастель с Base 1:1 - клип остается
    синим/зеленым, но не светится белым пятном на темном фоне.
    """
    tint = QColor({"video": "#90caf9", "audio": "#a5d6a7", "text": "#ce93d8"}[kind])
    try:
        if _is_dark_palette(pal):
            base = pal.color(QPalette.ColorRole.Base)
            return QColor(
                (base.red() + tint.red()) // 2,
                (base.green() + tint.green()) // 2,
                (base.blue() + tint.blue()) // 2,
            )
    except Exception:
        pass
    return tint


class ClipItem(QGraphicsRectItem):
    def __init__(
        self,
        clip_iid: str,
        track_iid: str,
        x: float,
        y: float,
        w: float,
        label: str,
        color: QColor,
        wave: QPixmap | None = None,
        wave_span: tuple[float, float, float] = (0.0, 1.0, 1.0),
        wave_mode: str = "full",
        strip: QPixmap | None = None,
    ) -> None:
        super().__init__(0, 0, max(8, w), TRACK_H - 8)
        self.setPos(x, y)
        self.clip_iid = clip_iid
        self.track_iid = track_iid
        self.setBrush(QBrush(color))
        self.setPen(QPen(color.darker(140), 1))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape, True)
        self.setAcceptHoverEvents(True)
        self._label = QGraphicsSimpleTextItem(label, self)
        self._label.setBrush(QBrush(_contrast_text(color)))
        self._label.setPos(4, 4)
        self._drag_mode: str | None = None
        # форма волны всего ассета + границы клипа (src_in, src_out, dur):
        # рисуем подотрезок, подпись-ребенок красится поверх.
        # full: волна на весь клип (аудио), strip: полоса снизу (видео).
        self._wave = wave
        self._wave_span = wave_span
        self._wave_mode = wave_mode
        # лента превью видеоклипа (N кадров across src_in..src_out),
        # рисуется верхней полосой, подпись поверх.
        self._strip = strip
        self._audio_envelope: tuple[float, float, float, float, float] | None = None

    def paint(self, painter, option, widget=None) -> None:  # noqa: N802
        super().paint(painter, option, widget)
        inner = self.rect().adjusted(2, 2, -2, -2)
        if self._strip is not None and not self._strip.isNull():
            try:
                strip_h = inner.height()
                if self._wave is not None and not self._wave.isNull():
                    # снизу полоса волны: ленте оставляем верхние ~60%
                    strip_h = inner.height() * 0.6
                sw, sh = self._strip.width(), self._strip.height()
                if sw > 0 and sh > 0 and strip_h > 0:
                    # без деформации: высота в полосу, ширина по аспекту,
                    # шкала заполняется повтором ленты, лишнее режется клипом
                    dw = sw * strip_h / sh
                    target = QRectF(inner.left(), inner.top(), inner.width(), strip_h)
                    painter.save()
                    painter.setClipRect(target)
                    painter.setOpacity(0.9)
                    src = QRectF(self._strip.rect())
                    x = target.left()
                    while x < target.right():
                        painter.drawPixmap(
                            QRectF(x, target.top(), dw, strip_h), self._strip, src
                        )
                        x += dw
                    painter.restore()
            except Exception:
                pass
        if self._wave is None or self._wave.isNull():
            return
        try:
            si, so, dur = self._wave_span
            pw = self._wave.width()
            if dur > 0 and pw > 0:
                x0 = max(0.0, min(1.0, si / dur)) * pw
                x1 = max(0.0, min(1.0, so / dur)) * pw
            else:
                x0, x1 = 0.0, float(pw)
            if x1 - x0 < 1:
                return
            inner = self.rect().adjusted(2, 2, -2, -2)
            if self._wave_mode == "strip":
                # полоса волны снизу клипа, подпись сверху не перекрываем
                strip_h = max(10.0, inner.height() * 0.4)
                target = QRectF(
                    inner.left(),
                    inner.bottom() - strip_h,
                    inner.width(),
                    strip_h,
                )
            else:
                target = QRectF(inner)
            painter.save()
            painter.setOpacity(0.55)
            painter.drawPixmap(
                target, self._wave, QRectF(x0, 0, x1 - x0, self._wave.height())
            )
            painter.restore()
            if self._audio_envelope:
                fade_in, fade_out, offset, original, duration = self._audio_envelope
                painter.save()
                painter.setPen(QPen(QColor("#ffb300"), 1.5))
                for direction in (-1, 1):
                    path = QPainterPath()
                    for index in range(65):
                        time = duration * index / 64 + offset
                        factor = min(1.0, max(0.0, time / fade_in)) if fade_in else 1.0
                        factor *= (
                            min(1.0, max(0.0, (original - time) / fade_out))
                            if fade_out
                            else 1.0
                        )
                        x, y = (
                            inner.left() + inner.width() * index / 64,
                            inner.center().y()
                            + direction * inner.height() / 2 * factor,
                        )
                        if index:
                            path.lineTo(x, y)
                        else:
                            path.moveTo(x, y)
                    painter.drawPath(path)
                painter.restore()
        except Exception:
            pass

    def edge_at(self, pos) -> str | None:
        r = self.rect()
        # Даже у короткого блока доступны оба края; середина остаётся для переноса.
        handle = min(HANDLE, r.width() / 3)
        if pos.x() <= handle:
            return "left"
        if pos.x() >= r.width() - handle:
            return "right"
        return None

    def hoverMoveEvent(self, event: QGraphicsSceneHoverEvent) -> None:  # noqa: N802
        editable = bool(self.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setCursor(
            Qt.CursorShape.SizeHorCursor
            if editable and self.edge_at(event.pos())
            else Qt.CursorShape.ArrowCursor
        )
        super().hoverMoveEvent(event)

    def hoverEnterEvent(self, event: QGraphicsSceneHoverEvent) -> None:  # noqa: N802
        self.hoverMoveEvent(event)

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent) -> None:  # noqa: N802
        self.unsetCursor()
        super().hoverLeaveEvent(event)


class TrackHeaders(QWidget):
    """Непрокручиваемые по горизонтали заголовки с выбором и контекстом."""

    def __init__(self, timeline: TimelineWidget) -> None:
        super().__init__(timeline)
        self.timeline = timeline
        self.setFixedWidth(28)
        self.setMouseTracking(True)
        self._hint = "Выберите дорожку; правая кнопка — свойства, порядок и удаление"
        self.setToolTip(self._hint)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.window())
        offset = self.timeline.view.mapToScene(0, 0).y()
        for index, track in enumerate(self.timeline._tracks):
            rect = QRectF(0, RULER_H + index * TRACK_H - offset, self.width(), TRACK_H)
            selected = self.timeline._selected_track == track.iid
            painter.fillRect(
                rect, palette.highlight() if selected else palette.button()
            )
            painter.setPen(
                palette.color(
                    QPalette.ColorRole.HighlightedText
                    if selected
                    else QPalette.ColorRole.ButtonText
                )
            )
            text_rect = rect.adjusted(3, 2, -3, -2)
            label = painter.fontMetrics().elidedText(
                track.name, Qt.TextElideMode.ElideRight, round(text_rect.width())
            )
            flags = ("🔒" if track.locked else "") + ("◌" if track.hidden else "")
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignVCenter,
                label + (f"\n{flags}" if flags else ""),
            )
            painter.setPen(palette.color(QPalette.ColorRole.Mid))
            painter.drawRect(rect)

    def _track_at(self, position_y: float) -> Track | None:
        y = self.timeline.view.mapToScene(0, round(position_y)).y()
        index = int((y - RULER_H) // TRACK_H)
        if y < RULER_H or not 0 <= index < len(self.timeline._tracks):
            return None
        return self.timeline._tracks[index]

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        track = self._track_at(event.position().y())
        if track:
            states = [
                text for active, text in (
                    (track.locked, "Заблокирована"), (track.hidden, "Скрыта")
                ) if active
            ]
            self.setToolTip("\n".join([track.name, *states, self._hint]))
        else:
            self.setToolTip(self._hint)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        track = self._track_at(event.position().y())
        if track is None or track.kind == "text":
            return
        self.timeline.select_track(track.iid)
        if (
            event.button() == Qt.MouseButton.RightButton
            and self.timeline.context_menu_factory
        ):
            menu = self.timeline.context_menu_factory("track")
            menu.exec(self.mapToGlobal(event.position().toPoint()))
            menu.deleteLater()


class TimelineWidget(QWidget):
    seeked = Signal(float)
    clip_edit_requested = Signal(object)  # ClipEdit, модель меняет контроллер
    clip_selected = Signal(str, str)  # clip_iid, track_iid
    drop_asset = Signal(str, str, float)  # asset_iid, track_iid, t
    text_selected = Signal(str)
    text_edit_requested = Signal(str, float, float)
    clip_action_requested = Signal(str, str)
    track_selected = Signal(str)
    split_clip_requested = Signal(str, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.tool = "select"
        self._selected_track = None
        self._blade_line = None
        self.context_menu_factory = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.zoom_controls = QWidget(self)
        top = QHBoxLayout(self.zoom_controls)
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(QLabel("Масштаб:"))
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(5, 400)
        # по умолчанию - минимальный зум: весь проект виден целиком,
        # иначе метки/клипы длинных файлов оказываются за кадром
        self.zoom.setValue(self.zoom.minimum())
        self.zoom.valueChanged.connect(lambda *a: self.refresh())
        top.addWidget(self.zoom, 1)
        fit = _mk_tool_btn("ve_fit_project", "Показать весь проект", self)
        self.fit_project_button = fit
        fit.clicked.connect(lambda: self.zoom.setValue(self.zoom.minimum()))
        top.addWidget(fit)
        selection = _mk_tool_btn(
            "ve_fit_selection", "Показать выделенный фрагмент", self
        )
        self.fit_selection_button = selection
        selection.clicked.connect(self.fit_selection)
        top.addWidget(selection)
        self.snapping = QCheckBox("Привязка", self)
        self.snapping.setChecked(True)
        self.snapping.setToolTip(
            "К стыкам, In/Out и курсору; Alt временно отключает привязку"
        )
        top.addWidget(self.snapping)
        lay.addWidget(self.zoom_controls)
        self.view = QGraphicsView()
        self.view.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.view.setAcceptDrops(True)
        self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.view.setMouseTracking(True)
        self.view.viewport().setMouseTracking(True)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        row = QHBoxLayout()
        row.setSpacing(0)
        self.headers = TrackHeaders(self)
        row.addWidget(self.headers)
        row.addWidget(self.view, 1)
        lay.addLayout(row, 1)
        self.view.verticalScrollBar().valueChanged.connect(
            lambda: self.headers.update()
        )
        self.scene = QGraphicsScene(self)
        self.view.setScene(self.scene)
        self._tracks: list = []
        self._assets: dict = {}
        self._waves: dict = {}
        self._strips: dict = {}
        self._items: dict[str, ClipItem] = {}
        self._clip_media: dict[str, tuple] = {}
        self._playhead: QGraphicsLineItem | None = None
        self._playhead_t = 0.0
        self._mark_in: float | None = None
        self._mark_out: float | None = None
        self._mark_items: list = []
        self._markers: list = []
        self._moving: ClipItem | None = None
        self._mode: str | None = None
        self._press_x = 0.0
        self._drag_offset_y = 0.0
        self._cur_px = 60.0
        self._trim_delta: float | None = None
        self._drag_peers: list[str] = []
        self.scene.selectionChanged.connect(self._on_sel)
        # первый layout до показа окна дает урезанный viewport: после
        # показа/лейаута пересчитать fit, иначе таймлайн не на всю ширину
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self._refresh_fit_if_min)
        self.view.viewport().installEventFilter(self)
        self._fit_timer.start()

    def select_track(self, iid: str) -> None:
        self._selected_track = iid
        self.scene.blockSignals(True)
        self.scene.clearSelection()
        self.scene.blockSignals(False)
        self.headers.update()
        self.track_selected.emit(iid)

    def set_tool(self, tool: str) -> None:
        self._cancel_drag()
        self.tool = tool
        if self._blade_line is not None:
            self._blade_line.hide()
        self.view.viewport().setCursor(
            Qt.CursorShape.CrossCursor
            if tool == "blade"
            else Qt.CursorShape.ArrowCursor
        )

    def _blade_target(self, point, modifiers):
        """Позиция разреза использует ту же привязку, но не геометрию 8 px."""
        sp = self.view.mapToScene(point)
        index = int((sp.y() - RULER_H) // TRACK_H)
        if sp.y() < RULER_H or not 0 <= index < len(self._tracks):
            return None
        track = self._tracks[index]
        if track.kind == "text":
            return None
        time = sp.x() / self._cur_px
        clip = next(
            (c for c in track.clips if c.timeline_start < time < c.timeline_end), None
        )
        if clip is None:
            return None
        if (
            self.snapping.isChecked()
            and not modifiers & Qt.KeyboardModifier.AltModifier
        ):
            targets = [0.0, self._playhead_t] + [
                t for t in (self._mark_in, self._mark_out) if t is not None
            ]
            targets.extend(
                t
                for tr in self._tracks
                for c in tr.clips
                for t in (c.timeline_start, c.timeline_end)
            )
            closest = min(targets, key=lambda t: abs(t - time))
            if abs(closest - time) <= 6 / self._cur_px:
                time = closest
        if not clip.timeline_start + 1e-8 < time < clip.timeline_end - 1e-8:
            return None
        return clip.iid, time

    def _show_blade(self, point, modifiers) -> None:
        target = self._blade_target(point, modifiers)
        if self._blade_line is not None:
            self._blade_line.setVisible(target is not None)
        if target:
            x = target[1] * self._cur_px
            height = RULER_H + len(self._tracks) * TRACK_H
            if self._blade_line is None:
                self._blade_line = self.scene.addLine(
                    x,
                    RULER_H,
                    x,
                    height,
                    QPen(
                        self.palette().color(QPalette.ColorRole.Highlight),
                        2,
                        Qt.PenStyle.DashLine,
                    ),
                )
                self._blade_line.setZValue(101)
                self._blade_line.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            else:
                self._blade_line.setLine(x, RULER_H, x, height)

    def fit_selection(self) -> None:
        clips = [
            clip
            for track in self._tracks
            for clip in track.clips
            if clip.iid
            in {
                item.clip_iid
                for item in self.scene.selectedItems()
                if isinstance(item, ClipItem)
            }
        ]
        if (
            self._mark_in is not None
            and self._mark_out is not None
            and self._mark_out > self._mark_in
        ):
            start, end = self._mark_in, self._mark_out
        elif clips:
            start, end = min(clip.timeline_start for clip in clips), max(
                clip.timeline_end for clip in clips
            )
        else:
            self.zoom.setValue(self.zoom.minimum())
            return
        fit_px = self.view.viewport().width() / self._content_seconds()
        wanted = self.view.viewport().width() / max(0.001, (end - start) * 1.1)
        value = self.zoom.minimum() + (wanted - fit_px) / max(
            1e-6, self.zoom.maximum() - fit_px
        ) * (self.zoom.maximum() - self.zoom.minimum())
        self.zoom.setValue(round(value))
        self.view.centerOn((start + end) / 2 * self._cur_px, RULER_H + TRACK_H)

    def _snap(
        self, position: float, mode: str, modifiers: Qt.KeyboardModifier
    ) -> float:
        from src.domain.video.navigation import VideoNavigation

        if not self.snapping.isChecked() or modifiers & Qt.KeyboardModifier.AltModifier:
            return position
        moving = self._moving
        clip = next(
            (
                clip
                for track in self._tracks
                for clip in track.clips
                if moving and clip.iid == moving.clip_iid
            ),
            None,
        )
        if clip is None:
            return position
        targets = [0.0, self._playhead_t] + [
            value for value in (self._mark_in, self._mark_out) if value is not None
        ]
        for track in self._tracks:
            for other in track.clips:
                if other.iid != clip.iid and not (
                    clip.link_iid and other.link_iid == clip.link_iid
                ):
                    targets.extend((other.timeline_start, other.timeline_end))
        return (
            VideoNavigation.snap(
                position / self._cur_px, clip.duration, targets, 6 / self._cur_px, mode
            )
            * self._cur_px
        )

    def _effective_px(self) -> float:
        """Масштаб px с растяжкой: мин. зум всегда показывает весь проект.

        zoom слайдера [5..400] маппится в px [fit_px..zoom_max] линейно,
        где fit_px = viewport / длительность. При мин. зуме px = fit_px:
        вся дорожка (и аудио) ровно в ширину окна, пустот нет. При
        большом зуме px стремится к значению слайдера для покадровой
        работы. Так 4-часовой проект не превращается в 70к px прокрутки.
        """
        zmin, zmax = float(self.zoom.minimum()), float(self.zoom.maximum())
        zoom = float(self.zoom.value())
        vw = max(50.0, float(self.view.viewport().width()))
        total = self._content_seconds()
        fit_px = vw / total if total > 0 else zmin
        if zmax <= zmin:
            return fit_px
        frac = min(1.0, max(0.0, (zoom - zmin) / (zmax - zmin)))
        return fit_px + frac * (zmax - fit_px)

    def _content_seconds(self) -> float:
        """Ширина сцены в секундах: контент + маленький пропорциональный хвост.

        Хвост 3% (мин 0.5с) - место за концом проекта для дропа/движения
        playhead. Был фикс +10с - он съедал до 15-25% ширины вьюпорта,
        и таймлайн выглядел нерастянутым при открытии.
        """
        total = 1.0
        for tr in self._tracks:
            for c in tr.clips:
                total = max(total, c.timeline_end)
        return total + max(0.5, total * 0.03)

    def set_data(
        self,
        tracks: list,
        assets: dict,
        waves: dict | None = None,
        strips: dict | None = None,
        markers: list | None = None,
        texts: list[TextOverlay] | None = None,
    ) -> None:
        self._tracks = self._with_text_tracks(tracks, texts or [])
        self._assets = assets
        self._markers = list(markers or ())
        # держать ССЫЛКУ на словари диалога (не `or {}`: пустой dict falsy
        # и таймлайн привязался бы к новому объекту - поздние волны/ленты
        # в кэше были бы ему не видны, висели бы плейсхолдеры)
        if waves is not None:
            self._waves = waves
        if strips is not None:
            self._strips = strips
        self.refresh()

    @staticmethod
    def _with_text_tracks(tracks: list[Track], texts: list[TextOverlay]) -> list[Track]:
        """Закрепить наложения за строками в порядке панели, независимо от времени."""
        project = Project(tracks=tracks, text_overlays=texts)
        locked_clips = {c.iid for tr in tracks if tr.locked for c in tr.clips}
        locked_titles = {t.iid for t in texts if t.clip_iid in locked_clips}
        intervals = {item.iid: item for item in TextTiming.intervals(project)}
        lanes: list[Track] = []
        for index, text in enumerate(texts):
            # У строки своя идентичность: подрезка и исчезновение пересечения
            # не меняют её положение или соседей. В проект эти дорожки не пишутся.
            lane = Track(
                iid=f"text-lane-{text.iid}",
                name=f"T{index + 1} · Наложения",
                kind="text",
            )
            lanes.append(lane)
            interval = intervals.get(text.iid)
            if interval is None:
                continue
            lane.clips.append(
                Clip(
                    iid=interval.iid,
                    asset_iid=interval.text,
                    timeline_start=interval.start,
                    src_out=interval.end - interval.start,
                    mute=not interval.enabled,
                )
            )
            lane.clips[-1]._title_locked = interval.iid in locked_titles
        return lanes + list(tracks)

    def set_texts(self, texts: list[TextOverlay]) -> None:
        self._tracks = self._with_text_tracks(
            [track for track in self._tracks if track.kind != "text"], texts
        )
        self.refresh()

    def set_playhead(self, t: float) -> None:
        self._playhead_t = max(0.0, t)
        self._draw_playhead()

    def select_clip(self, clip_iid: str) -> None:
        """Визуально выделить клип (после автопостановки/undo)."""
        self._selected_track = None
        self.headers.update()
        it = self._items.get(clip_iid)
        if it is None:
            self.scene.blockSignals(True)
            self.scene.clearSelection()
            self.scene.blockSignals(False)
            return
        if not it.isSelected():
            self.scene.blockSignals(True)
            for other in self.scene.selectedItems():
                other.setSelected(False)
            it.setSelected(True)
            self.scene.blockSignals(False)
        self.view.ensureVisible(it)

    def set_marks(
        self, mi: float | None, mo: float | None, focus: str | None = None
    ) -> None:
        """Метки In/Out: линии + подсветка диапазона на таймлайне.

        focus='in'/'out' - прокрутить вид к только что поставленной метке.
        """
        self._mark_in = mi
        self._mark_out = mo
        self._draw_marks()
        target = mi if focus == "in" else (mo if focus == "out" else None)
        if target is not None:
            x = target * self._cur_px
            h = RULER_H + len(self._tracks) * TRACK_H
            self.view.ensureVisible(x, h / 2, 80, h / 2)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # ширина вьюпорта изменилась - пересчитать fit-масштаб.
        # На минимуме (fit) пересчет идет через _effective_px, но при
        # задранном пользователем зуме не трогаем его выбор.
        if self._tracks and self.zoom.value() == self.zoom.minimum():
            self.refresh()
        # и на будущее открытие: отложенный пересчет после лейаута
        self._fit_timer.start()

    def changeEvent(self, event) -> None:  # noqa: N802
        """Смена темы (PaletteChange): перекрасить сцену под новую палитру."""
        super().changeEvent(event)
        try:
            from PySide6.QtCore import QEvent

            if event.type() == QEvent.Type.PaletteChange:
                self.refresh_theme()
        except Exception:
            pass

    def refresh_theme(self) -> None:
        """Перечитать цвета под текущую палитру, сохранив выделение."""
        try:
            selected = [
                it.clip_iid
                for it in self.scene.selectedItems()
                if isinstance(it, ClipItem)
            ]
        except Exception:
            selected = []
        playhead_t = self._playhead_t
        self.refresh()
        self._playhead_t = playhead_t
        self._draw_playhead()
        if selected:
            try:
                self.scene.blockSignals(True)
                for iid in selected:
                    it = self._items.get(iid)
                    if it is not None:
                        it.setSelected(True)
            finally:
                try:
                    self.scene.blockSignals(False)
                except Exception:
                    pass

    def _refresh_fit_if_min(self) -> None:
        """Отложенный refresh: после show/layout viewport может вырасти.

        Только при fit-зуме: пользовательский зум не трогаем.
        """
        if self._tracks and self.zoom.value() == self.zoom.minimum():
            vw = float(self.view.viewport().width())
            if abs(self._cur_px * self._content_seconds() - vw) > 2:
                self.refresh()

    # -- отрисовка --
    def refresh(self) -> None:
        selected = [
            item.clip_iid
            for item in self.scene.selectedItems()
            if isinstance(item, ClipItem)
        ]
        horizontal = self.view.horizontalScrollBar().value()
        vertical = self.view.verticalScrollBar().value()
        self.scene.blockSignals(True)
        try:
            self._rebuild_scene()
            for iid in selected:
                if iid in self._items:
                    self._items[iid].setSelected(True)
            self.view.horizontalScrollBar().setValue(horizontal)
            self.view.verticalScrollBar().setValue(vertical)
        finally:
            self.scene.blockSignals(False)
        self._on_sel()
        self.headers.update()

    def _rebuild_scene(self) -> None:
        # Перестройка уничтожит C++-объекты блоков. Незавершённый жест
        # отменяется до clear, чтобы MouseMove/Release не использовали их.
        self._cancel_drag()
        self.scene.clear()
        self._blade_line = None
        self._items = {}
        self._clip_media = {}
        self._playhead = None
        px = self._effective_px()
        self._cur_px = px
        total = self._content_seconds()
        vw_all = max(50.0, float(self.view.viewport().width()))
        y = RULER_H
        pal = _app_palette()
        role = QPalette.ColorRole
        scene_bg = pal.color(role.Base)
        track_bg = pal.color(role.AlternateBase)
        track_border = pal.color(role.Mid)
        ruler_pen = QPen(pal.color(role.Mid))
        text_brush = QBrush(pal.color(role.Text))
        self.scene.setBackgroundBrush(QBrush(scene_bg))
        try:
            self.view.setBackgroundBrush(QBrush(scene_bg))
        except Exception:
            pass
        # линейка
        step = 1.0
        while step * px < 60:
            step *= 2 if (step * px < 30) else 5 / 2
            if step > 3600:
                break
        t = 0.0
        while t <= total:
            x = t * px
            self.scene.addLine(x, 0, x, RULER_H - 6, ruler_pen)
            txt = self.scene.addSimpleText(f"{int(t // 60)}:{int(t % 60):02d}")
            txt.setBrush(text_brush)
            txt.setPos(x + 2, 2)
            t += step
        locked_links = {
            clip.link_iid
            for track in self._tracks
            if track.locked
            for clip in track.clips
            if clip.link_iid
        }
        for ti, tr in enumerate(self._tracks):
            yy = y + ti * TRACK_H
            self.scene.addRect(
                0,
                yy,
                total * px,
                TRACK_H,
                QPen(track_border),
                QBrush(track_bg),
            )
            for c in tr.clips:
                a = self._assets.get(c.asset_iid)
                name = (a.name if a else "?")[:18]
                if tr.kind == "text":
                    name = c.asset_iid.replace("\n", " ")[:48]
                    if not name:
                        name = "Пустой текст"
                    elif c.asset_iid.isspace():
                        name = "Пробельный текст"
                color = _clip_color(tr.kind, pal)
                label = f"{name}\n{c.duration:.1f}s"
                if c.link_iid:
                    label = "V+A · " + label
                wave = None
                wave_span = (c.src_in, c.src_out, 1.0)
                wave_mode = "full"
                # V1 - только кадры, A1 - только звук: волна и тег каналов
                # рисуются только на аудиодорожке, видеоклип всегда чистый.
                if tr.kind == "audio" and a is not None:
                    # метка каналов из probe: layout или число каналов
                    layout = (getattr(a, "channel_layout", "") or "").strip().upper()
                    ch = int(getattr(a, "channels", 0) or 0)
                    if layout in ("MONO", "STEREO") or (
                        layout and layout not in ("UNKNOWN",)
                    ):
                        tag = layout
                    elif ch == 1:
                        tag = "MONO"
                    elif ch >= 2:
                        tag = "STEREO"
                    else:
                        tag = ""
                    if tag:
                        # две строки всегда: третья не влезает в высоту клипа
                        label += f" {tag}"
                    pix = self._waves.get(c.asset_iid)
                    dur = float(getattr(a, "duration", 0) or 0)
                    if pix is not None and not pix.isNull() and dur > 0:
                        wave = pix
                        wave_span = (c.src_in, c.src_out, dur)
                    elif getattr(a, "wave_error", ""):
                        label += " (волна: ошибка)"
                    elif c.duration * px >= 24:
                        label += " (волна...)"
                strip = self._strips.get(c.iid)
                if (
                    tr.kind == "video"
                    and a is not None
                    and bool(getattr(a, "has_video", False))
                    and (strip is None or strip.isNull())
                    and c.duration * px >= 24
                ):
                    label += " (кадры...)"
                it = ClipItem(
                    c.iid,
                    tr.iid,
                    c.timeline_start * px,
                    yy + 4,
                    c.duration * px,
                    label,
                    color,
                    wave,
                    wave_span,
                    wave_mode,
                    strip,
                )
                self.scene.addItem(it)
                it.setToolTip(
                    ("Наложение: " if tr.kind == "text" else "")
                    + name
                    + f"\n{c.timeline_start:.3f} — {c.timeline_end:.3f} с"
                    + (
                        "\nВидео и звук связаны. ПКМ → Разделить видео и аудио"
                        if c.link_iid
                        else ""
                    )
                )
                if (
                    tr.locked
                    or c.link_iid in locked_links
                    or getattr(c, "_title_locked", False)
                ):
                    it.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
                if (
                    tr.hidden
                    or (tr.kind == "audio" and tr.mute)
                    or (tr.kind == "text" and c.mute)
                ):
                    it.setOpacity(0.45)
                if tr.kind == "audio" and (c.fade_in or c.fade_out):
                    it._audio_envelope = (
                        c.fade_in,
                        c.fade_out,
                        c.audio_offset,
                        c.audio_duration or c.duration,
                        c.duration,
                    )
                self._items[c.iid] = it
                self._clip_media[c.iid] = (c, a, tr.kind)
        self.scene.setSceneRect(0, 0, total * px, y + len(self._tracks) * TRACK_H + 10)
        # при fit-зуме сцена не должна шире вьюпорта: никакой прокрутки
        if self.zoom.value() == self.zoom.minimum() and px * total <= vw_all:
            self.view.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
        else:
            self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._draw_playhead()
        self._draw_marks()

    def update_media(self, key: str, mode: str) -> None:
        """Заменить только текстуру затронутых блоков без смены выбора/scroll."""
        for iid, (clip, asset, kind) in self._clip_media.items():
            item = self._items.get(iid)
            if item is None:
                continue
            if mode == "strip" and iid == key:
                item._strip = self._strips.get(key)
                item._label.setText(item._label.text().replace(" (кадры...)", ""))
                item.update()
            elif (
                mode == "wave"
                and kind == "audio"
                and clip.asset_iid == key
                and asset is not None
            ):
                item._wave = self._waves.get(key)
                item._wave_span = (clip.src_in, clip.src_out, asset.duration)
                item._label.setText(
                    item._label.text()
                    .replace(" (волна...)", "")
                    .replace(" (волна: ошибка)", "")
                )
                item.update()

    def _draw_marks(self) -> None:
        for it in self._mark_items:
            try:
                self.scene.removeItem(it)
            except Exception:
                pass
        self._mark_items = []
        px = self._cur_px
        h = RULER_H + len(self._tracks) * TRACK_H
        for marker in self._markers:
            color = QColor(marker.color)
            if not color.isValid():
                color = QColor("#ffb300")
            line = self.scene.addLine(
                marker.start * px, RULER_H, marker.start * px, h, QPen(color, 1.5)
            )
            line.setToolTip(marker.name + ("\n" + marker.note if marker.note else ""))
            self._mark_items.append(line)
            if marker.end:
                rect = self.scene.addRect(
                    marker.start * px,
                    RULER_H - 5,
                    (marker.end - marker.start) * px,
                    4,
                    QPen(color),
                    QBrush(color),
                )
                rect.setToolTip(marker.name)
                self._mark_items.append(rect)
        mi, mo = self._mark_in, self._mark_out
        if mi is not None and mo is not None and mo > mi:
            # подсветка выбранного диапазона
            rect = self.scene.addRect(
                mi * px,
                0,
                (mo - mi) * px,
                h,
                QPen(Qt.PenStyle.NoPen),
                QBrush(QColor(76, 175, 80, 60)),
            )
            rect.setZValue(80)
            self._mark_items.append(rect)
        if mi is not None:
            ln = self.scene.addLine(mi * px, 0, mi * px, h, QPen(QColor("#2e7d32"), 2))
            ln.setZValue(90)
            self._mark_items.append(ln)
        if mo is not None:
            ln = self.scene.addLine(mo * px, 0, mo * px, h, QPen(QColor("#c62828"), 2))
            ln.setZValue(90)
            self._mark_items.append(ln)

    def _draw_playhead(self) -> None:
        h = RULER_H + len(self._tracks) * TRACK_H
        x = self._playhead_t * self._cur_px
        if self._playhead is None:
            self._playhead = self.scene.addLine(x, 0, x, h, QPen(QColor("#e53935"), 2))
            self._playhead.setZValue(100)
        else:
            # во время воспроизведения вызывается часто: двигаем линию,
            # не пересоздавая (пересоздание = мигание и лишние items)
            self._playhead.setLine(x, 0, x, h)
        # автопрокрутка за плейхедом (на fit-зуме view вмещает всё - no-op)
        self.view.ensureVisible(self._playhead, 50, 0)

    def _on_sel(self) -> None:
        items = [
            item for item in self.scene.selectedItems() if isinstance(item, ClipItem)
        ]
        if not items:
            if self._selected_track:
                return
            self.clip_selected.emit("", "")
            self.text_selected.emit("")
            return
        chosen = self._moving if self._moving in items else items[0]
        self._selected_track = None
        self.scene.blockSignals(True)
        try:
            for item in items:
                if item is not chosen:
                    item.setSelected(False)
        finally:
            self.scene.blockSignals(False)
        track = next(
            (track for track in self._tracks if track.iid == chosen.track_iid), None
        )
        if track and track.kind == "text":
            self.clip_selected.emit("", "")
            self.text_selected.emit(chosen.clip_iid)
        else:
            self.text_selected.emit("")
            self.clip_selected.emit(chosen.clip_iid, chosen.track_iid)

    def _clip_menu(self, item: ClipItem) -> QMenu:
        if self.context_menu_factory:
            track = next(tr for tr in self._tracks if tr.iid == item.track_iid)
            return self.context_menu_factory(track.kind)
        menu = QMenu(self)
        track = next(track for track in self._tracks if track.iid == item.track_iid)
        clip = next(clip for clip in track.clips if clip.iid == item.clip_iid)
        if track.kind == "text":
            entries = (
                ("Изменить наложение…", "edit_text", True),
                ("Удалить наложение", "delete", True),
            )
        else:
            linked = bool(clip.link_iid)
            entries = (
                ("Разделить видео и аудио", "unlink", linked),
                (
                    "Удалить только звук",
                    "remove_audio",
                    linked or track.kind == "audio",
                ),
                (
                    "Удалить только видео",
                    "remove_video",
                    linked or track.kind == "video",
                ),
                ("Удалить выбранный блок", "delete", True),
                ("Удалить фрагмент со сдвигом", "ripple", True),
            )
        for title, key, enabled in entries:
            action = menu.addAction(title)
            action.setData(key)
            action.setEnabled(enabled and not track.locked)
        return menu

    def _context_menu(self, point) -> None:
        if self.context_menu_factory:
            item = self.view.itemAt(point)
            if isinstance(item, QGraphicsSimpleTextItem):
                item = item.parentItem()
            if not isinstance(item, ClipItem):
                return
            self._selected_track = None
            self.scene.blockSignals(True)
            self.scene.clearSelection()
            item.setSelected(True)
            self.scene.blockSignals(False)
            self._on_sel()
            track = next(tr for tr in self._tracks if tr.iid == item.track_iid)
            menu = self.context_menu_factory(track.kind)
            menu.exec(self.view.viewport().mapToGlobal(point))
            menu.deleteLater()
            return
        item = self.view.itemAt(point)
        while item is not None and not isinstance(item, ClipItem):
            item = item.parentItem()
        if not isinstance(item, ClipItem) or not self._is_current_item(item):
            return
        self.select_clip(item.clip_iid)
        self._on_sel()
        iid = item.clip_iid
        menu = self._clip_menu(item)
        chosen = menu.exec(self.view.viewport().mapToGlobal(point))
        if chosen is not None:
            self.clip_action_requested.emit(str(chosen.data()), iid)
        menu.deleteLater()

    # -- мышь: seek по линейке, move/trim клипов --
    def _cancel_drag(self) -> None:
        item = self._moving
        if item is not None and isValid(item) and self.scene.mouseGrabberItem() is item:
            item.ungrabMouse()
        self._moving = None
        self._mode = None
        self._trim_delta = None
        self._drag_peers = []
        self.view.viewport().unsetCursor()

    def _resize_drag(self, dx: float, modifiers: Qt.KeyboardModifier) -> None:
        item = self._moving
        if item is None:
            return
        clip, asset, kind = self._clip_media[item.clip_iid]
        ox, _ = self._orig
        if self._mode == "left":
            edge = self._snap(max(0.0, ox + dx), "left", modifiers)
            delta = (edge - ox) / self._cur_px
            lower = -clip.timeline_start
            if kind != "text":
                lower = max(lower, -clip.src_in / clip.speed)
            delta = max(lower, min(clip.duration - 1e-6, delta))
            x, width = ox + delta * self._cur_px, (clip.duration - delta) * self._cur_px
        else:
            end = ox + clip.duration * self._cur_px
            edge = self._snap(end + dx, "right", modifiers)
            delta = max(-clip.duration + 1e-6, (edge - end) / self._cur_px)
            if kind != "text" and asset is not None:
                delta = min(delta, max(0.0, asset.duration - clip.src_out) / clip.speed)
            x, width = ox, (clip.duration + delta) * self._cur_px
        self._trim_delta = delta
        for iid in self._drag_peers:
            peer = self._items.get(iid)
            if peer is not None and self._is_current_item(peer):
                peer.setPos(x, peer.pos().y())
                peer.setRect(QRectF(0, 0, max(8.0, width), TRACK_H - 8))
        self.view.viewport().setCursor(Qt.CursorShape.SizeHorCursor)

    def _is_current_item(self, item: ClipItem) -> bool:
        return (
            isValid(item)
            and self._items.get(item.clip_iid) is item
            and item.scene() is self.scene
        )

    def eventFilter(self, obj, event):  # noqa: N802
        from PySide6.QtCore import QEvent

        if not isValid(self.view):
            return False
        if obj is self.view.viewport():
            if self.tool == "blade":
                if event.type() == QEvent.Type.MouseMove:
                    self._show_blade(event.position().toPoint(), event.modifiers())
                    self.view.viewport().setCursor(Qt.CursorShape.CrossCursor)
                    return True
                if event.type() == QEvent.Type.Leave and self._blade_line is not None:
                    self._blade_line.hide()
                if (
                    event.type() == QEvent.Type.MouseButtonPress
                    and event.button() == Qt.MouseButton.LeftButton
                ):
                    target = self._blade_target(
                        event.position().toPoint(), event.modifiers()
                    )
                    if target:
                        self.split_clip_requested.emit(*target)
                    return True
            if self._moving is not None and not self._is_current_item(self._moving):
                self._cancel_drag()
            if (
                event.type() == QEvent.Type.Wheel
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                point = event.position().toPoint()
                time = self.view.mapToScene(point).x() / self._cur_px
                self.zoom.setValue(
                    self.zoom.value() + (10 if event.angleDelta().y() > 0 else -10)
                )
                left = self.view.mapToScene(0, 0).x()
                self.view.horizontalScrollBar().setValue(
                    self.view.horizontalScrollBar().value()
                    + round(time * self._cur_px - left - point.x())
                )
                return True
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                sp = self.view.mapToScene(event.position().toPoint())
                if sp.y() < RULER_H:
                    self.seeked.emit(max(0.0, sp.x() / self._cur_px))
                    return True
                item = self.view.itemAt(event.position().toPoint())
                if isinstance(item, (ClipItem, QGraphicsSimpleTextItem)):
                    clip_item = (
                        item if isinstance(item, ClipItem) else item.parentItem()
                    )
                    if isinstance(clip_item, ClipItem):
                        if (
                            not clip_item.flags()
                            & QGraphicsItem.GraphicsItemFlag.ItemIsMovable
                        ):
                            return False
                        local = clip_item.mapFromScene(sp)
                        self._mode = clip_item.edge_at(local) or "move"
                        self._moving = clip_item
                        self._press_x = sp.x()
                        self._drag_offset_y = local.y()
                        self._orig = (clip_item.pos().x(), clip_item.rect().width())
                        clip = self._clip_media[clip_item.clip_iid][0]
                        self._trim_delta = None
                        self._drag_peers = [
                            iid
                            for iid, (peer, _, _) in self._clip_media.items()
                            if iid == clip.iid
                            or (clip.link_iid and peer.link_iid == clip.link_iid)
                        ]
                        return False  # дальше штатный drag Qt
            elif event.type() == QEvent.Type.MouseMove:
                if (
                    self._moving is not None
                    and event.buttons() & Qt.MouseButton.LeftButton
                ):
                    sp = self.view.mapToScene(event.position().toPoint())
                    dx = sp.x() - self._press_x
                    ox, ow = self._orig
                    if self._mode == "move":
                        nx = max(0.0, ox + dx)
                        nx = self._snap(nx, "move", event.modifiers())
                        moving = self._moving
                        kind = self._clip_media[moving.clip_iid][2]
                        ny = (
                            sp.y() - self._drag_offset_y
                            if kind != "text"
                            else moving.y()
                        )
                        for iid in self._drag_peers:
                            peer = self._items.get(iid)
                            if peer is not None and self._is_current_item(peer):
                                peer.setPos(nx, ny if peer is moving else peer.y())
                    elif self._mode in ("left", "right"):
                        self._resize_drag(dx, event.modifiers())
                    return True
            elif event.type() == QEvent.Type.MouseButtonRelease:
                if self._moving is not None:
                    item = self._moving
                    mode = self._mode or "move"
                    trim_delta = self._trim_delta
                    self._cancel_drag()
                    self._commit_geometry(item, mode, trim_delta)
                    return True
            elif event.type() == QEvent.Type.DragEnter:
                event.acceptProposedAction()
                return True
            elif event.type() == QEvent.Type.DragMove:
                event.acceptProposedAction()
                return True
            elif event.type() == QEvent.Type.Drop:
                from .project_panel import ASSET_MIME

                data = event.mimeData().data(ASSET_MIME)
                if data:
                    sp = self.view.mapToScene(event.position().toPoint())
                    track_iid = self._track_at(sp.y())
                    if track_iid:
                        self.drop_asset.emit(
                            bytes(data).decode(),
                            track_iid,
                            max(0.0, sp.x() / self._cur_px),
                        )
                        event.acceptProposedAction()
                        return True
        return super().eventFilter(obj, event)

    def _track_at(self, y: float) -> str | None:
        for i, tr in enumerate(self._tracks):
            yy = RULER_H + i * TRACK_H
            if yy <= y < yy + TRACK_H:
                return tr.iid if tr.kind != "text" else None
        return next(
            (track.iid for track in reversed(self._tracks) if track.kind != "text"),
            None,
        )

    def _commit_geometry(
        self, item: ClipItem, mode: str = "move", trim_delta: float | None = None
    ) -> None:
        """Передать намерение контроллеру; видимые 8 px не задают длину."""
        from src.domain.video.models import ClipEdit

        if not self._is_current_item(item):
            return
        track = next((tr for tr in self._tracks if tr.iid == item.track_iid), None)
        clip = (
            next((c for c in track.clips if c.iid == item.clip_iid), None)
            if track
            else None
        )
        if clip is None or self._cur_px <= 0:
            return
        if track.kind == "text":
            ox, ow = self._orig
            start, end = clip.timeline_start, clip.timeline_end
            if mode == "move":
                start = max(0.0, item.pos().x() / self._cur_px)
                end = start + clip.duration
            elif mode == "left":
                start = max(
                    0.0,
                    min(
                        end - 0.001,
                        start
                        + (
                            trim_delta
                            if trim_delta is not None
                            else (item.pos().x() - ox) / self._cur_px
                        ),
                    ),
                )
            elif mode == "right":
                end = max(
                    start + 0.001,
                    end
                    + (
                        trim_delta
                        if trim_delta is not None
                        else (item.rect().width() - ow) / self._cur_px
                    ),
                )
            if (
                abs(start - clip.timeline_start) > 1e-9
                or abs(end - clip.timeline_end) > 1e-9
            ):
                self.text_edit_requested.emit(clip.iid, start, end)
            else:
                self.refresh()
            return
        target = track
        start, src_in, src_out = clip.timeline_start, clip.src_in, clip.src_out
        if mode == "move":
            start = max(0.0, item.pos().x() / self._cur_px)
            # Дорожку определяет место отпускания клипа, а не свободное
            # окно или порядок начала клипов. Несовместимый drop отменяется.
            center_y = item.sceneBoundingRect().center().y()
            index = int((center_y - RULER_H) // TRACK_H)
            if (
                not 0 <= index < len(self._tracks)
                or self._tracks[index].kind != track.kind
            ):
                self.refresh()
                return
            target = self._tracks[index]
        else:
            ox, ow = self._orig
            asset = self._assets.get(clip.asset_iid)
            duration = float(getattr(asset, "duration", 0) or 0)
            if duration <= 0:
                self.refresh()
                return
            if mode == "left":
                delta = (
                    trim_delta
                    if trim_delta is not None
                    else (item.pos().x() - ox) / self._cur_px
                )
                delta = max(
                    -min(src_in / clip.speed, start), min(clip.duration - 1e-6, delta)
                )
                start += delta
                src_in += delta * clip.speed
            elif mode == "right":
                delta = (
                    trim_delta
                    if trim_delta is not None
                    else (item.rect().width() - ow) / self._cur_px
                )
                src_out = max(
                    src_in + 1e-6, min(duration, src_out + delta * clip.speed)
                )
            else:
                self.refresh()
                return
        self.clip_edit_requested.emit(
            ClipEdit(clip.iid, target.iid, start, src_in, src_out)
        )
