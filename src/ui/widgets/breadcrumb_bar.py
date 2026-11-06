"""Хлебные крошки (breadcrumb) для файловой панели SFTP-командера."""

import os
from pathlib import PurePath, PurePosixPath, PureWindowsPath

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QWidget,
)


class BreadcrumbBar(QWidget):
    """Адресная строка с кликабельными крошками и режимом редактирования.

    Режимы:
      - breadcrumb: кнопки сегментов пути.
      - edit: ``QLineEdit`` для ручного ввода.

    Переключение в режим редактирования: кнопка ✏ или ``enterEdit()``.
    Выход из режима: Enter (переход) / Esc (отмена) / потеря фокуса.
    """

    pathRequested = Signal(str)
    editRequested = Signal()

    def __init__(self, is_remote: bool, parent=None):
        super().__init__(parent)
        self.is_remote = is_remote
        self._path = ""
        self._build()
        self.set_path(self._path)

    def _build(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        self._crumb_layout = QHBoxLayout()
        self._crumb_layout.setContentsMargins(0, 0, 0, 0)
        self._crumb_layout.setSpacing(0)
        self._crumb_widget = QWidget(self)
        self._crumb_widget.setLayout(self._crumb_layout)
        self._scroll = QScrollArea(self)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setMinimumWidth(0)
        self._scroll.setWidget(self._crumb_widget)

        self._edit = QLineEdit(self)
        self._edit.setPlaceholderText("Путь...")
        self._edit.returnPressed.connect(self._on_edit_return)
        self._edit.installEventFilter(self)

        self._edit_btn = QPushButton("✏", self)
        self._edit_btn.setFixedWidth(28)
        self._edit_btn.setToolTip("Редактировать путь (Ctrl+L)")
        self._edit_btn.clicked.connect(self.enterEdit)
        self._edit_btn.setFocusPolicy(Qt.NoFocus)

        self._scroll.setFixedHeight(self._edit.sizeHint().height() + 4)
        outer.addWidget(self._scroll, 1)
        outer.addWidget(self._edit)
        outer.addWidget(self._edit_btn)
        self._edit.hide()

    # ------------------------------------------------------------------
    # Сегментация пути
    # ------------------------------------------------------------------

    def _parts(self, path: str) -> list:
        if self.is_remote:
            p = PurePosixPath(path)
        else:
            p = PureWindowsPath(path) if os.name == "nt" else PurePath(path)
        parts = list(p.parts)
        return parts

    def _join_parts(self, parts: list, upto: int) -> str:
        sub = parts[:upto]
        if self.is_remote:
            return str(PurePosixPath(*sub)) if sub else "/"
        if os.name == "nt":
            return str(PureWindowsPath(*sub)) if sub else os.path.sep
        return str(PurePath(*sub)) if sub else os.path.sep

    # ------------------------------------------------------------------
    # Установка пути
    # ------------------------------------------------------------------

    def set_path(self, path: str) -> None:
        self._path = path
        self._edit.setText(path)
        self._rebuild_crumbs()

    def path(self) -> str:
        return self._path

    def _rebuild_crumbs(self) -> None:
        clear_layout(self._crumb_layout)
        parts = self._parts(self._path)
        if not parts:
            btn = self._make_crumb("/", "/")
            self._crumb_layout.addWidget(btn)
            self._crumb_layout.addStretch(1)
            return

        for i, part in enumerate(parts):
            target = self._join_parts(parts, i + 1)
            if i > 0 and part == os.sep:
                continue
            if i > 0:
                sep = QFrame(self)
                sep.setFrameShape(QFrame.NoFrame)
                sep_lbl = QPushButton("›", self)
                sep_lbl.setFixedWidth(14)
                sep_lbl.setEnabled(False)
                sep_lbl.setStyleSheet(
                    "QPushButton{border:none;background:transparent;}"
                )
                self._crumb_layout.addWidget(sep_lbl)
            label = part if part else (os.sep if not self.is_remote else "/")
            if not label:
                label = "/"
            btn = self._make_crumb(label, target)
            self._crumb_layout.addWidget(btn)
        self._crumb_layout.addStretch(1)
        # Длинный путь прокручивается, сохраняя ширину обеих файловых панелей.
        QTimer.singleShot(0, self._scroll_to_end)

    def _scroll_to_end(self) -> None:
        """Показать последний сегмент длинного пути после раскладки."""
        bar = self._scroll.horizontalScrollBar()
        bar.setValue(bar.maximum())

    def _make_crumb(self, label: str, target: str) -> QPushButton:
        btn = QPushButton(label, self)
        btn.setFlat(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            "QPushButton { border: 1px solid transparent; padding: 2px 6px; "
            "background: transparent; border-radius: 4px; }"
            "QPushButton:hover { background: palette(midlight); "
            "border: 1px solid palette(mid); }"
            "QPushButton:pressed { background: palette(midlight); "
            "border: 1px solid palette(highlight); }"
        )
        btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        btn.clicked.connect(lambda _checked=False, t=target: self.pathRequested.emit(t))
        return btn

    # ------------------------------------------------------------------
    # Режим редактирования
    # ------------------------------------------------------------------

    def enterEdit(self) -> None:
        self._scroll.hide()
        self._edit_btn.hide()
        self._edit.setText(self._path)
        self._edit.show()
        self._edit.setFocus()
        self._edit.selectAll()

    def exitEdit(self) -> None:
        self._edit.hide()
        self._scroll.show()
        self._edit_btn.show()

    def _on_edit_return(self) -> None:
        path = self._edit.text().strip()
        self.exitEdit()
        if path:
            self.pathRequested.emit(path)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent

        if obj is self._edit and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.exitEdit()
                return True
        return super().eventFilter(obj, event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if self._edit.isVisible():
            self.exitEdit()


def clear_layout(layout) -> None:
    """Удалить все виджеты из layout."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.deleteLater()
