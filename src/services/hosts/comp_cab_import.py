"""Импорт списка компьютеров из comp-cab-setup (hosts.json / setup_hosts.json).

Модуль не зависит от Qt. Формат источника: словарь ``ключ -> запись`` либо
список записей; запись содержит ``hostname``, ``name``, ``mac_address``,
``cabinet`` и учётные данные, которые сознательно НЕ переносятся
(пароли в dno-tool хранятся только в связках).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 8 * 1024**2
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")

# Каталоги, где comp-cab-setup хранит данные (текущий и устаревший).
CANDIDATE_DIRS = ("~/.teacher_control", "~/.teacher_config")
CANDIDATE_FILES = ("hosts.json", "setup_hosts.json")


def find_default_files() -> list[Path]:
    """Найти существующие файлы хостов comp-cab-setup в домашнем каталоге."""
    found = []
    for directory in CANDIDATE_DIRS:
        for name in CANDIDATE_FILES:
            path = Path(os.path.expanduser(directory)) / name
            if path.is_file():
                found.append(path)
    return found


def parse_comp_cab_hosts(path: str | os.PathLike) -> dict[str, Any]:
    """Прочитать файл и вернуть дерево для ``DeviceTreeView.load_tree_data``.

    Результат: ``{"tree": [...], "count": int, "skipped": [str], "cabinets": int}``.
    ``tree`` - список папок (по кабинетам) и хостов без кабинета в формате hosts.json.
    """
    file = Path(path)
    if file.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Файл слишком большой")
    text = file.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except ValueError:
        # Текстовый формат comp-cab-setup: один адрес в строке.
        data = [
            {"hostname": line.strip()}
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    if isinstance(data, dict) and isinstance(data.get("hosts"), (dict, list)):
        data = data["hosts"]
    records = list(data.values()) if isinstance(data, dict) else data
    if not isinstance(records, list):
        raise ValueError("Неизвестный формат файла хостов")

    skipped: list[str] = []
    seen: set[str] = set()
    by_cabinet: dict[str, list[dict]] = {}
    loose: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        host = str(record.get("hostname") or "").strip()
        name = str(record.get("name") or "").strip() or host
        if not host:
            skipped.append(name or "(без имени)")
            continue
        if host.casefold() in seen:
            skipped.append(f"{name}: дубликат {host}")
            continue
        seen.add(host.casefold())
        device: dict[str, Any] = {"name": name, "host": host}
        mac = str(record.get("mac_address") or "").strip()
        if _MAC_RE.match(mac):
            device["mac_address"] = mac.replace("-", ":").lower()
        cabinet = str(record.get("cabinet") or "").strip()
        (by_cabinet.setdefault(cabinet, []) if cabinet else loose).append(device)

    tree: list[Any] = [{cab: devices} for cab, devices in sorted(by_cabinet.items())]
    tree.extend(loose)
    count = sum(len(v) for v in by_cabinet.values()) + len(loose)
    return {
        "tree": tree,
        "count": count,
        "skipped": skipped,
        "cabinets": len(by_cabinet),
    }
