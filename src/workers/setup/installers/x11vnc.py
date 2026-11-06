"""x11vnc: пакет из репозитория, служба и скрипт запуска от мастера."""

from src.workers.setup.installers.system_vnc import (
    SystemVncBackend,
    VncPackage,
)
from src.workers.setup.templates import (
    _X11VNC_SERVICE_TEMPLATE,
    _X11VNC_WRAPPER_TEMPLATE,
)


class X11VncBackend(SystemVncBackend):
    NAME = "x11vnc"
    TITLE = "x11vnc"
    PACKAGE = VncPackage(name="x11vnc", binary="x11vnc")
    UNIT_TEMPLATE = _X11VNC_SERVICE_TEMPLATE
    WRAPPER = ("/usr/local/bin/x11vnc-dno-tool.sh", _X11VNC_WRAPPER_TEMPLATE)
    LOG_FILE = "/var/log/x11vnc.log"
    FAILURE_HINT = (
        "x11vnc работает только в сеансе X11: для Wayland выберите kmsvnc."
    )

    def password_content(self, password: str):
        from src.utils.security.vnc_password import encode_vnc_password_file

        return encode_vnc_password_file(password)
