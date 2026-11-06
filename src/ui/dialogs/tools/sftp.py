"""Диалог SFTP-файлового командера: двухпанельный интерфейс для обмена файлами с удалённым хостом.

Реализует полноценный файловый командер с:
- Двумя панелями (локальная/удалённая) на базе ``FilePanel``.
- Тулбаром над каждой панелью (вверх/назад/вперёд/домой/обновить/новая папка/...).
- Breadcrumb-адресной строкой с режимом редактирования.
- Drag & drop: межпанельный, внутрипанельный, внешний (Windows Explorer).
- Расширенным контекстным меню (открыть/переименовать/упаковать/распаковать/копировать путь/...).
- Горячими клавишами (F2/F3/F5/F6/F7/F8/Ctrl+R/Alt+↑/Backspace/Ctrl+L/Tab).
- Асинхронными операциями (передача/удаление/архивация) с прогресс-баром.
"""

import os
import uuid
from datetime import datetime
from pathlib import PurePosixPath

import paramiko
from PySide6.QtCore import Qt, QThread, QTimer, Slot
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
)

from src.domain.models.device import DeviceModel
from src.logger import logger
from src.ui.dialogs.editors.remote_file_preview import RemoteFilePreviewDialog
from src.ui.icons import set_themed_icon
from src.ui.widgets.file_panel import FileItem, FilePanel
from src.ui.widgets.remote_terminal import RemoteTerminalWidget
from src.utils.filesystem.sftp_files import local_child, remote_child
from src.utils.text.remote_text import MAX_REMOTE_TEXT_BYTES
from src.workers.sftp_archive import SFTPArchiveWorker
from src.workers.sftp_tasks import (
    ConflictRequest,
    ConnectionWorker,
    DeleteWorker,
    FileActionWorker,
    SftpTaskWorker,
    TransferItem,
    TransferWorker,
)


class SFTPDialog(QDialog):
    """Диалоговое окно SFTP-командера."""

    PREVIEW_MAX_SIZE = MAX_REMOTE_TEXT_BYTES

    def __init__(self, device: DeviceModel, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.device = device
        self.sftp: paramiko.SFTPClient | None = None
        self.ssh: paramiko.SSHClient | None = None
        self._transfer_worker: SftpTaskWorker | None = None
        self._connection_worker: ConnectionWorker | None = None
        self._active_panel: FilePanel | None = None
        self.session_id = uuid.uuid4().hex
        self._closing = False
        self._close_result = QDialog.Rejected

        self.setWindowTitle(f"SFTP — {self.device.name or self.device.host}")
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowFullscreenButtonHint
        )
        self.resize(980, 720)
        self._build_ui()
        self._setup_shortcuts()
        self._connect_async()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.panes_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.panes_splitter.setChildrenCollapsible(False)

        # Локальная панель
        self.local_panel = FilePanel(is_remote=False, device=None, dialog=self)
        self.panes_splitter.addWidget(self.local_panel)

        # Удалённая панель
        self.remote_panel = FilePanel(is_remote=True, device=self.device, dialog=self)
        self.panes_splitter.addWidget(self.remote_panel)

        # Тумблер терминала на тулбаре удалённой панели (toggle-иконка
        # icon/icon_on, как главные тулбары - с авто-refresh при смене темы)
        self.terminal_action = QAction("Терминал", self)
        set_themed_icon(self.terminal_action, "icon", toggle=True)
        self.terminal_action.setCheckable(True)
        self.terminal_action.setChecked(False)
        self.terminal_action.setVisible(False)
        self.terminal_action.toggled.connect(self._on_terminal_toggled)
        self.remote_panel.toolbar.addSeparator()
        self.remote_panel.toolbar.addAction(self.terminal_action)

        self.splitter.addWidget(self.panes_splitter)

        # Встроенная консоль (скрыта по умолчанию, показывается тумблером)
        self.terminal_widget = RemoteTerminalWidget(self)
        self.splitter.addWidget(self.terminal_widget)
        self.terminal_widget.hide()

        # Лог
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.document().setMaximumBlockCount(1500)
        self.log_edit.setMaximumHeight(160)
        self.splitter.addWidget(self.log_edit)

        self.splitter.setStretchFactor(0, 8)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setStretchFactor(2, 1)
        layout.addWidget(self.splitter)

        # Статус-бар
        self._setup_status_bar(layout)

        # Сигналы панелей
        self.local_panel.activated.connect(
            lambda: self._set_active_panel(self.local_panel)
        )
        self.remote_panel.activated.connect(
            lambda: self._set_active_panel(self.remote_panel)
        )
        # Синхронизация: навигация в удалённой панели → cd в терминале.
        # Односторонняя (panel → terminal); терминал работает в фоне даже
        # когда скрыт, поэтому cd уходит всегда, пока шелл запущен.
        # Обратная сторона (terminal → panel) — в _on_terminal_cwd_changed.
        self.remote_panel.pathChanged.connect(self._on_remote_path_changed)
        self.terminal_widget.commandEntered.connect(self._on_terminal_cwd_changed)

        # Исходные пути
        local_home = os.path.expanduser("~")
        self.local_panel.set_initial_path(local_home)
        self.remote_panel.current_path = ""
        self.remote_panel.breadcrumb.set_path("/")

        self._set_active_panel(self.local_panel)
        self.panes_splitter.setSizes([self.width() // 2, self.width() // 2])

    def _setup_status_bar(self, layout: QVBoxLayout) -> None:
        self.statusbar = QStatusBar()
        self.statusbar.setSizeGripEnabled(False)

        self.op_label = QLabel("")
        self.op_label.setMinimumWidth(110)
        self.op_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.op_label.setVisible(False)

        self.status_label = QLabel("")
        self.status_label.setMinimumWidth(200)

        self.status_progress = QProgressBar()
        self.status_progress.setRange(0, 100)
        self.status_progress.setMinimumWidth(260)
        self.status_progress.setMaximumWidth(440)
        self.status_progress.setTextVisible(True)
        self.status_progress.setVisible(False)

        self.status_cancel_btn = QPushButton("Отмена")
        self.status_cancel_btn.setVisible(False)
        self.status_cancel_btn.clicked.connect(self._cancel_transfer)

        self.reconnect_btn = QPushButton("Переподключить")
        self.reconnect_btn.setVisible(False)
        self.reconnect_btn.clicked.connect(self.reconnect_to_remote)

        self.statusbar.addWidget(self.op_label, 0)
        self.statusbar.addWidget(self.status_label, 1)
        self.statusbar.addPermanentWidget(self.status_progress, 0)
        self.statusbar.addPermanentWidget(self.status_cancel_btn, 0)
        self.statusbar.addPermanentWidget(self.reconnect_btn, 0)

        layout.addWidget(self.statusbar)

    def refresh_icons(self) -> None:
        """Перечитать иконки под текущую тему иконок (live-переключение).

        Реестр themed-виджетов (тулбары панелей, тумблер терминала,
        кнопка синхронизации терминала) обновляется глобально, панели
        дополнительно перечитывают иконки папок в листинге.
        """
        from src.ui.icons import refresh_themed_icons

        try:
            refresh_themed_icons()
        except Exception as e:
            logger.debug(f"SFTPDialog: refresh_themed_icons: {e}")
        for panel in (
            getattr(self, "local_panel", None),
            getattr(self, "remote_panel", None),
        ):
            refresh = getattr(panel, "refresh_icons", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception as e:
                    logger.debug(f"SFTPDialog: panel refresh_icons: {e}")

    def _setup_shortcuts(self) -> None:
        # Горячие клавиши навигации привязаны к деревьям панелей
        # (WidgetShortcut): они срабатывают только когда фокус в панели файлов
        # и не перехватывают клавиши встроенного терминала (Backspace/Delete/
        # Tab/Ctrl+R/Ctrl+L/F5/F3...) при активном shell.
        bindings = [
            ("Ctrl+R", self._refresh_active),
            ("F5", self._transfer_active),
            ("F7", self._mkdir_active),
            ("F8", self._delete_active),
            ("Delete", self._delete_active),
            ("F2", self._rename_active),
            ("Alt+Up", self._up_active),
            ("Backspace", self._back_active),
            ("Ctrl+L", self._edit_path_active),
            ("F3", self._view_active),
        ]
        for panel in (self.local_panel, self.remote_panel):
            for key, slot in bindings:
                sc = QShortcut(
                    QKeySequence(key),
                    panel.view,
                    context=Qt.ShortcutContext.WidgetShortcut,
                )
                sc.activated.connect(slot)
            # Tab — переключение панелей
            sc_tab = QShortcut(
                QKeySequence("Tab"),
                panel.view,
                context=Qt.ShortcutContext.WidgetShortcut,
            )
            sc_tab.activated.connect(self._switch_panels)

    # ------------------------------------------------------------------
    # Активная панель
    # ------------------------------------------------------------------

    def _set_active_panel(self, panel: FilePanel) -> None:
        self._active_panel = panel
        try:
            panel.view.setFocus()
        except Exception:
            pass

    def _active_or_local(self) -> FilePanel:
        return self._active_panel or self.local_panel

    def _refresh_active(self):
        self._active_or_local().refresh()

    def _transfer_active(self):
        self._active_or_local().transfer_to_other()

    def _mkdir_active(self):
        self._active_or_local().create_folder()

    def _delete_active(self):
        self._active_or_local().delete_selected()

    def _rename_active(self):
        self._active_or_local().rename_selected()

    def _up_active(self):
        self._active_or_local().go_up()

    def _back_active(self):
        self._active_or_local().go_back()

    def _edit_path_active(self):
        self._active_or_local().breadcrumb.enterEdit()

    def _view_active(self):
        panel = self._active_or_local()
        items = panel.selected_items()
        if len(items) == 1 and panel.is_remote:
            self.preview_remote_file(items[0])

    def _switch_panels(self):
        if self._active_panel is self.local_panel:
            self._set_active_panel(self.remote_panel)
        else:
            self._set_active_panel(self.local_panel)

    # ------------------------------------------------------------------
    # Подключение
    # ------------------------------------------------------------------

    def _connect_async(self) -> None:
        if self._closing or self._connection_worker is not None:
            return
        self.status_label.setText(
            f"Подключение к {self.device.name or self.device.host}..."
        )
        self.status_progress.setVisible(True)
        self.status_progress.setRange(0, 0)
        self._connection_worker = ConnectionWorker(self.device, self)
        self._connection_worker.finished.connect(self._on_connection_finished)
        self._connection_worker.start()

    def _on_connected(self, ssh, sftp, home: str) -> None:
        self.ssh = ssh
        self.sftp = sftp
        self.log_operation(f"Подключено к {self.device.name or self.device.host}")
        self.reconnect_btn.setVisible(False)
        self.remote_panel.set_initial_path(home)
        self.terminal_action.setVisible(True)
        self.terminal_action.setChecked(False)
        self.terminal_widget.hide()

    @Slot(str, str)
    def _on_connection_error(self, title, message) -> None:
        self.terminal_widget.stop()
        self.terminal_action.setVisible(False)
        self.terminal_action.setChecked(False)
        self.terminal_widget.hide()
        self._show_error(title, message)
        self.log_operation(f"{title}: {message}")
        self.reconnect_btn.setVisible(True)

    @Slot()
    def _on_connection_finished(self) -> None:
        worker = self._connection_worker
        self._connection_worker = None
        self.status_progress.setVisible(False)
        self.status_label.setText("")
        if worker is None:
            return
        if self._closing:
            if worker.sftp is not None:
                worker.sftp.close()
            if worker.ssh is not None:
                worker.ssh.close()
        elif worker.error is not None or worker.ssh is None:
            self._on_connection_error(
                "Ошибка подключения", worker.error or "Нет SSH-соединения"
            )
        else:
            self._on_connected(worker.ssh, worker.sftp, worker.home)
            worker.ssh = None
            worker.sftp = None
        worker.deleteLater()

    def reconnect_to_remote(self) -> None:
        if self.operation_busy():
            return
        self.remote_panel.cancel_listing()
        self.terminal_widget.stop()
        self._cleanup_connections()
        self._connect_async()

    def _on_terminal_toggled(self, checked: bool) -> None:
        if (
            checked
            and self.ssh is not None
            and self.terminal_widget.terminal_view._worker is None
        ):
            self.terminal_widget.start(
                self.ssh,
                self.device.name or self.device.host,
                initial_cwd=self.remote_panel.current_path or "/",
            )
        self.terminal_widget.setVisible(checked)
        if checked:
            self.terminal_widget._update_size()
            self.terminal_widget.setFocus()

    @Slot(str)
    def _on_remote_path_changed(self, path: str) -> None:
        if not path or self._closing:
            return
        self.terminal_widget.terminal_view.set_shell_cwd(path)
        if self.terminal_widget.act_follow_panel.isChecked():
            self.terminal_widget.send_cd(path)

    @Slot(str)
    def _on_terminal_cwd_changed(self, target: str) -> None:
        view = self.terminal_widget.terminal_view
        resolved = view.resolve_cd_target(target)
        if (
            not resolved
            or self.ssh is None
            or not self.terminal_widget.act_follow_terminal.isChecked()
        ):
            return
        if self.remote_panel.current_path != resolved:
            self.remote_panel.navigate(resolved)

    # ------------------------------------------------------------------
    # Лог
    # ------------------------------------------------------------------

    def log_operation(self, message: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_edit.appendPlainText(f"[{ts}] {message}")
        logger.info(message)

    # ------------------------------------------------------------------
    # Передача файлов (вызывается из FilePanel.transfer_to_other)
    # ------------------------------------------------------------------

    def transfer_items(self, source_panel: FilePanel, items: list) -> None:
        if not items:
            return
        if source_panel.is_remote:
            # remote → local: скачивание в текущую локальную папку
            dest = self.local_panel.current_path
            self._do_download(items, source_panel.current_path, dest)
        else:
            # local → remote: загрузка в текущую удалённую папку
            dest = self.remote_panel.current_path
            self._do_upload(items, source_panel.current_path, dest)

    def _do_download(self, items, remote_base: str, local_base: str) -> None:
        self._queue_transfer(items, remote_base, local_base, False)

    def _do_upload(self, items, local_base: str, remote_base: str) -> None:
        self._queue_transfer(items, local_base, remote_base, True)

    def _describe_worker(self, worker: SftpTaskWorker) -> tuple[str, str]:
        return worker.badge, worker.description

    def _set_op_badge(self, text: str) -> None:
        self.op_label.setText(text)
        self.op_label.setVisible(True)

    def _clear_op_badge(self) -> None:
        self.op_label.setText("")
        self.op_label.setVisible(False)

    def _start_transfer(self, worker: SftpTaskWorker) -> None:
        if self.operation_busy():
            worker.deleteLater()
            return
        self._transfer_worker = worker
        worker.progress.connect(self._on_transfer_progress)
        worker.finished.connect(self._on_operation_finished)
        worker.log_message.connect(self.log_operation)
        worker.conflict.connect(self._resolve_conflict)
        self.status_progress.setRange(0, 0)
        self.status_progress.setVisible(True)
        self.status_cancel_btn.setVisible(True)
        self.status_cancel_btn.setEnabled(True)
        self._set_op_badge(worker.badge)
        self.status_label.setText(worker.description)
        worker.start()

    @Slot(int, str)
    def _on_transfer_progress(self, percent, message) -> None:
        if percent < 0:
            # Фаза подсчета/подготовки: бегущий индикатор вместо висящих 0%.
            self.status_progress.setRange(0, 0)
            self.status_progress.setFormat("подготовка...")
        else:
            if self.status_progress.maximum() == 0:
                self.status_progress.setRange(0, 100)
            self.status_progress.setRange(0, 100)
            self.status_progress.setValue(max(0, min(100, percent)))
            self.status_progress.setFormat("%p%")
        self.status_label.setText(message)
        self.status_label.setToolTip(message)

    def _cancel_transfer(self) -> None:
        if self._transfer_worker is not None:
            self._transfer_worker.cancel()
            self.status_label.setText("Отмена операции...")
            self.status_cancel_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # Удаление (вызывается из FilePanel)
    # ------------------------------------------------------------------

    def delete_local_items(self, items: list) -> None:
        if self.operation_busy() or not self._confirm_delete_multiple(items):
            return
        paths = [
            (
                os.path.join(self.local_panel.current_path, it.text()),
                bool(it.data(Qt.UserRole + 2)),
            )
            for it in items
        ]
        self._start_transfer(DeleteWorker(None, paths, False, self))

    def delete_remote_items(self, items: list) -> None:
        if (
            self.operation_busy()
            or self.ssh is None
            or not self._confirm_delete_multiple(items)
        ):
            return
        paths = [
            (
                remote_child(self.remote_panel.current_path, it.text()),
                bool(it.data(Qt.UserRole + 2)),
            )
            for it in items
        ]
        self._start_transfer(DeleteWorker(self.ssh.get_transport(), paths, True, self))

    def _confirm_delete_multiple(self, items) -> bool:
        if not items:
            return False
        if len(items) == 1:
            it = items[0]
            is_dir = bool(it.data(Qt.UserRole + 2))
            obj = "папку" if is_dir else "файл"
            tail = " и всё её содержимое" if is_dir else ""
            reply = QMessageBox.question(
                self,
                "Подтверждение удаления",
                f"Удалить {obj} '{it.text()}'{tail}?",
                QMessageBox.Yes | QMessageBox.No,
            )
            return reply == QMessageBox.Yes
        reply = QMessageBox.question(
            self,
            "Подтверждение удаления",
            f"Удалить {len(items)} объектов?",
            QMessageBox.Yes | QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    # ------------------------------------------------------------------
    # Drag & Drop — межпанельный
    # ------------------------------------------------------------------

    def download_items_from_drop(self, items_data: list, target_path: str) -> None:
        self._queue_drop(items_data, target_path, False)

    def upload_items_from_drop(self, items_data: list, target_path: str) -> None:
        self._queue_drop(items_data, target_path, True)

    def upload_external_to_remote(self, local_paths: list, remote_dir: str) -> None:
        if self.operation_busy() or self.ssh is None:
            return
        try:
            tasks = [
                TransferItem(path, remote_child(remote_dir, os.path.basename(path)))
                for path in local_paths
            ]
        except ValueError as exc:
            self._show_error("Ошибка пути", str(exc))
            return
        self._start_transfer(
            TransferWorker(self.ssh.get_transport(), tasks, True, self)
        )

    # ------------------------------------------------------------------
    # Архивы
    # ------------------------------------------------------------------

    def run_archive(
        self, operation: str, is_remote: bool, item_path: str, archive_or_dest: str
    ) -> None:
        if self.operation_busy() or (is_remote and self.ssh is None):
            return
        worker = SFTPArchiveWorker(
            self.ssh.get_transport() if self.ssh else None,
            operation,
            item_path,
            archive_or_dest,
            is_remote,
            parent=self,
        )
        self._start_transfer(worker)

    # ------------------------------------------------------------------
    # Просмотр / свойства / права (вызывается из FilePanel)
    # ------------------------------------------------------------------

    def preview_remote_file(self, item: FileItem) -> None:
        if item.data(Qt.UserRole + 2):
            self._show_info("Просмотр", "Просмотр доступен только для файлов.")
            return
        if self.sftp is None:
            return
        remote_path = str(
            PurePosixPath(self.remote_panel.current_path).joinpath(item.text())
        )
        try:
            dlg = RemoteFilePreviewDialog(
                item.text(),
                remote_path=remote_path,
                transport=self.ssh.get_transport() if self.ssh else None,
                parent=self,
            )
            dlg.exec()
            self.remote_panel.refresh()
        except Exception as e:
            self._show_error("Ошибка просмотра", str(e))
            self.log_operation(f"Ошибка предпросмотра {remote_path}: {e}")

    def show_remote_info(self, item: FileItem) -> None:
        if self.operation_busy() or self.ssh is None:
            return
        path = remote_child(self.remote_panel.current_path, item.text())
        worker = FileActionWorker(
            self.ssh.get_transport(), True, "stat", [(path, "")], self
        )
        self._start_transfer(worker)

    def show_local_info(self, item: FileItem) -> None:
        local_path = os.path.join(self.local_panel.current_path, item.text())
        try:
            st = os.stat(local_path)
            info = (
                f"Путь: {local_path}\n"
                f"Размер: {st.st_size} байт\n"
                f"Права: {oct(st.st_mode)[-3:]}\n"
                f"Изменён: {datetime.fromtimestamp(st.st_mtime)}"
            )
            self._show_info("Свойства файла", info)
            self.log_operation(f"Просмотрены свойства: {local_path}")
        except Exception as e:
            self._show_error("Ошибка", str(e))

    # ------------------------------------------------------------------
    # Хелперы
    # ------------------------------------------------------------------

    def _show_error(self, title, message) -> None:
        QMessageBox.critical(self, title, message)

    def _show_warning(self, title, message) -> None:
        QMessageBox.warning(self, title, message)

    def _show_info(self, title, message) -> None:
        QMessageBox.information(self, title, message)

    # ------------------------------------------------------------------
    # Закрытие
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.done(QDialog.Rejected)

    def reject(self) -> None:
        self.done(QDialog.Rejected)

    def _cleanup_connections(self) -> None:
        for name in ("sftp", "ssh"):
            resource = getattr(self, name, None)
            setattr(self, name, None)
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    logger.debug(f"Ошибка закрытия SFTP/SSH: {exc}")

    def operation_busy(self) -> bool:
        """Единый запрет наложения операций и действий во время закрытия."""
        if self._closing:
            return True
        if self._transfer_worker is not None or self._connection_worker is not None:
            QMessageBox.information(
                self,
                "Операция выполняется",
                "Дождитесь завершения или отмените текущую операцию.",
            )
            return True
        return False

    def _queue_transfer(
        self, items: list, source_base: str, destination_base: str, upload: bool
    ) -> None:
        if self.operation_busy() or self.ssh is None:
            return
        try:
            tasks = []
            for item in items:
                name = item.text()
                source = (
                    os.path.join(source_base, name)
                    if upload
                    else remote_child(source_base, name)
                )
                destination = (
                    remote_child(destination_base, name)
                    if upload
                    else local_child(destination_base, name)
                )
                tasks.append(
                    TransferItem(source, destination, bool(item.data(Qt.UserRole + 2)))
                )
        except ValueError as exc:
            self._show_error("Ошибка пути", str(exc))
            return
        self._start_transfer(
            TransferWorker(self.ssh.get_transport(), tasks, upload, self)
        )

    def _queue_drop(self, items: list, destination: str, upload: bool) -> None:
        if self.operation_busy() or self.ssh is None:
            return
        try:
            tasks = [
                TransferItem(
                    str(item["path"]),
                    (
                        remote_child(destination, item["name"])
                        if upload
                        else local_child(destination, item["name"])
                    ),
                    bool(item.get("is_dir")),
                )
                for item in items
            ]
        except (KeyError, TypeError, ValueError) as exc:
            self._show_error("Ошибка переноса", str(exc))
            return
        self._start_transfer(
            TransferWorker(self.ssh.get_transport(), tasks, upload, self)
        )

    @Slot(object)
    def _resolve_conflict(self, request: ConflictRequest) -> None:
        if (
            self._closing
            or self._transfer_worker is None
            or self._transfer_worker._cancel_event.is_set()
        ):
            request.ready.set()
            return
        box = QMessageBox(self)
        box.setWindowTitle("Объект существует")
        box.setText(f"Уже существует: {request.destination}")
        box.setInformativeText(
            "Объединить каталоги? Совпадения файлов проверяются отдельно."
            if request.is_dir
            else "Заменить существующий файл после успешного копирования?"
        )
        replace_button = box.addButton(
            "Объединить" if request.is_dir else "Заменить", QMessageBox.AcceptRole
        )
        skip_button = box.addButton("Пропустить", QMessageBox.NoRole)
        box.addButton("Отменить операцию", QMessageBox.RejectRole)
        box.setDefaultButton(skip_button)
        checkbox = QCheckBox("Применить ко всем таким конфликтам", box)
        box.setCheckBox(checkbox)
        box.exec()
        request.decision = (
            "replace"
            if box.clickedButton() == replace_button
            else "skip" if box.clickedButton() == skip_button else "cancel"
        )
        request.apply_all = checkbox.isChecked()
        request.ready.set()

    @Slot()
    def _on_operation_finished(self) -> None:
        worker = self._transfer_worker
        self._transfer_worker = None
        self.status_progress.setVisible(False)
        self.status_cancel_btn.setVisible(False)
        self.status_label.setText("")
        self._clear_op_badge()
        if worker is None:
            return
        result = worker.result
        self.log_operation(result.message)
        if not self._closing:
            if result.errors:
                self._show_error("Операция завершена с ошибками", result.message)
            self.local_panel.refresh()
            self.remote_panel.refresh()
            if (
                isinstance(worker, FileActionWorker)
                and worker.operation == "stat"
                and result.success
            ):
                self._edit_remote_mode(worker.paths[0][0], worker.attributes.st_mode)
        worker.deleteLater()

    def _edit_remote_mode(self, path: str, mode: int | None) -> None:
        from src.ui.dialogs.editors.remote_file_info import RemoteFileInfoDialog

        if mode is None:
            self._show_error("Права доступа", "Сервер не вернул текущие права.")
            return
        dialog = RemoteFileInfoDialog(path, mode, self)
        if dialog.exec() == QDialog.Accepted:
            self.run_file_action(True, "chmod", [(path, "")], mode=dialog.get_mode())

    def run_file_action(
        self,
        remote: bool,
        operation: str,
        paths: list[tuple[str, str]],
        *,
        mode: int = 0o644,
    ) -> None:
        if self.operation_busy() or (remote and self.ssh is None):
            return
        self._start_transfer(
            FileActionWorker(
                self.ssh.get_transport() if self.ssh else None,
                remote,
                operation,
                paths,
                self,
                mode=mode,
            )
        )

    def done(self, result: int) -> None:
        """Закрыть окно после асинхронного завершения всех дочерних потоков."""
        if self._closing:
            return
        if self._transfer_worker is not None:
            answer = QMessageBox.question(
                self,
                "Операция выполняется",
                "Отменить операцию и закрыть окно?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self._closing = True
        self._close_result = result
        self._cancel_transfer()
        if self._connection_worker is not None:
            self._connection_worker.cancel()
        self.local_panel.cancel_listing()
        self.remote_panel.cancel_listing()
        self.terminal_widget.stop()
        self.setEnabled(False)
        self._finish_close()

    def _finish_close(self) -> None:
        if (
            self._connection_worker is not None
            or self._transfer_worker is not None
            or any(worker.isRunning() for worker in self.findChildren(QThread))
        ):
            QTimer.singleShot(50, self._finish_close)
            return
        self._cleanup_connections()
        super().done(self._close_result)
