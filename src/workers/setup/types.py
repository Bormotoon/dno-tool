"""Setup results, parsers and installer registry."""

from dataclasses import dataclass
import re


def validate_port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("Порт должен быть целым числом от 1 до 65535")


def validate_service_parameters(service, parameters):
    if service == "packages":
        from src.workers.setup.packages import validate_package_names

        validate_package_names(parameters.extra_packages)
    elif service == "ssh":
        validate_port(parameters.ssh_port)
    elif service == "veyon":
        validate_port(parameters.veyon_server_port)
        if not re.fullmatch(
            r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}", parameters.veyon_key_name or ""
        ):
            raise ValueError("Недопустимое имя ключа Veyon")
        if parameters.veyon_access_group is not None and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.\-]*\$?", parameters.veyon_access_group
        ):
            raise ValueError("Недопустимая группа доступа Veyon")
    elif service == "vnc":
        validate_port(parameters.vnc_port)
        from src.config.settings import VNC_SERVERS

        if parameters.vnc_server not in VNC_SERVERS:
            raise ValueError("Неизвестный VNC backend")
        if any(c in parameters.vnc_password for c in ("\0", "\n", "\r")):
            raise ValueError("Пароль VNC должен быть одной строкой без нулевых байтов")
        if parameters.vnc_server == "krfb" and not parameters.vnc_password:
            raise ValueError("Для автоматического доступа KRFB требуется пароль")
        if parameters.vnc_server == "kmsvnc" and not parameters.vnc_password:
            raise ValueError("Для kmsvnc требуется пароль")


RESULT_SUCCESS: int = 0


RESULT_ERROR: int = 1


RESULT_IGNORE: int = 2


RESULT_ABORT: int = 3


RESULT_WAITING: int = 4


def parse_dnf_available(output: str, wanted) -> set:
    """Выделить из `dnf list available` имена найденных пакетов.

    Строки вида `mos-auth-core.x86_64  1.0  repo` - имя до последней точки
    первого токена. Строки ошибок пропускаются. Чистая функция (тестируема).
    """
    found = set()
    wanted = set(wanted)
    for line in (output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        first = line.split()[0]
        if first.startswith("Error") or first.startswith("Failed"):
            continue
        name = first.rsplit(".", 1)[0] if "." in first else first
        if name in wanted:
            found.add(name)
    return found


def parse_apt_candidate(output: str) -> bool:
    """True, если `apt-cache policy <pkg>` показывает кандидата на установку."""
    for line in (output or "").splitlines():
        s = line.strip()
        if s.startswith("Candidate:"):
            return s.split(":", 1)[1].strip() not in ("(none)", "")
    return False


@dataclass(frozen=True)
class InstallerDescriptor:
    key: str
    label: str
    implementation: str
    capabilities: tuple[str, ...] = ()
    reversible: bool = False
    parameters: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(
            self, "parameters", tuple(tuple(item) for item in self.parameters)
        )

    def validate(self, parameters):
        validate_service_parameters(self.key, parameters)

    def build_parameters(self, values):
        """Validate declared extension options and freeze their plan representation."""
        if set(values) - {name for name, _, _ in self.parameters}:
            raise ValueError("Неизвестные параметры сервиса")
        options = []
        for name, label, default in self.parameters:
            value = values.get(name, default)
            if not isinstance(value, str) or "\0" in value or len(value) > 4096:
                raise ValueError(f"Недопустимое значение: {label}")
            options.append((name, value))
        return tuple(options)


INSTALLERS = [
    InstallerDescriptor(
        "ssh",
        "SSH",
        "src.workers.setup.installers.ssh:SshInstaller",
        ("key_deployment", "key_login"),
    ),
    InstallerDescriptor(
        "packages",
        "Пакеты",
        "src.workers.setup.installers.packages:PackagesInstaller",
        ("packages", "post_install"),
    ),
    InstallerDescriptor(
        "veyon",
        "Veyon",
        "src.workers.setup.installers.veyon:VeyonInstaller",
        ("key_auth", "listener", "operator_auth"),
    ),
    InstallerDescriptor(
        "vnc",
        "VNC",
        "src.workers.setup.installers.vnc:VncInstaller",
        ("x11vnc", "krfb", "kmsvnc", "rfb_probe"),
        True,
    ),
]


def register_installer(descriptor):
    if any(item.key == descriptor.key for item in INSTALLERS):
        raise ValueError("Сервис уже зарегистрирован")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", descriptor.key):
        raise ValueError("Недопустимый ключ сервиса")
    INSTALLERS.append(descriptor)


SERVICE_ORDER = tuple(item.key for item in INSTALLERS)
