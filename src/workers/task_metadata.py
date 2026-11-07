"""Источник запроса, сохраняемый при передаче работы между пулами."""

from contextvars import ContextVar
from typing import Any

operation_source: ContextVar[str | None] = ContextVar("operation_source", default=None)

# Операции, которых ждёт человек: они обгоняют пакетные задачи своей роли и могут
# занимать резервные потоки. Новый тип задачи по умолчанию считается пакетным,
# поэтому кнопочные операции нужно добавлять сюда явно.
INTERACTIVE_TASK_TYPES = frozenset(
    {
        # Роль commands
        "veyon_feature",
        "node_veyon_feature",
        "node_feature",
        "web_veyon_feature",
        "web_power",
        "node_host_crud",
        # Роль services
        "host_info",
        "node_info",
        "node_vnc_url",
        "vnc_screens",
        "veyon_status",
        "node_server_settings",
        "web_server_settings",
        "web_ldap_check",
        "web_highlight",
        "web_resources",
        "web_ping",
        "web_camera_snapshot",
        "web_veyon_restart",
        "web_device_info",
    }
)


def is_interactive_task(metadata: dict[str, Any]) -> bool:
    """Неизвестный тип считается пакетным: резерв ему недоступен."""
    return metadata.get("type") in INTERACTIVE_TASK_TYPES


def task_metadata(
    metadata: dict[str, Any] | None, default_source: str | None = None
) -> dict[str, Any]:
    """Сохранить явный источник или унаследовать инициатора текущей операции."""
    info = dict(metadata or {})
    source = operation_source.get() or default_source
    if "source" not in info and source is not None:
        info["source"] = source
    return info
