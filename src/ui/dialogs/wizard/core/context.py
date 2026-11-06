"""Общее состояние мастера настройки (WizardContext).

Единственный канал обмена данными между шагами. Шаги читают и пишут
только сюда, прямых обращений к чужим виджетам нет. Запись в config /
datastore выполняется только store.save_all() на Finish.
"""

from dataclasses import dataclass, field
from typing import Dict, List
from types import MappingProxyType
from uuid import uuid4
from src.workers.setup.types import INSTALLERS
from src.config.settings import DEFAULT_VNC_SERVER


@dataclass
class SshState:
    """Состояние шага SSH-ключей."""

    bundle_iid: str = ""
    use_sudo: bool = False
    pubkey: str = ""
    private_key_path: str = ""


@dataclass
class VeyonState:
    """Состояние шага Veyon."""

    key_name: str = ""
    pem: str = ""
    settings: dict = field(default_factory=dict)


@dataclass
class VncState:
    """Состояние шага VNC."""

    server: str = DEFAULT_VNC_SERVER
    port: int = 5900
    password: str = field(default="", repr=False)


@dataclass(frozen=True)
class DeviceSnapshot:
    iid: str
    host: str
    name: str = ""
    port: int | None = None
    vnc_port: int | None = None
    use_sudo: bool | None = None
    credential_bundle_iid: str | None = None
    node_iid: str | None = None

    @classmethod
    def from_device(cls, device):
        return cls(
            **{key: getattr(device, key, None) for key in cls.__dataclass_fields__}
        )


@dataclass(frozen=True)
class InstallPlan:
    """Снапшот параметров удаленной установки (immutable после сборки).

    Заменяет десять полей _pending_* старого монолита.
    """

    devices: tuple = ()
    services: Dict[str, bool] = field(default_factory=dict)
    ssh_pubkey: str = ""
    veyon_pem: str = ""
    veyon_private_key: str = field(default="", repr=False)
    veyon_key_name: str = ""
    vnc_password: str = field(default="", repr=False)
    vnc_port: int = 5900
    veyon_server_port: int = 11100
    vnc_server: str = DEFAULT_VNC_SERVER
    extra_packages: tuple = ()
    use_sudo_override: bool | None = None
    default_bundle_iid: str | None = None
    ssh_private_key_path: str = ""
    ssh_settings: tuple = ()
    credential_bundles: tuple = field(default=(), repr=False)
    service_options: tuple = ()
    run_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self):
        from src.workers.setup.types import INSTALLERS
        options = dict(self.service_options)
        object.__setattr__(self, "service_options", tuple(
            (item.key, item.build_parameters(dict(options.get(item.key, ()))))
            for item in INSTALLERS if self.services.get(item.key) and item.parameters
        ))
        object.__setattr__(
            self, "devices", tuple(DeviceSnapshot.from_device(d) for d in self.devices)
        )
        object.__setattr__(self, "services", MappingProxyType(dict(self.services)))
        object.__setattr__(self, "extra_packages", tuple(self.extra_packages))
        object.__setattr__(
            self, "credential_bundles", tuple(tuple(b) for b in self.credential_bundles)
        )
        object.__setattr__(
            self, "ssh_settings", tuple(tuple(value) for value in self.ssh_settings)
        )


@dataclass
class InstallResult:
    """Результаты удаленной установки (таблица + логи)."""

    host_row_map: dict = field(default_factory=dict)  # iid -> row
    row_host_map: dict = field(default_factory=dict)  # row -> iid
    row_host_name: dict = field(default_factory=dict)  # iid -> host
    service_col_map: dict = field(default_factory=dict)  # service -> col
    host_logs: dict = field(default_factory=dict)  # iid -> {service: output}
    ran: bool = False


@dataclass
class WizardContext:
    """Межшаговое состояние мастера."""

    mode: str = "fresh"  # fresh | existing
    services: Dict[str, bool] = field(
        default_factory=lambda: {item.key: True for item in INSTALLERS}
    )
    vnc_server: str = DEFAULT_VNC_SERVER
    hosts_data: list = field(default_factory=list)
    commands_data: list = field(default_factory=list)
    baseline: dict = field(default_factory=dict, repr=False)
    credentials: object = field(default=None, repr=False)
    applied_actions: list = field(default_factory=list)
    commands_loaded: bool = False
    ssh: SshState = field(default_factory=SshState)
    veyon: VeyonState = field(default_factory=VeyonState)
    vnc: VncState = field(default_factory=VncState)
    packages: List[str] = field(default_factory=list)
    service_options: dict = field(default_factory=dict)
    install_plan: InstallPlan | None = None
    install_result: InstallResult = field(default_factory=InstallResult)
