"""Cancellable SSH operations sharing one deadline from channel creation."""

import shlex
import socket
import threading
import time


def probe_rfb_banner(host, port, abort):
    """Verify operator-side reachability without input or framebuffer requests."""
    connection = None
    try:
        if abort.is_set():
            return False
        connection = socket.create_connection((host, port), timeout=3)
        deadline = time.monotonic() + 3
        banner = bytearray()
        while len(banner) < 12:
            if abort.is_set() or time.monotonic() >= deadline:
                return False
            connection.settimeout(max(0.01, deadline - time.monotonic()))
            part = connection.recv(12 - len(banner))
            if not part:
                return False
            banner.extend(part)
        return bytes(banner) in (b"RFB 003.003\n", b"RFB 003.007\n", b"RFB 003.008\n")
    except OSError:
        return False
    finally:
        if connection is not None:
            connection.close()


class SetupTransport:
    def __init__(self, ssh, abort, use_sudo=False, password=None):
        self.ssh, self.abort = ssh, abort
        self.use_sudo, self.password = use_sudo, password
        self._channels = {}
        self._lock = threading.Lock()

    def cancel(self):
        with self._lock:
            for channel, recovering in tuple(self._channels.items()):
                if not recovering:
                    channel.close()

    def run(
        self, command, timeout=60, stdin_data=None, sensitive=False, ignore_abort=False
    ):
        deadline = time.monotonic() + timeout
        channel = None
        deadline_timer = None

        def remaining():
            if not ignore_abort and self.abort.is_set():
                raise InterruptedError("aborted")
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("timeout")
            return value

        try:
            data = (
                stdin_data.encode("utf-8")
                if isinstance(stdin_data, str)
                else (stdin_data or b"")
            )
            if self.use_sudo:
                # Use a unique prompt marker so the password is never prefixed
                # to the command's stdin when sudo uses a cached credential.
                command = "sudo -S -p 'DNO_SETUP_SUDO:' -- bash -c " + shlex.quote(
                    "printf 'DNO_SETUP_READY\\n'; " + command
                )
            channel = self.ssh.get_transport().open_session(timeout=remaining())
            with self._lock:
                self._channels[channel] = ignore_abort
            channel.settimeout(remaining())
            # Paramiko's exec request wait is not covered by recv timeouts.
            # Closing the channel also wakes that wait when the peer is silent.
            deadline_timer = threading.Timer(remaining(), channel.close)
            deadline_timer.daemon = True
            deadline_timer.start()
            channel.exec_command(command)
            out, err = bytearray(), bytearray()
            sudo_pending = self.use_sudo
            eof = False
            while True:
                remaining()
                if channel.recv_stderr_ready():
                    err.extend(channel.recv_stderr(65536))
                    if sudo_pending and b"DNO_SETUP_SUDO:" in err:
                        if self.password is None:
                            raise PermissionError("sudo requires a password")
                        self._send(channel, (self.password + "\n").encode(), remaining)
                        err.clear()
                        sudo_pending = False
                if channel.recv_ready():
                    out.extend(channel.recv(65536))
                if not self.use_sudo or b"DNO_SETUP_READY\n" in out:
                    if self.use_sudo:
                        out[:] = out.replace(b"DNO_SETUP_READY\n", b"")
                        sudo_pending = False
                    if not eof:
                        self._send(channel, data, remaining)
                        channel.shutdown_write()
                        eof = True
                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    code = channel.recv_exit_status()
                    return (
                        code,
                        ""
                        if sensitive and code
                        else out.decode("utf-8", "replace").strip(),
                        (
                            "Ошибка операции с секретом"
                            if sensitive and code
                            else ""
                            if sensitive
                            else err.decode("utf-8", "replace").strip()
                        ),
                    )
                time.sleep(0.01)
        except Exception as error:
            return (
                -1,
                "",
                "Ошибка операции с секретом" if sensitive else type(error).__name__,
            )
        finally:
            if deadline_timer is not None:
                deadline_timer.cancel()
            if channel is not None:
                with self._lock:
                    self._channels.pop(channel, None)
                channel.close()

    @staticmethod
    def _send(channel, data, remaining):
        sent = 0
        while sent < len(data):
            channel.settimeout(min(0.5, remaining()))
            try:
                count = channel.send(data[sent:])
            except socket.timeout:
                continue
            if not count:
                raise OSError("stdin truncated")
            sent += count

    def write_file(self, path, content, mode=0o600):
        """Записать файл и выставить права явно.

        ``umask 077`` закрывает файл на время записи, затем ``chmod`` задаёт
        итоговый режим: ``cat >`` сохраняет права существующего файла, а
        новый получил бы 0600 — пользовательский systemd такой юнит из
        /etc/systemd/user не видит ("Unit ... does not exist").
        """
        quoted = shlex.quote(path)
        code, _, error = self.run(
            f"umask 077; cat > {quoted} && chmod {mode:o} {quoted}",
            stdin_data=content,
            sensitive=True,
        )
        return code, error
