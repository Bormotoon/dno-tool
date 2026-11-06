"""Ядра мастера настройки: контекст, реестр шагов, потоки, сохранение."""

from .context import (
    InstallPlan,
    InstallResult,
    SshState,
    VeyonState,
    VncState,
    WizardContext,
)
from .guard import ThreadGuard
from .registry import (
    GROUP_TITLES,
    REMOTE_SERVICES,
    SERVICE_LABELS,
    STEP_ORDER,
    StepDescriptor,
    active_steps,
    describe,
)

__all__ = [
    "InstallPlan",
    "InstallResult",
    "SshState",
    "VeyonState",
    "VncState",
    "WizardContext",
    "ThreadGuard",
    "GROUP_TITLES",
    "REMOTE_SERVICES",
    "SERVICE_LABELS",
    "STEP_ORDER",
    "StepDescriptor",
    "active_steps",
    "describe",
]
