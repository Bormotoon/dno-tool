"""Валидация настроек карты при чтении и атомарном сохранении."""

from copy import deepcopy
import logging

RANGES = {
    "topology_max_hosts": (254, 1, 65536),
    "topology_max_addresses": (65536, 1, 65536),
    "topology_threads": (32, 1, 128),
    "topology_trace_threads": (8, 1, 32),
    "topology_max_hops": (20, 1, 40),
    "topology_trace_timeout": (5000, 200, 10000),
    "topology_trace_retries": (1, 0, 3),
    "topology_spread": (100, 40, 150),
}
BOOLEANS = {
    "topology_scan_found_subnets": False,
    "topology_exact_routes": False,
    "topology_merge_unknown": False,
    "topology_params_expanded": True,
}
CHOICES = {
    "topology_discovery_mode": ("icmp", "icmp_tcp"),
    "topology_layout": ("circles", "rings"),
    "topology_edge_style": ("ants", "plain"),
    "topology_node_style": ("scheme", "icons"),
}


def validate_option(name: str, value: object) -> object:
    """Проверить одно поле без записи и изменения исходного объекта."""
    if name in RANGES:
        _, low, high = RANGES[name]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name}: допустимо целое {low}..{high}")
    elif name in BOOLEANS:
        if type(value) is not bool:
            raise ValueError(f"{name}: требуется bool")
    elif name in CHOICES:
        if value not in CHOICES[name]:
            raise ValueError(f"{name}: неизвестное значение")
    elif name == "topology_tcp_ports":
        if (
            not isinstance(value, (list, tuple))
            or not 1 <= len(value) <= 8
            or any(type(p) is not int or not 1 <= p <= 65535 for p in value)
            or len(set(value)) != len(value)
        ):
            raise ValueError("TCP: от 1 до 8 уникальных портов 1..65535")
    elif name == "topology_subnets":
        if not isinstance(value, str) or len(value) > 65536:
            raise ValueError("Список подсетей слишком велик")
    elif name == "topology_node_names":
        if (
            not isinstance(value, dict)
            or len(value) > 65536
            or any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in value.items()
            )
        ):
            raise ValueError("Некорректный словарь подписей")
    else:
        raise ValueError(f"Неизвестная настройка карты: {name}")
    return deepcopy(value)


def normalize_settings(network: object) -> None:
    """Поврежденные поля заменить defaults независимо от соседних, без persist."""
    defaults = {
        **{k: v[0] for k, v in RANGES.items()},
        **BOOLEANS,
        **{k: v[0] for k, v in CHOICES.items()},
        "topology_tcp_ports": [22, 80, 443],
        "topology_subnets": "",
        "topology_node_names": {},
    }
    for name, default in defaults.items():
        try:
            validate_option(name, getattr(network, name, default))
        except (ValueError, TypeError):
            logging.getLogger(__name__).warning(
                "Поврежденная настройка %s заменена значением по умолчанию", name
            )
            setattr(network, name, deepcopy(default))
