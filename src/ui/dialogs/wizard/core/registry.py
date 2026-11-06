"""Реестр шагов мастера: дескрипторы, порядок, группы, гейтинг по сервисам.

Сервисы, их возможности и параметры задаёт INSTALLERS. Для объявленных
параметров создаются общие страницы; отдельные страницы встроенных сервисов
описаны в _BUILTIN_STEPS. Оболочка и worker не ветвятся по ключам расширений.
"""

from dataclasses import dataclass
from collections.abc import Sequence, Mapping

# Сервисы в порядке отображения в таблице удаленной установки.
# packages - единая колонка для всех доп. RPM-пакетов.
from src.workers.setup.types import INSTALLERS


class ServiceKeys(Sequence):
    def __getitem__(self, index):
        return tuple(item.key for item in INSTALLERS)[index]

    def __len__(self):
        return len(INSTALLERS)


class ServiceLabels(Mapping):
    def __getitem__(self, key):
        for item in INSTALLERS:
            if item.key == key:
                return item.label
        raise KeyError(key)

    def __iter__(self):
        return iter(REMOTE_SERVICES)

    def __len__(self):
        return len(INSTALLERS)


REMOTE_SERVICES = ServiceKeys()
SERVICE_LABELS = ServiceLabels()

# Группы навигационного дерева (None = верхний уровень).
GROUP_TITLES = {
    "local": "Локальная машина",
    "remote": "Удалённые",
}


@dataclass(frozen=True)
class StepDescriptor:
    """Декларативное описание шага."""

    sid: str
    nav_title: str
    page_title: str
    icon_key: str
    group: str | None = None  # None | "local" | "remote"
    requires_service: str | None = None  # "ssh"|"veyon"|"vnc"|"packages"|None


# Порядок шагов в мастере (фиксированный, как _STACK_ORDER раньше).
_BUILTIN_STEPS = (
    StepDescriptor("intro", "Начало", "Мастер настройки DNO Tool", "menu_info"),
    StepDescriptor("hosts", "Хосты", "Настройка списка хостов", "menu_hosts"),
    StepDescriptor("commands", "Команды", "Загрузка команд", "menu_commands"),
    StepDescriptor(
        "local_ssh",
        "SSH-ключи",
        "Локальная машина: SSH-ключи",
        "key_exists",
        group="local",
        requires_service="ssh",
    ),
    StepDescriptor(
        "local_veyon",
        "Veyon",
        "Локальная машина: Veyon",
        "menu_veyon",
        group="local",
        requires_service="veyon",
    ),
    StepDescriptor(
        "local_vnc",
        "VNC-параметры",
        "Локальная машина: VNC",
        "menu_vnc",
        group="local",
        requires_service="vnc",
    ),
    StepDescriptor(
        "remote_packages",
        "Пакеты",
        "Удалённые: дополнительные пакеты",
        "menu_commands",
        group="remote",
        requires_service="packages",
    ),
    StepDescriptor(
        "remote",
        "Установка",
        "Удалённые: потоковая установка сервисов",
        "menu_sftp",
        group="remote",
    ),
    StepDescriptor("summary", "Готово", "Сводка и сохранение", "result_success"),
)


class StepOrder(Sequence):
    def _snapshot(self):
        extras = tuple(
            StepDescriptor(
                "service_" + item.key,
                item.label,
                item.label,
                "menu_commands",
                group="remote",
                requires_service=item.key,
            )
            for item in INSTALLERS
            if item.parameters
        )
        return _BUILTIN_STEPS[:-2] + extras + _BUILTIN_STEPS[-2:]

    def __getitem__(self, index):
        return self._snapshot()[index]

    def __len__(self):
        return len(self._snapshot())


STEP_ORDER = StepOrder()


def active_steps(services: dict) -> list:
    """Вернуть sid активных шагов с учетом выбранных сервисов."""
    result = []
    for step in STEP_ORDER:
        if step.requires_service and not services.get(step.requires_service, False):
            continue
        result.append(step.sid)
    return result


def describe(sid: str) -> StepDescriptor:
    """Найти дескриптор шага по sid."""
    for step in STEP_ORDER:
        if step.sid == sid:
            return step
    raise KeyError(f"Unknown wizard step: {sid}")
