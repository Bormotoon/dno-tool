"""Разбор политик и построение команды изменения одного действия Polkit."""

import json
import shlex
import xml.etree.ElementTree as ET


POLKIT_ACTIONS_DIR = "/usr/share/polkit-1/actions"
MAX_POLICY_BYTES = 4 * 1024 * 1024
PERMISSION_KEYS = ("allow_any", "allow_inactive", "allow_active")
PERMISSION_VALUES = {
    "yes": "yes — Разрешить",
    "no": "no — Запретить",
    "auth_admin": "auth_admin — Аутентификация администратора",
    "auth_admin_keep": "auth_admin_keep — Аутентификация админа (сохранённая)",
    "auth_self": "auth_self — Аутентификация пользователя",
    "auth_self_keep": "auth_self_keep — Аутентификация пользователя (сохранённая)",
}


class PolkitPolicy:
    """Операции с XML без зависимости от интерфейса и SSH."""

    @staticmethod
    def _localized(action: ET.Element, tag: str) -> str:
        values = {
            node.get("{http://www.w3.org/XML/1998/namespace}lang", "").replace("_", "-").lower(): "".join(node.itertext())
            for node in action.findall(tag)
        }
        for language in ("ru", "ru-ru", "", "en", "en-us"):
            if values.get(language):
                return values[language]
        return next(iter(values.values()), "")

    @staticmethod
    def parse(root: ET.Element) -> dict[str, dict]:
        if root.tag != "policyconfig":
            raise ValueError("Корневой элемент XML должен быть policyconfig")
        policies = {}
        for action in root.findall("action"):
            action_id = action.get("id", "")
            if not action_id:
                continue
            if action_id in policies:
                raise ValueError(f"Повторяющийся action: {action_id}")
            groups = action.findall("defaults")
            if len(groups) > 1:
                raise ValueError(f"Повторяющийся defaults: {action_id}")
            defaults = groups[0] if groups else None
            if defaults is not None and any(len(defaults.findall(key)) > 1 for key in PERMISSION_KEYS):
                raise ValueError(f"Повторяющийся параметр прав доступа: {action_id}")
            policies[action_id] = {
                "description": PolkitPolicy._localized(action, "description"),
                "message": PolkitPolicy._localized(action, "message"),
                "defaults": {
                    key: (node.text or "").strip()
                    for key in PERMISSION_KEYS
                    if defaults is not None and (node := defaults.find(key)) is not None
                },
                "annotations": {
                    node.get("key"): node.get("value", node.text or "")
                    for node in action.findall("annotate") if node.get("key")
                },
                **{
                    key: action.findtext(tag, root.findtext(tag, ""))
                    for key, tag in (("vendor", "vendor"), ("vendor_url", "vendor_url"), ("icon", "icon_name"))
                },
            }
        return policies

    @staticmethod
    def update_command(source_file: str, action_id: str, defaults: dict, expected: dict) -> str:
        """Команда использует тот же алгоритм, что и кнопка сохранения."""
        payload = json.dumps(
            {"path": source_file, "action": action_id, "defaults": defaults, "expected": expected},
            ensure_ascii=True,
        )
        return f"python3 -c {shlex.quote(_UPDATE_SCRIPT)} {shlex.quote(payload)}"


# Скрипт самодостаточен: выполняется на удалённом Linux через python3.
# stdin зарезервирован для sudo; XML и пароль никогда не смешиваются.
_UPDATE_SCRIPT = r'''
import json
import os
import shutil
import stat
import sys
import tempfile
from xml.dom import minidom


def children(node: minidom.Node, name: str) -> list:
    return [item for item in node.childNodes
            if item.nodeType == item.ELEMENT_NODE and item.tagName == name]


def text(node: minidom.Node) -> str:
    return "".join(item.data for item in node.childNodes
                   if item.nodeType in (item.TEXT_NODE, item.CDATA_SECTION_NODE)).strip()


def update(data: dict) -> dict:
    path = data["path"]
    directory = "/usr/share/polkit-1/actions"
    if os.path.dirname(path) != directory or not path.endswith(".policy"):
        raise ValueError("Недопустимый путь .policy")
    keys = ("allow_any", "allow_inactive", "allow_active")
    values = {"yes", "no", "auth_admin", "auth_admin_keep", "auth_self", "auth_self_keep"}
    expected = data["expected"]
    defaults = data["defaults"]
    if set(defaults) - set(keys) or set(expected) - set(keys):
        raise ValueError("Неизвестный параметр прав доступа")
    for key, value in defaults.items():
        if value not in values and value != expected.get(key):
            raise ValueError("Недопустимое значение прав доступа")
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Сохранение разрешено только в обычный файл без жёстких ссылок")
    with open(path, "rb") as stream:
        original = stream.read(4 * 1024 * 1024 + 1)
    if len(original) > 4 * 1024 * 1024:
        raise ValueError("Файл политики превышает 4 МиБ")
    doc = minidom.parseString(original)
    if doc.documentElement.tagName != "policyconfig":
        raise ValueError("Корневой элемент XML должен быть policyconfig")
    actions = [node for node in children(doc.documentElement, "action")
               if node.getAttribute("id") == data["action"]]
    if len(actions) != 1:
        raise ValueError("Действие отсутствует или повторяется в файле")
    action = actions[0]
    groups = children(action, "defaults")
    if len(groups) > 1:
        raise ValueError("Повторяющийся элемент defaults")
    group = groups[0] if groups else None
    current = {}
    for key in keys:
        nodes = children(group, key) if group is not None else []
        if len(nodes) > 1:
            raise ValueError("Повторяющийся параметр прав доступа")
        if nodes:
            current[key] = text(nodes[0])
    if current != expected:
        raise ValueError("Политика изменена другим процессом. Обновите список перед сохранением")
    if current == defaults:
        return {"backup": "", "changed": False}
    if group is None:
        group = doc.createElement("defaults")
        action.appendChild(group)
    for key in keys:
        nodes = children(group, key)
        if key not in defaults:
            for node in nodes:
                group.removeChild(node)
            continue
        node = nodes[0] if nodes else doc.createElement(key)
        if not nodes:
            group.appendChild(node)
        for child in list(node.childNodes):
            node.removeChild(child)
        node.appendChild(doc.createTextNode(defaults[key]))
    modified = doc.toxml(encoding="utf-8")
    minidom.parseString(modified)
    temporary = backup = None
    replaced = False
    try:
        fd, temporary = tempfile.mkstemp(prefix=".dno-polkit-", dir=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(modified)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
        shutil.copystat(path, temporary)
        # Новый mtime нужен наблюдателю Polkit для перечитывания .policy.
        os.utime(temporary, None)
        fd, backup = tempfile.mkstemp(prefix=os.path.basename(path) + ".dno-backup-", dir=directory)
        os.close(fd)
        shutil.copy2(path, backup)
        os.chown(backup, info.st_uid, info.st_gid)
        # Перед заменой проверяем весь свежепрочитанный файл, включая соседние действия.
        with open(path, "rb") as stream:
            latest = os.lstat(path)
            signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
            if stream.read(4 * 1024 * 1024 + 1) != original or signature(latest) != signature(info):
                raise ValueError("Файл изменён во время сохранения. Повторите после обновления")
        os.replace(temporary, path)
        replaced = True
        with open(path, "rb") as stream:
            if stream.read(4 * 1024 * 1024 + 1) != modified:
                raise OSError("Проверка записанного файла не пройдена; резервная копия: " + backup)
        return {"backup": backup, "changed": True}
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
        if backup and not replaced:
            os.unlink(backup)


try:
    import fcntl
    # Все экземпляры редактора используют одну блокировку каталога.
    lock = os.open("/usr/share/polkit-1/actions", os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(update(json.loads(sys.argv[1])), ensure_ascii=True))
    finally:
        os.close(lock)
except Exception as error:
    print(str(error), file=sys.stderr)
    sys.exit(1)
'''
