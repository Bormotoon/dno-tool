"""Фоновые операции редактора через отдельный канал существующего SSH Transport."""

import errno

import paramiko
from PySide6.QtCore import QThread

from src.utils.text.remote_text import MAX_REMOTE_TEXT_BYTES


def read_remote_text(sftp, path: str) -> bytes:
    with sftp.open(path, 'rb') as stream:
        data = stream.read(MAX_REMOTE_TEXT_BYTES + 1)
    if len(data) > MAX_REMOTE_TEXT_BYTES:
        raise ValueError('Файл превышает 10 МБ')
    return data


class RemoteTextWorker(QThread):
    """Хранит результат до настоящего finished; виджеты доступны только GUI-потоку."""

    def __init__(self, transport, path: str, operation: str, *, baseline=b'', data=b'', force=False, parent=None):
        super().__init__(parent)
        self.transport = transport
        self.path = path
        self.operation = operation
        self.baseline = baseline
        self.data = data
        self.force = force
        self.result = None
        self.error = None

    def run(self):
        sftp = None
        try:
            sftp = paramiko.SFTPClient.from_transport(self.transport)
            sftp.get_channel().settimeout(20)
            if self.operation == 'load':
                self.result = ('loaded', read_remote_text(sftp, self.path))
                return
            if len(self.data) > MAX_REMOTE_TEXT_BYTES:
                raise ValueError('Файл превышает 10 МБ')
            if not self.force:
                try:
                    current = read_remote_text(sftp, self.path)
                except OSError as exc:
                    if exc.errno != errno.ENOENT:
                        raise
                    current = None
                except ValueError:
                    self.result = ('conflict', 'Файл на сервере теперь превышает 10 МБ.')
                    return
                if current != self.baseline:
                    self.result = ('conflict', 'Файл удалён с сервера.' if current is None else 'Файл на сервере изменён.')
                    return
            # Запись по существующему пути сохраняет права и следует символической ссылке.
            # Между проверкой и записью сторонний процесс может изменить файл.
            with sftp.open(self.path, 'wb') as stream:
                stream.write(self.data)
                stream.flush()
            self.result = ('saved', self.data)
        except Exception as exc:
            self.error = str(exc)
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass
