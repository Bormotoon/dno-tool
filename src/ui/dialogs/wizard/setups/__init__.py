"""Классы сервисов мастера (по одному на устанавливаемую сущность)."""

from .base import SetupPage, clear_layout, hint_label
from .hosts_commands import CommandsPage, HostsPage
from .packages import PackagesPage
from .remote_install import RemoteInstallPage
from .ssh import SshSetupPage
from .veyon import VeyonSetupPage
from .vnc import VncSetupPage

__all__ = [
    "SetupPage",
    "clear_layout",
    "hint_label",
    "HostsPage",
    "CommandsPage",
    "SshSetupPage",
    "VeyonSetupPage",
    "VncSetupPage",
    "PackagesPage",
    "RemoteInstallPage",
]
