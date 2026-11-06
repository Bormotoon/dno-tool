"""Private command library with the same import rules as CommandService."""

from copy import deepcopy
import json
from uuid import uuid4

from src.services.commands.command_import import (
    find_conflicts,
    flatten_tasks,
    format_stats_message,
    merge_command_trees,
    preview_merge,
)


def normalize_tree(items, kind):
    if not isinstance(items, list):
        raise ValueError("Ожидался список")
    result = deepcopy(items)
    seen = set()

    def walk(nodes):
        for node in nodes:
            if not isinstance(node, dict):
                raise ValueError("Недопустимая запись дерева")
            if (kind == "hosts" and "host" in node) or (
                kind == "commands" and "commands" in node
            ):
                if not node.get("name") or (kind == "hosts" and not node.get("host")):
                    raise ValueError("Заполните имя и адрес хоста/имя команды")
                if kind == "commands" and not isinstance(node["commands"], list):
                    raise ValueError("commands должен быть списком")
                if kind == "commands" and node.get("type", "ssh") not in (
                    "ssh",
                    "sftp",
                    "local",
                ):
                    raise ValueError("Неизвестный тип команды")
                if kind == "hosts":
                    if (
                        node.get("use_sudo") is not None
                        and type(node["use_sudo"]) is not bool
                    ):
                        raise ValueError("Некорректное значение sudo")
                    for name in ("port", "vnc_port"):
                        port = node.get(name)
                        if port is not None and (
                            type(port) is not int or not 1 <= port <= 65535
                        ):
                            raise ValueError("Недопустимый порт хоста")
                iid = node.get("iid") or uuid4().hex
                if iid in seen:
                    raise ValueError("Повторяющийся iid в дереве")
                node["iid"] = iid
                seen.add(iid)
            elif len(node) == 1 and isinstance(next(iter(node.values())), list):
                walk(next(iter(node.values())))
            else:
                raise ValueError("Недопустимая структура дерева")

    walk(result)
    if kind == "commands":
        from src.domain.command_schema import validate_command_tree

        validate_command_tree(result)
    return result


class CommandDraft:
    def __init__(self, data=()):
        self.data = normalize_tree(list(data), "commands")

    def get_all_commands(self):
        return deepcopy(self.data)

    def get_tasks_count(self):
        return len(flatten_tasks(self.data))

    def _read(self, path):
        with open(path, encoding="utf-8-sig") as stream:
            return normalize_tree(json.load(stream), "commands")

    def preview_import_from_file(self, path):
        try:
            return True, "", preview_merge(deepcopy(self.data), self._read(path))
        except Exception as error:
            return False, str(error), None

    def find_import_conflicts(self, path):
        return find_conflicts(self.data, self._read(path))

    def import_commands_from_file(self, path, mode="merge", resolution=None):
        try:
            imported = self._read(path)
            if mode == "replace":
                result = imported
                stats = dict(mode=mode, total_imported=len(flatten_tasks(imported)))
                message = f"Импортировано команд: {stats['total_imported']}"
            elif mode == "merge":
                result = deepcopy(self.data)
                stats = merge_command_trees(result, imported, resolution=resolution)
                message = format_stats_message(stats, mode)
            else:
                raise ValueError("Неизвестный режим импорта")
            self.data = normalize_tree(result, "commands")
            return True, message, stats
        except Exception as error:
            return False, str(error), None
