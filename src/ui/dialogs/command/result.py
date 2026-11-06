"""Диалоги отображения результатов выполнения команд с обновлением в реальном времени."""

import os

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
    QLabel,
    QTextEdit,
    QLineEdit,
    QDialogButtonBox,
    QCheckBox,
    QPushButton,
)
from PySide6.QtCore import Qt, QObject, QMetaObject, QTimer, Slot
from PySide6.QtGui import (
    QFont,
    QFontDatabase,
    QFontMetrics,
    QTextCharFormat,
    QTextCursor,
    QTextOption,
)
from src.workers import CommandWorker
from src.domain.models.device import DeviceModel
from src.domain.device_identity import device_identity, device_key
from src.utils.text.command_output import CommandOutputText

_TERMINAL_STATUSES = ("Успех", "Неудача", "Отменено", "Потеря связи")
# Живой вывод обновляется по таймеру; старые строки документа вытесняются.
_LIVE_RENDER_INTERVAL_MS = 100
_LIVE_MAX_LINES = 50_000
# Окно показывает начало и конец очень длинного вывода: раскладка при
# автопрокрутке занимает GUI-поток примерно на 300 мс на каждые 1 Ми символов
# и в другой поток не выносится. Полный текст сохраняется из таблицы результатов.
_DISPLAY_MAX_CHARS = 1536 * 1024
_DISPLAY_HEAD_CHARS = 256 * 1024
# Большой конец документа быстрее заменить целиком: setPlainText в 10 раз
# быстрее insertText, но теряет позицию чтения и восстанавливает её вручную.
_INSERT_TAIL_MAX_CHARS = 128 * 1024


def _limit_for_display(text: str) -> str:
    """Оставить начало и конец вывода, не помещающегося в окно."""
    if len(text) <= _DISPLAY_MAX_CHARS:
        return text
    skipped = len(text) - _DISPLAY_MAX_CHARS
    return (
        text[:_DISPLAY_HEAD_CHARS]
        + f"\n[… пропущено {skipped} символов; полный текст: "
        "«Сохранить текущий результат» в меню таблицы …]\n"
        + text[-(_DISPLAY_MAX_CHARS - _DISPLAY_HEAD_CHARS) :]
    )


class BaseCommandResultDialog(QDialog):
    """Base dialog for displaying command results"""

    def __init__(
        self,
        hostname: str,
        output: str,
        status: str,
        parent: None,
        progress_source: CommandWorker = None,
        device_iid: str = None,
        node_iid: str | None = None,
    ):
        super().__init__()
        self.setSizeGripEnabled(True)
        self.hostname = hostname
        self.device_iid = device_iid
        self.device_key = device_key(device_iid, node_iid) if device_iid else None
        self.progress_source = progress_source
        self._terminal = status in _TERMINAL_STATUSES
        self._updates_closed = False
        self._live_connections: list[QMetaObject.Connection] = []
        # Выбор переноса строк общий для всех окон вывода и переживает перезапуск.
        self._view_state = self._resolve_view_state()
        self._wrap_lines = False
        if self._view_state is not None:
            try:
                self._wrap_lines = (
                    self._view_state.get("command_result", "wrap_lines", False) is True
                )
            except Exception:
                self._wrap_lines = False
        self.finished.connect(self._disconnect_progress_source)
        self.setWindowTitle(f"Результат выполнения команды на {hostname}")
        self.setup_ui(hostname, output, status)
        self.resize(680, 400)

        # Если диалог открыт для уже завершённой команды - ввод не нужен
        if status and status in _TERMINAL_STATUSES:
            self._set_input_visible(False)

    def setup_ui(self, hostname: str, output: str, status: str):
        layout = QVBoxLayout()

        # Host and status info
        info_layout = QHBoxLayout()
        host_label = QLabel(f"Хост: {hostname}")
        self.status_label = QLabel(f"Статус: {status}")
        info_layout.addWidget(host_label)
        info_layout.addStretch()
        info_layout.addWidget(self.status_label)
        layout.addLayout(info_layout)

        # Result text field - консольный вид: моноширинный шрифт,
        # перенос строк выключен по умолчанию (галочка «Перенос строк»),
        # таб в 8 позиций символа.
        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        if os.name == "nt":
            self._output_font = QFontDatabase.systemFont(
                QFontDatabase.SystemFont.FixedFont
            )
        else:
            # В Linux системный FixedFont может разрешиться в шрифт интерфейса.
            # Подсказки результатов уже используют это семейство для вывода.
            self._output_font = QFont("DejaVu Sans Mono")
        self._output_font.setStyleHint(QFont.StyleHint.TypeWriter)
        self._output_font.setFixedPitch(True)
        self.text_edit.setFont(self._output_font)
        self.text_edit.document().setDefaultFont(self._output_font)
        self._output_char_format = QTextCharFormat()
        self._output_char_format.setFont(self._output_font)
        # Перенос строк (если включён) рвёт длинную строку ровно по краю поля,
        # как терминал: слова и токены без пробелов не «прыгают» целиком.
        self.text_edit.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        tab_width = QFontMetrics(self.text_edit.font()).horizontalAdvance(" ") * 8
        self.text_edit.setTabStopDistance(tab_width)
        self._output_text = CommandOutputText(_limit_for_display(output))
        self.text_edit.setPlainText(self._output_text.text())
        self._output_text.drain_completed()
        # Журнал отмены не нужен окну только для чтения и не должен копить вывод.
        self.text_edit.setUndoRedoEnabled(False)
        self._rendered_tail = self._output_text.current_line()
        self._pending_output: list[str] = []
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._flush_pending_output)
        self.text_edit.setFont(self._output_font)
        self.text_edit.document().setDefaultFont(self._output_font)
        layout.addWidget(self.text_edit)

        # Поле ввода для интерактивных команд (отправка в stdin активной SSH-сессии).
        # Обёрнуто в QWidget-контейнер, чтобы скрытие схлопывало строку без пустого места.
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText(
            "Введите текст и нажмите Enter (или просто Enter для подтверждения)…"
        )
        self.send_btn = QPushButton("Отправить")
        self.send_btn.clicked.connect(self._on_send_input)
        self.input_edit.returnPressed.connect(self._on_send_input)

        self.input_container = QWidget()
        input_layout = QHBoxLayout(self.input_container)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.addWidget(self.input_edit, 1)
        input_layout.addWidget(self.send_btn)
        layout.addWidget(self.input_container)

        self.scroll_checkbox = QCheckBox("Авто прокрутка")
        self.scroll_checkbox.setChecked(True)

        self.wrap_checkbox = QCheckBox("Перенос строк")
        self.wrap_checkbox.setToolTip(
            "Переносить строки, которые не помещаются в ширину поля"
        )
        self.wrap_checkbox.setChecked(self._wrap_lines)
        self.wrap_checkbox.toggled.connect(self._on_wrap_toggled)
        self._apply_wrap(self._wrap_lines)

        self.btnClose = QPushButton("Закрыть")
        self.btnClose.clicked.connect(self.reject)

        button_box = QHBoxLayout()
        button_box.addWidget(self.scroll_checkbox, 0, Qt.AlignmentFlag.AlignLeft)
        button_box.addSpacing(16)
        button_box.addWidget(self.wrap_checkbox, 0, Qt.AlignmentFlag.AlignLeft)
        button_box.addStretch()
        button_box.addWidget(self.btnClose)

        layout.addLayout(button_box)

        self.setLayout(layout)

    def update_output(self, device: DeviceModel, current_output: str) -> None:
        """Обновить вывод, сохраняя явное состояние ожидания ввода."""
        if self._terminal or self._updates_closed:
            return
        if self.device_key and device_identity(device) != self.device_key:
            return
        if not current_output:
            return
        self._pending_output.append(current_output)
        if not self._render_timer.isActive():
            self._render_timer.start(_LIVE_RENDER_INTERVAL_MS)
        if self.status_label.text() != "Статус: Ожидание ввода":
            self.status_label.setText("Статус: Выполнение")

    def _flush_pending_output(self) -> None:
        """Показать накопленное: сначала вывод, затем действие, зависящее от порядка."""
        self._render_timer.stop()
        if not self._pending_output:
            return
        pending, self._pending_output = self._pending_output, []
        if self._terminal or self._updates_closed:
            return
        self._output_text.feed("".join(pending))
        self._render_output()

    @Slot(DeviceModel, bool)
    def set_input_wait_changed(self, device: DeviceModel, waiting: bool) -> None:
        """Синхронизировать статус с SSH-исполнителем для своего устройства."""
        if self._terminal or self._updates_closed:
            return
        if self.device_key and device_identity(device) != self.device_key:
            return
        # Приглашение должно появиться раньше статуса ожидания ввода.
        self._flush_pending_output()
        status = "Ожидание ввода" if waiting else "Выполнение"
        self.status_label.setText(f"Статус: {status}")

    def _render_output(self) -> None:
        """Дописать новые строки: меняется только последняя незавершённая строка.

        Работает за O(новых данных), без полной копии текста. Старые строки
        документа вытесняются, поэтому длинный живой вывод не растит память.
        """
        lines = self._output_text.drain_completed()
        tail = self._output_text.current_line()
        if not lines and tail == self._rendered_tail:
            return
        document = self.text_edit.document()
        if document.maximumBlockCount() != _LIVE_MAX_LINES:
            document.setMaximumBlockCount(_LIVE_MAX_LINES)
        end = document.characterCount() - 1
        # QTextCursor positions count UTF-16 units (e.g. emoji take two).
        rendered = len(self._rendered_tail.encode("utf-16-le")) // 2
        self._replace_document_tail(end - rendered, "".join(lines) + tail)
        self._rendered_tail = tail

    def _render_final_output(self) -> None:
        """Показать итог: совпадающий текст не трогает позицию чтения."""
        text = self._output_text.text()
        document = self.text_edit.document()
        document.setMaximumBlockCount(0)
        old = self.text_edit.toPlainText()
        if text != old:
            # Общее начало ищется делением пополам: сравнение срезов идёт на C.
            low, high = 0, min(len(old), len(text))
            while low < high:
                middle = (low + high + 1) // 2
                if old[:middle] == text[:middle]:
                    low = middle
                else:
                    high = middle - 1
            if len(text) - low > _INSERT_TAIL_MAX_CHARS:
                self._replace_document(text)
            else:
                offset = len(old[:low].encode("utf-16-le")) // 2
                self._replace_document_tail(offset, text[low:])
        self._output_text.drain_completed()
        self._rendered_tail = self._output_text.current_line()

    def _replace_document(self, text: str) -> None:
        """Заменить весь документ; позиция прокрутки сохраняется, если не конец."""
        vertical = self.text_edit.verticalScrollBar()
        horizontal = self.text_edit.horizontalScrollBar()
        scroll_y, scroll_x = vertical.value(), horizontal.value()
        self.text_edit.setPlainText(text)
        self.text_edit.setFont(self._output_font)
        self.text_edit.document().setDefaultFont(self._output_font)
        if self.scroll_checkbox.isChecked():
            self.move_cursor_to_end()
        else:
            vertical.setValue(scroll_y)
            horizontal.setValue(scroll_x)

    def _replace_document_tail(self, offset: int, text: str) -> None:
        """Заменить конец документа, сохранив позицию чтения вручную."""
        saved_cursor = self.text_edit.textCursor()
        position, anchor = saved_cursor.position(), saved_cursor.anchor()
        vertical = self.text_edit.verticalScrollBar()
        horizontal = self.text_edit.horizontalScrollBar()
        scroll_y, scroll_x = vertical.value(), horizontal.value()
        cursor = QTextCursor(self.text_edit.document())
        cursor.setPosition(offset)
        cursor.movePosition(
            QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor
        )
        cursor.insertText(text, self._output_char_format)
        if self.scroll_checkbox.isChecked() and position == anchor:
            self.move_cursor_to_end()
        else:
            end = self.text_edit.document().characterCount() - 1
            saved_cursor.setPosition(min(anchor, end))
            saved_cursor.setPosition(
                min(position, end), QTextCursor.MoveMode.KeepAnchor
            )
            self.text_edit.setTextCursor(saved_cursor)
            vertical.setValue(scroll_y)
            horizontal.setValue(scroll_x)

    def _on_send_input(self):
        """Отправить содержимое поля ввода в активную SSH-сессию устройства.

        Пустая отправка разрешена - для промптов вида ``press enter`` / ``нажмите
        enter`` отправляется просто ``\\n``. Если активной сессии нет (команда
        завершена), ``send_input`` вернёт False и ввод будет проигнорирован.
        """
        if self._terminal or self._updates_closed:
            return
        text = self.input_edit.text()
        sent = False
        if self.progress_source and getattr(self, "device_iid", None):
            send_fn = getattr(self.progress_source, "send_input", None)
            if callable(send_fn):
                try:
                    sent = bool(send_fn(self.device_iid, text))
                except Exception:
                    sent = False
        if sent:
            # Эхо отправленного ввода в область вывода.
            # Пустой ввод (Enter) показываем как ⏎ - для наглядности при отправке
            # на промпты «press enter» / «нажмите enter».
            echo = text if text.strip() else "\u23ce"
            self._flush_pending_output()
            self._output_text.feed(f"\n[stdin] {echo}\n")
            self._render_output()
        self.input_edit.clear()

    def result_ready(
        self, device: DeviceModel, success: bool, output: str = ""
    ) -> None:
        """Зафиксировать итог и запретить запоздалое изменение статуса."""
        if self._terminal or self._updates_closed:
            return
        if self.device_key and device_identity(device) != self.device_key:
            return
        self._terminal = True
        self._render_timer.stop()
        self._pending_output.clear()

        try:
            if output:
                self._output_text.reset(_limit_for_display(output))
                self._render_final_output()

            if success:
                status_text = "Успех"
            elif output and (
                "connection lost" in output.lower() or "потеря связи" in output.lower()
            ):
                status_text = "Потеря связи"
            elif output and (
                "aborted" in output.lower() or "прервано" in output.lower()
            ):
                status_text = "Отменено"
            else:
                status_text = "Неудача"
            self.status_label.setText(f"Статус: {status_text}")
        except Exception:
            pass

        # Команда завершена - ввод больше не актуален
        self._set_input_visible(False)

    def _resolve_view_state(self):
        """Получить общий ViewState из DI-контейнера (None - персист недоступен)."""
        try:
            from src.architecture import ViewState
            from src.di import get_container

            state = get_container().resolve_optional(ViewState)
            if state is None:
                return None
            # persist=True подтягивает сохранённый выбор из QSettings
            # при первом открытом окне за сессию.
            state.register("command_result", persist=True)
            return state
        except Exception:
            return None

    def _apply_wrap(self, enabled: bool) -> None:
        """Включить перенос строк по ширине поля или вернуть прокрутку вбок."""
        self.text_edit.setLineWrapMode(
            QTextEdit.LineWrapMode.WidgetWidth
            if enabled
            else QTextEdit.LineWrapMode.NoWrap
        )

    def _on_wrap_toggled(self, enabled: bool) -> None:
        """Переключить перенос строк, сохранив позицию чтения и выбор в конфиге."""
        cursor = self.text_edit.textCursor()
        at_end = (
            cursor.position() == cursor.anchor()
            and cursor.position() == self.text_edit.document().characterCount() - 1
        )
        vertical = self.text_edit.verticalScrollBar()
        scroll_y = vertical.value()
        self._apply_wrap(enabled)
        if self.scroll_checkbox.isChecked() and at_end:
            self.move_cursor_to_end()
        else:
            vertical.setValue(scroll_y)
        self._wrap_lines = enabled
        if self._view_state is not None:
            try:
                self._view_state.set(
                    "command_result",
                    "wrap_lines",
                    enabled,
                    track_change=False,
                    source="command_result",
                )
            except Exception:
                pass

    def move_cursor_to_end(self) -> None:
        """Прокрутить вывод до конца, если разрешена автопрокрутка."""

        if not self.scroll_checkbox.isChecked():
            return

        # 1. Получаем копию текущего курсора
        cursor = self.text_edit.textCursor()

        # 2. Перемещаем курсор в конец документа
        # Используем QTextCursor.MoveOperation.End
        cursor.movePosition(QTextCursor.MoveOperation.End)

        # 3. Устанавливаем измененный курсор обратно в QTextEdit
        self.text_edit.setTextCursor(cursor)

        self.text_edit.ensureCursorVisible()

    def _set_input_visible(self, visible: bool):
        """Показать/скрыть строку ввода и кнопку отправки.

        Скрывает весь контейнер целиком, чтобы строка схлопывалась без пустого места.
        """
        try:
            self.input_container.setVisible(visible)
        except AttributeError:
            pass

    def _disconnect_progress_source(self) -> None:
        """Отключить все подписки при закрытии, включая кнопку и Escape."""
        self._updates_closed = True
        for connection in self._live_connections:
            try:
                QObject.disconnect(connection)
            except (RuntimeError, TypeError):
                pass
        self._live_connections.clear()

    def closeEvent(self, event):
        """Отключить подписки при закрытии окна."""
        self._disconnect_progress_source()
        super().closeEvent(event)


class CommandResultDialog(BaseCommandResultDialog):
    """Dialog for displaying command results with real-time updates"""

    def __init__(
        self,
        hostname: str,
        output: str,
        status: str,
        parent=None,
        worker: CommandWorker = None,
        device_iid: str = None,
        node_iid: str | None = None,
    ):
        super().__init__(
            hostname,
            output,
            status,
            parent,
            worker,
            device_iid=device_iid,
            node_iid=node_iid,
        )
        self.worker: CommandWorker = worker

        # Подписки хранятся для отключения при любом способе закрытия диалога.
        if worker is not None:
            self.main_window = parent
            self._live_connections.extend(
                [
                    worker.progress_update.connect(self.update_output),
                    worker.result_ready.connect(self.result_ready),
                ]
            )
            if hasattr(worker, "input_wait_changed"):
                self._live_connections.append(
                    worker.input_wait_changed.connect(self.set_input_wait_changed)
                )
