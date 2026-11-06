"""Запуск отдельной Linux-сессии с подтверждением создания процесса."""

from __future__ import annotations

import base64
import shlex
import uuid


# Popen ждёт закрытия служебного exec-pipe: ошибки setsid/exec/потоков
# возвращаются до подтверждения, а ошибки пользовательского shell идут в журнал.
REMOTE_BACKGROUND_HELPER = r'''
import base64, os, pwd, signal, subprocess, sys, tempfile

marker = sys.argv[2]
try:
    if sys.platform != "linux":
        raise RuntimeError("Фоновый SSH-запуск поддерживается только на Linux")
    text = base64.b64decode(sys.argv[1]).decode("utf-8")
    home = pwd.getpwuid(os.geteuid()).pw_dir
    directory = os.path.join(home, ".local", "state", "dno-tool", "background")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise RuntimeError("Небезопасные права каталога журналов")
        fd, path = tempfile.mkstemp(prefix="launch-", suffix=".log",
                                    dir="/proc/self/fd/" + str(directory_fd))
        path = os.path.join(directory, os.path.basename(path))
        with os.fdopen(fd, "wb") as log:
            def ignore_hup():
                signal.signal(signal.SIGHUP, signal.SIG_IGN)
            process = subprocess.Popen(
                ["/bin/bash", "--noprofile", "--norc", "-c", text],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                close_fds=True, start_new_session=True, preexec_fn=ignore_hup)
    finally:
        os.close(directory_fd)
except Exception as error:
    print("Не удалось подготовить фоновый запуск: " + str(error) + "\n" + marker + ":ERROR", flush=True)
    sys.exit(1)
print("Фоновый процесс запущен: PID " + str(process.pid) +
      "\nЖурнал stdout/stderr: " + path + "\n" + marker + ":OK", flush=True)
'''


class BackgroundLaunch:
    """Обёртка одного запуска; повторная отправка команды не выполняется."""

    def __init__(self, text: str) -> None:
        self.marker = "__DNO_BACKGROUND_" + uuid.uuid4().hex + "__"
        self.dispatched = False
        self.completed = False
        self._progress_tail = ""
        self.command = "/usr/bin/python3 -I -c " + " ".join(
            shlex.quote(arg)
            for arg in (
                REMOTE_BACKGROUND_HELPER,
                base64.b64encode(text.encode("utf-8")).decode("ascii"),
                self.marker,
            )
        )

    def filter_progress(self, text: str, *, final: bool = False) -> str:
        """Убрать служебное подтверждение даже при разрыве между SSH-чанками."""
        if final and not text:
            self._progress_tail = ""
            return ""
        text = self._progress_tail + text
        self._progress_tail = ""
        tokens = [
            self.marker + suffix
            for suffix in (":OK\n", ":OK\r\n", ":ERROR\n", ":ERROR\r\n")
        ]
        for token in tokens:
            text = text.replace(token, "")
        if not final:
            keep = max(
                (size for token in tokens
                 for size in range(1, min(len(text), len(token)) + 1)
                 if text.endswith(token[:size])),
                default=0,
            )
            if keep:
                text, self._progress_tail = text[:-keep], text[-keep:]
        return text

    def finish(self, output: str, success: bool) -> tuple[str, bool]:
        """Отличить подтверждение от неизвестного результата при обрыве."""
        lines = output.replace("\r\n", "\n").splitlines()
        confirmed = self.marker + ":OK" in lines
        failed = self.marker + ":ERROR" in lines
        clean = "\n".join(line for line in lines if line not in (
            self.marker + ":OK", self.marker + ":ERROR"
        ))
        if confirmed:
            return clean, True
        if self.dispatched and not failed and (not self.completed or success):
            return clean + "\nСтатус запуска неизвестен; автоматический повтор не выполняется", False
        return clean, False
