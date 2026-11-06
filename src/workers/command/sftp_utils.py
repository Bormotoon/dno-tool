"""Диагностика SFTP-подсистемы поверх SSH.

Dropbear на OpenWrt/RouteRich не содержит SFTP-сервера: чистый SSH работает,
а ``open_sftp()`` падает с ``SSHException: EOF during negotiation``. Модуль
дает одну точку обработки для всех мест открытия SFTP: пробует открыть SFTP
и при обрыве negotiation контрольным каналом ``invoke_subsystem('sftp')``
проверяет, есть ли подсистема на хосте вообще.
"""

import time

import paramiko

from src.utils.filesystem.sftp_files import OperationControl, open_sftp_channel

INSTALL_HINT = "Установите на хосте SFTP-сервер (подсистема sftp) и повторите попытку"


class SftpSubsystemMissing(Exception):
    """SSH жив, но SFTP-подсистемы на хосте нет (типично Dropbear/OpenWrt)."""

    def __init__(self, host: str = "", probe_exit: int | None = None) -> None:
        self.host = host
        self.probe_exit = probe_exit
        where = f" на {host}" if host else ""
        detail = (
            f" (проба подсистемы: exit {probe_exit})" if probe_exit is not None else ""
        )
        super().__init__(
            f"SFTP недоступен{where}: SSH работает, но SFTP-подсистема "
            f"отсутствует{detail}. {INSTALL_HINT}"
        )


def is_negotiation_eof(exc: BaseException) -> bool:
    """True если исключение - обрыв вида EOF during negotiation."""
    if isinstance(exc, EOFError):
        return True
    if isinstance(exc, paramiko.SSHException):
        return "eof during negotiation" in str(exc).lower()
    return False


def probe_sftp_subsystem(
    ssh_client: paramiko.SSHClient,
    timeout: int = 10,
    *,
    control: OperationControl | None = None,
) -> int | None:
    """Проверить наличие SFTP-подсистемы отдельным каналом.

    Returns:
        exit-код подсистемы (127 = нет sftp-server), None = подсистема жива
        (канал остался открыт после invoke).
    """
    transport = ssh_client.get_transport()
    if transport is None or not transport.is_active():
        return None
    chan = transport.open_session(timeout=timeout)
    try:
        parent = control or OperationControl()
        probe_control = OperationControl(
            parent.cancelled,
            min(parent.deadline or float("inf"), time.monotonic() + timeout),
        )
        with probe_control.watch(chan):
            try:
                chan.invoke_subsystem("sftp")
            except paramiko.SSHException:
                probe_control.check()
                return 127
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                probe_control.check()
                if chan.exit_status_ready():
                    return chan.recv_exit_status()
                time.sleep(0.1)
            if chan.exit_status_ready():
                return chan.recv_exit_status()
            return None
    finally:
        try:
            chan.close()
        except Exception:
            pass


def open_sftp_with_diagnosis(
    ssh_client: paramiko.SSHClient,
    host: str = "",
    *,
    control: OperationControl | None = None,
) -> paramiko.SFTPClient:
    """Открыть SFTP с понятной ошибкой при отсутствии подсистемы на хосте.

    При обычном обрыве negotiation делает контрольную пробу подсистемы: если
    проба доказала отсутствие сервера - кидает SftpSubsystemMissing, иначе
    пробрасывает исходное исключение без изменений (без ложных диагнозов).
    """
    try:
        transport = ssh_client.get_transport()
        if transport is None:
            raise paramiko.SSHException("Нет SSH transport")
        return open_sftp_channel(transport, control or OperationControl())
    except SftpSubsystemMissing:
        raise
    except (EOFError, paramiko.SSHException) as exc:
        if not is_negotiation_eof(exc):
            raise
        try:
            probe_exit = probe_sftp_subsystem(ssh_client, control=control)
        except Exception:
            if control is not None:
                control.check()
            probe_exit = None
        if probe_exit in (126, 127):
            raise SftpSubsystemMissing(host=host, probe_exit=probe_exit) from exc
        raise
