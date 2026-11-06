"""Compatibility exports; implementation lives in :mod:`src.workers.setup`."""

from .setup.worker import RemoteServiceInstallerWorker as RemoteServiceInstallerWorker
from .setup.types import (
    RESULT_SUCCESS as RESULT_SUCCESS,
    RESULT_ERROR as RESULT_ERROR,
    RESULT_IGNORE as RESULT_IGNORE,
    RESULT_ABORT as RESULT_ABORT,
    RESULT_WAITING as RESULT_WAITING,
    SERVICE_ORDER as SERVICE_ORDER,
    parse_dnf_available as parse_dnf_available,
    parse_apt_candidate as parse_apt_candidate,
)
