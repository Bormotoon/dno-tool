"""Реестр иконок тем: PAINTERS + COLORS из модулей групп.

Точечное изменение/добавление иконки = правка одного модуля
scripts/icons/<группа>.py. Дубли имен валятся сразу при импорте.
"""

from ._base import NO_BACKING_NAMES

from . import logo
from . import folders
from . import devices
from . import results
from . import terminal
from . import scanner
from . import hostinfo
from . import tools
from . import files
from . import creds
from . import nodes
from . import veyon
from . import toolbar
from . import topology
from . import video
from . import topo
from . import cameras
from . import command_steps

_MODULES = (logo, folders, devices, results, terminal, scanner, hostinfo, tools, files, creds, nodes, veyon, toolbar, topology, video, topo, cameras, command_steps,)

PAINTERS: dict = {}
COLORS: dict = {}

for _mod in _MODULES:
    for _k, _v in _mod.PAINTERS.items():
        if _k in PAINTERS:
            raise ImportError(f"Дубль иконки {_k}: {_mod.__name__}")
        PAINTERS[_k] = _v
    for _k, _v in _mod.COLORS.items():
        if _k in COLORS:
            raise ImportError(f"Дубль цвета {_k}: {_mod.__name__}")
        COLORS[_k] = _v

NO_BACKING_NAMES.update(topo.TOPO_NO_BACKING)
NO_BACKING_NAMES.add("camera")
NO_BACKING_NAMES.update(command_steps.PAINTERS)

TOPO_NAMES = list(topo.TOPO_NAMES)

def get_painter(name: str):
    """Painter (mode, ink) -> inner. KeyError - нет иконки."""
    return PAINTERS[name]

def get_color(name: str, default: str = "#64748B") -> str:
    """Акцент иконки."""
    return COLORS.get(name, default)

def all_names() -> list:
    """Все имена иконок реестра."""
    return sorted(PAINTERS)
