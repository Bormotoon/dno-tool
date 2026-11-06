"""Пакет диалоговых окон приложения."""

# UI Dialogs package

from .common.base import BaseDialog
from .common.about import AboutDialog
from .common.common import (
    DeviceEditDialog,
    ConfigDialog,
    CommandInfoDialog,
    ExportCommandDlg,
    ImportCommandDlg,
    BashViewerDialog,
)  # noqa: F401 — re-export below
from .common.veyon_settings import VeyonSettingsDialog
from .common.vnc_settings import VncSettingsDialog
from .common.ssh_manage import SSHManageDialog
from .device.device_info import DeviceInfoDialog
from .common.params import ParamsInputDialog

from .command.edit import CommandEditorDialog
from .command.result import CommandResultDialog
from .command.sftp_edit import SFTPCommandEditorDialog
from .command.import_dialog import CommandImportDialog
from .command.conflict_dialog import CommandConflictDialog
from .command.ansible_import_dialog import AnsibleImportDialog

from .editors.bash_edit import BashEditorDialog

from .tools.ip_scanner import IPScannerDialog
from .tools.known_hosts import KnownHostsDialog
from .tools.pinger import PingMonitorDialog
from .tools.sftp import SFTPDialog
from .tools.remote_recording import RemoteRecordingDialog
from .tools.polkit_editor import PolkitEditorDialog
from .wizard import SetupWizardDialog
from .tools.diagnostics import DiagnosticsDialog
from .tools.cameras import CamerasDialog
from .tools.camera_settings import CameraSettingsDialog
from .tools.video_editor import VideoEditorDialog

__all__ = [
    # Base
    "BaseDialog",
    # Common
    "AboutDialog",
    "DeviceEditDialog",
    "ConfigDialog",
    "VeyonSettingsDialog",
    "VncSettingsDialog",
    "SSHManageDialog",
    "DeviceInfoDialog",
    "CommandInfoDialog",
    "ExportCommandDlg",
    "ImportCommandDlg",
    "BashViewerDialog",
    "ParamsInputDialog",
    # Command
    "CommandEditorDialog",
    "CommandImportDialog",
    "CommandConflictDialog",
    "AnsibleImportDialog",
    "CommandResultDialog",
    "SFTPCommandEditorDialog",
    # Editors
    "BashEditorDialog",
    # Tools
    "IPScannerDialog",
    "KnownHostsDialog",
    "PingMonitorDialog",
    "RemoteRecordingDialog",
    "SFTPDialog",
    "PolkitEditorDialog",
    "SetupWizardDialog",
    "DiagnosticsDialog",
    "CamerasDialog",
    "CameraSettingsDialog",
    "VideoEditorDialog",
]
