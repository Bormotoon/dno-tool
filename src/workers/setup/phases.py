"""Typed lifecycle shared by registered remote installers.

Apply may install packages or authorized_keys irreversibly. A non-reversible
installer returns UNSUPPORTED from rollback; this never claims recovery.
VNC overrides rollback with a checked remote snapshot restore.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol, TYPE_CHECKING
from src.workers.setup.context import InstallerContext
from src.workers.setup.types import (
    RESULT_SUCCESS,
    RESULT_IGNORE,
    RESULT_ERROR,
    RESULT_ABORT,
)

if TYPE_CHECKING:
    from paramiko import SSHClient


class InstallTarget(Protocol):
    iid: str
    host: str
    port: int | None
    vnc_port: int | None


class CommandRunner(Protocol):
    def __call__(
        self,
        command: str,
        timeout: float = 60,
        stdin_data: str | bytes | None = None,
        sensitive: bool = False,
        ignore_abort: bool = False,
    ) -> tuple[int, str, str]: ...


@dataclass(frozen=True)
class InstallRequest:
    device: InstallTarget
    ssh: "SSHClient" = field(repr=False)
    run: CommandRunner = field(repr=False)
    write: Callable[[str, str | bytes], tuple[int, str]] = field(repr=False)
    append: Callable[[str, str], tuple[bool, str]] = field(repr=False)


@dataclass(frozen=True)
class PhaseResult:
    code: int = RESULT_SUCCESS
    message: str = ""


class RecoveryState(Enum):
    RESTORED = "restored"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    NOT_NEEDED = "not_needed"


@dataclass(frozen=True)
class RecoveryResult:
    state: RecoveryState
    message: str


class Installer(Protocol):
    def prepare(self, request: InstallRequest) -> PhaseResult: ...
    def apply(self, request: InstallRequest) -> PhaseResult: ...
    def verify(self, request: InstallRequest, applied: PhaseResult) -> PhaseResult: ...
    def rollback(self, request: InstallRequest) -> RecoveryResult: ...


class PhasedInstaller(InstallerContext):
    service = ""
    recovery_limit = (
        "Установленные пакеты и добавленные SSH-ключи автоматически не удаляются."
    )

    def prepare(self, request: InstallRequest) -> PhaseResult:
        from src.workers.setup.types import validate_service_parameters

        validate_service_parameters(self.service, self.parameters)
        if self._local_abort.is_set():
            return PhaseResult(RESULT_ABORT, "Прервано до изменений")
        return PhaseResult()

    def apply(self, request: InstallRequest) -> PhaseResult:
        self._results = []
        try:
            self._apply_install(
                request.device, request.ssh, request.run, request.write, request.append
            )
            if not self._results:
                return PhaseResult(
                    RESULT_ERROR, "Установщик не вернул результат применения"
                )
            return PhaseResult(*self._results[-1])
        finally:
            self._results = None

    def verify(self, request: InstallRequest, applied: PhaseResult) -> PhaseResult:
        # Each concrete implementation supplies service-specific read-back.
        raise NotImplementedError

    def rollback(self, request: InstallRequest) -> RecoveryResult:
        return RecoveryResult(RecoveryState.UNSUPPORTED, self.recovery_limit)

    def install(self, device, ssh, run, write, append):
        request = InstallRequest(device, ssh, run, write, append)
        changed = False
        try:
            result = self.prepare(request)
            if result.code == RESULT_SUCCESS:
                changed = True
                result = self.apply(request)
                if result.code in (RESULT_SUCCESS, RESULT_IGNORE):
                    result = self.verify(request, result)
            if self._local_abort.is_set():
                result = PhaseResult(RESULT_ABORT, "Установка прервана")
        except Exception:
            result = PhaseResult(
                RESULT_ABORT if self._local_abort.is_set() else RESULT_ERROR,
                "Ошибка выполнения фазы установки",
            )
        if changed and result.code in (RESULT_ERROR, RESULT_ABORT):
            try:
                recovery = self.rollback(request)
            except Exception:
                recovery = RecoveryResult(RecoveryState.FAILED, "Ошибка восстановления")
            result = PhaseResult(
                RESULT_ERROR if recovery.state == RecoveryState.FAILED else result.code,
                result.message + "\n" + recovery.message,
            )
        self.context.report(device, self.service, result.code, result.message)
        return result
