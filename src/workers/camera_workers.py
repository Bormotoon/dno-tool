"""Фоновые операции камер: опрос, запись, проверка RTSP и плеер."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, wait
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from PySide6.QtCore import QLockFile, QObject, QThread, Signal

from src.domain.models import CameraModel
from src.domain.models.camera import redact_camera_text
from src.logger import logger
from src.utils.filesystem.fs_utils import ensure_file_user_owned
from src.utils.filesystem.recording_identity import recording_file_identity
from src.utils.system.subprocess_env import (
    build_clean_env,
    ensure_dir_as_user,
    safe_cwd_for_user,
    wrap_as_user,
)

if TYPE_CHECKING:
    from src.config.settings import CameraConfig

PING_INTERVAL_MS = 20000
DEFAULT_PING_INTERVAL = 20
TIMER_INTERVAL_MS = 1000
DEFAULT_PING_TIMEOUT = 2


class CameraHardwareInfoWorker(QThread):
    """Прочитать названия устройств, не задерживая открытие настроек."""

    result = Signal(str, str)

    def run(self) -> None:
        from src.utils.system.camera_hardware_names import camera_hardware_names

        try:
            gpu, cpu = camera_hardware_names()
        except OSError:
            gpu, cpu = "GPU", "CPU"
        self.result.emit(gpu, cpu)


def stop_worker_blocking(worker: CameraRecordWorker) -> bool:
    worker.stop()
    while worker.isRunning():
        worker.wait(250)
    return not worker.is_running


class CameraPingWorker(QThread):
    """TCP-опрос камер через общий пробер с отменой очереди.

    max_workers (настройка «Параллельных проверок») ограничивает число проверок этого
    опроса одновременно: пробер общий, а камеры не должны вытеснять проверки хостов.
    """

    result = Signal(str, bool)

    def __init__(
        self,
        cameras: list[CameraModel],
        timeout: int = DEFAULT_PING_TIMEOUT,
        parent: QObject | None = None,
        max_workers: int = 8,
    ) -> None:
        super().__init__(parent)
        self._cameras = list(cameras)
        self._timeout = timeout
        self._max_workers = max(1, max_workers)

    def run(self) -> None:
        from src.utils.network.reachability import get_reachability_prober

        prober = get_reachability_prober()
        waiting = deque(self._cameras)
        pending = {}
        while (waiting or pending) and not self.isInterruptionRequested():
            while waiting and len(pending) < self._max_workers:
                camera = waiting.popleft()
                pending[
                    prober.probe(
                        camera.rtsp_host,
                        camera.rtsp_port,
                        self._timeout,
                        metadata={
                            "host": camera.rtsp_host,
                            "type": "camera_ping",
                            "source": "application",
                        },
                    )
                ] = camera
            done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                camera = pending.pop(future)
                self.result.emit(camera.identifier, bool(future.result()))
        for future in pending:
            future.cancel()


def _popen_kwargs(*, visible: bool = False) -> dict:
    """Подготовить окружение процесса без утечек frozen-приложения."""
    kwargs = {}
    env = build_clean_env()
    if env is not None:
        kwargs["env"] = env
    if sys.platform == "win32":
        if not visible:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["cwd"] = safe_cwd_for_user()
        kwargs["start_new_session"] = True
    return kwargs


def launch_player(cmd: list[str], caption: str) -> bool:
    """Запустить видимое окно плеера без записи URL в журнал."""
    try:
        subprocess.Popen(
            wrap_as_user(cmd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_popen_kwargs(visible=True),
        )
        return True
    except OSError:
        logger.error("Не удалось запустить плеер")
        return False


def launch_system_player(url: str, caption: str) -> bool:
    """Открыть поток через ассоциацию операционной системы."""
    try:
        if sys.platform == "win32":
            os.startfile(url)
            return True
        opener = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
        return bool(opener and launch_player([opener, url], caption))
    except OSError:
        logger.error("Не удалось открыть системный плеер")
        return False


def resolve_player_executable(player: str) -> str | None:
    return (
        shutil.which(player)
        if player and player.lower() not in ("системный", "system", "default")
        else None
    )


def build_player_cmd(player_exe: str, camera: CameraModel) -> list[str]:
    base = Path(player_exe).name.lower()
    if "vlc" in base:
        return [player_exe, camera.rtsp_url, f"--meta-title={camera.caption}"]
    if "mpv" in base:
        return [player_exe, f"--title={camera.caption}", camera.rtsp_url]
    return [player_exe, camera.rtsp_url]


class CameraRecordWorker(QThread):
    """Сессия PyAV живёт в QThread; извне разрешён только запрос отмены."""

    completed = Signal(str, object)
    started_rec = Signal(str, object)
    start_failed = Signal(str, str)
    progress = Signal(str, object)
    diagnostic = Signal(str, str, bool)

    def __init__(
        self,
        camera_id: str,
        source: str,
        output: str,
        parent: QObject | None = None,
        *,
        policy: CameraConfig | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        super().__init__(parent)
        self._camera_id, self._source, self._output = camera_id, source, output
        self._policy = policy
        self._secrets = secrets
        self._manual_stop = False
        self._reconnect_stop = False
        self._cancel = threading.Event()
        self._running = threading.Event()
        self._exit_code = -1
        self.reason = ""
        self.permanent = False
        self.rotate = False
        self.session_id = uuid4().hex
        self._last_position = -1.0
        self._last_media_at = 0.0
        self._last_report = 0.0
        self._started_at = 0.0

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def output_file(self) -> str:
        return self._output

    def stop(self) -> None:
        self._manual_stop = True
        self._cancel.set()

    def request_rotation(self) -> None:
        self.rotate = True
        self._cancel.set()

    def _fail(self, message: str, permanent: bool) -> None:
        self.reason = redact_camera_text(message, self._secrets)
        self.permanent = permanent
        self.diagnostic.emit(self._camera_id, self.reason, permanent)

    def _reserve_output(self) -> tuple[Path, QLockFile]:
        """Выбрать свободное имя и удерживать его между потоками и процессами."""
        original = Path(self.output_file)
        for number in range(10000):
            if self._cancel.is_set():
                raise InterruptedError
            target = (
                original
                if number == 0
                else original.with_name(f"{original.stem}_{number}{original.suffix}")
            )
            if target.exists() or target.is_symlink():
                continue
            token = sha256(os.path.normcase(target.name).encode("utf-8")).hexdigest()
            lock = QLockFile(str(target.with_name(f".dno-recording-{token}.lock")))
            lock.setStaleLockTime(0)
            if not lock.tryLock(0):
                if lock.error() != QLockFile.LockError.LockFailedError:
                    raise OSError("Не удалось зарезервировать имя файла записи")
                continue
            if target.exists() or target.is_symlink():
                lock.unlock()
                continue
            self._output = str(target)
            return target, lock
        raise OSError("Не удалось подобрать свободное имя файла записи")

    def _advance(self, progress) -> None:
        now = time.monotonic()
        if progress.media_time > self._last_position:
            self._last_position, self._last_media_at = progress.media_time, now
        policy = self._policy
        if now - self._last_report >= 1:
            if self._last_position >= 0:
                self.progress.emit(self._camera_id, progress.bytes_written)
            self._last_report = now
            if policy:
                if (
                    policy.emergency_free_gb
                    and shutil.disk_usage(Path(self._output).parent).free
                    < policy.emergency_free_gb * 1024**3
                ):
                    self._fail(
                        "Запись остановлена: критически мало места на диске", True
                    )
                    self._cancel.set()
                elif now - self._last_media_at > policy.stale_timeout:
                    self._fail("Поток не продвигается по медиавремени", False)
                    self._cancel.set()
                elif (
                    policy.segment_minutes
                    and now - self._started_at >= policy.segment_minutes * 60
                ) or (
                    policy.segment_mb
                    and progress.bytes_written >= policy.segment_mb * 1024**2
                ):
                    self.request_rotation()

    def run(self) -> None:
        from src.services.media import (
            OperationContext,
            get_media_service,
        )
        from src.services.media.types import RecordOptions

        output_lock = None
        empty_identity = None
        try:
            context = OperationContext(cancelled=self._cancel, progress=self._advance)
            context.check()
            get_media_service().capabilities()
            target = Path(self._output)
            if not ensure_dir_as_user(target.parent):
                raise OSError("Не удалось создать каталог записи")
            if (
                self._policy
                and self._policy.min_free_gb
                and shutil.disk_usage(target.parent).free
                < self._policy.min_free_gb * 1024**3
            ):
                raise OSError("Недостаточно свободного места для старта записи")
            target, output_lock = self._reserve_output()
            context.check()
            self._started_at = self._last_media_at = time.monotonic()
            self._running.set()
            # Эксклюзивное создание сохраняет появившийся чужой файл.
            with target.open("xb") as output:
                ensure_file_user_owned(output.fileno())
                empty_identity = recording_file_identity(target)
                self.started_rec.emit(self._camera_id, self.session_id)
                get_media_service().record_camera(
                    self._source,
                    output,
                    context,
                    self._policy.network_timeout if self._policy else 10,
                    RecordOptions.from_policy(self._policy),
                )
            self._exit_code = 0
            if not self._manual_stop and not self.rotate and not self.reason:
                self._fail("Поток завершён", False)
        except InterruptedError:
            self._exit_code = 0
        except Exception as error:
            detail = get_media_service().error(error, "record")
            self._fail(
                detail.message,
                not self._running.is_set()
                or detail.category in ("auth", "disk")
                or isinstance(error, (ValueError, FileExistsError, PermissionError)),
            )
            if not self._running.is_set():
                self.start_failed.emit(self._camera_id, self.reason)
        finally:
            self._running.clear()
            # Отмена до первого пакета не оставляет пустую запись. Проверка
            # идентичности защищает появившийся вместо неё чужой файл.
            if empty_identity is not None and self._last_position < 0:
                try:
                    if recording_file_identity(self.output_file) == empty_identity:
                        Path(self.output_file).unlink()
                except OSError:
                    logger.warning(
                        "Не удалось убрать пустую запись камеры %s", self._camera_id
                    )
            if output_lock is not None:
                output_lock.unlock()
            if self.reason:
                logger.error("Запись камеры %s: %s", self._camera_id, self.reason)
            self.completed.emit(self._camera_id, self._exit_code)


class CameraProbeWorker(QThread):
    """Проверка авторизации и первого кадра через API avformat/PyAV."""

    result = Signal(bool, str)

    def __init__(
        self,
        camera: CameraModel,
        timeout: int = 10,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.camera, self.timeout = camera, timeout
        self._cancel = threading.Event()

    def requestInterruption(self) -> None:
        self._cancel.set()
        super().requestInterruption()

    def run(self) -> None:
        from src.services.media import OperationContext, get_media_service

        ok, message = False, "Проверка отменена"
        frames = None
        try:
            context = OperationContext.with_timeout(
                max(15, self.timeout * 2), self._cancel
            )
            context.check()
            frames = get_media_service().camera_frames(
                self.camera.rtsp_url, None, context, self.timeout
            )
            frame = next(frames, None)
            context.check()
            if frame is None:
                raise ValueError("RTSP-поток не содержит видеокадра.")
            ok, message = True, "Кадр получен"
        except InterruptedError:
            pass
        except Exception as error:
            message = get_media_service().error(error, "camera").message
        finally:
            if frames is not None:
                frames.close()
            self.result.emit(ok, message)
