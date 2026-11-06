"""Модальный редактор удалённого файла с контролем кодировки и конфликтов."""

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QFileDialog,
                               QMenuBar, QLabel, QMessageBox, QInputDialog)

from src.ui.widgets.script_editor import ScriptEditor
from src.ui.widgets.syntax_highlight import syntax_language
from src.ui.icons import set_themed_icon
from src.utils.text.remote_text import RemoteTextFormat, ENCODINGS, MAX_REMOTE_TEXT_BYTES
from src.workers.remote_text import RemoteTextWorker


class RemoteFilePreviewDialog(QDialog):
    """Диалог владеет снимком сервера; локальная копия не меняет его состояние."""

    def __init__(self, filename, file_content=None, remote_path=None, sftp=None, parent=None, *, transport=None):
        super().__init__(parent)
        self.filename = filename
        self.remote_path = remote_path
        self.sftp = sftp
        self.transport = transport or (sftp.get_channel().get_transport() if sftp is not None else None)
        self._worker = None
        self._baseline = b''
        self._loaded = False
        self._close_pending = None
        self._format = RemoteTextFormat('utf-8', b'', '\n')
        self.file_content = ''
        self.setSizeGripEnabled(True)
        self.resize(1000, 700)
        layout = QVBoxLayout(self)
        menu = QMenuBar(self)
        menu.setNativeMenuBar(False)
        file_menu = menu.addMenu('📁 Файл')
        layout.setMenuBar(menu)
        self.remote_action = file_menu.addAction('Сохранить на сервер')
        set_themed_icon(self.remote_action, 'menu_save')
        self.remote_action.setShortcut(QKeySequence('Ctrl+S'))
        self.addAction(self.remote_action)
        self.remote_action.triggered.connect(self.save_to_remote)
        self.local_action = file_menu.addAction('Сохранить локальную копию')
        set_themed_icon(self.local_action, 'menu_export')
        self.local_action.setShortcut(QKeySequence('Ctrl+Shift+S'))
        self.local_action.triggered.connect(self.save_file)
        self.addAction(self.local_action)
        self.reload_action = file_menu.addAction('Перечитать с сервера')
        set_themed_icon(self.reload_action, 'menu_refresh')
        self.reload_action.triggered.connect(self.reload_file)
        file_menu.addSeparator()
        close_action = file_menu.addAction('Закрыть', self.reject)
        set_themed_icon(close_action, 'menu_exit')
        self.file_status = QLabel()
        self.file_status.setTextFormat(Qt.TextFormat.PlainText)
        self.file_status.setWordWrap(True)
        self.file_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.file_status)
        self.script_editor = ScriptEditor(self)
        self.script_editor.attach_menu_bar(menu)
        self.script_editor.save_action.setShortcut(QKeySequence())
        self.text_edit = self.script_editor.editor
        self.script_editor.set_language(syntax_language(filename))
        self.script_editor.save_requested.connect(self.save_to_remote)
        layout.addWidget(self.script_editor, 1)
        buttons = QHBoxLayout()
        self.save_local_btn = QPushButton('Локальная копия')
        self.save_local_btn.clicked.connect(self.save_file)
        buttons.addWidget(self.save_local_btn)
        buttons.addStretch()
        self.save_btn = QPushButton('Сохранить на сервер')
        self.save_btn.clicked.connect(self.save_to_remote)
        buttons.addWidget(self.save_btn)
        self.close_btn = QPushButton('Закрыть')
        self.close_btn.clicked.connect(self.reject)
        buttons.addWidget(self.close_btn)
        for button in (self.save_btn, self.save_local_btn, self.close_btn):
            button.setAutoDefault(False)
        layout.addLayout(buttons)
        self.text_edit.document().modificationChanged.connect(self.update_window_title)
        if file_content is not None:
            data = file_content.encode('utf-8') if isinstance(file_content, str) else file_content
            self._install_bytes(data)
        self._set_busy(False)
        if file_content is None and self.transport is not None:
            QTimer.singleShot(0, self.reload_file)

    @property
    def _is_modified(self):
        return self.text_edit.document().isModified()

    def _set_busy(self, busy):
        writable = self._loaded and not busy
        self.script_editor.setReadOnly(not writable)
        remote = writable and self.transport is not None and bool(self.remote_path)
        self.script_editor.set_save_enabled(remote)
        self.remote_action.setEnabled(remote)
        self.save_btn.setEnabled(remote)
        self.local_action.setEnabled(writable)
        self.save_local_btn.setEnabled(writable)
        self.reload_action.setEnabled(not busy and self.transport is not None and bool(self.remote_path))
        self.update_window_title()
        if busy:
            self.file_status.setText(self.file_status.text() + '  ·  Выполняется операция…')

    def update_window_title(self, *_):
        self.setWindowTitle(('* ' if self._is_modified else '') + f'Редактирование файла: {self.filename}')
        newline = {'\n': 'LF', '\r\n': 'CRLF', '\r': 'CR', None: 'Смешанные'}.get(self._format.newline)
        state = ('Изменён' if self._is_modified else 'Сохранён') if self._loaded else 'Не загружен'
        self.file_status.setText(f'{self.remote_path or self.filename}\n{self._format.encoding}'
                                 f'{" + BOM" if self._format.bom else ""}  ·  {newline}  ·  {state}')

    def _install_bytes(self, data):
        if len(data) > MAX_REMOTE_TEXT_BYTES:
            QMessageBox.warning(self, 'Файл слишком большой', 'Файл превышает 10 МБ')
            return False
        encoding = None
        while True:
            try:
                text, fmt = RemoteTextFormat.decode(data, encoding)
                break
            except UnicodeError as exc:
                encoding, ok = QInputDialog.getItem(self, 'Кодировка файла',
                    f'Не удалось декодировать файл: {exc}\nВыберите кодировку:', list(ENCODINGS), 4, False)
                if not ok:
                    return False
        self._format = fmt
        self._baseline = data
        self.file_content = text
        self._loaded = True
        self.text_edit.setPlainText(text)
        self.text_edit.document().setModified(False)
        self.update_window_title()
        return True

    def get_content(self):
        return self.text_edit.toPlainText()

    def _encoded_content(self):
        if not self._is_modified:
            return self._baseline
        if self._format.newline is None:
            choice, ok = QInputDialog.getItem(self, 'Окончания строк',
                'В файле смешанные окончания строк. Выберите единый формат:', ['LF', 'CRLF', 'CR'], 0, False)
            if not ok:
                return None
            self._format = replace(self._format, newline={'LF': '\n', 'CRLF': '\r\n', 'CR': '\r'}[choice])
            self.update_window_title()
        try:
            data = self._format.encode(self.get_content())
            if len(data) > MAX_REMOTE_TEXT_BYTES:
                raise ValueError('Файл превышает 10 МБ')
            return data
        except (UnicodeError, ValueError) as exc:
            QMessageBox.critical(self, 'Ошибка кодирования', str(exc))
            return None

    def save_file(self):
        if self._worker or not self._loaded:
            return False
        path, _ = QFileDialog.getSaveFileName(self, 'Сохранить локальную копию', self.filename)
        if not path:
            return False
        data = self._encoded_content()
        if data is None:
            return False
        try:
            Path(path).write_bytes(data)
            return True
        except OSError as exc:
            QMessageBox.critical(self, 'Ошибка сохранения', str(exc))
            return False

    def _start(self, operation, **kwargs):
        if self._worker or self.transport is None or not self.remote_path:
            return False
        self._worker = RemoteTextWorker(self.transport, self.remote_path, operation,
                                         baseline=self._baseline, parent=self, **kwargs)
        self._worker.finished.connect(self._operation_finished)
        self._set_busy(True)
        self._worker.start()
        return True

    def reload_file(self):
        if self._worker:
            return
        if self._is_modified and QMessageBox.question(self, 'Перечитать файл',
                'Удалить локальные изменения и загрузить файл с сервера?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            self._close_pending = None
            return
        self._close_pending = None
        self._start('load')

    def save_to_remote(self):
        if self._worker or not self._loaded:
            return False
        data = self._encoded_content()
        if data is None:
            self._close_pending = None
            return False
        return self._start('save', data=data)

    def _operation_finished(self):
        worker = self._worker
        self._worker = None
        self._set_busy(False)
        if worker.error is not None:
            self._close_pending = None
            QMessageBox.critical(self, 'Ошибка SFTP', worker.error)
        elif worker.result[0] == 'loaded':
            self._install_bytes(worker.result[1])
            self._set_busy(False)
        elif worker.result[0] == 'conflict':
            box = QMessageBox(self)
            box.setWindowTitle('Конфликт файла')
            box.setText(worker.result[1] + '\nПроверка не исключает изменений другими процессами во время записи.')
            reload_button = box.addButton('Перезагрузить', QMessageBox.ButtonRole.ActionRole)
            overwrite = box.addButton('Перезаписать', QMessageBox.ButtonRole.DestructiveRole)
            cancel = box.addButton('Отмена', QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(cancel)
            box.exec()
            if box.clickedButton() is reload_button:
                self.reload_file()
            elif box.clickedButton() is overwrite:
                self._start('save', data=worker.data, force=True)
            else:
                self._close_pending = None
        else:
            self._baseline = worker.result[1]
            self.file_content = self.get_content()
            self.text_edit.document().setModified(False)
            if self._close_pending is not None:
                result = self._close_pending
                self._close_pending = None
                super().done(result)
        worker.deleteLater()

    def _can_close(self, result):
        if self._worker:
            return False
        if not self._is_modified:
            return True
        reply = QMessageBox.question(self, 'Несохранённые изменения', 'Сохранить изменения файла?',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save)
        if reply == QMessageBox.StandardButton.Discard:
            return True
        if reply == QMessageBox.StandardButton.Save:
            if self.transport is not None and self.remote_path:
                self._close_pending = result
                if not self.save_to_remote():
                    self._close_pending = None
            elif self.save_file():
                return True
        return False

    def done(self, result):
        if self._can_close(result):
            super().done(result)

    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def accept(self):
        self.done(QDialog.DialogCode.Accepted)

    def closeEvent(self, event):
        if self._can_close(QDialog.DialogCode.Rejected):
            event.accept()
        else:
            event.ignore()
