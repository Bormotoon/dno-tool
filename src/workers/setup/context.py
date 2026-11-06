"""Explicit, per-operation dependencies. Installers never receive the worker/UI."""

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.workers.setup.phases import InstallTarget


@dataclass(frozen=True)
class InstallerParameters:
    ssh_public_key: str = ""
    ssh_port: int = 22
    veyon_public_key_pem: str = ""
    veyon_key_name: str = ""
    veyon_access_group: str | None = None
    veyon_private_key: str = field(default="", repr=False)
    veyon_server_port: int = 11100
    vnc_password: str = field(default="", repr=False)
    vnc_port: int = 5900
    vnc_server: str = "kmsvnc"  # DEFAULT_VNC_SERVER (src.config.settings)
    extra_packages: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperationContext:
    parameters: InstallerParameters
    cancelled: Event = field(repr=False)
    report: Callable = field(repr=False)
    log: Callable = field(repr=False)
    resolve_credentials: Callable = field(repr=False)
    register_connection: Callable = field(repr=False)
    unregister_connection: Callable = field(repr=False)
    options: tuple = ()

    @classmethod
    def from_worker(cls, worker):
        """Compatibility boundary; copy only the declared inputs and callbacks."""
        parameters = InstallerParameters(
            **{
                name: getattr(worker, name)
                for name in InstallerParameters.__dataclass_fields__
                if name not in ("ssh_port", "extra_packages")
            },
            ssh_port=worker._ssh_settings.port,
            extra_packages=tuple(worker.extra_packages),
        )

        def register(connection):
            with worker._result_lock:
                if worker._local_abort.is_set():
                    raise InterruptedError("Setup cancelled")
                (
                    worker._connections
                    if hasattr(connection, "cancel")
                    else worker._connecting
                ).add(connection)

        def unregister(connection):
            with worker._result_lock:
                worker._connecting.discard(connection)
                worker._connections.discard(connection)

        return cls(
            parameters,
            worker._local_abort,
            worker._report,
            worker.host_log.emit,
            worker._resolve_credentials,
            register,
            unregister,
        )


class InstallerContext:
    """Small compatibility base with explicitly named capabilities only."""

    def __init__(self, context: OperationContext):
        self.context = (
            context
            if isinstance(context, OperationContext)
            else OperationContext.from_worker(context)
        )
        self.parameters = self.context.parameters
        self._local_abort = self.context.cancelled
        self._resolve_credentials = self.context.resolve_credentials
        self._results: list[tuple[int, str]] | None = None

    def _report(
        self, device: "InstallTarget", service: str, code: int, output: str
    ) -> None:
        if self._results is not None:
            self._results.append((code, output))
        else:
            self.context.report(device, service, code, output)

    @staticmethod
    def _validate_package_names(packages):
        from src.workers.setup.packages import validate_package_names

        validate_package_names(packages)
