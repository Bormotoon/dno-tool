"""Окно предпросмотра: QMediaPlayer + транспорт + текст на кадре."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtMultimedia import (
    QAudioOutput,
    QMediaDevices,
    QMediaPlayer,
    QVideoFrame,
    QVideoSink,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.icons import set_themed_icon
from src.utils.system.qt_multimedia import stop_media_player
from src.ui.widgets.device_list_toolbar import FLAT_TOOLBUTTON_QSS
from src.workers.video_editor.ffmpeg_utils import (
    overlay_origin,
)
from src.workers.video_editor.frame_worker import FrameExportWorker
from src.workers.video_editor.render_command import RenderCommand
from src.domain.video.render_plan import RenderPlan
from .preview_controller import PreviewController
from .frame_mailbox import FrameMailbox
from .graphic_loader import GraphicLoader
from src.utils.video_text import text_font, text_geometry


# Больший кадр перед поворотом уменьшается: на экране он всё равно не нужен целиком.
FRAME_TRANSFORM_MAX_SIDE = 1920
BOX_HANDLE = 5
BOX_GRAB = 9
BOX_SNAP_PX = 6


def transform_frame(
    image: QImage, rotation: int, flip_h: bool, flip_v: bool, max_side: int
) -> QImage:
    """Кадр в той же ориентации, что даёт экспорт: поворот, затем отражение."""
    if max(image.width(), image.height()) > max_side:
        image = image.scaled(
            max_side,
            max_side,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
    transform = QTransform()
    if flip_h or flip_v:
        transform.scale(-1 if flip_h else 1, -1 if flip_v else 1)
    if rotation:
        transform.rotate(rotation)
    return image.transformed(transform)


class _FrameView(QLabel):
    """Кадр с текстами: готовый QImage вписывается с полями.

    Геометрия титров сохраняет координаты исходника; растризация выполняется
    после уменьшения кадра до размера виджета, без копии полного 4K-кадра.
    Рамка выбранного клипа рисуется поверх и меняет модель только по отпусканию.
    """

    title_dragged = Signal(float, float)
    transform_committed = Signal(float, float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._img: QImage | None = None
        self.canvas_size = QSize()
        self.empty_text = (
            "Добавьте видео на таймлайн\nВыберите место разреза или добавьте текст"
        )
        self._overlays: list = []
        self._graphic_loader = GraphicLoader(self)
        self._graphic_loader.ready.connect(self.refresh)
        self._title_drag = False
        self._drag_start = None
        # Рамка: (смещение X, смещение Y, масштаб) в единицах модели клипа.
        self._box: tuple[float, float, float] | None = None
        self._box_mode = ""
        self._box_press = QPointF()
        self._box_start = (0.0, 0.0, 1.0)
        self._box_changed = False
        self.setMouseTracking(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: black; color: #b0b0b0;")
        self.setMinimumSize(64, 36)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def begin_title_drag(self) -> None:
        self._title_drag = True
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("Перетащите выбранный титр. Один жест — одна операция Undo.")

    def set_transform_box(self, values: tuple[float, float, float] | None) -> None:
        """Показать рамку клипа; во время жеста значение определяет сам жест."""
        if self._box_mode or values == self._box:
            return
        self._box = values
        if values is None:
            self.unsetCursor()
        self.update()

    def _canvas_rect(self) -> QRectF:
        """Область холста в координатах виджета, как её вписывает _refresh."""
        image = self._img
        if image is None or image.isNull():
            return QRectF()
        canvas = self.canvas_size if self.canvas_size.isValid() else image.size()
        fitted = canvas.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF(
            (self.width() - fitted.width()) / 2,
            (self.height() - fitted.height()) / 2,
            fitted.width(),
            fitted.height(),
        )

    def _box_rect(self) -> QRectF:
        area = self._canvas_rect()
        if self._box is None or area.isEmpty():
            return QRectF()
        x, y, scale = self._box
        width, height = area.width() * scale, area.height() * scale
        center = area.center() + QPointF(x * area.width(), y * area.height())
        return QRectF(
            center.x() - width / 2, center.y() - height / 2, width, height
        )

    def _box_hit(self, point: QPointF) -> str:
        rect = self._box_rect()
        if rect.isEmpty():
            return ""
        for corner in (
            rect.topLeft(),
            rect.topRight(),
            rect.bottomLeft(),
            rect.bottomRight(),
        ):
            delta = point - corner
            if abs(delta.x()) <= BOX_GRAB and abs(delta.y()) <= BOX_GRAB:
                return "scale"
        return "move" if rect.contains(point) else ""

    def _update_box_cursor(self, point: QPointF) -> None:
        hit = self._box_hit(point)
        if hit == "scale":
            rect = self._box_rect()
            falling = (point.x() < rect.center().x()) == (point.y() < rect.center().y())
            self.setCursor(
                Qt.CursorShape.SizeFDiagCursor
                if falling
                else Qt.CursorShape.SizeBDiagCursor
            )
        elif hit == "move":
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()

    def _drag_box(self, point: QPointF, modifiers: Qt.KeyboardModifier) -> None:
        area = self._canvas_rect()
        if area.isEmpty():
            return
        x, y, scale = self._box_start
        if self._box_mode == "move":
            delta = point - self._box_press
            x += delta.x() / area.width()
            y += delta.y() / area.height()
            # Центр притягивается к центру холста; Alt отключает, как на таймлайне.
            if not modifiers & Qt.KeyboardModifier.AltModifier:
                if abs(x) * area.width() <= BOX_SNAP_PX:
                    x = 0.0
                if abs(y) * area.height() <= BOX_SNAP_PX:
                    y = 0.0
        else:
            center = area.center() + QPointF(x * area.width(), y * area.height())
            start = self._box_press - center
            now = point - center
            distance = max(1.0, (start.x() ** 2 + start.y() ** 2) ** 0.5)
            scale *= (now.x() ** 2 + now.y() ** 2) ** 0.5 / distance
        self._box = (
            round(max(-4.0, min(4.0, x)), 4),
            round(max(-4.0, min(4.0, y)), 4),
            round(max(0.01, min(4.0, scale)), 4),
        )
        self._box_changed = self._box != self._box_start
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._box_mode:
            self._drag_box(event.position(), event.modifiers())
            event.accept()
            return
        if self._box is not None and not self._title_drag:
            self._update_box_cursor(event.position())
        super().mouseMoveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        rect = self._box_rect()
        if rect.isEmpty():
            return
        painter = QPainter(self)
        color = self.palette().color(QPalette.ColorRole.Highlight)
        painter.setPen(QPen(QColor("black"), 3))
        painter.drawRect(rect)
        painter.setPen(QPen(color, 1))
        painter.drawRect(rect)
        painter.setBrush(color)
        for corner in (
            rect.topLeft(),
            rect.topRight(),
            rect.bottomLeft(),
            rect.bottomRight(),
        ):
            painter.drawRect(
                QRectF(
                    corner.x() - BOX_HANDLE,
                    corner.y() - BOX_HANDLE,
                    BOX_HANDLE * 2,
                    BOX_HANDLE * 2,
                )
            )
        painter.end()

    def mousePressEvent(self, event) -> None:
        if self._title_drag and event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position()
            event.accept()
            return
        if self._box is not None and event.button() == Qt.MouseButton.LeftButton:
            self._box_mode = self._box_hit(event.position())
            if self._box_mode:
                self._box_press = event.position()
                self._box_start = self._box
                self._box_changed = False
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._box_mode and event.button() == Qt.MouseButton.LeftButton:
            self._box_mode = ""
            if self._box_changed and self._box is not None:
                self.transform_committed.emit(*self._box)
            event.accept()
            return
        if (
            self._title_drag
            and self._drag_start is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            delta = event.position() - self._drag_start
            image = self._img
            scale = (
                min(self.width() / image.width(), self.height() / image.height())
                if image and not image.isNull()
                else 0
            )
            self._drag_start = None
            self._title_drag = False
            self.unsetCursor()
            self.setToolTip("")
            if scale:
                self.title_dragged.emit(
                    delta.x() / (image.width() * scale),
                    delta.y() / (image.height() * scale),
                )
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def show_image(self, img: QImage | None, overlays: list | None = None) -> None:
        self._img = img
        self._overlays = list(overlays or [])
        self._refresh()

    def refresh(self) -> None:
        """Перемасштабировать кешированный кадр (ресайз окна)."""
        self._refresh()

    def _refresh(self) -> None:
        img = self._img
        if img is None or img.isNull():
            self.clear()
            self.setText(self.empty_text)
            return
        ratio = self.devicePixelRatioF()
        w = max(1, round(self.width() * ratio))
        h = max(1, round(self.height() * ratio))
        canvas = self.canvas_size if self.canvas_size.isValid() else img.size()
        if (
            img.width() == w
            and img.height() == h
            and not self._overlays
            and canvas == img.size()
        ):
            pixmap = QPixmap.fromImage(img)
            pixmap.setDevicePixelRatio(ratio)
            self.setPixmap(pixmap)
            return
        scaled = img.scaled(
            w,
            h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        if canvas != img.size():
            target = canvas.scaled(QSize(w, h), Qt.AspectRatioMode.KeepAspectRatio)
            scaled = QImage(target, QImage.Format.Format_RGB32)
            scaled.fill(QColor("black"))
            content = img.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter = QPainter(scaled)
            painter.drawImage(
                (target.width() - content.width()) // 2,
                (target.height() - content.height()) // 2,
                content,
            )
            painter.end()
        if self._overlays:
            # scaled может разделять буфер с исходником при равных размерах.
            if scaled.size() == img.size():
                scaled = scaled.copy()
            _paint_overlays(scaled, self._overlays, canvas, self._graphic_loader)
        pixmap = QPixmap.fromImage(scaled)
        pixmap.setDevicePixelRatio(ratio)
        self.setPixmap(pixmap)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refresh()


def _paint_overlays(
    img: QImage,
    items: list,
    source_size: QSize | None = None,
    graphic_loader: GraphicLoader | None = None,
) -> None:
    """Нарисовать текстовые блоки прямо на кадре (in-place).

    Геометрия - в пикселях кадра, без масштаба: бокс и origin как
    drawtext при экспорте (advance + pad*2 ширина, ink + pad*2 высота,
    origin блока = origin строки), якорь - overlay_origin: внешняя
    граница подложки ровно на margin от края угла. Поэтому отступы
    совпадают с готовым видео на любом размере окна превью.
    """
    from src.domain.video.models import TextOverlay

    if img.isNull():
        return
    size = source_size or img.size()
    fw, fh = size.width(), size.height()
    p = QPainter(img)
    p.scale(img.width() / fw, img.height() / fh)
    if graphic_loader is not None:
        graphic_loader.set_count(len(items))
    try:
        for it in items:
            ov = it if isinstance(it, TextOverlay) else TextOverlay.from_dict(it)
            if not (ov.enabled and ov.has_content):
                continue
            if ov.kind != "text":
                from src.utils.video_overlays import graphic_image, graphic_origin

                x, y = graphic_origin(ov, fw, fh)
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                try:
                    graphic = (
                        graphic_loader.image(ov)
                        if graphic_loader
                        else graphic_image(ov)
                    )
                    if graphic is not None:
                        p.save()
                        try:
                            p.translate(x + ov.width / 2, y + ov.height / 2)
                            p.rotate(ov.rotation)
                            p.drawImage(
                                QRectF(
                                    -ov.width / 2, -ov.height / 2, ov.width, ov.height
                                ),
                                graphic,
                            )
                        finally:
                            p.restore()
                except ValueError:
                    p.setPen(QColor("red"))
                    p.drawRect(x, y, ov.width, ov.height)
                    p.drawLine(x, y, x + ov.width, y + ov.height)
                    p.drawLine(x + ov.width, y, x, y + ov.height)
                continue
            lines = (ov.text or "").split("\n")
            if not any(lines):
                continue
            font = _preview_font(getattr(ov, "font_family", "") or "", ov.fontfile)
            try:
                px = max(1, int(getattr(ov, "fontsize", 24) or 24))
            except (TypeError, ValueError):
                px = 24
            font.setPixelSize(px)
            try:
                pad = max(0, min(30, int(getattr(ov, "box_pad", 6))))
            except (TypeError, ValueError):
                pad = 6
            geometry = text_geometry(font, ov.text)
            adv, ink = geometry.width, geometry.ink
            if ink.isNull():
                continue
            # размеры ТЕКСТА (без pad) - overlay_origin считает origin
            # текста как drawtext (text_w/text_h); бокс = текст + pad
            text_w = adv
            text_h = ink.height()
            try:
                mx = int(getattr(ov, "margin_x", getattr(ov, "margin", 20)))
            except (TypeError, ValueError):
                mx = 20
            try:
                my = int(getattr(ov, "margin_y", getattr(ov, "margin", 20)))
            except (TypeError, ValueError):
                my = 20
            ox, oy = overlay_origin(ov.corner, mx, my, pad, text_w, text_h, fw, fh)
            p.save()
            cx, cy = ox + text_w / 2, oy + text_h / 2
            p.translate(cx, cy)
            p.rotate(ov.rotation)
            p.translate(-cx, -cy)
            try:
                alpha = max(0, min(100, int(ov.back_alpha)))
            except (TypeError, ValueError):
                alpha = 50
            if alpha > 0:
                bg = QColor(getattr(ov, "back_color", "black") or "black")
                if not bg.isValid():
                    bg = QColor("black")
                bg.setAlpha(round(alpha * 255 / 100))
                p.fillRect(ox - pad, oy - pad, text_w + pad * 2, text_h + pad * 2, bg)
            fg = QColor(getattr(ov, "color", "white") or "white")
            p.setFont(font)
            p.setPen(fg if fg.isValid() else QColor("white"))
            for number, line in enumerate(lines):
                p.drawText(
                    QPoint(ox, oy - ink.top() + number * geometry.line_spacing), line
                )
            p.restore()
    finally:
        p.end()


def _mk_tool_btn(key: str, tip: str, parent=None) -> QToolButton:
    """Плоская кнопка с иконкой как в главных тулбарах (device_list_toolbar).

    Без рамки/фона, иконка 32x32 - однородно с тулбаром списка устройств.
    """
    btn = QToolButton(parent)
    set_themed_icon(btn, key)
    btn.setIconSize(QSize(32, 32))
    btn.setFixedSize(32, 32)
    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    btn.setStyleSheet(FLAT_TOOLBUTTON_QSS)
    btn.setToolTip(tip)
    btn.setAccessibleName(tip)
    return btn


def _fmt_tc(ms: int, fps: float = 25.0) -> str:
    s = max(0, ms // 1000)
    h, r = divmod(int(s), 3600)
    m, sec = divmod(r, 60)
    fr = int((ms % 1000) / 1000 * (fps or 25.0))
    return f"{h:02d}:{m:02d}:{sec:02d}:{fr:02d}"


def _preview_font(family: str, fontfile: str = "") -> QFont:
    """Общий шрифт для показа текста и его повёрнутого растра."""
    return text_font(family, fontfile)


class PreviewWidget(QWidget):
    seek_requested = Signal(float)  # seconds on timeline
    timeline_pos = Signal(float)  # позиция воспроизведения для плейхеда
    range_ended = Signal(bool)
    mark_in_requested = Signal()
    mark_out_requested = Signal()
    split_requested = Signal()
    delete_requested = Signal()
    cut_range_requested = Signal()
    keep_range_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.render_controller: PreviewController | None = None
        self._boundary_reached = False
        self._timeline_len = 0.0
        self._segments: list[tuple[float, float, str, float]] = []
        # (tl_start, tl_end, src_path, src_pos_at_start)
        self._seg_idx = 0
        self._frame_generation = 0
        self._frame_worker: FrameExportWorker | None = None
        self._suppress = False
        self._scrubbing = False
        self._seeking = False
        self._pending_pos_ms: int | None = None
        # воспроизведение продолжить после загрузки следующего сегмента
        self._resume_after_load = False
        # докачка кадра: seek на паузе бэкенд не отрисовывает, поэтому
        # после seek идет короткий play с нулевым звуком, а пауза встает
        # по готовому кадру (первый тик позиции для MKV приходит раньше
        # кадра - пауза по тику оставляла черный экран)
        self._priming = False
        self._prime_target_ms = 0
        self._prime_timer = QTimer(self)
        self._prime_timer.setSingleShot(True)
        self._prime_timer.setInterval(1500)
        self._prime_timer.timeout.connect(self._on_prime_timeout)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.stack = QWidget()
        # Кадр показывает обычный QLabel: мультимедийный бэкенд отдает
        # кадры в наш QVideoSink, тексты рисуются прямо на QImage
        # в пикселях исходника - один координатный базис с экспортом,
        # отступы совпадают на любом размере окна, "оторванности"
        # текста от видео нет в принципе (QVideoWidget + отдельный
        # слой виджетов давали дрейф округлений и лаги слоя за кадром).
        self.view = _FrameView(self.stack)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self._last_frame: QImage | None = None
        self._display_key: tuple | None = None
        self._display_image: QImage | None = None
        self._overlay_items: list = []
        # разрешение видеокадра (для API/совместимости; размер берется
        # из самих кадров)
        self._video_w = 0
        self._video_h = 0
        vlay = QVBoxLayout(self.stack)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.addWidget(self.view)
        layout.addWidget(self.stack, 1)
        self.controls = QWidget(self)
        layout.addWidget(self.controls)
        layout = QVBoxLayout(self.controls)
        layout.setContentsMargins(0, 0, 0, 0)
        self.frame_status = QLabel(self)
        self.frame_status.setWordWrap(True)
        self.frame_status.hide()
        layout.addWidget(self.frame_status)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        # Явное устройство вывода (иначе на некоторых системах WMF молчит)
        self.audio.setDevice(QMediaDevices.defaultAudioOutput())
        self._clip_volume = 1.0
        self._clip_muted = False
        self._master_volume = 1.0
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoOutput(self.sink)
        self.frame_mailbox = FrameMailbox(self)
        self.sink.videoFrameChanged.connect(
            self.frame_mailbox.offer, Qt.ConnectionType.DirectConnection
        )
        self.frame_mailbox.available.connect(
            self._consume_frame, Qt.ConnectionType.QueuedConnection
        )
        self.player.positionChanged.connect(self._on_pos)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorChanged.connect(self._on_error)
        self.err_label = QLabel("")
        self.err_label.setStyleSheet("color: #e53935;")
        self.err_label.setWordWrap(True)
        self.err_label.hide()

        self.time_label = QLabel("00:00:00:00 / 00:00:00:00")
        self.marks_label = QLabel("In/Out: -")
        tc_row = QHBoxLayout()
        tc_row.setContentsMargins(0, 0, 0, 0)
        tc_row.addWidget(self.time_label)
        self.timecode = QLineEdit(self)
        self.timecode.setPlaceholderText("Перейти к таймкоду")
        self.timecode.setMaximumWidth(168)
        self.timecode.setAccessibleName("Перейти к таймкоду")
        self.timecode.setToolTip(
            "Секунды, ЧЧ:ММ:СС.мс или ЧЧ:ММ:СС:кадры. Шаг соответствует CFR проекта; исходный VFR приблизителен."
        )
        self.timecode.returnPressed.connect(self._seek_timecode)
        tc_row.addWidget(self.timecode)
        tc_row.addStretch(1)
        tc_row.addWidget(self.marks_label)
        layout.addLayout(tc_row)
        from .audio_meter import AudioMeter

        self.audio_meter = AudioMeter(self.player)
        layout.addWidget(self.audio_meter)
        layout.addWidget(self.err_label)
        self.prepare_bar = QProgressBar(self)
        self.prepare_bar.setRange(0, 100)
        self.prepare_bar.setFormat("Подготовка предпросмотра… %p%")
        self.prepare_bar.setAccessibleName("Подготовка предпросмотра")
        self.prepare_bar.hide()
        layout.addWidget(self.prepare_bar)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 10000)
        # sliderMoved: тянуть = перематывать видео; sliderPressed - запомнить
        self.slider.sliderPressed.connect(self._on_scrub_start)
        self.slider.sliderReleased.connect(self._on_scrub_end)
        self.slider.sliderMoved.connect(self._on_slider)
        self.slider.valueChanged.connect(self._on_slider_value_changed)
        layout.addWidget(self.slider)

        row = QHBoxLayout()
        self.button_row = row
        self.btn_play = _mk_tool_btn("ve_play", "Play (Space)", self)
        self.btn_play.clicked.connect(self.toggle)
        self.btn_pause = _mk_tool_btn("ve_pause", "Pause", self)
        self.btn_pause.clicked.connect(self.pause)
        self.btn_stop = _mk_tool_btn("ve_stop", "Стоп и возврат в начало монтажа", self)
        self.btn_stop.clicked.connect(self.stop_and_rewind)
        self.btn_prev = _mk_tool_btn("ve_prev", "На кадр назад (Left)", self)
        self.btn_prev.clicked.connect(lambda: self.step_frame(-1))
        self.btn_next = _mk_tool_btn("ve_next", "На кадр вперед (Right)", self)
        self.btn_next.clicked.connect(lambda: self.step_frame(1))
        self.btn_mark_in = _mk_tool_btn("ve_mark_in", "Метка In (I)", self)
        self.btn_mark_in.clicked.connect(self.mark_in_requested.emit)
        self.btn_mark_out = _mk_tool_btn("ve_mark_out", "Метка Out (O)", self)
        self.btn_mark_out.clicked.connect(self.mark_out_requested.emit)
        self.btn_split = _mk_tool_btn("ve_split", "Разрезать (S)", self)
        self.btn_split.clicked.connect(self.split_requested.emit)
        self.btn_del = _mk_tool_btn(
            "ve_delete", "Удалить выбранный фрагмент и сдвинуть следующие", self
        )
        self.btn_del.clicked.connect(self.delete_requested.emit)
        self.btn_cut_range = _mk_tool_btn(
            "ve_cut_range", "Вырезать диапазон (I..O)", self
        )
        self.btn_cut_range.clicked.connect(self.cut_range_requested.emit)
        self.btn_keep_range = _mk_tool_btn(
            "ve_keep_range", "Оставить только фрагмент (I..O)", self
        )
        self.btn_keep_range.clicked.connect(self.keep_range_requested.emit)
        self.btn_start = _mk_tool_btn("ve_to_start", "В начало таймлайна", self)
        self.btn_start.clicked.connect(lambda: self.seek_requested.emit(0.0))
        # Слева проигрывание, справа операции с клипами и диапазоном.
        for b in (
            self.btn_start,
            self.btn_play,
            self.btn_pause,
            self.btn_stop,
            self.btn_prev,
            self.btn_next,
        ):
            row.addWidget(b)
        row.addStretch(1)
        for b in (
            self.btn_split,
            self.btn_del,
            self.btn_cut_range,
            self.btn_keep_range,
            self.btn_mark_in,
            self.btn_mark_out,
        ):
            row.addWidget(b)
        layout.addLayout(row)
        self._fps = 25.0

    # -- API, вызывается диалогом --
    def set_render_plan(self, plan: RenderPlan | None, text: str | None) -> None:
        if self.render_controller is None:
            self.render_controller = PreviewController(self)
        self.render_controller.configure(plan, text)

    def shutdown(self) -> None:
        self._preview_closed = True
        self.frame_mailbox.close()
        self._last_frame = None
        self.view.show_image(None)
        self.stop()
        # Сначала stop с освобождением GIL, затем отключение sink:
        # setVideoOutput сам ждёт видеорендерер и не должен встретить
        # выполняющийся Python-обработчик доставки кадра.
        self.player.setVideoOutput(None)
        self.sink.setVideoFrame(QVideoFrame())
        if self.render_controller is not None:
            self.render_controller.close()
        self.player.setSource(QUrl())

    def show_preparing(self, percent: int = 0) -> None:
        self.err_label.hide()
        self.prepare_bar.setValue(percent)
        self.prepare_bar.show()

    def hide_preparing(self) -> None:
        self.prepare_bar.hide()

    def set_transform_box(self, values: tuple[float, float, float] | None) -> None:
        self.view.set_transform_box(values)

    def clear_frame(self) -> None:
        self._last_frame = None
        self.view.show_image(None)

    def set_marks_text(self, mi: float | None, mo: float | None) -> None:
        """Строка In/Out справа от хронометража."""
        f = lambda v: f"{v:.2f}s" if v is not None else "-"
        self.marks_label.setText(f"In/Out: {f(mi)} / {f(mo)}")

    def _eff_volume(self) -> float:
        """Итоговая громкость вывода: клип * мастер, Mute клипа."""
        if self.render_controller is not None:
            if (
                self.render_controller.direct is not None
                and self.render_controller.plan is not None
            ):
                return self.render_controller.direct.volume_at(
                    self.render_controller.position, self.render_controller.plan.master
                )
            return 1.0
        vol = 0.0 if self._clip_muted else self._clip_volume * self._master_volume
        return max(0.0, min(1.0, vol))

    def _cancel_prime(self) -> None:
        """Снять докачку кадра: управление у пользователя.

        Паузу не трогает (вызыватели сами решают: toggle смотрит
        состояние, stop останавливает) - только флаг, таймер и звук.
        """
        if self._priming:
            self._priming = False
            try:
                self._prime_timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self.audio.setVolume(self._eff_volume())

    def _start_prime(self, target_ms: int) -> None:
        """Начать докачку: тихий play, пауза по готовому кадру."""
        self._priming = True
        self._prime_target_ms = int(target_ms)
        self.audio.setVolume(0.0)
        try:
            self._prime_timer.start()
        except Exception:  # noqa: BLE001
            pass
        self.player.play()

    def _finish_prime(self) -> None:
        """Завершить докачку: стоим на паузе с кадром, звук вернуть."""
        if not self._priming:
            return
        self._priming = False
        try:
            self._prime_timer.stop()
        except Exception:  # noqa: BLE001
            pass
        self.player.pause()
        self.audio.setVolume(self._eff_volume())

    def _on_prime_timeout(self) -> None:
        # кадры так и не пошли (битый источник без InvalidMedia):
        # дольше 1.5с тихий play не держим - встать как есть
        self._finish_prime()

    def set_audio(
        self, clip_volume: float, clip_muted: bool, master_volume: float
    ) -> None:
        """Громкость превью: клип * мастер, Mute клипа.

        0.0 - легальное значение (заглушить): никаких `or 1.0`, иначе
        ноль превращался в единицу и 0% в превью было неслышимым.
        """
        if self.render_controller is not None:
            return
        try:
            cv = float(clip_volume)
        except (TypeError, ValueError):
            cv = 1.0
        try:
            mv = float(master_volume)
        except (TypeError, ValueError):
            mv = 1.0
        self._clip_volume = max(0.0, min(2.0, cv))
        self._clip_muted = bool(clip_muted)
        self._master_volume = max(0.0, min(2.0, mv))
        self.audio.setVolume(self._eff_volume())

    def set_timeline(
        self,
        segments: list[tuple[float, float, str, float]],
        total: float,
        fps: float = 25.0,
    ) -> None:
        # идентичные сегменты (фоновый probe дергает refresh) - не трогать
        # плеер: иначе воспроизведение рвется на каждом обновлении списка
        if (
            segments == self._segments
            and total == self._timeline_len
            and self._fps == (fps or 25.0)
        ):
            return
        self._cancel_frame_save()
        self._boundary_reached = False
        self._segments = list(segments)
        self._timeline_len = total
        self._fps = fps or 25.0
        self._seg_idx = 0
        self._resume_after_load = False
        self.err_label.hide()
        if segments:
            _s0, _e0, _p0, base0 = segments[0]
            if self._load_seg(0):
                # источник сменился: позиция применится по LoadedMedia,
                # там же стартует докачка кадра (seek на паузе бэкенд
                # не отрисовывает - иначе окно висит черным)
                self._pending_pos_ms = int(base0 * 1000)
        else:
            self._suppress = False
            self._pending_pos_ms = None
            self._cancel_prime()
            self._resume_after_load = False
            self.player.setSource(QUrl())
            self._last_frame = None
            self.view.show_image(None)
            self.time_label.setText("00:00:00:00 / 00:00:00:00")
            self.slider.blockSignals(True)
            self.slider.setValue(0)
            self.slider.blockSignals(False)

    def _seek_timecode(self) -> None:
        from fractions import Fraction
        from src.domain.video.navigation import VideoNavigation

        try:
            position = VideoNavigation.parse_timecode(
                self.timecode.text(),
                Fraction(str(getattr(self, "_fps", 25) or 25)).limit_denominator(
                    1001000
                ),
            )
            if position > self._timeline_len:
                raise ValueError("Таймкод находится за концом монтажа.")
        except ValueError as error:
            self.timecode.setToolTip(str(error))
            self.timecode.selectAll()
            return
        self.seek_requested.emit(position)
        self.timecode.clear()

    def _on_frame(self, frame) -> None:
        """Новый кадр от бэкенда: нарисовать тексты, показать.

        Соединение queued - всегда в GUI-потоке. toImage делает копию,
        рисуем прямо на ней. Пауза докачки встает по первому показанному
        кадру: что на экране, то и валидно (отдельной проверки не надо).
        """
        if getattr(self, "_preview_closed", False):
            return
        if self.render_controller is not None and self.render_controller.in_gap:
            return
        try:
            img: QImage | None = None
            if frame.isValid() and frame.map(QVideoFrame.MapMode.ReadOnly):
                try:
                    conv = frame.toImage()
                    if not conv.isNull():
                        img = conv
                finally:
                    frame.unmap()
            if img is None or img.isNull():
                return
            self._last_frame = img
            self._compose()
            if self._priming:
                self._finish_prime()
        except Exception:  # noqa: BLE001
            pass

    @Slot()
    def _consume_frame(self) -> None:
        frame = self.frame_mailbox.take()
        if frame is not None:
            self._on_frame(frame)

    def _display_frame(self) -> QImage | None:
        """Кадр с поворотом клипа; исходный хранится, чтобы правка не ждала плеер."""
        base = self._last_frame
        if base is None or base.isNull() or self.render_controller is None:
            return base
        transform = self.render_controller.frame_transform()
        if not any(transform):
            return base
        key = (base.cacheKey(), transform)
        if key != self._display_key:
            self._display_image = transform_frame(
                base, *transform, FRAME_TRANSFORM_MAX_SIDE
            )
            self._display_key = key
        return self._display_image

    def bake_frame_transform(self) -> None:
        """Сохранить видимую ориентацию кадра перед уходом с прямого воспроизведения."""
        frame = self._display_frame()
        if frame is not None and frame is not self._last_frame:
            self._last_frame = frame

    def _compose(self) -> None:
        """Пересобрать видимый кадр из кеша + текущие тексты."""
        base = self._display_frame()
        if base is None or base.isNull():
            return
        try:
            items = list(self._overlay_items)
        except Exception:  # noqa: BLE001
            items = []
        if not items:
            self.view.show_image(base)
            return
        self.view.show_image(base, items)

    def set_overlays(self, items: list) -> None:
        """Синхронизировать блоки текстов; перерисовать из кеша кадра.

        Мгновенно, без участия плеера: тексты лежат на последнем кадре.
        """
        self._overlay_items = list(items or [])
        self._compose()

    def set_video_size(self, w: int, h: int) -> None:
        """Разрешение видеокадра (API для диалога)."""
        try:
            w, h = int(w or 0), int(h or 0)
        except (TypeError, ValueError):
            w, h = 0, 0
        if (w, h) == (self._video_w, self._video_h):
            return
        self._video_w, self._video_h = w, h
        self.view.canvas_size = QSize(w, h) if w > 0 and h > 0 else QSize()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # только перемасштаб кеша: позиции текстов в пикселях кадра,
        # пересчет не нужен
        try:
            self.view.refresh()
        except Exception:  # noqa: BLE001
            pass

    def toggle(self) -> None:
        if self.render_controller is not None:
            self.render_controller.toggle()
            return
        # докачка кадра держит play с нулевым звуком: управление
        # у пользователя, звук вернуть до переключения
        self._cancel_prime()
        if self.player.playbackState().value == 1:
            self.player.pause()
        else:
            self.player.play()

    def pause(self) -> None:
        self._cancel_prime()
        if self.render_controller is not None:
            self.render_controller.resume = False
        self.player.pause()

    def stop(self) -> None:
        if self.render_controller is not None:
            self.render_controller.stop()
        self._cancel_frame_save()
        self._resume_after_load = False
        self._pending_pos_ms = None
        self._suppress = False
        self._cancel_prime()
        stop_media_player(self.player)

    def stop_and_rewind(self) -> None:
        """Остановить монтаж и вернуть оба плейхеда в начало таймлайна."""
        self.stop()
        self.frame_mailbox.clear()
        self.seek_timeline(0)
        self._display_position(0)

    def step_frame(self, direction: int) -> None:
        from fractions import Fraction

        rate = Fraction(str(self._fps or 25)).limit_denominator(1001000)
        if self.render_controller is not None:
            position = self.render_controller.position
            frame = round(Fraction(str(position)) * rate)
            target = float(max(0, frame + direction) / rate)
            self.seek_requested.emit(min(target, self._timeline_len))
        else:
            frame = round(Fraction(self.player.position(), 1000) * rate)
            self.player.setPosition(round(max(0, frame + direction) * 1000 / rate))

    def seek_timeline(self, t: float) -> None:
        if self.render_controller is not None and not self.render_controller.seek(t):
            return
        if (
            self.render_controller is not None
            and self.render_controller.direct is not None
        ):
            t = self.render_controller.position
        self._boundary_reached = False
        self._seeking = True
        try:
            for i, (s, e, _p, base) in enumerate(self._segments):
                if s <= t < e:
                    # позиция внутри исходника = base + смещение внутри сегмента
                    self._seek_to_seg(i, int((base + (t - s)) * 1000))
                    return
            # t вне клипов: ставим начало ближайшего сегмента справа или конец
            if self._segments:
                nxt = next(
                    (i for i, (s, _e, _p, _b) in enumerate(self._segments) if s >= t),
                    None,
                )
                i = nxt if nxt is not None else len(self._segments) - 1
                s, _e, _p, base = self._segments[i]
                self._seek_to_seg(i, int(base * 1000))
        finally:
            self._seeking = False

    def _seek_to_seg(self, i: int, ms: int) -> None:
        """Перейти на сегмент i с позицией ms внутри исходника.

        Смена файла идет через асинхронную загрузку: позиция кладется
        в pending и применяется по LoadedMedia, а шедшее воспроизведение
        продолжается после загрузки (иначе клик по линейке во время play
        глушил плеер и оставлял черный кадр). Тот же файл - seek сразу.
        """
        from PySide6.QtMultimedia import QMediaPlayer as P

        if self._seg_idx != i:
            self._seg_idx = i
            was_playing = self.player.playbackState() == P.PlaybackState.PlayingState
            if self._load_seg(i):
                # источник сменился: позиция дождется LoadedMedia,
                # воспроизведение продолжим только если оно шло
                self._pending_pos_ms = ms
                self._resume_after_load = was_playing
                return
            self._resume_after_load = False
        self._set_position(ms)

    # -- internals --
    def _load_seg(self, i: int) -> bool:
        """Загрузить источник сегмента. True - файл сменился, идет
        асинхронная загрузка (позиция meaningful только после нее)."""
        if not (0 <= i < len(self._segments)):
            return False
        _s, _e, path, _base = self._segments[i]
        url = QUrl.fromLocalFile(path)
        # тот же файл - не перезагружать: reload рвал звук и позицию
        if self.player.source() == url:
            return False
        # глушим события позиции до конца загрузки: старый ms через
        # новую карту сегментов дал бы ложный скачок плейхеда
        self._suppress = True
        self.player.setSource(url)
        return True

    def _set_position(self, ms: int) -> None:
        """Seek сразу если медиа загружено, иначе pending до загрузки.

        Загружено - любое состояние кроме NoMedia/LoadingMedia: seek
        в EndOfMedia/Stopped прежним кодом уходил в pending навсегда
        (LoadedMedia без смены source больше не придет) - плейхед ехал,
        а картинка оставалась черной.
        """
        from PySide6.QtMultimedia import QMediaPlayer as P

        if self.player.mediaStatus() in (
            P.MediaStatus.NoMedia,
            P.MediaStatus.LoadingMedia,
        ):
            self._pending_pos_ms = ms
        else:
            self._pending_pos_ms = None
            self.player.setPosition(ms)
            if self.player.playbackState() != P.PlaybackState.PlayingState:
                # seek на паузе кадр не показывает - докачать тихим play
                # (пауза встанет по готовому кадру, см. _on_pos)
                self._start_prime(ms)

    def _on_slider(self, v: int) -> None:
        t = v / 10000 * self._timeline_len
        self.seek_requested.emit(t)

    def _on_scrub_start(self) -> None:
        self._scrubbing = True

    def _on_scrub_end(self) -> None:
        self._scrubbing = False

    def _on_slider_value_changed(self, v: int) -> None:
        # Клик по дорожке слайдера и программные апдейты тоже ведут к seek.
        # Во время драга (_scrubbing) события идут через sliderMoved,
        # обновление от плеера подавлено в _on_pos через blockSignals.
        if (
            self._scrubbing
            or self._suppress
            or self._seeking
            or self._timeline_len <= 0
        ):
            return
        t = v / 10000 * self._timeline_len
        self.seek_requested.emit(t)

    def _on_pos(self, ms: int) -> None:
        if self.render_controller is not None and self.render_controller.in_gap:
            return
        if not self._segments or self._seg_idx >= len(self._segments):
            return
        s, e, _p, base = self._segments[self._seg_idx]
        tl_now = s + (ms / 1000 - base)
        tl_now = max(0.0, min(self._timeline_len, tl_now))
        # во время перезагрузки источника позиция скачет - плейхед не двигать
        if self._seeking or self._suppress:
            return
        if ms / 1000 >= base + (e - s) and not self._boundary_reached:
            self._boundary_reached = True
            was_playing = self.player.playbackState().value == 1
            self._cancel_prime()
            self.player.pause()
            self.player.setPosition(round((base + e - s) * 1000))
            self.timeline_pos.emit(e)
            self.range_ended.emit(was_playing)
            return
        if self._priming:
            from PySide6.QtMultimedia import QMediaPlayer as P

            if (
                self.player.playbackState() == P.PlaybackState.PlayingState
                and ms > self._prime_target_ms
            ):
                # докачка завершена: позиция ушла за цель. Кадр на
                # экране держит QLabel (черного экрана не бывает -
                # виден последний пришедший кадр), основная пауза
                # встает по первому показанному кадру (см. _on_frame)
                self._finish_prime()
        self._display_position(tl_now)

    def _display_position(self, tl_now: float) -> None:
        self.timeline_pos.emit(tl_now)
        self.time_label.setText(
            f"{_fmt_tc(int(tl_now * 1000), self._fps)} / "
            f"{_fmt_tc(int(self._timeline_len * 1000), self._fps)}"
        )
        if not self.slider.isSliderDown():
            pos = int(tl_now / self._timeline_len * 10000) if self._timeline_len else 0
            self.slider.blockSignals(True)
            self.slider.setValue(pos)
            self.slider.blockSignals(False)

    def _on_status(self, status) -> None:
        if self.render_controller is not None and self.render_controller.in_gap:
            return
        try:
            from PySide6.QtMultimedia import QMediaPlayer as P

            if status in (
                P.MediaStatus.LoadedMedia,
                P.MediaStatus.BufferedMedia,
                P.MediaStatus.InvalidMedia,
            ):
                # загрузка кончилась (успех или битый файл): события
                # позиции снова meaningful
                self._suppress = False
            if status == P.MediaStatus.InvalidMedia:
                self._pending_pos_ms = None
                self._resume_after_load = False
                self._cancel_prime()
            # отложенный seek: позиция пришла до конца загрузки
            if (
                status in (P.MediaStatus.LoadedMedia, P.MediaStatus.BufferedMedia)
                and self._pending_pos_ms is not None
            ):
                ms = self._pending_pos_ms
                self._pending_pos_ms = None
                self.player.setPosition(ms)
                if (
                    not self._resume_after_load
                    and self.player.playbackState() != P.PlaybackState.PlayingState
                ):
                    # seek на паузе кадр не показывает - докачать тихим
                    # play (пауза встанет по готовому кадру, см. _on_pos)
                    self._start_prime(ms)
            if (
                status in (P.MediaStatus.LoadedMedia, P.MediaStatus.BufferedMedia)
                and self._resume_after_load
            ):
                # воспроизведение шло через смену файла: не останавливаться
                self._resume_after_load = False
                self.player.play()
                return
            # Автопереход на следующий сегмент монтажа
            if status == P.MediaStatus.EndOfMedia and not self._suppress:
                if self.render_controller is not None:
                    if not self._boundary_reached:
                        self._boundary_reached = True
                        self.timeline_pos.emit(self._segments[self._seg_idx][1])
                        self.range_ended.emit(self.render_controller.resume)
                    return
                nxt = self._seg_idx + 1
                if nxt < len(self._segments):
                    s_next = self._segments[nxt][0]
                    self._seg_idx = nxt
                    base_next = self._segments[nxt][3]
                    # "тот же файл" определяем ДО _load_seg: после setSource
                    # source() уже новый, reload-детект по нему не сработает
                    same_file = self.player.source() == QUrl.fromLocalFile(
                        self._segments[nxt][2]
                    )
                    self._load_seg(nxt)
                    if same_file:
                        # reload no-op, EndOfMedia сам не сбросится; статус
                        # EndOfMedia - источник уже загружен, seek напрямую
                        # (через _set_position уйдет в pending навсегда)
                        self.player.setPosition(int(base_next * 1000))
                        self.player.play()
                    else:
                        # новый файл: позиция и play после LoadedMedia
                        self._pending_pos_ms = int(base_next * 1000)
                        self._resume_after_load = True
                    self.timeline_pos.emit(s_next)
                else:
                    # закончился последний сегмент: плейхед в конец проекта
                    self._cancel_prime()
                    self.timeline_pos.emit(self._timeline_len)
        except Exception:
            pass

    # -- ПКМ: кадр --
    def _context_menu(self, pos) -> None:
        seg = (
            self._segments[self._seg_idx]
            if self._seg_idx < len(self._segments)
            else None
        )
        if seg is None:
            return
        menu = QMenu(self)
        act = menu.addAction("Сохранить кадр исходника как PNG")
        act.setEnabled(self._frame_worker is None and not self._suppress)
        montage = None
        if self.render_controller is not None:
            montage = menu.addAction("Сохранить кадр монтажа с обработкой как PNG")
            montage.setEnabled(act.isEnabled())
            plan = self.render_controller.plan
            position = self.render_controller.position
            act.setEnabled(
                act.isEnabled()
                and bool(
                    plan
                    and any(clip.start <= position < clip.end for clip in plan.video)
                )
            )
        cancel = None
        if self._frame_worker is not None:
            cancel = menu.addAction("Отменить сохранение кадра")
        chosen = menu.exec(self.view.mapToGlobal(pos))
        if chosen is act:
            self._save_frame()
        elif montage is not None and chosen is montage:
            self._save_frame(montage=True)
        elif cancel is not None and chosen is cancel:
            self._cancel_frame_save()

    def _save_frame(self, montage: bool = False) -> None:
        """Сохранить source-позицию в фоне, не добавляя src_in повторно."""
        if self._seg_idx >= len(self._segments) or self._frame_worker is not None:
            return
        _s, _e, path, _base = self._segments[self._seg_idx]
        ms = self._pending_pos_ms
        src_t = (self.player.position() if ms is None else ms) / 1000.0
        stream = 0
        if self.render_controller is not None and not montage:
            plan = self.render_controller.plan
            position = self.render_controller.position
            clip = (
                next(
                    (clip for clip in plan.video if clip.start <= position < clip.end),
                    None,
                )
                if plan
                else None
            )
            if clip is None:
                return
            path, src_t, stream = (
                clip.path,
                clip.src_in + (position - clip.start) * clip.speed,
                clip.stream,
            )
        filter_chain = ""
        if montage and self.render_controller is not None:
            # Прямое воспроизведение идёт из исходника: ориентацию задаём сами.
            filter_chain = RenderCommand.orientation_filters(
                *self.render_controller.frame_transform()
            ).rstrip(",")
        generation = self._frame_generation
        out, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить кадр монтажа" if montage else "Сохранить кадр исходника",
            "frame.png",
            "PNG (*.png)",
        )
        if not out or generation != self._frame_generation:
            return
        if not Path(out).suffix:
            out += ".png"
        protected = (
            self.render_controller.plan.sources
            if self.render_controller is not None and self.render_controller.plan
            else (path,)
        )
        worker = FrameExportWorker(
            path,
            src_t,
            out,
            generation,
            stream=stream,
            protected_sources=protected,
            overwrite=Path(out).exists(),
            filter_chain=filter_chain,
        )
        self._frame_worker = worker
        worker.finished.connect(self._on_frame_saved)
        worker.error.connect(self._on_frame_save_error)
        self.frame_status.setText("Сохранение кадра… ПКМ — отменить.")
        self.frame_status.show()
        worker.start(self)
        worker.thread().finished.connect(self._on_frame_save_stopped)

    def _cancel_frame_save(self) -> None:
        """Инвалидировать поздние уведомления при смене монтажа/закрытии."""
        self._frame_generation += 1
        if self._frame_worker is not None:
            self._frame_worker.cancel()
        self.frame_status.hide()

    def _frame_save_is_current(self) -> bool:
        return (
            self._frame_worker is not None
            and self._frame_worker.generation == self._frame_generation
        )

    @Slot(str)
    def _on_frame_saved(self, path: str) -> None:
        if self._frame_save_is_current():
            self.frame_status.setText(f"Кадр сохранён: {path}")

    @Slot(str)
    def _on_frame_save_error(self, error: str) -> None:
        if self._frame_save_is_current():
            self.frame_status.setText("Не удалось сохранить кадр.")
            QMessageBox.critical(self, "Кадр", error)

    @Slot()
    def _on_frame_save_stopped(self) -> None:
        self._frame_worker = None

    def _on_error(self) -> None:
        # битый источник: зависших pending/suppress не оставляем
        self._suppress = False
        try:
            err = self.player.error()
            msg = self.player.errorString()
            if err and msg:
                self.err_label.setText(f"Ошибка воспроизведения: {msg}")
                self.err_label.show()
            else:
                self.err_label.hide()
        except Exception:
            pass
