"""Remote scripts and service templates, independent of Qt and application state."""

from .ssh import (
    AUTHORIZED_KEYS_SCRIPT as AUTHORIZED_KEYS_SCRIPT,
)

from .shell import (
    _as_user_func as _as_user_func,
    _as_user_call as _as_user_call,
    _shell_quote_bare as _shell_quote_bare,
)

from .vnc import (
    _X11VNC_WRAPPER_TEMPLATE as _X11VNC_WRAPPER_TEMPLATE,
    _X11VNC_SERVICE_TEMPLATE as _X11VNC_SERVICE_TEMPLATE,
    _KRFB_CONFIG_SCRIPT as _KRFB_CONFIG_SCRIPT,
    _KRFB_UNIT_TEMPLATE as _KRFB_UNIT_TEMPLATE,
    _KRFB_WAYLAND_UNIT_TEMPLATE as _KRFB_WAYLAND_UNIT_TEMPLATE,
    _KRFB_HIDE_TRAY_SCRIPT as _KRFB_HIDE_TRAY_SCRIPT,
    _KMSVNC_SERVICE_TEMPLATE as _KMSVNC_SERVICE_TEMPLATE,
)
