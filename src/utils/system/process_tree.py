"""Конечное завершение процесса вместе со всеми его потомками."""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any

import psutil


def _signal_group(process: subprocess.Popen[Any], signum: int) -> None:
    """Послать сигнал группе процесса; она существует при start_new_session."""
    try:
        os.killpg(process.pid, signum)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def terminate_process_tree(
    process: subprocess.Popen[Any], *, kill_group: bool = False
) -> None:
    """Сначала terminate (0.5 с), затем kill и обязательный wait (2 с).

    kill_group нужен для процессов с start_new_session=True на POSIX: потомка,
    уже потерявшего родителя, перебор дерева через psutil найти не может.
    """
    use_group = kill_group and os.name == "posix"
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except (AttributeError, psutil.NoSuchProcess, psutil.AccessDenied):
        children = []
    if use_group:
        _signal_group(process, signal.SIGTERM)
    for child in reversed(children):
        try:
            child.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=2.0)
    else:
        process.wait()
    _, remaining = psutil.wait_procs(children, timeout=0.5)
    for child in remaining:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    if use_group:
        _signal_group(process, signal.SIGKILL)
    _, remaining = psutil.wait_procs(remaining, timeout=2.0)
    if remaining:
        raise RuntimeError("Не удалось завершить дочерние процессы задания.")
