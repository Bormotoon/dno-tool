"""Nonblocking application teardown while Qt still owns running workers.

Порядок важен: сначала запрет приёма и отмена очередей, затем закрытие ресурсов,
которое прерывает выполняющиеся операции, и только после этого ожидание потоков.
Сторож работает в обычном daemon-потоке и не зависит от Qt: он нужен и тогда, когда
event loop уже остановлен, а интерпретатор ждёт незавершённые потоки при выходе.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer

from src.logger import logger

# Мягкий дедлайн: журнал, стеки потоков и вопрос пользователю.
SOFT_DEADLINE_SEC = 20.0
# Жёсткий дедлайн: принудительный выход, если не идёт запись камер.
HARD_DEADLINE_SEC = 120.0
# Окно закрыто, но интерпретатор ждёт незавершённые потоки.
POST_QT_DEADLINE_SEC = 15.0
# Коды выхода: зависшие операции при завершении и подтверждение пользователя.
EXIT_CODE_FORCED = 3

# Результат шага не используется: важны только завершение и исключения.
Step = Callable[[], object]


def force_exit(code: int) -> None:
    """Выйти, минуя деструкторы: работающий QThread нельзя уничтожать."""
    logging.shutdown()
    os._exit(code)


def write_stack_dump(directory: str, reason: str) -> str | None:
    """Сохранить стеки всех потоков; значения переменных в файл не попадают."""
    path = os.path.join(directory, f"shutdown-{time.strftime('%Y%m%d-%H%M%S')}.log")
    try:
        with open(path, "a", encoding="utf-8", errors="replace") as stream:
            stream.write(f"{reason}\n\nПотоки:\n")
            stream.writelines(
                f"  {thread.ident:#x} {thread.name} daemon={thread.daemon}\n"
                for thread in threading.enumerate()
            )
            stream.write("\n")
            stream.flush()
            faulthandler.dump_traceback(file=stream, all_threads=True)
    except (OSError, ValueError) as error:
        logger.warning(f"Не удалось сохранить стеки потоков: {error}")
        return None
    return path


def _run_step(step: Step) -> None:
    try:
        step()
    except Exception:  # noqa: BLE001 - ошибка шага не отменяет остальные шаги
        logger.exception("Application shutdown step failed")


def _run_parallel(steps: Sequence[Step]) -> None:
    """Выполнить шаги одновременно: закрытие одного ресурса не задерживает другие."""
    threads = [
        threading.Thread(
            target=_run_step, args=(step,), name=f"shutdown-step-{index}", daemon=True
        )
        for index, step in enumerate(steps)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


@dataclass(frozen=True)
class ShutdownPlan:
    """Этапы завершения: ничего не ждать, закрыть ресурсы, только затем ждать."""

    stop_admission: tuple[Step, ...]
    close_resources: tuple[Step, ...]
    wait_for_workers: tuple[Step, ...]


def run_shutdown_plan(plan: ShutdownPlan) -> None:
    for step in plan.stop_admission:
        _run_step(step)
    _run_parallel(plan.close_resources)
    for step in plan.wait_for_workers:
        _run_step(step)


class _Watchdog:
    """Состояние и цикл сторожа: обычный объект, без ссылок на QObject.

    Поток сторожа не должен удерживать координатор: иначе последняя ссылка могла бы
    исчезнуть в этом потоке, и деструктор QObject выполнился бы не в своём потоке.
    """

    def __init__(
        self,
        hard_deadline: float,
        post_qt_deadline: float,
        exit_process: Callable[[int], None],
        stacks_dir: str | None,
    ) -> None:
        self.hard_deadline = hard_deadline
        self.post_qt_deadline = post_qt_deadline
        self.exit_process = exit_process
        self.stacks_dir = stacks_dir
        self.started_at = time.monotonic()
        self.completed_at: float | None = None
        # Обновляется в GUI-потоке: сторож не обращается к объектам Qt.
        self.recordings_busy = False

    def dump_stacks(self, reason: str) -> None:
        if self.stacks_dir is None:
            return
        path = write_stack_dump(self.stacks_dir, reason)
        if path is not None:
            logger.warning(f"Стеки потоков при завершении сохранены: {path}")

    def run(self) -> None:
        """Гарантировать выход, даже если GUI-поток или интерпретатор зависли."""
        while True:
            time.sleep(0.1)
            now = time.monotonic()
            completed_at = self.completed_at
            if completed_at is not None:
                if now - completed_at >= self.post_qt_deadline:
                    self.dump_stacks("Интерпретатор не завершился после закрытия окна")
                    self.exit_process(0)
                    return
                continue
            # Незавершённую запись камер прерывает только сам пользователь.
            if now - self.started_at >= self.hard_deadline and not self.recordings_busy:
                self.dump_stacks("Жёсткий дедлайн завершения")
                self.exit_process(EXIT_CODE_FORCED)
                return


class ApplicationShutdown(QObject):
    """Wait for Python cleanup and Qt-owned workers without blocking the GUI."""

    def __init__(
        self,
        cleanup: Callable[[], None],
        owners_stopped: Callable[[], bool],
        finished: Callable[[], None],
        parent: QObject | None = None,
        *,
        poll_ms: int = 100,
        soft_deadline_sec: float | None = None,
        hard_deadline_sec: float | None = None,
        post_qt_deadline_sec: float | None = None,
        describe_pending: Callable[[], list[str]] | None = None,
        recordings_pending: Callable[[], bool] | None = None,
        on_soft_deadline: Callable[[list[str]], None] | None = None,
        on_progress: Callable[[int], None] | None = None,
        force_exit: Callable[[int], None] = force_exit,
        stacks_dir: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._cleanup = cleanup
        self._owners_stopped = owners_stopped
        self._finished = finished
        self._soft_deadline = (
            SOFT_DEADLINE_SEC if soft_deadline_sec is None else soft_deadline_sec
        )
        self._watch = _Watchdog(
            HARD_DEADLINE_SEC if hard_deadline_sec is None else hard_deadline_sec,
            POST_QT_DEADLINE_SEC if post_qt_deadline_sec is None else post_qt_deadline_sec,
            force_exit,
            stacks_dir,
        )
        self._describe_pending = describe_pending
        self._recordings_pending = recordings_pending
        self._on_soft_deadline = on_soft_deadline
        self._on_progress = on_progress
        self._done = threading.Event()
        self._started = False
        self._completed = False
        self._soft_fired = False
        self._last_pending_count: int | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self._poll)
        self._thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None

    @property
    def cleanup_finished(self) -> bool:
        return self._done.is_set()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._watch.started_at = time.monotonic()
        cleanup, done = self._cleanup, self._done

        def run() -> None:
            try:
                cleanup()
            except Exception:  # noqa: BLE001 - preserve Qt ownership on teardown errors
                logger.exception("Application background shutdown failed")
            finally:
                done.set()

        self._thread = threading.Thread(
            target=run, name="application-shutdown", daemon=True
        )
        self._thread.start()
        self._watchdog = threading.Thread(
            target=self._watch.run, name="application-shutdown-watchdog", daemon=True
        )
        self._watchdog.start()
        self._timer.start()

    def pending(self) -> list[str]:
        """Что ещё не завершилось: владельцы потоков и фоновое закрытие пулов."""
        if self._describe_pending is not None:
            names = self._describe_pending()
        else:
            names = [] if self._owners_stopped() else ["Фоновые операции"]
        if not self._done.is_set():
            names = [*names, "Закрытие сетевых ресурсов и пулов"]
        return names

    def force_exit_now(self) -> None:
        """Принудительный выход по решению пользователя."""
        self._watch.dump_stacks("Принудительное завершение по решению пользователя")
        self._watch.exit_process(EXIT_CODE_FORCED)

    def _poll(self) -> None:
        if self._completed:
            return
        if self._recordings_pending is not None:
            self._watch.recordings_busy = self._recordings_pending()
        pending = self.pending()
        if self._on_progress is not None and len(pending) != self._last_pending_count:
            self._last_pending_count = len(pending)
            self._on_progress(len(pending))
        if (
            not self._soft_fired
            and time.monotonic() - self._watch.started_at >= self._soft_deadline
            and pending
        ):
            self._soft_fired = True
            logger.warning(
                "Завершение затянулось, не остановлены: " + "; ".join(pending)
            )
            self._watch.dump_stacks("Мягкий дедлайн завершения: " + "; ".join(pending))
            if self._on_soft_deadline is not None:
                self._on_soft_deadline(pending)
        if pending:
            return
        self._completed = True
        self._watch.completed_at = time.monotonic()
        self._timer.stop()
        self._finished()
