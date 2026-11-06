"""Merge-импорт задач: учитывает iid и категории.

Qt-free модуль - используется из DataStore/CommandService и тестов
без инициализации Qt.

Структура дерева команд:
- Категория - dict с одним ключом ``{имя: [дети]}``.
- Задача (Task) - dict с полями ``name`` + ``commands`` (+ ``iid`` и прочие).

Правила merge (режим "Обновить"):
- Совпадение только по ``iid``. Совпадение ``путь + имя`` обновлением НЕ считается.
- Знакомый ``iid`` - обновить поля на месте, позиция в дереве не меняется,
  категория из файла импорта игнорируется.
- Новый ``iid`` - добавить в категорию из файла импорта при совпадении
  цепочки категорий, иначе создать недостающие категории.
- Без ``iid`` - всегда новая задача с генерацией ``iid``.
- Merge никогда ничего не удаляет и не перемещает локальное.
"""

import copy
import uuid
from typing import Any, Dict, List, Optional, Tuple

Path = Tuple[str, ...]


def is_task(item: Any) -> bool:
    """Проверить, что элемент - задача (есть name и commands)."""
    return isinstance(item, dict) and "name" in item and "commands" in item


def is_category(item: Any) -> bool:
    """Проверить, что элемент - категория (один ключ со списком)."""
    if not isinstance(item, dict) or len(item) != 1:
        return False
    return isinstance(next(iter(item.values())), list)


def validate_task(task: Any) -> Tuple[bool, str]:
    """Проверить задачу перед импортом тем же правилом, что и при загрузке."""
    from src.domain.command_schema import task_error

    error = task_error(task)
    return not error, error


def flatten_tasks(items: List[Dict], parent_path: Path = ()) -> List[Tuple[Path, Dict]]:
    """Развернуть дерево в плоский список (путь категории, задача)."""
    result: List[Tuple[Path, Dict]] = []
    if not isinstance(items, list):
        return result
    for item in items:
        if is_task(item):
            result.append((parent_path, item))
        elif is_category(item):
            name = next(iter(item.keys()))
            children = item[name]
            result.extend(flatten_tasks(children, parent_path + (name,)))
    return result


Conflict = Dict[str, Any]
"""Конфликт: {iid, local_name, imported_name, local_path, imported_path,
name_differs, parent_differs}."""

Resolution = Dict[str, str]
"""Решение конфликтов: {"name": "mine"|"theirs", "parent": "mine"|"theirs"}."""

DEFAULT_RESOLUTION: Resolution = {"name": "theirs", "parent": "mine"}


def _build_iid_index(items: List[Dict]) -> Dict[str, Tuple[Path, Dict, List[Dict]]]:
    """Построить индекс iid -> (путь, ссылка на задачу, список категории)."""
    index: Dict[str, Tuple[Path, Dict, List[Dict]]] = {}

    def _walk(node: List[Dict], path: Path) -> None:
        for item in node:
            if is_task(item):
                iid = item.get("iid")
                if iid and iid not in index:
                    index[iid] = (path, item, node)
            elif is_category(item):
                name = next(iter(item.keys()))
                children = item[name]
                if isinstance(children, list):
                    _walk(children, path + (name,))

    if isinstance(items, list):
        _walk(items, ())
    return index


def _ensure_category_path(tree: List[Dict], path: Path) -> Tuple[List[Dict], int]:
    """Найти (или создать) список детей для цепочки категорий.

    Returns:
        (целевой список, число созданных категорий)
    """
    created = 0
    current = tree
    for segment in path:
        if not segment:
            continue
        found: Optional[List[Dict]] = None
        for item in current:
            if isinstance(item, dict) and len(item) == 1 and segment in item:
                children = item[segment]
                if isinstance(children, list):
                    found = children
                    break
        if found is None:
            new_children: List[Dict] = []
            current.append({segment: new_children})
            created += 1
            found = new_children
        current = found
    return current, created


def empty_stats() -> Dict[str, int]:
    """Пустая статистика merge."""
    return {
        "updated": 0,
        "added": 0,
        "added_categories": 0,
        "skipped_invalid": 0,
        "duplicate_iids": 0,
        "total_imported": 0,
    }


def find_conflicts(existing: List[Dict], imported: List[Dict]) -> List[Conflict]:
    """Найти конфликты: тот же iid, но другое имя или другая категория.

    Args:
        existing: локальное дерево (не мутируется).
        imported: дерево из файла импорта (не мутируется).

    Returns:
        Список конфликтов (пустой если расхождений нет).
    """
    result: List[Conflict] = []
    if not isinstance(imported, list):
        return result
    index = _build_iid_index(existing)
    seen: set = set()
    for path, task in flatten_tasks(imported):
        valid, _ = validate_task(task)
        if not valid:
            continue
        iid = task.get("iid")
        if not iid or iid in seen or iid not in index:
            if iid:
                seen.add(iid)
            continue
        seen.add(iid)
        local_path, local_task, _parent = index[iid]
        name_differs = bool(task.get("name")) and task.get("name") != local_task.get(
            "name"
        )
        parent_differs = tuple(path) != tuple(local_path)
        if name_differs or parent_differs:
            result.append(
                {
                    "iid": iid,
                    "local_name": local_task.get("name", ""),
                    "imported_name": task.get("name", ""),
                    "local_path": local_path,
                    "imported_path": tuple(path),
                    "name_differs": name_differs,
                    "parent_differs": parent_differs,
                }
            )
    return result


def merge_command_trees(
    existing: List[Dict],
    imported: List[Dict],
    resolution: Optional[Resolution] = None,
) -> Dict[str, int]:
    """Влить импортированное дерево в существующее (мутирует existing).

    Args:
        existing: локальное дерево (мутируется на месте).
        imported: дерево из файла импорта (не мутируется).
        resolution: решение конфликтов {"name": "mine"|"theirs",
            "parent": "mine"|"theirs"}. По умолчанию {"name": "theirs",
            "parent": "mine"} - прежнее поведение.

    Returns:
        Статистика {updated, added, added_categories, skipped_invalid,
        duplicate_iids, total_imported}.
    """
    stats = empty_stats()
    if not isinstance(imported, list):
        return stats
    policy: Resolution = {**DEFAULT_RESOLUTION, **(resolution or {})}

    index = _build_iid_index(existing)
    used_iids = set(index.keys())
    seen_in_file = set()

    flat = flatten_tasks(imported)
    stats["total_imported"] = len(flat)

    for path, task in flat:
        valid, _ = validate_task(task)
        if not valid:
            stats["skipped_invalid"] += 1
            continue

        iid = task.get("iid")
        if iid:
            if iid in seen_in_file:
                stats["duplicate_iids"] += 1
                continue
            seen_in_file.add(iid)

        if iid and iid in index:
            # Знакомый iid: содержимое всегда из файла, имя и позиция - по resolution.
            local_path, existing_task, parent_list = index[iid]
            fresh = copy.deepcopy(task)
            fresh["iid"] = iid
            if policy.get("name") == "mine":
                fresh["name"] = existing_task.get("name", fresh.get("name"))
            if policy.get("parent") == "theirs" and tuple(path) != tuple(local_path):
                target, created = _ensure_category_path(existing, tuple(path))
                stats["added_categories"] += created
                try:
                    parent_list.remove(existing_task)
                except ValueError:
                    pass
                target.append(existing_task)
                index[iid] = (tuple(path), existing_task, target)
                stats["moved"] = stats.get("moved", 0) + 1
            existing_task.clear()
            existing_task.update(fresh)
            stats["updated"] += 1
            continue

        # Новая задача.
        new_task = copy.deepcopy(task)
        new_iid = new_task.get("iid")
        if not new_iid or new_iid in used_iids:
            new_iid = uuid.uuid4().hex
            new_task["iid"] = new_iid
        seen_in_file.add(new_iid)
        used_iids.add(new_iid)

        target, created = _ensure_category_path(existing, path)
        stats["added_categories"] += created
        target.append(new_task)
        index[new_iid] = (tuple(path), target[-1], target)
        stats["added"] += 1

    return stats


def preview_merge(existing: List[Dict], imported: List[Dict]) -> Dict[str, int]:
    """Посчитать статистику merge без мутации исходных данных."""
    working_copy = copy.deepcopy(existing)
    imported_copy = copy.deepcopy(imported)
    return merge_command_trees(working_copy, imported_copy)


def format_stats_message(stats: Dict[str, int], mode: str) -> str:
    """Собрать короткое русское сообщение об итоге импорта."""
    if mode == "replace":
        total = stats.get("total_imported", 0)
        return f"Заменено задач: {total}"
    parts = [
        f"Обновлено: {stats.get('updated', 0)}",
        f"добавлено: {stats.get('added', 0)}",
    ]
    if stats.get("added_categories"):
        parts.append(f"новых разделов: {stats['added_categories']}")
    if stats.get("skipped_invalid"):
        parts.append(f"пропущено невалидных: {stats['skipped_invalid']}")
    if stats.get("duplicate_iids"):
        parts.append(f"повторов в файле: {stats['duplicate_iids']}")
    if stats.get("moved"):
        parts.append(f"перемещено: {stats['moved']}")
    if stats.get("conflicts_resolved") == "mine":
        parts.append("конфликты: оставлены мои имена и места")
    elif stats.get("conflicts_resolved") == "theirs":
        parts.append("конфликты: взяты из файла")
    return "Импорт завершен - " + ", ".join(parts)
