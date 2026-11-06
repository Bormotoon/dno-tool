"""Импорт Ansible-заданий в задачи dno-tool.

Qt-free модуль - парсинг YAML и маппинг Ansible-модулей в шаги
``commands: [{type: ssh, text}]``. Используется из диалога импорта
и может тестироваться без инициализации Qt.

Вход: текст YAML - полный playbook (список plays с ``tasks:``)
или bare-список tasks (``- name: ...`` + модуль). Автоопределение.

Выход: ``parse_ansible_yaml(text, base_dir="")`` -> dict с ключами:
- ``tasks``: список Task-словарей (``name``, ``description``,
  ``commands``, ``params``, ``timeout``, ``iid`` + внутренние
  ``ansible_source``, ``ansible_name``, ``ansible_notify``,
  ``ansible_listen``, ``ansible_handler`` - только для превью
  и связки notify, диалог удаляет их при импорте).
- ``warnings``: строки о возможном отличии поведения, требующие проверки.
- ``info``: строки о преобразовании или выбранном режиме.
- ``skipped``: список ``{task, module, reason}`` - что не понято.
- ``errors``: список строк фатальных ошибок (пусто = можно импортировать).
- ``stats``: ``{plays, ansible_tasks, created, steps, skipped}``.
"""

import fnmatch
import os
import re
import shlex
import sys
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, FrozenSet, List, Set, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover - зависимость прописана в requirements
    yaml = None  # type: ignore[assignment]

DEFAULT_TIMEOUT = 30
MAX_YAML_CHARS = 1_000_000
MAX_YAML_NODES = 30_000
MAX_TASKS = 1000
MAX_STEPS = 2000
MAX_LOOP_ITEMS = 20
MAX_RESULT_CHARS = 4_000_000


class _SourceDict(dict):
    """Mapping with a source mark; metadata never becomes an Ansible keyword."""

    line = 0


if yaml is not None:
    class _ImportLoader(yaml.SafeLoader):
        """Bound construction and reject duplicate explicit keys (including aliases)."""

        def __init__(self, stream):
            super().__init__(stream)
            self._nodes = 0
            self._depth = 0
            self._checked_mappings = set()

        def get_single_data(self):
            node = self.get_single_node()
            active = set()
            count = 0

            def visit(value, depth=0):
                nonlocal count
                count += 1
                if count > MAX_YAML_NODES or depth > 64:
                    raise ValueError("YAML: превышен лимит раскрытия aliases или глубины")
                if id(value) in active:
                    raise ValueError("Циклический YAML alias")
                active.add(id(value))
                try:
                    if isinstance(value, yaml.MappingNode):
                        for key, child in value.value:
                            visit(key, depth + 1)
                            visit(child, depth + 1)
                    elif isinstance(value, yaml.SequenceNode):
                        for child in value.value:
                            visit(child, depth + 1)
                finally:
                    active.remove(id(value))

            if node is None:
                return None
            # Check aliases before SafeConstructor expands YAML merge mappings.
            visit(node)
            return self.construct_document(node)

        def compose_node(self, parent, index):
            self._nodes += 1
            self._depth += 1
            try:
                if self._nodes > MAX_YAML_NODES or self._depth > 64:
                    raise ValueError("YAML: превышен лимит узлов или глубины (64)")
                return super().compose_node(parent, index)
            finally:
                self._depth -= 1

        def construct_yaml_map(self, node):
            data = _SourceDict()
            data.line = node.start_mark.line + 1
            yield data
            data.update(self.construct_mapping(node))

        def flatten_mapping(self, node):
            if id(node) in self._checked_mappings:
                return super().flatten_mapping(node)
            self._checked_mappings.add(id(node))
            seen = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    key = "<<"
                else:
                    key = self.construct_object(key_node, deep=True)
                if key in seen:
                    raise ValueError(
                        f"Дублирующий ключ {key!r}, строка {key_node.start_mark.line + 1}"
                    )
                seen.add(key)
            return super().flatten_mapping(node)

    _ImportLoader.add_constructor("tag:yaml.org,2002:map", _ImportLoader.construct_yaml_map)


def _check_yaml_graph(data: Any) -> None:
    """Limit expanded aliases too, before any stringification or traversal."""
    active: Set[int] = set()
    count = 0

    def visit(value, depth=0):
        nonlocal count
        count += 1
        if count > MAX_YAML_NODES or depth > 64:
            raise ValueError("YAML: превышен лимит раскрытия aliases или глубины")
        if isinstance(value, (dict, list)):
            if id(value) in active:
                raise ValueError("Циклический YAML alias")
            active.add(id(value))
            try:
                for child in (value.values() if isinstance(value, dict) else value):
                    visit(child, depth + 1)
            finally:
                active.remove(id(value))

    visit(data)


@dataclass
class ImportOptions:
    """Настройки преобразования (панель диалога импорта).

    Все поля имеют безопасные дефолты, повторяющие историческое
    поведение (options=None эквивалентен ImportOptions()).
    """

    privileges: str = "playbook"  # playbook|sudo|su|none
    soften_errors: bool = True  # ignore_errors -> || true
    when_mode: str = "inline"  # inline|comment
    single_task: bool = True  # слить все в одну задачу
    host_facts: bool = True  # setup -> шаг вывода facts
    comments: bool = True  # поясняющие #-шаги
    handlers: bool = True  # секция handlers
    unknown_modules: str = "skip"  # skip|todo
    stage_dir: str = "/tmp"  # стейджинг sftp на хосте
    register_mode: str = "comment"  # comment|fusion|session (D0)
    normalize_dnf4: bool = True  # Ansible backend dnf4 -> штатный CLI dnf

    def normalized(self) -> "ImportOptions":
        """Санитизированная копия (защита от мусора из настроек)."""
        priv = (
            self.privileges
            if self.privileges in ("playbook", "sudo", "su", "none")
            else "playbook"
        )
        when = self.when_mode if self.when_mode in ("inline", "comment") else "inline"
        unknown = (
            self.unknown_modules if self.unknown_modules in ("skip", "todo") else "skip"
        )
        reg = (
            self.register_mode
            if self.register_mode in ("comment", "fusion", "session")
            else "comment"
        )
        return ImportOptions(
            privileges=priv,
            soften_errors=bool(self.soften_errors),
            when_mode=when,
            single_task=bool(self.single_task),
            host_facts=bool(self.host_facts),
            comments=bool(self.comments),
            handlers=bool(self.handlers),
            unknown_modules=unknown,
            stage_dir=str(self.stage_dir or "/tmp"),
            register_mode=reg,
            normalize_dnf4=bool(self.normalize_dnf4),
        )


def _resolve_stage_dir(options: ImportOptions | None) -> str:
    """Каталог стейджинга (валидирован в parse, здесь без warning)."""
    stage = (options.stage_dir if options else "/tmp") or "/tmp"
    stage = stage.strip().rstrip("/") or "/"
    if not stage.startswith("/"):
        return "/tmp"
    return stage


# Шаг маппинга: строка (ssh-команда) или кортеж (тип, текст).
# Кортежи выдают только файловые маппинги (copy/script/unarchive/patch/fetch)
# для sftp-заливок; остальной конвейер тип сохраняет, ssh-трансформы
# (elevation, until, || true, when-гейт) sftp не касаются.
StepSpec = str | Tuple[str, str]

# Ключи задачи, которые НЕ являются именем модуля.
_NON_MODULE_KEYS = frozenset(
    {
        "name",
        "become",
        "become_user",
        "become_method",
        "when",
        "loop",
        "with_items",
        "with_dict",
        "with_list",
        "with_fileglob",
        "with_nested",
        "with_subelements",
        "with_together",
        "with_sequence",
        "with_file",
        "loop_control",
        "register",
        "notify",
        "tags",
        "vars",
        "args",
        "delegate_to",
        "run_once",
        "ignore_errors",
        "changed_when",
        "failed_when",
        "environment",
        "no_log",
        "check_mode",
        "async",
        "poll",
        "retries",
        "delay",
        "until",
        "block",
        "rescue",
        "always",
        "listen",
        "handlers",
        "tasks",
        "pre_tasks",
        "post_tasks",
        "hosts",
        "connection",
        "remote_user",
        "sudo",
        "sudo_user",
        "su",
        "su_user",
        "timeout",
        "any_errors_fatal",
        "max_fail_percentage",
        "strategy",
        "serial",
        "order",
    }
)

# Модули без shell-эквивалента - пропускаются с причиной.
_NO_SHELL_EQUIVALENT = {
    "meta": "управление выполнением playbook, shell-эквивалента нет",
    "set_stats": "статистика Ansible, shell-эквивалента нет",
    "include": "включение файла, вставьте содержимое вручную",
    "include_tasks": "включение файла задач, вставьте содержимое вручную",
    "include_role": "включение роли, вставьте задачи роли вручную",
    "include_vars": "подключение переменных, shell-эквивалента нет",
    "import_tasks": "импорт файла задач, вставьте содержимое вручную",
    "import_playbook": "импорт playbook, вставьте содержимое вручную",
    "import_role": "импорт роли, вставьте задачи роли вручную",
    "service_facts": "сбор фактов о сервисах, shell-эквивалента нет",
    "package_facts": "сбор фактов о пакетах, shell-эквивалента нет",
    "add_host": "добавление хоста в инвентарь контроллера, shell-эквивалента нет",
    "group_by": "группировка хостов инвентаря, shell-эквивалента нет",
    "template": None,  # обрабатывается отдельно (см. _map_template)
}

_JINJA_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}")
_JINJA_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)\b[^}]*\}\}")


def _shell_quote(value: Any) -> str:
    """Экранировать значение для shell."""
    return shlex.quote(str(value))


def _sh_doublequote(text: str) -> str:
    """Обернуть команду в двойные кавычки для `su -c "..."`.

    Экранируются только спецсимволы двойных кавычек (порядок важен:
    сначала бэкслэш). Одинарные кавычки, `%paramN%` и `!` внутри
    двойных кавычек безопасны в неинтерактивном shell. `$`, backtick
    и `"` доезжают до внутреннего shell нетронутыми и раскрываются там.
    """
    escaped = (
        str(text)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    return f'"{escaped}"'


def _jinja_vars(text: str) -> List[str]:
    """Найти имена переменных в ``{{ ... }}`` (без дублей, по порядку)."""
    seen: List[str] = []
    for match in _JINJA_VAR_RE.finditer(text):
        var = match.group(1)
        if var not in seen:
            seen.append(var)
    return seen


def _jinja_to_params(
    text: str, params: List[str], internal: Set[str] | frozenset | None = None
) -> str:
    """Заменить ``{{ var }}`` на ``%paramN%``, пополнить список params.

    Args:
        text: исходная строка команды.
        params: накапливаемый список имен переменных (мутируется).
        internal: internal-имена (D1) - никогда не попадают в params,
            их ``{{ }}`` оставляются как есть для стратегий.

    Returns:
        Строка с подстановками ``%paramN%``.
    """
    inner_set = internal or frozenset()

    def _replace(match: "re.Match[str]") -> str:
        inner = match.group(1).strip()
        base = inner.split("|")[0].strip().split()[0] if inner else ""
        base = base.split(".")[0].split("[")[0]
        if not base or not re.match(r"^[a-zA-Z_]\w*$", base):
            return match.group(0)
        if base in inner_set:
            return match.group(0)
        if base not in params:
            params.append(base)
        return f"%param{params.index(base) + 1}%"

    return _JINJA_RE.sub(_replace, text)


def _step_internal_hit(
    text: str, internal: Set[str] | FrozenSet[str] | None
) -> str | None:
    """Первое internal-имя в ``{{ }}`` текста шага или None."""
    inner = internal or frozenset()
    for match in _JINJA_RE.finditer(text or ""):
        expr = match.group(1).strip()
        base = expr.split("|")[0].strip().split()[0] if expr else ""
        base = base.split(".")[0].split("[")[0]
        if base in inner:
            return base
    return None


def _raw_task_internal_hit(
    task: Dict[str, Any],
    internal: Set[str] | FrozenSet[str],
) -> str | None:
    """Internal-имя в НЕ-Jinja позициях (loop/changed_when/assert...).

    Ловит ссылки без ``{{ }}``: ``loop: "{{ r.stdout_lines }}"`` (скобки
    есть, но шаг их не содержит - там ``{{ item }}``), ``changed_when``
    с ``r.stdout``, ``assert that: r.rc == 0``. Собственное ``register:``
    и ``set_fact``-ключи исключены (иначе продюсер закомментировал бы себя).
    """
    if not internal:
        return None
    own: Set[str] = set()
    reg = task.get("register")
    if isinstance(reg, str) and reg.strip():
        own.add(reg.strip())
    for key in ("set_fact", "ansible.builtin.set_fact"):
        val = task.get(key)
        if isinstance(val, dict):
            own.update(k for k in val if isinstance(k, str))
    from src.services.commands.ansible_register import when_refs as _wr

    texts: List[str] = []
    for key in (
        "loop",
        "with_items",
        "with_list",
        "with_dict",
        "changed_when",
        "failed_when",
        "until",
    ):
        val = task.get(key)
        if isinstance(val, str):
            texts.append(val)
        elif isinstance(val, list):
            texts.extend(str(v) for v in val if isinstance(v, (str, int)))
    # Модульные аргументы assert/fail/debug без скобок (that: r.rc == 0).
    for key, val in task.items():
        if key in (
            "assert",
            "ansible.builtin.assert",
            "fail",
            "ansible.builtin.fail",
            "debug",
            "ansible.builtin.debug",
        ):
            texts.append(str(val))
    for text in texts:
        for ref in _wr(text):
            if ref in internal and ref not in own:
                return ref
    return None


def _comment_reason(
    task: Dict[str, Any], hit: str, internal: Set[str] | FrozenSet[str]
) -> str:
    """Причина комментария шага (нет продюсера / раньше / фильтр / self)."""
    reg = str(task.get("register", "") or "")
    if reg.strip() == hit:
        return "self-ссылка в своей задаче"
    # Продюсер неизвестен здесь (порядок виден только в parse) -
    # детальная причина ("нет продюсера выше" и т.п.) дается
    # предпроходом collect_internal; здесь короткая пометка.
    return "значение существует только в рантайме"


_PKG_MGR_BINARIES = {
    "apt": "apt-get",
    "apt_rpm": "apt-get",
    "dnf": "dnf",
    "dnf4": "dnf4",
    "dnf5": "dnf5",
    "yum": "yum",
    "apk": "apk",
    "pacman": "pacman",
    "zypper": "zypper",
    "urpmi": "urpmi",
    "portage": "emerge",
    "pkg": "pkg",
    "homebrew": "brew",
}

_OS_FAMILY_FILES = {
    "debian": "/etc/debian_version",
    "redhat": "/etc/redhat-release",
    "suse": "/etc/SuSE-release",
    "archlinux": "/etc/arch-release",
    "alpine": "/etc/alpine-release",
    "gentoo": "/etc/gentoo-release",
}

_FACT_ATOM_RE = re.compile(
    r"^(ansible_facts\s*\[\s*['\"]([\w.]+)['\"]\s*\]"
    r"|ansible_(pkg_mgr|os_family|distribution|distribution_major_version|"
    r"distribution_release|service_mgr|architecture|hostname|fqdn|system|"
    r"kernel|user_id|virtualization_type|virtualization_role)"
    r"|ansible_selinux\.status)"
    r"\s*(==|!=)\s*(.+)$"
)


def _bool_parts(text: str, operator: str) -> list[str] | None:
    """Split only at unquoted, top-level boolean operators."""
    quote = ""
    escaped = False
    depth = 0
    start = 0
    parts = []
    i = 0
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
        elif ch == "\\" and quote:
            escaped = True
        elif quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0 and text.startswith(operator, i):
            before = text[i - 1] if i else ""
            after = text[i + len(operator):i + len(operator) + 1]
            if not (before and (before.isalnum() or before in "_.")) and not (
                after and (after.isalnum() or after in "_.")
            ):
                parts.append(text[start:i].strip())
                i += len(operator)
                start = i
                continue
        i += 1
    if quote or depth:
        return None
    parts.append(text[start:].strip())
    return parts if all(parts) else None


def _negate(cond: str, compound: bool = False) -> str:
    """Отрицание shell-условия; составное - через сабшелл."""
    if compound:
        return f"! ( {cond} )"
    return f"! {cond}"


def _fact_atom(atom: str, normalize_dnf4: bool = True) -> str | None:
    """Атом вида ФАКТ ==/!= 'знач' -> shell-условие или None."""
    match = _FACT_ATOM_RE.match(atom.strip())
    if not match:
        return None
    key = match.group(2) or match.group(3)
    if key is None and (match.group(1) or "") == "ansible_selinux.status":
        key = "selinux_status"
    if key is None:
        return None
    # ansible_facts['selinux.status'] - та же точечная форма.
    key = key.replace(".", "_")
    op = match.group(4)
    value = match.group(5).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if not value or not re.match(r"^[\w.\-]+$", value):
        return None
    cond: str | None = None
    compound = False
    if key == "pkg_mgr":
        binary = _PKG_MGR_BINARIES.get(value)
        if value == "dnf4" and normalize_dnf4:
            binary = "dnf"
        if binary is None:
            return None
        cond = f"command -v {binary} >/dev/null 2>&1"
    elif key == "os_family":
        path = _OS_FAMILY_FILES.get(value.lower())
        if path is None:
            return None
        cond = f"[ -f {path} ]"
    elif key == "distribution":
        cond = f"grep -qi '^ID={value.lower()}' /etc/os-release"
    elif key == "distribution_major_version":
        cond = (
            '[ "$(. /etc/os-release 2>/dev/null; '
            'printf %s \\"${VERSION_ID%%.*}\\")" = '
            f"'{value}' ]"
        )
    elif key == "distribution_release":
        cond = (
            '[ "$(. /etc/os-release 2>/dev/null; '
            'printf %s \\"$VERSION_CODENAME\\")" = '
            f"'{value}' ]"
        )
    elif key == "service_mgr":
        if value == "systemd":
            cond = "[ -d /run/systemd/system ]"
        elif value == "sysvinit":
            cond = "[ ! -d /run/systemd/system ]"
        elif value == "openrc":
            cond = "[ -d /run/openrc ]"
        else:
            return None
    elif key == "selinux_status":
        enabled = (
            "command -v getenforce >/dev/null 2>&1 && "
            '[ "$(getenforce 2>/dev/null)" != Disabled ]'
        )
        disabled = (
            "! command -v getenforce >/dev/null 2>&1 || "
            '[ "$(getenforce 2>/dev/null)" = Disabled ]'
        )
        if value == "enabled":
            cond, compound = enabled, True
        elif value == "disabled":
            cond, compound = disabled, True
        else:
            return None
    elif key == "virtualization_type":
        if value == "docker":
            cond = "[ -f /.dockerenv ] || grep -qa docker /proc/1/cgroup 2>/dev/null"
            compound = True
        elif value == "podman":
            cond = (
                "[ -f /run/.containerenv ] || "
                "grep -qa podman /proc/1/cgroup 2>/dev/null"
            )
            compound = True
        elif value == "lxc":
            cond = "grep -qa lxc /proc/1/cgroup 2>/dev/null"
        else:
            return None
    elif key == "virtualization_role":
        flag = "grep -q hypervisor /proc/cpuinfo 2>/dev/null"
        if value == "guest":
            cond = flag
        elif value == "host":
            # Дешево отличим только наличие флага: host/NA без флага.
            if op == "==":
                return _negate(flag)
            return flag
        elif value == "NA":
            cond = f"{_negate(flag)} && [ ! -f /.dockerenv ]"
            compound = True
        else:
            return None
    elif key == "architecture":
        cond = f"[ \"$(uname -m)\" = '{value}' ]"
    elif key == "hostname":
        cond = f"[ \"$(hostname)\" = '{value}' ]"
    elif key == "fqdn":
        cond = f"[ \"$(hostname -f 2>/dev/null)\" = '{value}' ]"
    elif key == "system":
        cond = f"[ \"$(uname -s)\" = '{value}' ]"
    elif key == "kernel":
        cond = f"[ \"$(uname -r)\" = '{value}' ]"
    elif key == "user_id":
        if value == "root":
            cond = '[ "$(id -u)" = 0 ]'
        else:
            cond = f"[ \"$(id -un)\" = '{value}' ]"
    if cond is None:
        return None
    if op == "!=":
        cond = _negate(cond, compound)
    return cond


def _when_operand(
    token: str, preserve_fields: bool = False
) -> tuple[str, str | None] | None:
    """Операнд сравнения -> (shell-текст, имя переменной|None)."""
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        inner = token[1:-1]
        if token[0] == "'" and "'" not in inner:
            return f"'{inner}'", None
        return _sh_doublequote(inner), None
    if re.match(r"^\d+(\.\d+)?$", token):
        return token, None
    lowered = token.lower()
    if lowered in ("true", "yes"):
        return "true", None
    if lowered in ("false", "no"):
        return "false", None
    if lowered in ("null", "none", "~"):
        return '""', None
    # Сабскрипты (ansible_facts['x'], arr[0]) в shell-переменные не ложатся:
    # известное маппится в facts-режиме, остальное - fallback.
    if "[" in token or "]" in token:
        return None
    base = token.split(".")[0]
    if re.fullmatch(r"[a-zA-Z_]\w*(?:\.\w+)*", token):
        if preserve_fields:
            if not re.fullmatch(r"[a-zA-Z_]\w*(?:\.\w+)*", token):
                return None
            return f'"${token}"', token
        return f'"${base}"', base
    return None


def _generic_atom(
    atom: str, preserve_fields: bool = False
) -> tuple[str, list[str]] | None:
    """Атом общего вида (сравнение/is defined/переменная)."""
    atom = atom.strip()
    if atom.lower() in ("true", "false", "yes", "no"):
        return ("true" if atom.lower() in ("true", "yes") else "false"), []
    if not atom or "|" in atom or "$" in atom or "`" in atom:
        return None
    cmp_match = re.match(r"^(.+?)\s*(==|!=)\s*(.+)$", atom)
    if cmp_match:
        left = _when_operand(cmp_match.group(1), preserve_fields)
        right = _when_operand(cmp_match.group(3), preserve_fields)
        if left is None or right is None:
            return None
        op = "=" if cmp_match.group(2) == "==" else "!="
        vars_found = [v for _, v in (left, right) if v]
        return f"[ {left[0]} {op} {right[0]} ]", vars_found
    defined_match = re.match(r"^(.+?)\s+is\s+(not\s+)?defined$", atom)
    if defined_match:
        base = defined_match.group(1).strip()
        if "[" in base or "]" in base:
            return None
        if not preserve_fields:
            base = base.split(".")[0]
        if not re.fullmatch(r"[a-zA-Z_]\w*(?:\.\w+)*", base):
            return None
        if defined_match.group(2):
            return f'[ -z "${base}" ]', [base]
        return f'[ -n "${base}" ]', [base]
    if re.match(r"^[a-zA-Z_]\w*$", atom):
        return f'[ -n "${atom}" ]', [atom]
    if preserve_fields and re.fullmatch(r"[a-zA-Z_]\w*\.stat\.exists", atom):
        return f'[ "${atom}" = true ]', [atom]
    return None


def _when_to_shell(
    expr: Any, preserve_fields: bool = False, normalize_dnf4: bool = True
) -> tuple[str, bool, list[str]]:
    """Перевести when-выражение в shell-условие.

    Returns:
        (cond, mapped, var_names). mapped=False - перевести нельзя,
        оставить комментарий (старое поведение).
    """
    if isinstance(expr, list):
        conditions: List[str] = []
        names: List[str] = []
        for item in expr:
            cond, mapped, variables = _when_to_shell(item, preserve_fields, normalize_dnf4)
            if not mapped:
                return "", False, []
            conditions.append("{ " + cond + "; }")
            names.extend(name for name in variables if name not in names)
        return " && ".join(conditions), bool(conditions), names
    text = str(expr).replace("{{", "").replace("}}", "").strip()
    if not text:
        return "", False, []
    def translate(value: str, depth: int = 0):
        if depth > 64:
            return None
        for operator, shell_op in (("or", " || "), ("and", " && ")):
            parts = _bool_parts(value, operator)
            if parts is None:
                return None
            if len(parts) > 1:
                translated = [translate(part, depth + 1) for part in parts]
                if any(part is None for part in translated):
                    return None
                names = list(dict.fromkeys(n for _, ns in translated for n in ns))
                return shell_op.join("{ " + cond + "; }" for cond, _ in translated), names
        if value.startswith("not "):
            inner = translate(value[4:].strip(), depth + 1)
            return ("! ( " + inner[0] + " )", inner[1]) if inner else None
        if value.startswith("(") and value.endswith(")"):
            return translate(value[1:-1].strip(), depth + 1)
        cond = _fact_atom(value, normalize_dnf4)
        if cond is not None:
            return cond, []
        return _generic_atom(value, preserve_fields)

    mapped = translate(text)
    return (mapped[0], True, mapped[1]) if mapped else ("", False, [])



def _normalize_args(value: Any, extra_args: Any = None, module: str = "") -> Dict[str, Any]:
    from .ansible_arguments import normalize_args

    return normalize_args(value, extra_args, module)


def _detect_module(task: Dict[str, Any]) -> Tuple[str, Any]:
    """Определить имя модуля и его значение в задаче.

    Возвращает ``(имя, значение)`` или ``("", None)`` если модуль
    не найден (например, block без прямых модулей).
    """
    if any(not isinstance(key, str) for key in task):
        raise ValueError("ключи задачи должны быть строками")
    action = task.get("action")
    candidates = [k for k in task if k not in _NON_MODULE_KEYS
                  and k not in ("action", "local_action")]
    if len(candidates) > 1 or (action is not None and candidates):
        raise ValueError("конфликт модулей/неизвестных директив: " + ", ".join(map(str, candidates)))
    if isinstance(action, dict) and "module" in action:
        name = action["module"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("action.module должен быть именем модуля")
        return name, {k: v for k, v in action.items() if k != "module"}
    if isinstance(action, dict) and len(action) == 1:
        name = next(iter(action.keys()))
        return str(name), action[name]
    if isinstance(action, str):
        parts = action.strip().split(None, 1)
        if parts:
            raw: Dict[str, Any] = {}
            if len(parts) > 1:
                raw["_raw"] = parts[1]
            return parts[0], raw or None
    if action is not None:
        raise ValueError("action: ожидается строка или словарь с module")
    for key, value in task.items():
        if key in ("action", "local_action"):
            continue
        if key not in _NON_MODULE_KEYS:
            return str(key), value
    local_action = task.get("local_action")
    if isinstance(local_action, str):
        parts = local_action.strip().split(None, 1)
        if parts:
            return parts[0], {"_raw": parts[1]} if len(parts) > 1 else None
    return "", None


def _loop_info(task: Dict[str, Any]) -> Tuple[str, List[Any], str | None]:
    """Описание цикла задачи.

    Returns:
        (kind, values, warn): kind - flat ({{ item }}), dict
        ({{ item.key }}/{{ item.value }}), indexed ({{ item[0] }}),
        passthrough (развернуть нельзя - warn объясняет).
    """
    for key in ("loop", "with_items", "with_list"):
        value = task.get(key)
        if isinstance(value, list):
            return ("flat", value, None)
    value = task.get("with_dict")
    if value:
        pairs: List[Any] = []
        if isinstance(value, dict):
            pairs = list(value.items())
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    pairs.extend(entry.items())
        if pairs:
            return ("dict", pairs, None)
        return (
            "passthrough",
            [],
            "with_dict: пустой или сложный - подставьте item.key/item.value вручную",
        )
    nested = task.get("with_nested")
    together = task.get("with_together")
    if isinstance(nested, list) and nested:
        import itertools as _it

        lists = [i if isinstance(i, list) else [i] for i in nested]
        size = 1
        for values in lists:
            size *= len(values)
            if size > MAX_LOOP_ITEMS:
                return "passthrough", [], "with_nested: превышен лимит 20 элементов; перенесите цикл вручную"
        return ("indexed", list(_it.product(*lists)), None)
    if isinstance(together, list) and together:
        lists = [i if isinstance(i, list) else [i] for i in together]
        if not lists:
            return ("passthrough", [], "with_together: пустой")
        shortest = min(len(i) for i in lists)
        return ("indexed", [tuple(i[n] for i in lists) for n in range(shortest)], None)
    seq = task.get("with_sequence")
    if seq is not None:
        nums = _sequence_numbers(seq)
        if nums is None:
            return (
                "passthrough",
                [],
                "with_sequence: непонятный формат - подставьте item вручную",
            )
        return ("flat", nums, None)
    if task.get("with_subelements"):
        return (
            "passthrough",
            [],
            "with_subelements: вложенные списки - подставьте item вручную",
        )
    if task.get("with_fileglob"):
        return (
            "passthrough",
            [],
            "with_fileglob: glob резолвится на контроллере - подставьте item вручную",
        )
    if task.get("with_file"):
        return (
            "passthrough",
            [],
            "with_file: содержимое файлов - подставьте item вручную",
        )
    return ("none", [], None)


def _sequence_numbers(spec: Any) -> List[str] | None:
    """Развернуть with_sequence в список строк."""
    if isinstance(spec, str):
        # "start=1 end=5 stride=2 format=%02d"
        params: Dict[str, str] = {}
        for token in spec.split():
            if "=" in token:
                key, val = token.split("=", 1)
                params[key] = val
        start = int(params.get("start", "1"))
        end = params.get("end")
        count = params.get("count")
        stride = int(params.get("stride", "1"))
        fmt = params.get("format", "%d")
        if end is not None:
            stop = int(end) + (1 if stride > 0 else -1)
        elif count is not None:
            stop = start + int(count) * stride
        else:
            return None
        try:
            values = range(start, stop, stride)
            if len(values) > MAX_LOOP_ITEMS:
                return None
            return [fmt % n for n in values]
        except (TypeError, ValueError):
            return None
    return None


def _manual_task_reason(task: Dict[str, Any]) -> str:
    failure = task.get("failed_when")
    if failure is not None and not isinstance(failure, bool) and str(failure).lower() not in ("true", "false", "yes", "no"):
        return "failed_when: сложное условие отказа требует ручного переноса"
    if "until" in task or "retries" in task:
        return "until/retries: условие повторения требует ручного переноса"
    if "loop_control" in task:
        return "loop_control: настройка цикла требует ручного переноса"
    loop_keys = [k for k in task if k == "loop" or k.startswith("with_")]
    if len(loop_keys) > 1:
        return "конфликт директив цикла"
    if not loop_keys:
        return ""
    together = task.get("with_together")
    if isinstance(together, list) and len({len(v) if isinstance(v, list) else 1 for v in together}) > 1:
        return "with_together: списки разной длины требуют ручного переноса"
    kind, values, warning = _loop_info(task)
    if warning:
        return warning
    if kind == "none":
        # Runtime stdout_lines loops are handled by register strategies.
        value = task[loop_keys[0]]
        if isinstance(value, str) and re.fullmatch(
            r"\s*\{\{\s*[a-zA-Z_]\w*\.stdout_lines\s*\}\}\s*", value
        ):
            return ""
        return "неподдерживаемая директива или значение цикла; перенесите вручную"
    if not values:
        return "пустой цикл: задача не выполняется"
    if len(values) > MAX_LOOP_ITEMS:
        return "цикл: превышен лимит 20 элементов; перенесите вручную"
    if kind == "indexed":
        complex_items = any(isinstance(i, (dict, list)) for row in values for i in row)
    elif kind == "dict":
        complex_items = any(isinstance(i, (dict, list)) for row in values for i in row)
    else:
        complex_items = any(isinstance(i, (dict, list)) for i in values)
        if kind == "flat" and all(isinstance(i, dict) and all(
            isinstance(k, str) and not isinstance(v, (dict, list)) for k, v in i.items()
        ) for i in values):
            short = _detect_module(task)[0].rsplit(".", 1)[-1]
            complex_items = short in ("shell", "raw")
    if complex_items:
        return "цикл со сложными элементами требует ручного переноса"
    return ""


def _expand_loop(
    command: str, kind: str, values: List[Any]
) -> List[Tuple[str, str | None]]:
    """Развернуть цикл в команде.

    Returns:
        Список ``(команда, метка_элемента)``. Списки длиннее 20
        или со сложными элементами не разворачиваются.
    """
    if kind in ("none", "passthrough"):
        return [(command, None)]
    if not values:
        return []
    if kind == "indexed":
        complex_items = any(
            isinstance(i, (dict, list)) for combo in values for i in combo
        )
    else:
        complex_items = any(isinstance(i, (dict, list)) for i in values)
    if len(values) > 20 or complex_items:
        return [(command, None)]
    if kind == "flat":
        expanded: List[Tuple[str, str | None]] = []
        for item in values:
            text = command.replace("{{ item }}", str(item))
            text = text.replace("{{item}}", str(item))
            expanded.append((text, str(item)))
        return expanded
    if kind == "dict":
        expanded = []
        for pair in values:
            key, val = pair
            text = command
            for form in ("{{ item.key }}", "{{item.key}}", "{{ item['key'] }}"):
                text = text.replace(form, str(key))
            for form in ("{{ item.value }}", "{{item.value}}", '{{ item["value"] }}'):
                text = text.replace(form, str(val))
            expanded.append((text, str(key)))
        return expanded
    if kind == "indexed":
        expanded = []
        for combo in values:
            text = command
            for idx, element in enumerate(combo):
                text = text.replace(f"{{{{ item[{idx}] }}}}", str(element))
                text = text.replace(f"{{{{item[{idx}]}}}}", str(element))
                text = text.replace(f"{{{{ item.{idx} }}}}", str(element))
            expanded.append((text, ",".join(str(e) for e in combo)))
        return expanded
    return [(command, None)]


def _play_elevation_ctx(play: Dict[str, Any]) -> Dict[str, Any]:
    """Ключи повышения привилегий уровня play (defaults для задач)."""
    ctx: Dict[str, Any] = {}
    for key in ("become", "become_user", "become_method", "sudo", "sudo_user"):
        if key in play:
            ctx[key] = play[key]
    return ctx


def _elevation(
    task: Dict[str, Any], play_ctx: Dict[str, Any] | None = None
) -> Tuple[str, str, str | None]:
    """Определить повышение привилегий.

    Задача перекрывает play. Returns:
        (mode, user, warning): mode "" (нет), "sudo" или "su".
    """
    ctx = play_ctx or {}
    become = task.get(
        "become", task.get("sudo", ctx.get("become", ctx.get("sudo", False)))
    )
    if not become:
        return "", "", None
    user = str(
        task.get(
            "become_user",
            task.get("sudo_user", ctx.get("become_user", ctx.get("sudo_user", ""))),
        )
        or ""
    )
    method = str(task.get("become_method", ctx.get("become_method", "sudo"))).lower()
    if method not in ("sudo", "su"):
        return (
            "sudo",
            user,
            f"become_method {method}: использован sudo - проверьте вручную",
        )
    return method, user, None


def _apply_elevation(steps: List[str], mode: str, user: str) -> List[str]:
    """Добавить повышение привилегий к шагам (кроме комментариев)."""
    if not mode:
        return steps
    out: List[str] = []
    prefix = f"sudo -u {shlex.quote(user)} " if user and user != "root" else "sudo "
    for step in steps:
        comment_only = all(not line.strip() or line.lstrip().startswith("#") for line in step.splitlines())
        if comment_only or (mode == "sudo" and step.startswith(prefix)):
            out.append(step)
        elif mode == "sudo" and not re.search(r"[\n;&|<>()`$]|^\s*(?:\w+=|!|\{|\})", step):
            out.append(prefix + step)
        else:
            # sudo не может исполнить if/for, редирект или цепочку как argv.
            # Весь блок должен попасть в привилегированный shell.
            out.append(_elevate_block(step, mode, user))
    return out


def _elevate_typed(
    typed: List[Tuple[str, str]], mode: str, user: str
) -> List[Tuple[str, str]]:
    """Применить elevation только к ssh-шагам, порядок и sftp сохранить."""
    if not mode:
        return typed
    elevated = _apply_elevation([s for t, s in typed if t == "ssh"], mode, user)
    it = iter(elevated)
    return [(t, next(it) if t == "ssh" else s) for t, s in typed]


def _sftp_upload(local: str, staged: str) -> StepSpec:
    """Шаг заливки файла на хост (текст формата SFTP-редактора)."""
    return ("sftp", f"{_local_display(local)}::%login%@%hostname%:{staged}")


def _app_dir() -> str:
    """Каталог приложения (как в SFTP-редакторе: exe рядом или корень проекта)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _local_display(path: str) -> str:
    """Локальный путь для sftp-шага: относительный если внутри app dir.

    Та же конвенция что в SFTP-редакторе (_browse_file): задачи хранятся
    с относительными путями, абсолютные - только для файлов снаружи.
    """
    try:
        rel = os.path.relpath(path, _app_dir())
    except ValueError:
        return path
    if rel.startswith(".."):
        return path
    return rel


def _stage_name(src: str, stage_dir: str = "/tmp") -> str:
    """Путь стейджинга на хосте (<stage_dir>/<basename>)."""
    base = str(src).replace("\\", "/").split("/")[-1] or "upload.dat"
    return f"{stage_dir}/{base}"


def _is_abs(path: str) -> bool:
    """Абсолютный путь любой платформы.

    Windows не считает `/share/x` абсолютным, а playbook-пути обычно
    posix-стиля (целевые хосты - Linux). Склеивать такой с base_dir
    через join нельзя - получается мусор вида `D:\\share\\...`.
    """
    return os.path.isabs(path) or path.startswith("/")


def _resolve_local(src: str, base_dir: str) -> str | None:
    """Найти локальный файл playbook на машине оператора.

    Относительные пути резолвятся против base_dir (каталог .yml),
    затем против cwd. Jinja в пути проверить нельзя. Returns:
        Существующий путь или None.
    """
    if not src or "{{" in src or "}}" in src:
        return None
    if _is_abs(src):
        return src if os.path.isfile(src) else None
    candidates = []
    if base_dir:
        candidates.append(os.path.join(base_dir, src))
    candidates.append(os.path.abspath(src))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _assume_local(src: str, base_dir: str) -> str:
    """Путь как есть (для sftp-шага когда файла нет на месте).

    Отсутствие файла - не повод пропускать команду: путь подставляется
    как указан (относительный - против base_dir), в warnings уходит
    пометка проверить перед запуском.
    """
    if _is_abs(src):
        return src
    if base_dir:
        return os.path.normpath(os.path.join(base_dir, src))
    return src


# --- Маппинг отдельных модулей: (steps, warnings) ---


def _command_guards(cmd: str, args: Dict[str, Any]) -> str:
    """Пропуск по creates/removes успешен, относительные пути — внутри chdir."""
    checks = []
    if args.get("creates"):
        checks.append(f"test ! -e {_shell_quote(args['creates'])}")
    if args.get("removes"):
        checks.append(f"test -e {_shell_quote(args['removes'])}")
    if checks:
        cmd = "if " + " && ".join(checks) + "; then\n" + cmd + "\nfi"
    if args.get("chdir"):
        cmd = f"cd {_shell_quote(args['chdir'])} && (\n{cmd}\n)"
    return cmd


def _map_shell(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    cmd = args.get("cmd", args.get("_raw", ""))
    if not cmd:
        return [], ["пустая shell-команда"]
    # Block scalar `|` оставляет trailing newline - режем, иначе он
    # окажется внутри su -c "..." и в превью (висячая кавычка).
    cmd = str(cmd).strip()
    if not cmd:
        return [], ["пустая shell-команда"]
    warns: List[str] = []
    cmd = _command_guards(cmd, args)
    if args.get("executable"):
        warns.append(f"executable {args['executable']}: запущено через sh по умолчанию")
    return [cmd], warns


def _map_command(args: Dict[str, Any], module: str) -> Tuple[List[str], List[str]]:
    argv = args.get("argv")
    if argv is not None:
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) for a in argv):
            return [], ["command.argv: ожидается непустой список строк"]
        if "cmd" in args or "_raw" in args:
            return [], ["command: argv и cmd/free-form несовместимы"]
    else:
        cmd = args.get("cmd", args.get("_raw", ""))
        if not isinstance(cmd, str):
            return [], ["command: cmd/free-form должен быть строкой"]
        if "{{" in cmd or "{%" in cmd:
            return [], ["command/free-form с Jinja: границы аргументов неизвестны; используйте argv или перенесите вручную"]
        try:
            argv = shlex.split(cmd, posix=True)
        except ValueError as exc:
            return [], [f"command: неверные кавычки: {exc}"]
        if any(re.match(r"^(?:chdir|creates|removes|stdin|argv|expand_argument_vars)=", a) for a in argv):
            return [], ["command: параметры key=value перенесите в args вручную"]
    if not argv:
        return [], ["пустая команда"]
    expand = args.get("expand_argument_vars", True)
    if not isinstance(expand, bool):
        return [], ["command.expand_argument_vars: ожидается bool"]
    if expand and any("$" in a or "~" in a for a in argv):
        return [], ["command: раскрытие переменных окружения/~ требует ручного переноса; для литералов задайте expand_argument_vars: false"]
    if any(k in args for k in ("stdin", "stdin_add_newline", "executable")):
        return [], ["command: stdin/executable требуют ручного переноса"]
    cmd = " ".join(_shell_quote(a) for a in argv)
    cmd = _command_guards(cmd, args)
    return [cmd], []


def _map_script(
    args: Dict[str, Any], base_dir: str = "", stage_dir: str = "/tmp"
) -> Tuple[List[str], List[str]]:
    raw = str(args.get("_raw", args.get("cmd", ""))).strip()
    if not raw:
        return [], ["script: не указан путь к скрипту"]
    parts = raw.split(None, 1)
    path = parts[0]
    script_args = parts[1] if len(parts) > 1 else ""
    found = _resolve_local(path, base_dir)
    local = found or _assume_local(path, base_dir)
    staged = _stage_name(local, stage_dir)
    cmd = f"bash {_shell_quote(staged)} {script_args}".strip()
    if found:
        return [_sftp_upload(local, staged), cmd], [
            f"script: {path} зальется через SFTP в {staged} - сверьте"
        ]
    return [_sftp_upload(local, staged), cmd], [
        f"script: файл {local} не найден - проверьте перед запуском"
    ]


def _map_copy(
    args: Dict[str, Any], base_dir: str = "", stage_dir: str = "/tmp"
) -> Tuple[List[str], List[str]]:
    dest = args.get("dest")
    if not dest:
        return [], ["copy: не указан dest"]
    dest = str(dest)
    steps: List[str] = []
    warns: List[str] = []
    content = args.get("content")
    src = args.get("src")
    if str(args.get("backup", "")).lower() in ("yes", "true"):
        steps.append(
            f"test -e {_shell_quote(dest)} && cp -a {_shell_quote(dest)} {_shell_quote(dest)}.bak || true"
        )
    if args.get("validate"):
        return [], ["copy validate: требуется ручной перенос; проверка не может быть проигнорирована"]
    if content is not None:
        text = str(content)
        steps.append(f"printf '%s' {_shell_quote(text)} > {_shell_quote(dest)}")
    elif src is not None:
        if str(args.get("remote_src", "no")).lower() in ("yes", "true"):
            steps.append(f"cp -a {_shell_quote(src)} {_shell_quote(dest)}")
        else:
            found = _resolve_local(str(src), base_dir)
            local = found or _assume_local(str(src), base_dir)
            if os.path.isdir(local):
                return [], ["copy: SFTP-исполнитель передаёт только файлы; каталог перенесите вручную"]
            staged = _stage_name(local, stage_dir)
            steps.append(_sftp_upload(local, staged))
            steps.append(f"cp -a {_shell_quote(staged)} {_shell_quote(dest)}")
            if found:
                warns.append(
                    f"copy: {src} зальется через SFTP в {staged} "
                    "и скопируется дальше под become - сверьте"
                )
            else:
                warns.append(f"copy: файл {local} не найден - проверьте перед запуском")
    else:
        return [], ["copy: нужен src или content"]
    mode = args.get("mode")
    if mode is not None:
        from .ansible_arguments import mode_argument
        mode = mode_argument(mode)
        steps.append(f"chmod {mode} {_shell_quote(dest)}")
    owner = args.get("owner")
    group = args.get("group")
    if owner or group:
        steps.append(
            f"chown {owner or ''}:{group or ''} {_shell_quote(dest)}".replace(
                ": ", " "
            ).replace(" :", "")
        )
    return steps, warns


def _map_template(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    dest = args.get("dest", "?")
    src = args.get("src", "?")
    return (
        [],
        [f"template {src} -> {dest}: отрендерите шаблон вручную и загрузите через SFTP"],
    )


def _map_file(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    path = args.get("path", args.get("dest", args.get("name")))
    if not path:
        return [], ["file: не указан path"]
    path = str(path)
    state = str(args.get("state", "file"))
    steps: List[str] = []
    if state == "directory":
        steps.append(f"mkdir -p {_shell_quote(path)}")
    elif state == "absent":
        steps.append(f"rm -rf {_shell_quote(path)}")
    elif state == "touch":
        steps.append(f"touch {_shell_quote(path)}")
    elif state == "link":
        src = args.get("src", "")
        steps.append(f"ln -sfn {_shell_quote(src)} {_shell_quote(path)}")
    elif state == "hard":
        src = args.get("src", "")
        steps.append(f"ln -fn {_shell_quote(src)} {_shell_quote(path)}")
    elif state == "file":
        steps.append(f"test -f {_shell_quote(path)}")
    else:
        return [], [f"file: неизвестное state={state}"]
    recurse = ""
    if str(args.get("recurse", "")).lower() in ("yes", "true"):
        recurse = " -R"
    mode = args.get("mode")
    if mode is not None:
        from .ansible_arguments import mode_argument
        mode = mode_argument(mode)
        steps.append(f"chmod{recurse} {mode} {_shell_quote(path)}")
    owner = args.get("owner")
    group = args.get("group")
    if owner or group:
        who = f"{owner or ''}:{group or ''}".strip(":")
        steps.append(f"chown{recurse} {who} {_shell_quote(path)}")
    return steps, []


def _package_manager_args(args: Dict[str, Any]) -> Tuple[List[str], str]:
    """Общее: имена пакетов и state."""
    names = args.get("name", args.get("pkg"))
    if names is None:
        names = []
    if isinstance(names, str):
        names = [names]
    flat: List[str] = []
    for entry in names:
        text = str(entry)
        if "{{" in text:
            # Jinja-имя целиком (иначе split по пробелам порвет {{ var }}).
            if text.strip():
                flat.append(text.strip())
        else:
            # "a,b" или "a b" -> список
            flat.extend(n.strip() for n in re.split(r"[,\s]+", text) if n.strip())
    names = [str(n) for n in flat]
    state = str(args.get("state", "present"))
    return names, state


def _map_apt(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    names, state = _package_manager_args(args)
    steps: List[str] = []
    warns: List[str] = []
    if args.get("update_cache"):
        steps.append("apt-get update")
    deb = args.get("deb")
    if deb:
        pkg = str(deb)
        if pkg.startswith(("http://", "https://")):
            tmp = f"/tmp/{pkg.split('/')[-1].split('?')[0] or 'package.deb'}"
            steps.append(f"curl -fsSL -o {tmp} {_shell_quote(pkg)}")
            steps.append(f"dpkg -i {_shell_quote(tmp)}")
        else:
            steps.append(f"dpkg -i {_shell_quote(pkg)}")
    if not names:
        if not steps:
            return [], ["apt: не указаны пакеты"]
    else:
        quoted = " ".join(_shell_quote(n) for n in names)
        if state in ("absent", "removed"):
            steps.append(f"apt-get remove -y {quoted}")
            if args.get("purge"):
                steps.append(f"apt-get purge -y {quoted}")
        else:
            steps.append(f"apt-get install -y {quoted}")
    upgrade = str(args.get("upgrade", "")).lower()
    if upgrade in ("yes", "true", "safe"):
        steps.append("apt-get upgrade -y")
    elif upgrade in ("dist", "full"):
        steps.append("apt-get dist-upgrade -y")
    elif args.get("upgrade"):
        warns.append(
            f"apt upgrade={args['upgrade']}: непонятно - выполнен только install"
        )
    if str(args.get("autoremove", "")).lower() in ("yes", "true"):
        steps.append("apt-get autoremove -y")
    if args.get("default_release"):
        warns.append(
            f"apt default_release={args['default_release']}: пиннинг релиза не применен"
        )
    return steps, warns


def _map_apt_key(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    url = args.get("url")
    data = args.get("data")
    keyfile = args.get("file")
    keyserver = args.get("keyserver")
    key_id = args.get("id")
    warns = ["apt-key deprecated в новых Debian/Ubuntu - сверьте keyring"]
    if url:
        return [f"curl -fsSL {_shell_quote(url)} | apt-key add -"], warns
    if data:
        text = str(data)
        if text and not text.endswith("\n"):
            text += "\n"
        return [f"cat <<'DNO_EOF' | apt-key add -\n{text}DNO_EOF"], warns
    if keyfile:
        return [f"apt-key add {_shell_quote(keyfile)}"], warns
    if key_id:
        ks = str(keyserver or "keyserver.ubuntu.com")
        return [
            f"apt-key adv --keyserver {shlex.quote(ks)} --recv-keys {shlex.quote(str(key_id))}"
        ], warns
    return [], ["apt_key: нужны url, data, file или id"]


def _repo_slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (slug[:40] or "repo").strip("-") or "repo"


def _map_apt_repository(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    repo = args.get("repo", "")
    repo = str(repo)
    if not repo:
        return [], ["apt_repository: не указан repo"]
    filename = str(args.get("filename") or f"{_repo_slug(repo)}.list")
    if not filename.endswith(".list"):
        filename += ".list"
    path = f"/etc/apt/sources.list.d/{filename}"
    steps: List[str] = []
    warns: List[str] = []
    if str(args.get("state", "present")) == "absent":
        steps.append(f"rm -f {_shell_quote(path)}")
    elif repo.startswith("ppa:"):
        steps.append(f"add-apt-repository -y {_shell_quote(repo)}")
    else:
        steps.append(f"echo {_shell_quote(repo)} > {_shell_quote(path)}")
    if str(args.get("update_cache", "yes")).lower() not in ("no", "false"):
        steps.append("apt-get update")
    if "{{" in repo:
        warns.append("apt_repository: версия/кодовое имя из Jinja - станет параметром")
    return steps, warns


def _map_yum_repository(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    name = args.get("name")
    if not name:
        return [], ["yum_repository: не указан name"]
    name = str(name)
    path = f"/etc/yum.repos.d/{name}.repo"
    if str(args.get("state", "present")) == "absent":
        return [f"rm -f {_shell_quote(path)}"], []
    lines = [f"[{name}]"]
    if args.get("description"):
        lines.append(f"name={args['description']}")
    for key in ("baseurl", "mirrorlist", "metalink"):
        if args.get(key):
            lines.append(f"{key}={args[key]}")
    if args.get("enabled") is not None:
        enabled = "1" if str(args["enabled"]).lower() in ("yes", "true", "1") else "0"
        lines.append(f"enabled={enabled}")
    if args.get("gpgcheck") is not None:
        check = "1" if str(args["gpgcheck"]).lower() in ("yes", "true", "1") else "0"
        lines.append(f"gpgcheck={check}")
    if args.get("gpgkey"):
        lines.append(f"gpgkey={args['gpgkey']}")
    body = "\n".join(lines) + "\n"
    return [f"cat > {_shell_quote(path)} <<'DNO_EOF'\n{body}DNO_EOF"], []


def _map_rpm_key(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    key = args.get("key")
    if not key:
        return [], ["rpm_key: не указан key"]
    return [f"rpm --import {_shell_quote(key)}"], []


def _map_rpm_key(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    key = args.get("key")
    if not key:
        return [], ["rpm_key: не указан key"]
    return [f"rpm --import {_shell_quote(key)}"], []


def _map_mount(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    path = args.get("path", args.get("name"))
    if not path:
        return [], ["mount: не указан path"]
    path = str(path)
    src = str(args.get("src", ""))
    fstype = str(args.get("fstype", ""))
    opts = str(args.get("opts", ""))
    state = str(args.get("state", "mounted"))
    steps: List[str] = []
    warns: List[str] = []
    if state == "absent":
        steps.append(f"umount {_shell_quote(path)} 2>/dev/null || true")
        steps.append(
            f"sed -i '\\|[[:space:]]{_shell_quote(path)}[[:space:]]|d' /etc/fstab"
        )
        return steps, warns
    if state == "unmounted":
        return [f"umount {_shell_quote(path)}"], warns
    if state == "present":
        if src:
            line = f"{src} {path} {fstype or 'auto'} {opts or 'defaults'} 0 0"
            steps.append(
                f"grep -q -F {shlex.quote(path)} /etc/fstab || echo {shlex.quote(line)} >> /etc/fstab"
            )
        else:
            return [], ["mount present: нужен src для записи fstab"]
        return steps, ["mount present: только запись fstab, без монтирования"]
    # mounted / remounted
    if not src:
        return [f"mount {_shell_quote(path)}"], warns
    parts = ["mount"]
    if fstype:
        parts.append(f"-t {_shell_quote(fstype)}")
    if opts:
        parts.append(f"-o {_shell_quote(opts)}")
    parts.append(_shell_quote(src))
    parts.append(_shell_quote(path))
    steps.append(" ".join(parts))
    if state == "remounted":
        steps.append(f"mount -o remount {_shell_quote(path)}")
    return steps, warns


def _map_filesystem(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    dev = args.get("dev")
    if not dev:
        return [], ["filesystem: не указан dev"]
    fstype = str(args.get("fstype", "ext4"))
    opts = str(args.get("opts", ""))
    cmd = f"mkfs -t {_shell_quote(fstype)}"
    if opts:
        cmd += f" {opts}"
    cmd += f" {_shell_quote(dev)}"
    return [cmd], [f"filesystem: уничтожит данные на {dev} - сверьте"]


def _map_parted(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    device = args.get("device")
    if not device:
        return [], ["parted: не указан device"]
    device = str(device)
    number = args.get("number")
    state = str(args.get("state", "present"))
    warns = [f"parted: меняет таблицу разделов {device} - сверьте"]
    if state == "absent" and number:
        return [f"parted -s {_shell_quote(device)} rm {number}"], warns
    label = str(args.get("label", ""))
    steps = []
    if label in ("gpt", "msdos") and not number:
        return [f"parted -s {_shell_quote(device)} mklabel {label}"], warns
    start = str(args.get("part_start", "0%"))
    end = str(args.get("part_end", "100%"))
    steps.append(f"parted -s {_shell_quote(device)} mkpart primary {start} {end}")
    return steps, warns


def _map_lvg(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    vg = args.get("vg")
    if not vg:
        return [], ["lvg: не указан vg"]
    pvs = args.get("pvs", args.get("pv", []))
    if isinstance(pvs, str):
        pvs = [p.strip() for p in re.split(r"[,\s]+", pvs) if p.strip()]
    pvs = " ".join(_shell_quote(p) for p in pvs)
    if str(args.get("state", "present")) == "absent":
        return [f"vgremove -y {_shell_quote(vg)}"], ["lvg absent: удалит группу томов"]
    if not pvs:
        return [], ["lvg: не указаны pvs"]
    return [f"vgcreate {_shell_quote(vg)} {pvs}"], []


def _map_lvol(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    vg = args.get("vg")
    lv = args.get("lv")
    if not vg or not lv:
        return [], ["lvol: нужны vg и lv"]
    if str(args.get("state", "present")) == "absent":
        return [f"lvremove -y {_shell_quote(vg)}/{_shell_quote(lv)}"], [
            "lvol absent: удалит том"
        ]
    size = str(args.get("size", ""))
    if not size:
        return [], ["lvol: не указан size"]
    return [
        f"lvcreate -L {_shell_quote(size)} -n {_shell_quote(lv)} {_shell_quote(vg)}"
    ], []


def _map_firewalld(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    zone = str(args.get("zone", ""))
    zone_flag = f" --zone={shlex.quote(zone)}" if zone else ""
    permanent = str(args.get("permanent", "yes")).lower() not in ("no", "false")
    perm_flag = " --permanent" if permanent else ""
    state = str(args.get("state", "enabled"))
    action = "add" if state == "enabled" else "remove"
    target = ""
    if args.get("port"):
        target = f"--{action}-port={args['port']}"
    elif args.get("service"):
        target = f"--{action}-service={args['service']}"
    elif args.get("source"):
        target = f"--{action}-source={args['source']}"
    elif args.get("rich_rule"):
        target = f"--{action}-rich-rule={shlex.quote(str(args['rich_rule']))}"
    elif args.get("masquerade") is not None:
        on = str(args["masquerade"]).lower() in ("yes", "true")
        target = "--add-masquerade" if on else "--remove-masquerade"
    else:
        return [], ["firewalld: нужны port, service, source или rich_rule"]
    steps = [f"firewall-cmd{zone_flag}{perm_flag} {target}"]
    warns: List[str] = []
    if str(args.get("immediate", "no")).lower() in ("yes", "true") and permanent:
        steps.append(f"firewall-cmd{zone_flag} {target}")
    elif permanent:
        steps.append("firewall-cmd --reload")
        warns.append("firewalld: применено через --reload")
    if args.get("rich_rule"):
        warns.append("firewalld rich_rule: перенесено как есть - проверьте синтаксис")
    return steps, warns


def _map_ufw(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    state = str(args.get("state", ""))
    if state == "enabled":
        return ["ufw --force enable"], []
    if state == "disabled":
        return ["ufw disable"], []
    if state in ("reset",):
        return ["ufw --force reset"], ["ufw reset: сбросит все правила"]
    rule = str(args.get("rule", "allow"))
    if rule == "limit":
        cmd = "ufw limit"
    elif rule in ("allow", "deny", "reject"):
        cmd = f"ufw {rule}"
    else:
        return [], [f"ufw: неизвестное rule={rule}"]
    if str(args.get("direction", "in")) == "out":
        cmd += " out"
    port = str(args.get("port", args.get("to_port", "")))
    proto = str(args.get("proto", ""))
    if port:
        cmd += f" {port}"
        if proto:
            cmd += f"/{proto}"
    frm = args.get("from_ip", args.get("src", args.get("from", "")))
    if frm:
        cmd += f" from {frm}"
    to = args.get("to_ip", args.get("dest", args.get("to", "")))
    if to:
        cmd += f" to {to}"
    policy = args.get("policy")
    steps = [cmd]
    if policy:
        direction = str(args.get("direction", "incoming"))
        steps.append(f"ufw default {policy} {direction}")
    return steps, []


def _map_selinux(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    state = str(args.get("state", "")).lower()
    policy = str(args.get("policy", "targeted"))
    steps: List[str] = []
    warns: List[str] = []
    if state == "enforcing":
        steps.append("setenforce 1")
    elif state == "permissive":
        steps.append("setenforce 0")
    elif state == "disabled":
        steps.append("setenforce 0")
        warns.append("selinux disabled: вступит после перезагрузки")
    else:
        return [], ["selinux: state должен быть enforcing/permissive/disabled"]
    for key, value in (("SELINUX", state), ("SELINUXTYPE", policy)):
        updates, messages = _map_lineinfile({
            "path": "/etc/selinux/config", "regexp": f"^{key}=", "line": f"{key}={value}",
        })
        steps.extend(updates)
        warns.extend(messages)
    return steps, warns


def _map_seboolean(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    name = args.get("name")
    if not name:
        return [], ["seboolean: не указано имя"]
    state = str(args.get("state", "yes")).lower()
    value = "on" if state in ("yes", "true", "on") else "off"
    flag = " -P" if str(args.get("persistent", "no")).lower() in ("yes", "true") else ""
    return [f"setsebool{flag} {_shell_quote(name)} {value}"], []


def _map_modprobe(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    name = args.get("name")
    if not name:
        return [], ["modprobe: не указано имя"]
    params = str(args.get("params", ""))
    if str(args.get("state", "present")) == "absent":
        return [f"modprobe -r {_shell_quote(name)}"], []
    cmd = f"modprobe {_shell_quote(name)}"
    if params:
        cmd += f" {params}"
    return [cmd], [
        "modprobe: автозагрузка при старте не настроена - добавьте в modules-load.d"
    ]


def _map_timezone(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    name = args.get("name")
    if not name:
        return [], ["timezone: не указано имя"]
    return [f"timedatectl set-timezone {_shell_quote(name)}"], []


def _map_locale_gen(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    name = args.get("name")
    if not name:
        return [], ["locale_gen: не указано имя"]
    return [f"locale-gen {_shell_quote(name)}"], [
        "locale_gen: LANG по умолчанию не выставлен - используйте update-locale"
    ]


def _map_alternatives(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    name = args.get("name")
    path = args.get("path")
    if not name or not path:
        return [], ["alternatives: нужны name и path"]
    link = str(args.get("link", ""))
    priority = str(args.get("priority", "50"))
    if str(args.get("state", "selected")) == "selected" and not link:
        return [
            f"update-alternatives --set {_shell_quote(name)} {_shell_quote(path)}"
        ], []
    if not link:
        return [], ["alternatives: нужен link для установки варианта"]
    return [
        f"update-alternatives --install {_shell_quote(link)} {_shell_quote(name)} {_shell_quote(path)} {priority}"
    ], []


def _map_nmcli(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    conn = args.get("conn_name", args.get("connname"))
    if not conn:
        return [], ["nmcli: не указан conn_name"]
    conn = str(conn)
    if str(args.get("state", "present")) == "absent":
        return [f"nmcli con del {_shell_quote(conn)}"], []
    ctype = str(args.get("type", "ethernet"))
    parts = [f"nmcli con add type {shlex.quote(ctype)} con-name {_shell_quote(conn)}"]
    if args.get("ifname"):
        parts.append(f"ifname {_shell_quote(args['ifname'])}")
    if args.get("ip4"):
        parts.append(f"ip4 {_shell_quote(args['ip4'])}")
    if args.get("gw4"):
        parts.append(f"gw4 {_shell_quote(args['gw4'])}")
    if args.get("ip6"):
        parts.append(f"ip6 {_shell_quote(args['ip6'])}")
    if args.get("gw6"):
        parts.append(f"gw6 {_shell_quote(args['gw6'])}")
    if args.get("dns4"):
        dns = args["dns4"]
        dns = " ".join(dns) if isinstance(dns, list) else str(dns)
        parts.append(f"ipv4.dns {shlex.quote(dns)}")
    if str(args.get("autoconnect", "")).lower() in ("yes", "true"):
        parts.append("autoconnect yes")
    elif str(args.get("autoconnect", "")).lower() in ("no", "false"):
        parts.append("autoconnect no")
    steps = [" ".join(parts), f"nmcli con up {_shell_quote(conn)}"]
    warns = ["nmcli: приближенное создание соединения - сверьте адреса и маршруты"]
    for key in (
        "ssid",
        "psk",
        "wifi_sec",
        "method4",
        "method6",
        "routes4",
        "slave_type",
    ):
        if args.get(key) is not None:
            warns.append(f"nmcli {key}: не перенесено - настройте вручную")
            break
    return steps, warns


def _map_iptables(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    chain = str(args.get("chain", "INPUT"))
    jump = str(args.get("jump", ""))
    if not jump:
        return [], ["iptables: не указан jump"]
    table = str(args.get("table", "filter"))
    flag = "-A" if str(args.get("state", "present")) == "present" else "-D"
    parts = ["iptables"]
    if table != "filter":
        parts.append(f"-t {shlex.quote(table)}")
    parts.append(f"{flag} {shlex.quote(chain)}")
    proto = str(args.get("protocol", ""))
    if proto:
        parts.append(f"-p {shlex.quote(proto)}")
    src = str(args.get("source", ""))
    if src:
        parts.append(f"-s {shlex.quote(src)}")
    dst = str(args.get("destination", ""))
    if dst:
        parts.append(f"-d {shlex.quote(dst)}")
    in_if = str(args.get("in_interface", ""))
    if in_if:
        parts.append(f"-i {shlex.quote(in_if)}")
    out_if = str(args.get("out_interface", ""))
    if out_if:
        parts.append(f"-o {shlex.quote(out_if)}")
    dport = str(args.get("destination_port", args.get("destination_ports", "")))
    if dport:
        dport = dport.replace(",", ",")
        if "," in dport:
            parts.append(f"-m multiport --dports {shlex.quote(dport)}")
        else:
            parts.append(f"--dport {shlex.quote(dport)}")
    sport = str(args.get("source_port", ""))
    if sport:
        parts.append(f"--sport {shlex.quote(sport)}")
    ctstate = args.get("ctstate", args.get("match", ""))
    if isinstance(ctstate, list):
        ctstate = ",".join(str(c) for c in ctstate)
    if ctstate and "conntrack" in str(args.get("match", "conntrack")):
        parts.append(f"-m conntrack --ctstate {shlex.quote(str(ctstate))}")
    parts.append(f"-j {shlex.quote(jump)}")
    return [" ".join(parts)], [
        "iptables: правило не сохраняется - выполните iptables-save"
    ]


def _map_at(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    cmd = args.get("command", args.get("cmd"))
    if not cmd:
        return [], ["at: не указана команда"]
    if str(args.get("state", "present")) == "absent":
        return [], ["at absent: нужен id задания (atrm) - удалите вручную"]
    count = args.get("count")
    units = str(args.get("units", "minutes"))
    if count:
        when = f"now + {count} {units}"
    elif args.get("specific_time"):
        when = str(args["specific_time"])
    else:
        when = "now + 1 minute"
    return [f"echo {shlex.quote(str(cmd))} | at {when}"], []


def _map_known_hosts(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    host = args.get("name", args.get("host"))
    key = args.get("key")
    path = str(args.get("path", "~/.ssh/known_hosts"))
    if key and host:
        return [
            f"grep -q -F {shlex.quote(str(host))} {path} 2>/dev/null || echo {shlex.quote(str(key))} >> {path}"
        ], []
    if host:
        return [f"ssh-keyscan {_shell_quote(host)} >> {path} 2>/dev/null"], [
            "known_hosts: ключ получен с хоста без проверки - сверьте отпечаток"
        ]
    return [], ["known_hosts: нужны name и key (или только name для keyscan)"]


def _map_getent(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    db = args.get("database")
    key = args.get("key")
    if not db or not key:
        return [], ["getent: нужны database и key"]
    return [f"getent {_shell_quote(db)} {_shell_quote(key)}"], []


def _map_archive(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    paths = args.get("path", [])
    if isinstance(paths, str):
        paths = [paths]
    dest = args.get("dest")
    if not paths or not dest:
        return [], ["archive: нужны path и dest"]
    dest = str(dest)
    quoted = " ".join(_shell_quote(p) for p in paths)
    fmt = str(args.get("format", "")).lower()
    if not fmt:
        for ext, name in (
            (".tar.gz", "gz"),
            (".tgz", "gz"),
            (".tar.bz2", "bz2"),
            (".tar.xz", "xz"),
            (".zip", "zip"),
            (".tar", "tar"),
        ):
            if dest.endswith(ext):
                fmt = name
                break
        fmt = fmt or "gz"
    if fmt == "zip":
        cmd = f"zip -r {_shell_quote(dest)} {quoted}"
    elif fmt == "bz2":
        cmd = f"tar -cjf {_shell_quote(dest)} {quoted}"
    elif fmt == "xz":
        cmd = f"tar -cJf {_shell_quote(dest)} {quoted}"
    elif fmt == "tar":
        cmd = f"tar -cf {_shell_quote(dest)} {quoted}"
    else:
        cmd = f"tar -czf {_shell_quote(dest)} {quoted}"
    steps = [cmd]
    warns: List[str] = []
    if str(args.get("remove", "")).lower() in ("yes", "true"):
        steps.append(f"rm -rf {quoted}")
        warns.append("archive remove: исходники удалены после упаковки")
    return steps, warns


def _map_fetch(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    src = args.get("src")
    if not src:
        return [], ["fetch: не указан src"]
    dest = str(args.get("dest", "/tmp"))
    base = str(src).replace("\\", "/").split("/")[-1] or "fetched.dat"
    local = f"{dest}/%hostname%_{base}"
    return [("sftp", f"%login%@%hostname%:{src}::{local}")], [
        f"fetch: скачается на машину оператора в {local} - каталог должен существовать"
    ]


def _map_find(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    paths = args.get("paths", args.get("path", []))
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        return [], ["find: не указаны paths"]
    parts = ["find", " ".join(_shell_quote(p) for p in paths)]
    if str(args.get("recurse", "yes")).lower() in ("no", "false"):
        parts.append("-maxdepth 1")
    ftype = str(args.get("file_type", ""))
    if ftype == "file":
        parts.append("-type f")
    elif ftype == "directory":
        parts.append("-type d")
    elif ftype == "link":
        parts.append("-type l")
    patterns = args.get("patterns", [])
    if isinstance(patterns, str):
        patterns = [patterns]
    for pat in patterns:
        parts.append(f"-name {shlex.quote(str(pat))}")
    age = str(args.get("age", ""))
    if age:
        age_match = re.match(r"^(-?\d+)([smhdw])?$", age)
        if age_match:
            num, unit = age_match.group(1), age_match.group(2) or "s"
            flag = {
                "s": "-newermt",
                "m": "-mmin",
                "h": "-mmin",
                "d": "-mtime",
                "w": "-mtime",
            }[unit]
            if unit == "h":
                num = str(int(num) * 60)
            elif unit == "w":
                num = str(int(num) * 7)
            if unit == "s":
                parts.append(f"{flag} '{abs(int(num))} seconds ago'")
            else:
                parts.append(f"{flag} {num}")
        else:
            return [], [f"find age={age}: непонятный формат - проверьте"]
    contains = args.get("contains")
    if contains:
        parts.append(f"-exec grep -l {shlex.quote(str(contains))} {{}} +")
    if str(args.get("get_checksum", "")).lower() in ("yes", "true"):
        parts.append("-exec sha256sum {} +")
    return [" ".join(parts)], []


def _map_replace(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    path = args.get("path", args.get("dest"))
    regexp = args.get("regexp")
    if not path or regexp is None:
        return [], ["replace: нужны path и regexp"]
    path = str(path)
    steps: List[str] = []
    if str(args.get("backup", "")).lower() in ("yes", "true"):
        steps.append(f"cp -a {_shell_quote(path)} {_shell_quote(path)}.bak")
    replace = str(args.get("replace", ""))
    steps.append(f"sed -i -E 's|{regexp}|{replace}|g' {_shell_quote(path)}")
    return steps, ["replace: sed без проверки совпадений - сверьте файл"]


def _map_tempfile(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    state = str(args.get("state", "file"))
    tmpdir = str(args.get("path", "/tmp"))
    prefix = str(args.get("prefix", "ansible."))
    suffix = str(args.get("suffix", ""))
    tmpl = f"{tmpdir}/{prefix}XXXXXX{suffix}"
    if state == "directory":
        cmd = f"mktemp -d {_shell_quote(tmpl)}"
    else:
        cmd = f"mktemp {_shell_quote(tmpl)}"
    return [cmd], ["tempfile: путь виден только в выводе задачи"]


def _map_acl(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    path = args.get("path", args.get("name"))
    entity = args.get("entity")
    if not path or not entity:
        return [], ["acl: нужны path и entity"]
    etype = str(args.get("etype", "user"))[0]
    perms = str(args.get("permissions", "r"))
    if str(args.get("state", "present")) == "absent":
        cmd = f"setfacl -x {etype}:{shlex.quote(str(entity))} {_shell_quote(path)}"
    else:
        cmd = f"setfacl -m {etype}:{shlex.quote(str(entity))}:{shlex.quote(perms)} {_shell_quote(path)}"
    if str(args.get("recursive", "")).lower() in ("yes", "true"):
        cmd = cmd.replace("setfacl ", "setfacl -R ", 1)
    if str(args.get("default", "")).lower() in ("yes", "true"):
        cmd = cmd.replace("setfacl ", "setfacl -d ", 1)
    return [cmd], []


def _map_capabilities(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    path = args.get("path")
    cap = args.get("capability")
    if not path:
        return [], ["capabilities: не указан path"]
    if str(args.get("state", "present")) == "absent" or not cap:
        return [f"setcap -r {_shell_quote(path)}"], []
    return [f"setcap {shlex.quote(str(cap))} {_shell_quote(path)}"], []


def _map_patch(
    args: Dict[str, Any], base_dir: str = "", stage_dir: str = "/tmp"
) -> Tuple[List[str], List[str]]:
    src = args.get("src")
    if not src:
        return [], ["patch: не указан src"]
    dest = str(args.get("dest", args.get("basedir", ".")))
    strip = str(args.get("strip", "0"))
    steps: List[str] = []
    warns: List[str] = []
    if str(args.get("remote_src", "no")).lower() not in ("yes", "true"):
        found = _resolve_local(str(src), base_dir)
        local = found or _assume_local(str(src), base_dir)
        archive = _stage_name(local, stage_dir)
        steps.append(_sftp_upload(local, archive))
        if found:
            warns.append(f"patch: {src} зальется через SFTP в {archive} - сверьте")
        else:
            warns.append(f"patch: файл {local} не найден - проверьте перед запуском")
    else:
        archive = str(src)
    steps.append(f"patch -p{strip} -d {_shell_quote(dest)} < {_shell_quote(archive)}")
    return steps, warns


def _map_ini_file(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    path = args.get("path", args.get("dest"))
    section = args.get("section")
    option = args.get("option")
    if not path or not section or not option:
        return [], ["ini_file: нужны path, section и option"]
    if str(args.get("state", "present")) == "absent":
        cmd = f"crudini --del {_shell_quote(path)} {shlex.quote(str(section))} {shlex.quote(str(option))}"
    else:
        value = str(args.get("value", ""))
        cmd = f"crudini --set {_shell_quote(path)} {shlex.quote(str(section))} {shlex.quote(str(option))} {shlex.quote(value)}"
    return [cmd], ["ini_file: нужен crudini на хосте"]


def _map_npm(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    name = args.get("name", args.get("pkg"))
    version = args.get("version")
    state = str(args.get("state", "present"))
    exe = str(args.get("executable", "npm"))
    glob = " -g" if str(args.get("global", "")).lower() in ("yes", "true") else ""
    spec = ""
    if name:
        spec = f" {shlex.quote(str(name))}"
        if version:
            spec += f"@{shlex.quote(str(version))}"
    if state in ("absent", "removed"):
        cmd = f"{exe} uninstall{glob}{spec}"
    elif state == "latest":
        cmd = f"{exe} update{glob}{spec}"
    else:
        cmd = f"{exe} install{glob}{spec}"
    if args.get("registry"):
        cmd += f" --registry {shlex.quote(str(args['registry']))}"
    chdir = args.get("path")
    if chdir:
        cmd = f"cd {_shell_quote(chdir)} && ({cmd})"
    return [cmd], []


def _map_gem(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    name = args.get("name")
    if not name:
        return [], ["gem: не указано имя"]
    exe = str(args.get("executable", "gem"))
    version = args.get("version")
    state = str(args.get("state", "present"))
    if state in ("absent", "removed"):
        return [f"{exe} uninstall -x {_shell_quote(name)}"], []
    cmd = f"{exe} install {_shell_quote(name)}"
    if version:
        cmd += f" -v {shlex.quote(str(version))}"
    if str(args.get("user_install", "")).lower() in ("yes", "true"):
        cmd += " --user-install"
    return [cmd], []


def _map_composer(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    command = str(args.get("command", "install"))
    arguments = str(args.get("arguments", ""))
    exe = str(args.get("executable", "composer"))
    cmd = f"{exe} {command}"
    if arguments:
        cmd += f" {arguments}"
    if str(args.get("global", "")).lower() in ("yes", "true"):
        cmd = cmd.replace(f"{exe} ", f"{exe} global ", 1)
    workdir = args.get("working_dir")
    if workdir:
        cmd = f"cd {_shell_quote(workdir)} && ({cmd})"
    return [cmd], []


def _map_cpanm(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    name = args.get("name")
    if not name:
        return [], ["cpanm: не указано имя"]
    return [f"cpanm {_shell_quote(name)}"], []


def _map_dnf_yum(
    args: Dict[str, Any], manager: str, options: ImportOptions | None = None
) -> Tuple[List[str], List[str]]:
    names, state = _package_manager_args(args)
    steps: List[str] = []
    warns: List[str] = []
    backend = str(args.get("use_backend", manager) or manager).lower()
    if backend == "auto":
        backend = manager
    if backend == "dnf4" and (options is None or options.normalize_dnf4):
        backend = "dnf"
        warns.append("Ansible backend dnf4 преобразован в штатную команду dnf")
    if backend not in ("dnf", "dnf4", "dnf5", "yum"):
        warns.append(f"use_backend {backend}: неизвестный - использован {manager}")
        backend = manager
    elif backend != manager and not (
        manager == "dnf4" and backend == "dnf" and (options is None or options.normalize_dnf4)
    ):
        warns.append(
            f"use_backend {backend}: пакеты ставятся через {backend} (игнорируя pkg_mgr хоста) - сверьте"
        )
    if args.get("update_cache"):
        steps.append(f"{backend} makecache")
    if not names:
        if steps:
            return steps, warns
        return [], [f"{manager}: не указаны пакеты"]
    quoted = " ".join(_shell_quote(n) for n in names)
    if state in ("absent", "removed"):
        steps.append(f"{backend} remove -y {quoted}")
    elif state == "latest":
        steps.append(f"{backend} upgrade -y {quoted}")
    else:
        steps.append(f"{backend} install -y {quoted}")
    return steps, warns


def _map_package(
    args: Dict[str, Any], options: ImportOptions | None = None
) -> Tuple[List[str], List[str]]:
    manager = str(args.get("use", "auto") or "auto")
    if manager in ("dnf", "dnf4", "dnf5", "yum"):
        return _map_dnf_yum(args, manager, options)
    if manager in ("apt", "apt-get"):
        return _map_apt(args)
    if manager != "auto":
        return [], [f"package use={manager}: менеджер не поддерживается"]
    names, state = _package_manager_args(args)
    if not names:
        return [], ["package: не указаны пакеты"]
    quoted = " ".join(_shell_quote(n) for n in names)
    if state in ("absent", "removed"):
        action = "remove -y"
    elif state == "latest":
        action = "upgrade -y"
    else:
        action = "install -y"
    apt_action = "install -y" if state == "latest" else action
    cmd = (
        f"if command -v dnf >/dev/null; then dnf {action} {quoted}; "
        f"elif command -v apt-get >/dev/null; then apt-get {apt_action} {quoted}; "
        f"else yum {action} {quoted}; fi"
    )
    return [cmd], ["package: менеджер определен на хосте (dnf/apt-get/yum)"]


def _map_pip(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    names, state = _package_manager_args(args)
    if not names and not args.get("requirements"):
        return [], ["pip: не указаны пакеты"]
    version = args.get("version")
    executable = args.get("executable", "pip3")
    venv = args.get("virtualenv", args.get("virtual_env"))
    warns: List[str] = []
    steps: List[str] = []
    if venv:
        venv = str(venv)
        steps.append(
            f"test -x {_shell_quote(venv)}/bin/pip || python3 -m venv {_shell_quote(venv)}"
        )
        executable = f"{venv}/bin/pip"
    specs = []
    for name in names:
        if version and len(names) == 1:
            specs.append(f"{name}=={version}")
        else:
            specs.append(name)
    if version and len(names) != 1:
        warns.append("version: применена только для одиночного пакета")
    quoted = " ".join(_shell_quote(s) for s in specs)
    extra = str(args.get("extra_args", ""))
    if state in ("absent", "removed"):
        cmd = f"{executable} uninstall -y {quoted}".strip()
    elif state == "latest":
        cmd = f"{executable} install -U {quoted} {extra}".strip()
    else:
        cmd = f"{executable} install {quoted} {extra}".strip()
    if args.get("requirements"):
        cmd += f" -r {_shell_quote(args['requirements'])}"
    steps.append(" ".join(cmd.split()))
    chdir = args.get("chdir")
    if chdir:
        steps[-1] = f"cd {_shell_quote(chdir)} && ({steps[-1]})"
    return steps, warns


def _map_service(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    name = args.get("name", args.get("service", args.get("unit")))
    if not name:
        return [], ["service: не указано имя"]
    name = str(name)
    scope = "--user " if str(args.get("scope", "")) == "user" else ""
    state = str(args.get("state", ""))
    steps: List[str] = []
    warns: List[str] = []
    if str(args.get("daemon_reload", "")).lower() in ("yes", "true"):
        steps.append("systemctl daemon-reload")
    masked = args.get("masked")
    if masked is True or str(masked).lower() == "yes":
        steps.append(f"systemctl {scope}mask {_shell_quote(name)}")
        return steps, warns
    elif masked is False or str(masked).lower() == "no":
        steps.append(f"systemctl {scope}unmask {_shell_quote(name)}")
    state_map = {
        "started": "start",
        "stopped": "stop",
        "restarted": "restart",
        "reloaded": "reload",
    }
    if state in state_map:
        steps.append(
            f"systemctl {scope}{state_map[state]} {_shell_quote(name)}".replace(
                "  ", " "
            )
        )
    elif state:
        return [], [f"service: неизвестное state={state}"]
    enabled = args.get("enabled")
    if enabled is True or str(enabled).lower() == "yes":
        steps.append(f"systemctl {scope}enable {_shell_quote(name)}")
    elif enabled is False or str(enabled).lower() == "no":
        steps.append(f"systemctl {scope}disable {_shell_quote(name)}")
    if not steps:
        steps.append(f"systemctl {scope}status {_shell_quote(name)}")
    return steps, warns


def _map_user(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    name = args.get("name")
    if not name:
        return [], ["user: не указано имя"]
    name = str(name)
    state = str(args.get("state", "present"))
    if state == "absent":
        cmd = (
            f"userdel -r {_shell_quote(name)}"
            if args.get("remove")
            else f"userdel {_shell_quote(name)}"
        )
        return [cmd], []
    parts = ["useradd -m"]
    if args.get("shell"):
        parts.append(f"-s {_shell_quote(args['shell'])}")
    groups = args.get("groups", args.get("group"))
    if isinstance(groups, list) and groups:
        parts.append(f"-G {','.join(_shell_quote(g) for g in groups)}")
    elif isinstance(groups, str) and groups:
        parts.append(f"-G {_shell_quote(groups)}")
    if args.get("uid"):
        parts.append(f"-u {args['uid']}")
    if args.get("home"):
        parts.append(f"-d {_shell_quote(args['home'])}")
    if args.get("password"):
        return [], ["user: пароль Ansible нельзя перенести в shell - задайте вручную"]
    parts.append(_shell_quote(name))
    cmd = " ".join(parts)
    steps = [f"id {_shell_quote(name)} >/dev/null 2>&1 || {cmd}"]
    return steps, [
        "user: идемпотентно через id (существующий пользователь не меняется)"
    ]


def _map_group(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    name = args.get("name")
    if not name:
        return [], ["group: не указано имя"]
    if str(args.get("state", "present")) == "absent":
        return [f"groupdel {_shell_quote(name)}"], []
    cmd = f"getent group {_shell_quote(name)} >/dev/null || groupadd"
    if args.get("gid"):
        cmd += f" -g {args['gid']}"
    cmd += f" {_shell_quote(name)}"
    return [cmd], []


def _map_cron(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    job = args.get("job")
    cname = args.get("name", "")
    if str(args.get("state", "present")) == "absent" or not job:
        if not cname and not job:
            return [], ["cron: нужны name или job"]
        marker = cname or str(job)[:30]
        return [
            f"crontab -l 2>/dev/null | grep -v -F {shlex.quote(marker)} | crontab -"
        ], ["cron absent: удаляет строки по маркеру - проверьте"]
    minute = args.get("minute", "*")
    hour = args.get("hour", "*")
    dom = args.get("day", args.get("day_of_month", "*"))
    month = args.get("month", "*")
    dow = args.get("weekday", args.get("day_of_week", "*"))
    line = f"{minute} {hour} {dom} {month} {dow} {job} # {cname}".strip()
    cmd = (
        f"(crontab -l 2>/dev/null | grep -v -F {shlex.quote('# ' + str(cname))}; echo {shlex.quote(line)}) | crontab -"
        if cname
        else f"(crontab -l 2>/dev/null; echo {shlex.quote(line)}) | crontab -"
    )
    return [cmd], ["cron: применяется для текущего пользователя crontab"]


def _map_lineinfile(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    from .ansible_file_ops import lineinfile_command

    return lineinfile_command(args)


def _map_blockinfile(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    path = args.get("path")
    block = args.get("block")
    if not path or block is None:
        return [], ["blockinfile: нужны path и block"]
    marker = args.get("marker", "# {mark} ANSIBLE MANAGED BLOCK")
    begin = str(marker).replace("{mark}", "BEGIN")
    end = str(marker).replace("{mark}", "END")
    steps = [
        f"sed -i '/{re.escape(begin)}/,/{re.escape(end)}/d' {_shell_quote(path)}",
        f"printf '%s\\n' {shlex.quote(begin)} {shlex.quote(str(block))} {shlex.quote(end)} >> {_shell_quote(path)}",
    ]
    return steps, ["blockinfile: блок дописывается в конец файла"]


def _map_wait_loop(
    check_cmd: str, negate: bool, timeout: int, delay: int, sleep_secs: int
) -> str:
    """Цикл ожидания условия с таймаутом (для wait_for/wait_for_connection)."""
    tries = max(1, int(timeout) // max(1, int(sleep_secs)))
    if delay:
        prefix = f"sleep {int(delay)}; "
    else:
        prefix = ""
    check = f"({check_cmd}) >/dev/null 2>&1"
    if negate:
        check = "! " + check
    return (
        f"({prefix}for i in $(seq 1 {tries}); do "
        f"if {check}; then exit 0; fi; "
        f"[ $i -eq {tries} ] || sleep {max(0, sleep_secs)}; done; exit 1)"
    )


def _map_wait_for(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    timeout = int(args.get("timeout", 300))
    delay = int(args.get("delay", args.get("delay_before", 0)) or 0)
    sleep_secs = int(args.get("sleep", 1))
    state = str(args.get("state", "started"))
    port = args.get("port")
    path = args.get("path")
    host = str(args.get("host", "127.0.0.1"))
    warns = ["wait_for: цикл ожидания с таймаутом - проверьте параметры"]
    if port and state in ("started", "stopped", "present", "absent"):
        probe = f"timeout 1 bash -c 'echo > /dev/tcp/{host}/{port}'"
        negate = state in ("stopped", "absent")
        return [_map_wait_loop(probe, negate, timeout, delay, sleep_secs)], warns
    if path and state in ("present", "absent"):
        probe = (
            f"test ! -e {_shell_quote(path)}"
            if state == "absent"
            else f"test -e {_shell_quote(path)}"
        )
        return [_map_wait_loop(probe, False, timeout, delay, sleep_secs)], warns
    if state == "drained":
        return [], ["wait_for drained: нет shell-эквивалента"]
    return [], ["wait_for: нужны port или path"]


def _map_wait_for_connection(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    timeout = int(args.get("timeout", 600))
    delay = int(args.get("delay", 0) or 0)
    sleep_secs = int(args.get("sleep", 5))
    steps = [
        _map_wait_loop("uptime", False, timeout, delay, sleep_secs),
    ]
    warns = [
        (
            "wait_for_connection: в Ansible ждет доступности хоста со стороны "
            "контроллера (обычно после reboot) - здесь цикл-проверка на хосте, "
            "при падении связи задача все равно может упасть по таймауту SSH"
        )
    ]
    return steps, warns


def _map_uri(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    url = args.get("url")
    if not url:
        return [], ["uri: не указан url"]
    method = str(args.get("method", "GET")).upper()
    parts = ["curl -fsS", f"-X {method}"]
    headers = args.get("headers")
    if isinstance(headers, dict):
        for key, value in headers.items():
            parts.append(f"-H {shlex.quote(f'{key}: {value}')}")
    user = args.get("user", args.get("username"))
    password = args.get("password")
    if user:
        parts.append(f"-u {shlex.quote(str(user) + ':' + str(password or ''))}")
    if str(args.get("validate_certs", "yes")).lower() in ("no", "false"):
        parts.append("-k")
    if args.get("timeout"):
        parts.append(f"--max-time {int(args['timeout'])}")
    body = args.get("body")
    if body is not None:
        if isinstance(body, dict):
            import json as _json

            parts.append(f"-d {shlex.quote(_json.dumps(body))}")
            parts.append("-H 'Content-Type: application/json'")
        else:
            parts.append(f"-d {shlex.quote(str(body))}")
    dest = args.get("dest")
    if dest:
        parts.append(f"-o {_shell_quote(dest)}")
    elif str(args.get("return_content", "no")).lower() not in ("yes", "true"):
        parts.append("-o /dev/null")
    parts.append(shlex.quote(str(url)))
    cmd = " ".join(parts)
    warns: List[str] = []
    status_code = args.get("status_code")
    if status_code is not None:
        codes = (
            [str(c) for c in status_code]
            if isinstance(status_code, list)
            else [str(status_code)]
        )
        if codes != ["200"]:
            warns.append(
                f"uri status_code {','.join(codes)}: curl -f ловит только "
                "4xx/5xx - остальные коды проверьте вручную"
            )
    return [cmd], warns


def _map_set_fact(args: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    pairs: Dict[str, Any] = {}
    raw = args.get("_raw")
    if isinstance(raw, str) and raw.strip():
        try:
            for token in shlex.split(raw):
                if "=" in token:
                    key, value = token.split("=", 1)
                    pairs[key] = value
        except ValueError:
            pass
    for key, value in args.items():
        if key in ("_raw", "cacheable"):
            continue
        pairs[key] = value
    if args.get("cacheable") is not None:
        warns_cache = ["set_fact cacheable: игнорируется"]
    else:
        warns_cache = []
    if not pairs:
        return [], ["set_fact: нет переменных"]
    steps = [f"{k}={shlex.quote(str(v))}" for k, v in pairs.items()]
    warns = warns_cache + [
        (
            "set_fact: переменные видны только внутри этой задачи shell, "
            "между задачами dno-tool не переносятся"
        )
    ]
    return steps, warns


def _map_simple(
    args: Dict[str, Any], module: str, base_dir: str = "", stage_dir: str = "/tmp"
) -> Tuple[List[str] | None, List[str]]:
    """Простые однострочные маппинги. None = модуль не из простых."""
    if module == "ping":
        data = str(args.get("data", "pong"))
        return [f"echo {data}  # ansible ping: проверка связи"], []
    if module == "debug":
        msg = args.get("msg")
        var = args.get("var", args.get("varname"))
        if msg is not None:
            text = str(msg)
            if text.startswith("{{") or var is not None:
                return [f"echo {text}"], []
            return [f"echo {shlex.quote(text)}"], []
        if var is not None:
            # External-имя - через общий конвейер %paramN% (чинит echo "$X"):
            # выдаем Jinja, _jinja_to_params ниже превратит в %paramN%.
            # Internal-имя оставит Jinja как есть - закомментирует вызыватель.
            return [f"echo {{{{ {var} }}}}  # debug var {var}"], []
        return [], ["debug: нужны msg или var"]
    if module == "fail":
        msg = str(args.get("msg", "Failed as requested from task"))
        return [f"echo {shlex.quote(msg)} >&2; exit 1"], []
    if module == "assert":
        that = args.get("that", [])
        if isinstance(that, str):
            that = [that]
        if not that:
            return [], ["assert: нужен that"]
        guards = []
        for expr in that:
            cond = str(expr).strip()
            cond = re.sub(r"^\{\{\s*(.*?)\s*\}\}$", r"\1", cond)
            defined = re.match(r"^([\w.]+)\s+is\s+defined$", cond)
            not_defined = re.match(r"^([\w.]+)\s+is\s+not\s+defined$", cond)
            if defined:
                cond = f'-n "${defined.group(1)}"'
            elif not_defined:
                cond = f'-z "${not_defined.group(1)}"'
            else:
                cond = cond.replace("==", "=").replace(" and ", " ] && [ ")
                cond = cond.replace(" or ", " ] || [ ").replace(" not ", " ! ")
            guards.append(
                f"[ {cond} ] || {{ echo {shlex.quote('ASSERT FAILED: ' + str(expr))} >&2; exit 1; }}"
            )
        return guards, ["assert: условие перенесено приближенно - проверьте"]
    if module == "pause":
        seconds = args.get("seconds")
        minutes = args.get("minutes")
        prompt = args.get("prompt")
        echo = str(args.get("echo", "yes")).lower() not in ("no", "false")
        steps = []
        if prompt and echo:
            steps.append(f"echo {shlex.quote(str(prompt))}")
        if seconds:
            steps.append(f"sleep {int(seconds)}")
        elif minutes:
            steps.append(f"sleep {int(minutes)}m")
        elif prompt:
            steps.append(
                f"read -r -t 300 -p {shlex.quote(str(prompt) + ' ')} < /dev/tty || true"
            )
            return steps, ["pause prompt: в неинтерактивном SSH ввод недоступен"]
        else:
            return [], ["pause: нужны seconds, minutes или prompt"]
        return steps, []
    if module == "slurp":
        src = args.get("src", args.get("path"))
        if not src:
            return [], ["slurp: не указан src"]
        return [
            f"base64 -w0 {_shell_quote(src)} 2>/dev/null || base64 {_shell_quote(src)}"
        ], ["slurp: вывод base64 как в Ansible (переменная register не переносится)"]
    if module == "stat":
        path = args.get("path")
        if not path:
            return [], ["stat: не указан path"]
        warns = ["stat: нужен python3 на хосте; в register поддержано stat.exists"]
        if args.get("get_checksum"):
            warns.append(
                "stat get_checksum: контрольные суммы смотрите через sha256sum"
            )
        from .ansible_file_ops import STAT_SCRIPT
        from .ansible_arguments import boolean
        follow = str(int(boolean(args.get("follow", False))))
        return [f"python3 -c {_shell_quote(STAT_SCRIPT)} {_shell_quote(path)} {follow}"], warns
    if module == "hostname":
        name = args.get("name", args.get("_raw", ""))
        if not name:
            return [], ["hostname: не указано имя"]
        return [f"hostnamectl set-hostname {_shell_quote(name)}"], []
    if module == "sysctl":
        sname = args.get("name")
        if not sname:
            return [], ["sysctl: не указано имя"]
        if str(args.get("state", "present")) == "absent":
            return [
                f"sed -i '/^{re.escape(str(sname))}\\s*=/d' /etc/sysctl.conf && sysctl --system"
            ], []
        value = args.get("value", "")
        reload = str(args.get("reload", "yes")).lower() not in ("no", "false")
        steps = [
            (
                f"grep -q '^{re.escape(str(sname))}' /etc/sysctl.conf 2>/dev/null && "
                f"sed -i 's|^{re.escape(str(sname))}.*|{sname}={value}|' /etc/sysctl.conf || "
                f"echo '{sname}={value}' >> /etc/sysctl.conf"
            ),
        ]
        if reload:
            steps.append("sysctl --system")
        return steps, []
    if module == "git":
        repo = args.get("repo")
        dest = args.get("dest")
        if not repo or not dest:
            return [], ["git: нужны repo и dest"]
        version = args.get("version", "")
        depth = args.get("depth", "")
        clone = f"git clone {('--depth ' + str(depth) + ' ') if depth else ''}{_shell_quote(repo)} {_shell_quote(dest)}"
        if version:
            clone += f" && git -C {_shell_quote(dest)} checkout {_shell_quote(version)}"
        key_file = args.get("key_file")
        prefix = ""
        if key_file:
            prefix = f"GIT_SSH_COMMAND={shlex.quote(f'ssh -i {key_file}')} "
            clone = prefix + clone
        if str(args.get("accept_hostkey", "")).lower() in ("yes", "true"):
            host = (
                repo.split("@")[-1].split(":")[0].split("/")[0] if "@" in repo else ""
            )
            if host:
                clone = (
                    f"ssh-keyscan {shlex.quote(host)} >> ~/.ssh/known_hosts 2>/dev/null; "
                    + clone
                )
        if str(args.get("recursive", "")).lower() in ("yes", "true"):
            clone = clone.replace("git clone ", "git clone --recurse-submodules ", 1)
        if str(args.get("update", "yes")).lower() in ("no", "false"):
            cmd = f"test -d {_shell_quote(dest)}/.git || ({clone})"
        elif str(args.get("force", "")).lower() in ("yes", "true"):
            ref = version or "HEAD"
            cmd = (
                f"test -d {_shell_quote(dest)}/.git && (git -C {_shell_quote(dest)} fetch --all && "
                f"git -C {_shell_quote(dest)} reset --hard {_shell_quote(ref)}) || ({clone})"
            )
        else:
            cmd = f"test -d {_shell_quote(dest)}/.git && git -C {_shell_quote(dest)} pull || ({clone})"
        warns = []
        if key_file:
            warns.append("git key_file: ключ должен существовать на хосте")
        return [cmd], warns
    if module == "get_url":
        url = args.get("url")
        dest = args.get("dest")
        if not url or not dest:
            return [], ["get_url: нужны url и dest"]
        steps = [f"curl -fsSL -o {_shell_quote(dest)} {_shell_quote(url)}"]
        if args.get("mode") is not None:
            from .ansible_arguments import mode_argument
            steps.append(f"chmod {mode_argument(args['mode'])} {_shell_quote(dest)}")
        if args.get("checksum"):
            return steps, ["get_url: checksum не проверяется - сверьте вручную"]
        return steps, []
    if module == "unarchive":
        src = args.get("src")
        dest = args.get("dest", ".")
        if not src:
            return [], ["unarchive: не указан src"]
        remote = str(args.get("remote_src", "no")).lower() in ("yes", "true")
        steps = []
        if remote:
            archive = str(src)
        else:
            found = _resolve_local(str(src), base_dir)
            local = found or _assume_local(str(src), base_dir)
            archive = _stage_name(local, stage_dir)
            steps.append(_sftp_upload(local, archive))
            if found:
                warns = [f"unarchive: {src} зальется через SFTP в {archive} - сверьте"]
            else:
                warns = [
                    f"unarchive: файл {local} не найден - проверьте перед запуском"
                ]
            case_cmd = (
                f"case {shlex.quote(archive)} in *.tar.gz|*.tgz) tar -xzf {shlex.quote(archive)} -C {_shell_quote(dest)};; "
                f"*.tar.bz2) tar -xjf {shlex.quote(archive)} -C {_shell_quote(dest)};; "
                f"*.zip) unzip -o {shlex.quote(archive)} -d {_shell_quote(dest)};; "
                f"*) tar -xf {shlex.quote(archive)} -C {_shell_quote(dest)};; esac"
            )
            return (
                steps + [case_cmd],
                warns,
            )
        steps.append(
            f"case {shlex.quote(archive)} in *.tar.gz|*.tgz) tar -xzf {shlex.quote(archive)} -C {_shell_quote(dest)};; "
            f"*.tar.bz2) tar -xjf {shlex.quote(archive)} -C {_shell_quote(dest)};; "
            f"*.zip) unzip -o {shlex.quote(archive)} -d {_shell_quote(dest)};; "
            f"*) tar -xf {shlex.quote(archive)} -C {_shell_quote(dest)};; esac"
        )
        return steps, []
    if module == "authorized_key":
        key = args.get("key", "")
        user = args.get("user", "root")
        if not key:
            return [], ["authorized_key: не указан key"]
        home = "/root" if str(user) == "root" else f"/home/{user}"
        cmd = (
            f"mkdir -p {home}/.ssh && chmod 700 {home}/.ssh && "
            f"grep -q -F {shlex.quote(str(key)[:40])} {home}/.ssh/authorized_keys 2>/dev/null || "
            f"echo {shlex.quote(str(key))} >> {home}/.ssh/authorized_keys && chmod 600 {home}/.ssh/authorized_keys"
        )
        if str(user) != "root":
            cmd += f" && chown -R {shlex.quote(str(user))} {home}/.ssh"
        return [cmd], []
    if module == "reboot":
        return ["systemctl reboot"], ["reboot: хост перезагрузится"]
    if module == "shutdown":
        return ["systemctl poweroff"], ["shutdown: хост выключится"]
    return None, []


# --- setup/gather_facts: информационный вывод facts ---

_FACTS_SCRIPT_HEADER = (
    "# facts хоста (информационно): вывод шага можно сохранить "
    "в файл из таблицы результатов"
)

# Порядок детекта пакетного менеджера: первый найденный побеждает.
_PKG_MGR_DETECT_ORDER = (
    "apt:apt-get",
    "dnf:dnf",
    "dnf5:dnf5",
    "yum:yum",
    "zypper:zypper",
    "pacman:pacman",
    "apk:apk",
    "urpmi:urpmi",
    "portage:emerge",
    "pkg:pkg",
)

# Ключ факта -> строки скрипта (без заголовка и sourcing).
_FACTS_LINES = {
    "pkg_mgr": [
        "M=unknown; for p in "
        + " ".join(_PKG_MGR_DETECT_ORDER)
        + '; do if command -v "${p##*:}" >/dev/null 2>&1; then '
        'M=${p%%:*}; break; fi; done; echo "ansible_pkg_mgr=$M"'
    ],
    "distribution": ['echo "ansible_distribution=${ID:-unknown}"'],
    "distribution_version": [
        'echo "ansible_distribution_version=${VERSION_ID:-unknown}"'
    ],
    "distribution_major_version": [
        'echo "ansible_distribution_major_version=${VERSION_ID%%.*}"'
    ],
    "distribution_release": [
        'echo "ansible_distribution_release=${VERSION_CODENAME:-}"'
    ],
    "os_family": [
        "F=unknown; "
        'case "${ID:-}:${ID_LIKE:-}" in '
        "*debian*|*ubuntu*|*linuxmint*) F=Debian;; "
        "*rhel*|*centos*|*fedora*|*almalinux*|*rocky*|*oracle*) F=RedHat;; "
        "*suse*|*opensuse*) F=Suse;; "
        "*arch*|*manjaro*) F=Archlinux;; "
        "*alpine*) F=Alpine;; "
        "*gentoo*) F=Gentoo;; "
        "*slackware*) F=Slackware;; "
        "*mandriva*|*mageia*) F=Mandrake;; "
        'esac; echo "ansible_os_family=$F"'
    ],
    "service_mgr": [
        "if [ -d /run/systemd/system ]; then echo "
        '"ansible_service_mgr=systemd"; elif [ -d /run/openrc ]; then echo '
        '"ansible_service_mgr=openrc"; else echo '
        '"ansible_service_mgr=sysvinit"; fi'
    ],
    "selinux": [
        "if command -v getenforce >/dev/null 2>&1 && "
        '[ "$(getenforce 2>/dev/null)" != Disabled ]; then echo '
        '"ansible_selinux=enabled"; else echo "ansible_selinux=disabled"; fi'
    ],
    "virtualization_type": [
        "if [ -f /.dockerenv ]; then echo "
        '"ansible_virtualization_type=docker"; elif '
        "[ -f /run/.containerenv ]; then echo "
        '"ansible_virtualization_type=podman"; elif grep -qa container '
        "/proc/1/cgroup 2>/dev/null; then echo "
        '"ansible_virtualization_type=container"; else echo '
        '"ansible_virtualization_type=NA"; fi'
    ],
    "architecture": ['echo "ansible_architecture=$(uname -m 2>/dev/null)"'],
    "hostname": ['echo "ansible_hostname=$(hostname 2>/dev/null)"'],
    "fqdn": ['echo "ansible_fqdn=$(hostname -f 2>/dev/null || hostname 2>/dev/null)"'],
    "kernel": ['echo "ansible_kernel=$(uname -r 2>/dev/null)"'],
    "system": ['echo "ansible_system=$(uname -s 2>/dev/null)"'],
    "user_id": ['echo "ansible_user_id=$(id -un 2>/dev/null)"'],
    "memtotal_mb": [
        "echo \"ansible_memtotal_mb=$(awk '/^MemTotal:/"
        "{print int($2/1024)}' /proc/meminfo 2>/dev/null)\""
    ],
    "processor_vcpus": [
        'echo "ansible_processor_vcpus=$(nproc 2>/dev/null || '
        'grep -c ^processor /proc/cpuinfo 2>/dev/null)"'
    ],
    "default_ipv4": [
        "echo \"ansible_default_ipv4=$(hostname -I 2>/dev/null | awk '{print $1}')\""
    ],
    "mounts": [
        "echo \"ansible_mounts=$(awk '$2 ~ /^\\// {print $2}' "
        "/proc/mounts 2>/dev/null | tr '\\n' ',' | sed 's/,$//')\""
    ],
}

# Факты, читающие /etc/os-release: нужен sourcing перед ними.
_FACTS_NEED_OS_RELEASE = {
    "distribution",
    "distribution_version",
    "distribution_major_version",
    "distribution_release",
    "os_family",
}


def _facts_filter(args: Dict[str, Any], warns: List[str]) -> List[str]:
    """Какие факты печатать по ``filter`` задачи setup (по порядку словаря).

    Без filter - полный набор. Неизвестные имена - warning, пустого
    выхлопа не допускаем (тогда тоже полный набор).
    """
    raw = args.get("filter", args.get("gather_subset"))
    if raw is None:
        return list(_FACTS_LINES)
    if isinstance(raw, str):
        names = [n.strip() for n in raw.split(",") if n.strip()]
    elif isinstance(raw, (list, tuple)):
        names = [str(n).strip() for n in raw if str(n).strip()]
    else:
        names = []
    wanted: List[str] = []
    for name in names:
        short = name[8:] if name.startswith("ansible_") else name
        if short in _FACTS_LINES and short not in wanted:
            wanted.append(short)
        elif "*" in name:
            for key in _FACTS_LINES:
                full = f"ansible_{key}"
                if fnmatch.fnmatch(full, name) and key not in wanted:
                    wanted.append(key)
        else:
            warns.append(f"filter {name}: такой факт не печатаем - пропущен")
    if not wanted:
        warns.append("filter не дал известных фактов - печатаем полный набор")
        return list(_FACTS_LINES)
    ordered = [k for k in _FACTS_LINES if k in wanted]
    return ordered


def _facts_script(args: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Собрать информационный шаг вывода facts для setup/gather_facts."""
    warns: List[str] = []
    subset = args.get("gather_subset")
    if subset is not None and subset not in ("all", ["all"]):
        warns.append(
            "gather_subset поддерживается частично "
            "(печатается текущий набор) - сверьте глазами"
        )
    wanted = _facts_filter(args, warns)
    lines = [_FACTS_SCRIPT_HEADER]
    if any(k in _FACTS_NEED_OS_RELEASE for k in wanted):
        lines.append(". /etc/os-release 2>/dev/null || true")
    for key in wanted:
        lines.extend(_FACTS_LINES[key])
    return "\n".join(lines), warns


def _map_module(
    module: str,
    args: Dict[str, Any],
    base_dir: str = "",
    options: ImportOptions | None = None,
) -> Tuple[List[Any], List[str], str | None]:
    """Смаппить модуль в шаги.

    base_dir - каталог .yml для резолва локальных файлов (copy/script/...).
    options - настройки панели (None = дефолты); stage_dir едет дальше
    в мапперы с заливкой файлов.
    Returns:
        (steps, warnings, skip_reason). Шаги - строки (ssh) или кортежи
        (тип, текст) для sftp. Если skip_reason не None - модуль пропущен.
    """
    stage = _resolve_stage_dir(options)
    if module in ("shell",):
        steps, warns = _map_shell(args)
        return steps, warns, None
    if module in ("setup", "ansible.builtin.setup", "gather_facts"):
        # setup - не пропуск, а информационный шаг вывода facts.
        # Логика when при этом работает через inline-гейты (_fact_atom).
        script, warns = _facts_script(args)
        return [script], warns, None
    if module in ("command", "ansible.builtin.command"):
        steps, warns = _map_command(args, "command")
        return steps, warns, None if steps else "; ".join(warns)
    if module in ("raw",):
        raw = str(args.get("_raw", args.get("cmd", ""))).strip()
        if not raw:
            return [], [], "пустая raw-команда"
        return [raw], [], None
    if module in ("script",):
        steps, warns = _map_script(args, base_dir, stage)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("copy", "ansible.builtin.copy"):
        steps, warns = _map_copy(args, base_dir, stage)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("template",):
        steps, warns = _map_template(args)
        return steps, warns, "шаблон Jinja: отрендерите вручную"
    if module in ("file", "ansible.builtin.file"):
        steps, warns = _map_file(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("apt", "ansible.builtin.apt"):
        steps, warns = _map_apt(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("dnf", "ansible.builtin.dnf", "dnf4", "dnf5", "ansible.builtin.dnf5"):
        steps, warns = _map_dnf_yum(args, module.split(".")[-1], options)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("yum", "ansible.builtin.yum"):
        steps, warns = _map_dnf_yum(args, "yum", options)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("package", "ansible.builtin.package"):
        steps, warns = _map_package(args, options)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("pip", "ansible.builtin.pip"):
        steps, warns = _map_pip(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("apt_key", "ansible.builtin.apt_key"):
        steps, warns = _map_apt_key(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("apt_repository", "ansible.builtin.apt_repository"):
        steps, warns = _map_apt_repository(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in (
        "yum_repository",
        "ansible.builtin.yum_repository",
        "dnf_config_manager",
    ):
        steps, warns = _map_yum_repository(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("rpm_key", "ansible.builtin.rpm_key"):
        steps, warns = _map_rpm_key(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("mount", "ansible.builtin.mount", "ansible.posix.mount"):
        steps, warns = _map_mount(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("filesystem", "community.general.filesystem"):
        steps, warns = _map_filesystem(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("parted", "community.general.parted"):
        steps, warns = _map_parted(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("lvg", "community.general.lvg"):
        steps, warns = _map_lvg(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("lvol", "community.general.lvol"):
        steps, warns = _map_lvol(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("firewalld", "ansible.posix.firewalld"):
        steps, warns = _map_firewalld(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("ufw", "community.general.ufw"):
        steps, warns = _map_ufw(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("selinux", "ansible.posix.selinux"):
        steps, warns = _map_selinux(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("seboolean", "ansible.posix.seboolean"):
        steps, warns = _map_seboolean(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("modprobe", "community.general.modprobe"):
        steps, warns = _map_modprobe(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("timezone", "community.general.timezone"):
        steps, warns = _map_timezone(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("locale_gen", "community.general.locale_gen"):
        steps, warns = _map_locale_gen(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("alternatives", "community.general.alternatives"):
        steps, warns = _map_alternatives(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("nmcli", "community.general.nmcli"):
        steps, warns = _map_nmcli(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("iptables", "community.general.iptables"):
        steps, warns = _map_iptables(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("at", "community.general.at"):
        steps, warns = _map_at(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("known_hosts", "community.general.known_hosts"):
        steps, warns = _map_known_hosts(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("getent", "community.general.getent"):
        steps, warns = _map_getent(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("archive", "community.general.archive"):
        steps, warns = _map_archive(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("fetch", "ansible.builtin.fetch"):
        steps, warns = _map_fetch(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("find", "ansible.builtin.find"):
        steps, warns = _map_find(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("replace", "ansible.builtin.replace"):
        steps, warns = _map_replace(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("tempfile", "ansible.builtin.tempfile"):
        steps, warns = _map_tempfile(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("acl", "ansible.posix.acl"):
        steps, warns = _map_acl(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("capabilities", "community.general.capabilities"):
        steps, warns = _map_capabilities(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("patch", "ansible.posix.patch"):
        steps, warns = _map_patch(args, base_dir, stage)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("ini_file", "community.general.ini_file"):
        steps, warns = _map_ini_file(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("npm", "community.general.npm"):
        steps, warns = _map_npm(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("gem", "community.general.gem"):
        steps, warns = _map_gem(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("composer", "community.general.composer"):
        steps, warns = _map_composer(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("cpanm", "community.general.cpanm"):
        steps, warns = _map_cpanm(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in (
        "service",
        "systemd",
        "ansible.builtin.service",
        "ansible.builtin.systemd",
        "sysvinit",
    ):
        steps, warns = _map_service(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("user", "ansible.builtin.user"):
        steps, warns = _map_user(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("group", "ansible.builtin.group"):
        steps, warns = _map_group(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("cron", "ansible.builtin.cron"):
        steps, warns = _map_cron(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("lineinfile", "ansible.builtin.lineinfile"):
        steps, warns = _map_lineinfile(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("blockinfile", "ansible.builtin.blockinfile"):
        steps, warns = _map_blockinfile(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("wait_for", "ansible.builtin.wait_for"):
        steps, warns = _map_wait_for(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("wait_for_connection", "ansible.builtin.wait_for_connection"):
        steps, warns = _map_wait_for_connection(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("uri", "ansible.builtin.uri"):
        steps, warns = _map_uri(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    if module in ("set_fact", "ansible.builtin.set_fact"):
        steps, warns = _map_set_fact(args)
        if not steps:
            return [], warns, "; ".join(warns)
        return steps, warns, None
    simple, warns = _map_simple(args, module, base_dir, stage)
    if simple is not None:
        if not simple:
            return [], warns, "; ".join(warns)
        return simple, warns, None
    # Короткие имена коллекций: community.general.xyz -> xyz
    short = module.split(".")[-1]
    if short != module:
        return _map_module(short, args, base_dir, options)
    reason = _NO_SHELL_EQUIVALENT.get(module)
    if reason:
        return [], [], reason
    return [], [], f"модуль {module} не поддерживается"


# --- Сборка задач ---


def _format_source(short_module: str, args: Dict[str, Any]) -> str:
    """Компактная строка исходного вызова модуля для превью.

    Только для отображения ("что во что превращается"), при импорте
    в редактор поле ansible_source удаляется (см. get_tasks диалога).
    """
    keys = [k for k in args if k != "_raw"]
    raw = args.get("_raw")
    if raw is not None and not keys:
        text = " ".join(str(raw).split())
        return f"{short_module}: {text[:100]}"
    parts: List[str] = []
    if raw is not None:
        parts.append(" ".join(str(raw).split())[:60])
    for key in sorted(keys):
        value = " ".join(str(args[key]).split())
        if len(value) > 40:
            value = value[:40] + "..."
        parts.append(f"{key}={value}")
    return f"{short_module}: {' '.join(parts)}"[:140]


def _is_unsupported_module(module: str, short_module: str, skip_reason: str) -> bool:
    """Пропуск именно из-за неизвестного модуля (а не ошибки маппера)."""
    if not module:
        return False
    if skip_reason in _NO_SHELL_EQUIVALENT.values():
        return True
    return skip_reason in (
        f"модуль {module} не поддерживается",
        f"модуль {short_module} не поддерживается",
    )


def _convert_ansible_task(
    task: Dict[str, Any],
    play_name: str = "",
    play_ctx: Dict[str, Any] | None = None,
    section: str = "",
    base_dir: str = "",
    options: ImportOptions | None = None,
    internal: Set[str] | FrozenSet[str] | None = None,
) -> Tuple[List[Dict[str, Any]], List[str], List[Dict[str, str]]]:
    """Конвертировать одну Ansible-задачу в список Task dno-tool.

    Обычно возвращается одна Task; block/rescue дают несколько.
    play_ctx - become-контекст play (задача перекрывает).
    section - секция play (tasks/handlers/...), handlers помечаются.
    base_dir - каталог .yml для резолва локальных файлов (copy/script/...).
    options - настройки панели (None = дефолты).
    Returns:
        (tasks, warnings, skipped).
    """
    warnings: List[str] = []
    skipped: List[Dict[str, str]] = []

    # block/rescue/always - раскрыть рекурсивно.
    if any(k in task for k in ("block", "rescue", "always")):
        out: List[Dict[str, Any]] = []
        for part in ("block", "rescue", "always"):
            for sub in task.get(part, []) or []:
                if isinstance(sub, dict):
                    sub_tasks, sub_warns, sub_skipped = _convert_ansible_task(
                        sub,
                        play_name,
                        play_ctx,
                        section,
                        base_dir,
                        options,
                        internal,
                    )
                    out.extend(sub_tasks)
                    warnings.extend(sub_warns)
                    skipped.extend(sub_skipped)
        if not out:
            skipped.append(
                {
                    "task": str(task.get("name", "?")),
                    "module": "block",
                    "reason": "пустой block",
                }
            )
        return out, warnings, skipped

    task_name = str(task.get("name", "")).strip()
    module, raw_value = _detect_module(task)
    manual_reason = _manual_task_reason(task)
    if section == "handlers":
        manual_reason = "handler: notify/changed не моделируются; перенесите обработчик вручную"
    if manual_reason:
        return [], warnings, [{"task": task_name or module, "module": module,
                               "reason": manual_reason}]
    if "local_action" in task or task.get("delegate_to"):
        # Исполнитель не умеет менять целевой хост отдельного шага.
        skipped.append({
            "task": task_name or module or "?",
            "module": module or "-",
            "reason": "local_action/delegate_to: выполнить на указанном хосте вручную",
        })
        return [], warnings, skipped

    if any(task.get(key) == [] for key in ("loop", "with_items", "with_list")):
        skipped.append({
            "task": task_name or module,
            "module": module,
            "reason": "пустой цикл: задача не выполняется",
        })
        return [], warnings, skipped
    if not module:
        skipped.append(
            {
                "task": task_name or "?",
                "module": "-",
                "reason": "модуль не найден (возможно, только block/vars)",
            }
        )
        return [], warnings, skipped

    # Короткое имя для отображения (убрать префикс коллекции).
    short_module = module.split(".")[-1] if "." in module else module

    args = _normalize_args(raw_value, task.get("args"), module)
    source = _format_source(short_module, args)
    if (
        short_module in ("setup", "gather_facts")
        and options is not None
        and not options.host_facts
    ):
        skipped.append(
            {
                "task": task_name or "?",
                "module": short_module,
                "reason": "сбор facts отключен настройкой панели",
            }
        )
        return [], warnings, skipped
    loop_kind, loop_values, loop_warn = _loop_info(task)
    structured_loop = short_module not in ("shell", "raw") and loop_kind != "none"
    arg_sets = _expand_loop_arguments(args, loop_kind, loop_values) if structured_loop else [args]
    steps, map_warns, skip_reason = [], [], None
    for item_args in arg_sets:
        item_steps, item_warns, item_reason = _map_module(module, item_args, base_dir, options)
        if item_reason is not None and not item_steps:
            steps, skip_reason = [], item_reason
            map_warns.extend(item_warns)
            break
        steps.extend(item_steps)
        map_warns.extend(item_warns)
    warnings.extend(f"{task_name or short_module}: {w}" for w in map_warns)
    # Нормализовать: строка -> ("ssh", строка), кортеж (sftp) как есть.
    typed: List[Tuple[str, str]] = [
        s if isinstance(s, tuple) else ("ssh", s) for s in steps
    ]

    if skip_reason is not None and not steps:
        if (
            options is not None
            and options.unknown_modules == "todo"
            and _is_unsupported_module(module, short_module, skip_reason)
        ):
            # Заглушка вместо пропуска: шаг видно, задача импортируется.
            todo_text = f"# TODO вручную: {source}"
            params: List[str] = []
            todo_text = _jinja_to_params(todo_text, params)
            warnings.append(
                f"{task_name or short_module}: модуль без эквивалента - "
                "вставлена заглушка TODO"
            )
            dno_task = {
                "name": task_name or source,
                "description": f"Модуль: {short_module}\nЗаглушка TODO - выполнить вручную",
                "commands": [{"type": "ssh", "text": todo_text}],
                "timeout": DEFAULT_TIMEOUT,
                "params": params,
                "iid": uuid.uuid4().hex,
                "ansible_source": source,
                "ansible_name": task_name,
                "ansible_play": play_name,
                "ansible_notify": [],
                "ansible_listen": [],
                "ansible_handler": section == "handlers",
            }
            return [dno_task], warnings, skipped
        skipped.append(
            {
                "task": task_name or "?",
                "module": short_module,
                "reason": skip_reason,
            }
        )
        return [], warnings, skipped

    # Модификаторы задачи.
    elev_mode, elev_user, elev_warn = _elevation(task, play_ctx)
    if elev_warn:
        warnings.append(f"{task_name or short_module}: {elev_warn}")
    if options is not None and options.privileges != "playbook":
        # Привилегии переопределены панелью (глобальный warning - в parse).
        if options.privileges == "none":
            elev_mode, elev_user = "", ""
        else:
            elev_mode = options.privileges
    when = task.get("when")
    when_text = ""
    cond = ""
    cond_mapped = False
    cond_vars: List[str] = []
    inner: Set[str] | FrozenSet[str] = internal or frozenset()
    when_has_internal = False
    when_internal_hits: List[str] = []
    if when is not None and when != [] and when != "":
        if isinstance(when, list):
            when_text = " and ".join(str(w) for w in when)
        else:
            when_text = str(when)
        if options is not None and options.when_mode == "comment":
            # Режим аудита: условие только комментарием, шаги без гейта.
            cond, cond_mapped, cond_vars = "", False, []
            warnings.append(
                f"{task_name or short_module}: условие when оставлено "
                "комментарием (настройка панели)"
            )
        else:
            cond, cond_mapped, cond_vars = _when_to_shell(
                when, normalize_dnf4=options is None or options.normalize_dnf4
            )
            # Фикс бага when+register (D1): internal-имена никогда в params.
            # when с internal - весь комментарием (CommentStrategy).
            # Детект и по cond_vars (смапленные), и по тексту (несмапленные
            # вида "is changed" - там cond_vars пустые, но имя internal).
            from src.services.commands.ansible_register import when_refs as _when_refs

            text_hits = [r for r in _when_refs(when_text) if r in inner]
            hits = [v for v in cond_vars if v in inner]
            for h in text_hits:
                if h not in hits:
                    hits.append(h)
            if hits:
                when_has_internal = True
                when_internal_hits = list(hits)
                cond, cond_mapped = "", False
                warnings.append(
                    f"{task_name or short_module}: when ссылается на "
                    f"register ({', '.join(hits)}) - задача комментарием, "
                    "выполнить вручную"
                )
        if not when_has_internal:
            if cond_mapped:
                warnings.append(
                    f"{task_name or short_module}: условие when преобразовано "
                    "в shell-проверку - сверьте"
                )
            else:
                warnings.append(
                    f"{task_name or short_module}: условие when не выполняется "
                    "автоматически - проверьте перед запуском"
                )
    if when_text and (options is None or options.when_mode != "comment"):
        if any(kind != "ssh" for kind, _ in typed):
            skipped.append({
                "task": task_name or short_module,
                "module": short_module,
                "reason": "when с SFTP: транспортный шаг нельзя выполнить условно; перенесите задачу вручную",
            })
            return [], warnings, skipped
        if not cond_mapped and not when_has_internal:
            skipped.append({
                "task": task_name or short_module,
                "module": short_module,
                "reason": "when не поддерживается: условную задачу необходимо перенести вручную",
            })
            return [], warnings, skipped
    if not when_has_internal:
        # Ссылки без {{ }}: loop/changed_when/assert от register (F-LoopLines,
        # F-ChangedWhen, F-DebugAssert) - вся задача комментарием.
        raw_hit = _raw_task_internal_hit(task, inner)
        if raw_hit:
            when_has_internal = True
            when_internal_hits = [raw_hit]
            warnings.append(
                f"{task_name or short_module}: читает register '{raw_hit}' "
                "вне shell (loop/when/assert) - задача комментарием, "
                "выполнить вручную"
            )
    for key in (
        "register",
        "changed_when",
    ):
        if task.get(key) not in (None, False, [], ""):
            warnings.append(
                f"{task_name or short_module}: {key}={task[key]} "
                "не переносится - проверьте вручную"
            )
    failed_when = task.get("failed_when")
    never_fail = failed_when is False or (
        isinstance(failed_when, str) and failed_when.lower() in ("false", "no")
    )
    if failed_when not in (None, False, [], "") and not never_fail:
        warnings.append(
            f"{task_name or short_module}: failed_when={task['failed_when']} "
            "не переносится - проверьте вручную"
        )
    notifies = task.get("notify", [])
    if isinstance(notifies, str):
        notifies = [notifies]
    notifies = [str(n) for n in notifies if n]

    # Окружение: префикс VAR='val' к ssh-шагам (Jinja внутри станет params ниже).
    env = task.get("environment")
    if isinstance(env, dict) and env:
        prefix = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())
        typed = [
            (t, s if t != "ssh" or s.startswith("#") else f"{prefix} {s}")
            for t, s in typed
        ]

    # Цикл: развернуть по элементам.
    if loop_warn:
        warnings.append(f"{task_name or short_module}: {loop_warn}")
    elif loop_kind != "none":
        if len(loop_values) > 20:
            warnings.append(
                f"{task_name or short_module}: цикл из {len(loop_values)} "
                "элементов не развернут - подставьте item вручную"
            )
        elif not any(
            _expand_loop(s, loop_kind, loop_values) != [(s, None)] for _, s in typed
        ):
            warnings.append(
                f"{task_name or short_module}: цикл {loop_kind} "
                f"({len(loop_values)} эл.) - шаги без item-подстановок, проверьте"
            )

    params: List[str] = []
    final_typed: List[Tuple[str, str]] = []
    for ctype, step in typed:
        expanded = [(step, None)] if structured_loop else _expand_loop(step, loop_kind, loop_values)
        for text, _label in expanded:
            for match in _JINJA_RE.finditer(text):
                expr = match.group(1).strip()
                base_match = re.match(r"[a-zA-Z_]\w*", expr)
                base = base_match.group(0) if base_match else ""
                if base and base not in inner and expr != base:
                    warning = (
                        f"{task_name or short_module}: Jinja '{expr}' - "
                        f"использован внешний параметр '{base}', "
                        "атрибуты и фильтры не вычисляются - подставьте готовое значение"
                    )
                    if warning not in warnings:
                        warnings.append(warning)
            text = _jinja_to_params(text, params, inner)
            # CommentStrategy (фаза 0): ссылка на internal в ssh-шаге -
            # весь шаг в комментарий, отметка session никогда не ставится.
            hit = _step_internal_hit(text, inner)
            if hit is not None and ctype == "ssh" and not text.lstrip().startswith("#"):
                from src.services.commands.ansible_register import comment_step as _comment_step

                reason = _comment_reason(task, hit, inner)
                text = _comment_step(task_name or short_module, hit, reason, text)
                warnings.append(
                    f"{task_name or short_module}: шаг с register '{hit}' "
                    f"закомментирован ({reason})"
                )
            final_typed.append((ctype, text))

    # debug msg/var с internal: _jinja_to_params оставил {{ }} как есть -
    # такие echo-шаги тоже комментируем (а не оставляем мертвый echo).
    commented_debug: List[Tuple[str, str]] = []
    for ctype, step in final_typed:
        if (
            ctype == "ssh"
            and step.lstrip().startswith("echo")
            and "{{" in step
            and _step_internal_hit(step, inner) is not None
        ):
            from src.services.commands.ansible_register import comment_step as _comment_step2

            hit2 = _step_internal_hit(step, inner) or "?"
            commented_debug.append(
                (
                    ctype,
                    _comment_step2(
                        task_name or short_module,
                        hit2,
                        "debug читает register",
                        step,
                    ),
                )
            )
            warnings.append(
                f"{task_name or short_module}: debug с register '{hit2}' "
                "закомментирован"
            )
        else:
            commented_debug.append((ctype, step))
    final_typed = commented_debug

    if ("failed_when" in task or "ignore_errors" in task) and final_typed and all(t == "ssh" for t, _ in final_typed):
        body = "(\n" + _indent_shell(_checked_body([step for _, step in final_typed]), "  ") + "\n)"
        final_typed = [("ssh", _module_result(body, task, options or ImportOptions()))]
    until = task.get("until")
    cond_shell = ""
    if when_text and final_typed and cond_mapped:
        # Переменные условия - в общий список params (нумерация сквозная).
        # D1: только external; internal уже отведены выше (when_has_internal).
        for var in cond_vars:
            if var in inner:
                continue
            if var not in params:
                params.append(var)
        cond_shell = cond
        for var in sorted(cond_vars, key=len, reverse=True):
            cond_shell = re.sub(
                r"\$" + re.escape(var) + r"(?!\w)",
                f"%param{params.index(var) + 1}%",
                cond_shell,
            )
        # if-блок - всегда ОДИН шаг: шаги выполняются раздельными вызовами
        # (orchestrator), разбивка if/команда/fi по шагам сломается.
        # sudo красится внутрь построчно, su оборачивает блок целиком.
        # sftp-шаги в условие не заворачиваются (их нельзя гейтить) -
        # идут как есть, порядок сохраняется.
        if any(t != "ssh" for t, _ in final_typed):
            warnings.append(
                f"{task_name or short_module}: sftp-шаги выполняются "
                "вне условия when - проверьте"
            )
        if elev_mode != "su":
            final_typed = _elevate_typed(final_typed, elev_mode, elev_user)
        gated: List[Tuple[str, str]] = []
        run: List[str] = []
        for ctype, step in final_typed:
            if ctype != "ssh":
                if run:
                    block = f"if {cond_shell}; then\n" + _indent_shell(_checked_body(run), "  ") + "\nfi"
                    if elev_mode == "su":
                        block = _apply_elevation([block], elev_mode, elev_user)[0]
                    gated.append(("ssh", block))
                    run = []
                gated.append((ctype, step))
            else:
                run.append(step)
        if run:
            block = f"if {cond_shell}; then\n" + _indent_shell(_checked_body(run), "  ") + "\nfi"
            if elev_mode == "su":
                block = _apply_elevation([block], elev_mode, elev_user)[0]
            gated.append(("ssh", block))
        final_typed = gated
    else:
        final_typed = _elevate_typed(final_typed, elev_mode, elev_user)
        if when_text and final_typed:
            idx = next((i for i, (t, _) in enumerate(final_typed) if t == "ssh"), 0)
            ctype, step = final_typed[idx]
            final_typed[idx] = (ctype, f"# when: {when_text}\n{step}")
    if when_has_internal and final_typed:
        # CommentStrategy для when-register: вся задача комментарием.
        # Критерий - наличие исполняемой строки (шаг "# when: ..." сверху
        # тоже комментируем целиком, а не пропускаем).
        from src.services.commands.ansible_register import comment_step as _comment_step3

        hit_name = ", ".join(when_internal_hits) if when_internal_hits else "when"
        commented_all: List[Tuple[str, str]] = []
        for ctype, step in final_typed:
            has_exec = any(
                line.strip() and not line.lstrip().startswith("#")
                for line in (step or "").splitlines()
            )
            if ctype == "ssh" and has_exec:
                commented_all.append(
                    (
                        ctype,
                        _comment_step3(
                            task_name or short_module,
                            hit_name,
                            "условие читает register",
                            step,
                        ),
                    )
                )
            else:
                commented_all.append((ctype, step))
        final_typed = commented_all
        if not any(
            ctype != "ssh"
            or any(
                line.strip() and not line.lstrip().startswith("#")
                for line in (step or "").splitlines()
            )
            for ctype, step in final_typed
        ):
            # Вся задача - комментарии: params нигде не используются,
            # промпт оператору не нужен (F-LoopLines: `item` не спрашивать).
            params.clear()

    name = (
        task_name or f"{short_module}: {final_typed[0][1][:40]}"
        if final_typed
        else task_name or short_module
    )
    description_parts = []
    if play_name:
        description_parts.append(f"Ansible play: {play_name}")
    description_parts.append(f"Модуль: {short_module}")
    if elev_mode:
        description_parts.append(f"Повышение: {elev_mode} ({elev_user or 'root'})")
    if when_text:
        description_parts.append(f"Условие: {when_text}")
    if cond_mapped and cond_shell:
        shown = cond_shell if len(cond_shell) <= 100 else cond_shell[:100] + "..."
        description_parts.append(f"Проверка: {shown}")
    if loop_kind != "none":
        description_parts.append(f"Цикл: {loop_kind}, {len(loop_values)} эл.")
    if isinstance(env, dict) and env:
        description_parts.append(
            "Окружение: " + " ".join(f"{k}={v}" for k, v in env.items())
        )
    if until:
        description_parts.append(f"Повтор: {until}")
    description = "\n".join(description_parts)

    if options is not None and not options.comments:
        # Убрать шаги целиком из комментариев (смешанные и heredoc не трогаем).
        kept = [
            (t, s)
            for t, s in final_typed
            if t != "ssh"
            or any(
                line.strip() and not line.lstrip().startswith("#")
                for line in s.splitlines()
            )
        ]
        dropped = len(final_typed) - len(kept)
        final_typed = kept
        if dropped:
            warnings.append(
                f"{task_name or short_module}: убраны поясняющие "
                f"комментарии ({dropped})"
            )

    if any(t == "sftp" for t, _ in final_typed):
        warnings.append(
            f"{task_name or short_module}: sftp-шаги на хостах нод ищут файл "
            "на машине ноды-владельца, а не на вашей"
        )

    # Jinja-переменные, не покрытые params (например в description) - уже учтены.
    # ansible_source/ansible_* - только для превью и связки notify,
    # при импорте удаляются диалогом (см. get_tasks).
    listens = task.get("listen", [])
    if isinstance(listens, str):
        listens = [listens]
    dno_task = {
        "name": name,
        "description": description,
        "commands": [{"type": t, "text": s} for t, s in final_typed],
        "timeout": DEFAULT_TIMEOUT,
        "params": params,
        "iid": uuid.uuid4().hex,
        "ansible_source": source,
        "ansible_name": task_name,
        "ansible_play": play_name,
        "ansible_notify": notifies,
        "ansible_listen": [str(x) for x in listens if x],
        "ansible_handler": section == "handlers",
    }
    if not final_typed:
        skipped.append(
            {
                "task": task_name or "?",
                "module": short_module,
                "reason": skip_reason or "пустой результат",
            }
        )
        return [], warnings, skipped
    return [dno_task], warnings, skipped


def _looks_like_play(item: Any) -> bool:
    """Проверить, что элемент списка - play (есть hosts/tasks)."""
    if not isinstance(item, dict):
        return False
    return "hosts" in item or any(
        key in item for key in ("tasks", "pre_tasks", "post_tasks", "handlers")
    )


def _merge_tasks(tasks: List[Dict[str, Any]], warnings: List[str]) -> Dict[str, Any]:
    """Слить задачи в одну (импорт всегда одной задачей).

    Порядок обычных задач сохраняется. Handlers не исполняются.
    Параметры сливаются по имени (без дублей), токены %paramN%
    перенумеровываются. Notify превращается в предупреждение.
    """
    main = [t for t in tasks if not t.get("ansible_handler")]

    for task in main:
        for ref in task.get("ansible_notify", []):
            warnings.append(
                f"{task.get('ansible_name') or task.get('name')}: "
                f"notify '{ref}': перенесите handler вручную"
            )

    merged_params: List[str] = []
    commands: List[Dict[str, str]] = []

    def _append(task: Dict[str, Any]) -> None:
        remap: Dict[int, int] = {}
        for local, pname in enumerate(task.get("params", []), start=1):
            if pname not in merged_params:
                merged_params.append(pname)
            remap[local] = merged_params.index(pname) + 1
        for cmd in task.get("commands", []):
            text = cmd.get("text", "")
            # Через временные метки: прямые замены пересекались бы
            # (%param1% -> %param2% затирал исходный %param2%).
            for local in remap:
                text = text.replace(f"%param{local}%", f"{local}")
            for local, glob in remap.items():
                text = text.replace(f"{local}", f"%param{glob}%")
            commands.append({"type": cmd.get("type", "ssh"), "text": text})

    for task in main:
        _append(task)
    plays = sorted({t.get("ansible_play", "") for t in tasks if t.get("ansible_play")})
    if len(tasks) == 1:
        # Единственная задача - ее имя из YAML; display-имя вида
        # "shell: ..." (автоописание безымянной) не наследуем.
        only = (tasks[0].get("ansible_name") or "").strip()
        name = only if only else "Ansible импорт"
        desc_head = f"Ansible play: {plays[0]}" if len(plays) == 1 else "Одна задача"
    elif len(plays) == 1:
        name = f"Ansible: {plays[0]}"
        desc_head = f"Ansible play: {plays[0]}"
    else:
        name = "Ansible импорт"
        desc_head = f"Плеев: {len(plays)}" if plays else "Задачи без play"
    description = f"{desc_head}\nИсходных задач: {len(tasks)}"
    return {
        "name": name,
        "description": description,
        "commands": commands,
        "timeout": DEFAULT_TIMEOUT,
        "params": merged_params,
        "iid": uuid.uuid4().hex,
    }


# Ключи play, влияющие на выполнение, но не переносимые в задачи.
_PLAY_IGNORED_KEYS = (
    "ignore_unreachable",
    "serial",
    "strategy",
    "order",
    "max_fail_percentage",
    "any_errors_fatal",
)


def _expand_loop_arguments(
    args: dict[str, Any], kind: str, values: list[Any]
) -> list[dict[str, Any]]:
    """Expand structured item values before module mappers serialize/quote them."""
    if kind in ("none", "passthrough"):
        return [args]

    def resolve(expression, item):
        if not re.fullmatch(r"item(?:\.\w+|\[(?:\d+|'[^']+'|\"[^\"]+\")\])*", expression):
            raise ValueError(f"неподдерживаемое выражение item: {expression}")
        value = {"key": item[0], "value": item[1]} if kind == "dict" else item
        for match in re.finditer(r"\.(\w+)|\[(?:([0-9]+)|'([^']+)'|\"([^\"]+)\")\]", expression[4:]):
            key = next(group for group in match.groups() if group is not None)
            try:
                value = value[int(key)] if isinstance(value, (list, tuple)) else value[key]
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError(f"item: нет поля/индекса {key}") from exc
        if isinstance(value, (dict, list, tuple)):
            raise ValueError("item: используйте конкретное поле словаря/элемент списка")
        return value

    def expand(value: Any, item: Any) -> Any:
        if isinstance(value, str):
            full = _JINJA_RE.fullmatch(value)
            if full and re.match(r"^item\b", full.group(1).strip()):
                return resolve(full.group(1).strip(), item)

            def replace(match):
                expr = match.group(1).strip()
                return str(resolve(expr, item)) if re.match(r"^item\b", expr) else match.group(0)

            return _JINJA_RE.sub(replace, value)
        if isinstance(value, list):
            return [expand(element, item) for element in value]
        if isinstance(value, dict):
            return {key: expand(element, item) for key, element in value.items()}
        return value

    return [expand(args, item) for item in values]


def _fusion_shells_for_task(
    task: Dict[str, Any],
    base_dir: str,
    options: ImportOptions,
    params: List[str],
    internal: Set[str] | FrozenSet[str],
) -> Tuple[List[str] | None, str]:
    """Сырые ssh-shell шаги задачи для fusion (без elevation).

    Возврат (steps|None, причина_отказа). Jinja-external уже в %paramN%,
    Jinja-internal уже в $DNO_*. None = задача не фьюзится.
    """
    from src.services.commands.ansible_register import bare_hit as _bare_hit
    from src.services.commands.ansible_register import dno_var as _dno_var
    from src.services.commands.ansible_register import map_bare_refs as _bare_map
    from src.services.commands.ansible_register import session_map_text as _session_map

    manual_reason = _manual_task_reason(task)
    if manual_reason:
        return None, manual_reason
    if "local_action" in task or task.get("delegate_to"):
        return None, "local_action/delegate_to: требуется другой целевой хост"
    if any(task.get(key) == [] for key in ("loop", "with_items", "with_list")):
        return None, "пустой цикл: задача не выполняется"
    module, raw_value = _detect_module(task)
    if not module:
        return None, "модуль не найден"
    short = module.split(".")[-1] if "." in module else module
    if short in ("setup", "gather_facts") and not options.host_facts:
        return None, "сбор facts отключён настройкой панели"
    args = _normalize_args(raw_value, task.get("args"), module)
    try:
        _session_map(str(args), internal)
    except ValueError as exc:
        return None, str(exc)
    # loop от register stdout_lines - runtime while-read (F7).
    loop_src = task.get("loop", task.get("with_items"))
    loop_text = loop_src if isinstance(loop_src, str) else ""
    loop_is_register = bool(loop_text) and any(
        b in internal for b in _jinja_bases_for_fusion(loop_text)
    )
    if loop_is_register and not re.fullmatch(
        r"\s*\{\{\s*[a-zA-Z_]\w*\.stdout_lines\s*\}\}\s*", loop_text
    ):
        return None, f"неподдерживаемый loop от register: {loop_src}"
    kind, values, _w = _loop_info(task)
    structured = short not in ("shell", "raw")
    arg_sets = (
        _expand_loop_arguments(args, kind, values)
        if structured and not loop_is_register
        else [args]
    )
    raws: list[str] = []
    for item_args in arg_sets:
        steps, _warns, skip_reason = _map_module(module, item_args, base_dir, options)
        if skip_reason is not None and not steps:
            return None, f"модуль {short}: {skip_reason}"
        typed = [s if isinstance(s, tuple) else ("ssh", s) for s in steps]
        if any(t != "ssh" for t, _ in typed):
            return None, "sftp/local шаг в цепочке (F2)"
        raws.extend(s for _, s in typed)
    out_steps: List[str] = []
    for step in raws:
        if short == "set_fact":
            # Bare facts use the same storage as bare internal references.
            step = re.sub(
                r"^([a-zA-Z_]\w*)=",
                lambda match: _dno_var(match.group(1))[0] + "=",
                step,
            )
        # Static item expressions must be expanded before collecting prompts.
        expanded = (
            [(step, None)]
            if loop_is_register or structured
            else _expand_loop(step, kind, values)
        )
        for text, _label in expanded:
            exempt = frozenset(internal) | ({"item"} if loop_is_register else set())
            text = _jinja_to_params(text, params, exempt)
            text = _session_map(
                text,
                internal,
                quote_variables=short not in ("shell", "raw") or loop_is_register,
                runtime_names={"item": "line"} if loop_is_register else None,
            )
            if short == "assert" and "[ " in text:
                condition, separator, failure = text.partition(" || { echo ")
                if _bare_hit(condition, internal) and (
                    " in " in condition or " not " in condition
                ):
                    return None, "assert с in/not от register"
                text = (
                    _bare_map(condition, internal, quote_variables=True)
                    + separator
                    + failure
                )
            out_steps.append(text)
    if loop_is_register:
        body = "\n".join(out_steps) or ":"
        out_var = _dno_var(_loop_reg_name(loop_text, internal))[1]
        out_steps = [
            "printf '%s\\n' \"$"
            + out_var
            + '" | while IFS= read -r line; do\n'
            + body
            + "\ndone"
        ]
    # Bare words in debug messages, raw shell and failure messages remain literal.
    return out_steps, ""


def _jinja_bases_for_fusion(text: str) -> List[str]:
    from src.services.commands.ansible_register import jinja_bases as _jb

    return _jb(text)


def _loop_reg_name(loop_src: str, internal: Set[str] | FrozenSet[str]) -> str:
    from src.services.commands.ansible_register import jinja_bases as _jb

    for b in _jb(loop_src or ""):
        if b in internal:
            return b
    return "x"


def _try_fusion(
    collected: List[Tuple[str, Dict[str, Any], Dict[str, Any], str]],
    base_dir: str,
    options: ImportOptions,
    internal_names: Set[str] | FrozenSet[str],
    warnings: List[str],
) -> Dict[str, Any] | None:
    """Попытка FusionStrategy: одна цепочка - ОДНА command-запись.

    Возврат fused-задачи или None (тогда вызыватель оставляет CommentStrategy
    и добавляет warning с причиной). Предикаты F1-F7 - по плану.
    """
    from src.services.commands.ansible_register import dno_var as _dno_var

    mains = [(pn, pc, t) for pn, pc, t, s in collected if s != "handlers"]
    if not mains or not internal_names:
        return None
    plays = {pc.get("_play_id", 0) for _, pc, _ in mains}
    if len(plays) > 1:
        warnings.append("fusion невозможен (F1: несколько play) - оставлен комментарий")
        return None
    # F1: продюсер выше всех консьюмеров.
    from src.services.commands.ansible_register import collect_internal as _ci

    pre = _ci([t for _, _, t in mains])
    first: Dict[str, int] = pre.get("first_producer", {})
    cons: List[Tuple[int, str, str]] = pre.get("consumers", [])
    if not cons:
        return None
    min_prod = min(first.values())
    max_cons = max(i for i, _, _ in cons)
    if min_prod > min(i for i, _, _ in cons):
        warnings.append(
            "fusion невозможен (F1: ссылка раньше продюсера) - оставлен комментарий"
        )
        return None
    if min_prod != 0 or max_cons != len(mains) - 1:
        warnings.append(
            "fusion невозможен (цепочка не покрывает все задачи, "
            "сегментный фьюжн запрещен F2) - оставлен комментарий"
        )
        return None
    span = list(range(min_prod, max_cons + 1))
    # F3: единый elevation.
    modes: List[str] = []
    for i in span:
        _pn, pc, t = mains[i]
        mode, user, _w = _elevation(t, pc)
        if options.privileges != "playbook":
            mode = "" if options.privileges == "none" else options.privileges
        modes.append(f"{mode}:{user}")
    if len(set(modes)) > 1:
        warnings.append(
            "fusion невозможен (F3: разный elevation) - оставлен комментарий"
        )
        return None
    # F4/F5/F6 + delegate/local.
    for i in span:
        t = mains[i][2]
        if t.get("until") or t.get("retries"):
            warnings.append(
                "fusion невозможен (F4: until/retries) - оставлен комментарий"
            )
            return None
        cw = t.get("changed_when")
        if isinstance(cw, str) and ("{{" in cw or "}}" in cw):
            warnings.append(
                "fusion невозможен (F5: Jinja в changed_when) - оставлен комментарий"
            )
            return None
        if isinstance(cw, str):
            from src.services.commands.ansible_register import bare_hit as _cw_bare
            from src.services.commands.ansible_register import jinja_bases as _cw_jb

            if any(b in internal_names for b in _cw_jb(cw)) or _cw_bare(
                cw, internal_names
            ):
                warnings.append(
                    "fusion невозможен (F5: changed_when от register) "
                    "- оставлен комментарий"
                )
                return None
        if "local_action" in t or t.get("delegate_to"):
            warnings.append(
                "fusion невозможен (delegate/local в цепочке) - оставлен комментарий"
            )
            return None
        for mod in (
            "copy",
            "template",
            "ansible.builtin.copy",
            "ansible.builtin.template",
        ):
            val = t.get(mod)
            if isinstance(val, dict) and isinstance(val.get("content"), str):
                from src.services.commands.ansible_register import step_has_internal as _hit

                if _hit(val["content"], internal_names):
                    warnings.append(
                        "fusion невозможен (F6: copy/content от register) "
                        "- оставлен комментарий"
                    )
                    return None
    # Сборка fused-блока (ОДНА command-запись, E2).
    params: List[str] = []
    block: List[str] = ["# fused-блок register-цепочки: один exec, переменные живы"]
    changed_approx = False
    for i in span:
        _pn, _pc, t = mains[i]
        label = str(t.get("name", "") or f"шаг {i + 1}")
        steps, reason = _fusion_shells_for_task(
            t, base_dir, options, params, internal_names
        )
        if steps is None:
            warnings.append(f"fusion невозможен ({reason}) - оставлен комментарий")
            return None
        reg = t.get("register")
        cond, appr, fail = _chain_cond(t, mains, i, params, internal_names, options.normalize_dnf4)
        if fail:
            warnings.append(f"fusion невозможен ({fail}) - оставлен комментарий")
            return None
        changed_approx = changed_approx or appr
        if isinstance(reg, str) and reg.strip():
            rc, outv = _dno_var(reg.strip())
            body = _capture_register(steps, rc, outv, stat_result=_detect_module(t)[0].rsplit(".", 1)[-1] == "stat")
        else:
            short = _detect_module(t)[0].rsplit(".", 1)[-1]
            body = _checked_body(steps)
            if short != "set_fact":
                body = "(\n" + _indent_shell(body, "  ") + "\n)"
        body = _module_result(body, t, options) + " || exit $?"
        block.append("# === " + label.replace("\n", "\n# ") + " ===")
        block.append("if " + cond + "; then\n" + _indent_shell(body, "  ") + "\nfi" if cond is not None else body)
    # Failure policies were applied per Ansible task before whole-block elevation.
    text = "\n".join(block)
    # Whole-block elevation (минуя построчный _elevate_typed).
    mode, user, _w = _elevation(mains[span[0]][2], mains[span[0]][1])
    if options.privileges != "playbook":
        mode = "" if options.privileges == "none" else options.privileges
        user = ""
    text = _elevate_block(text, mode, user)
    n_ssh = sum(
        1 for line in block if line.strip() and not line.strip().startswith("#")
    )
    timeout = max(DEFAULT_TIMEOUT, n_ssh * DEFAULT_TIMEOUT)
    if changed_approx:
        warnings.append("changed аппроксимировано как rc==0 - проверьте вручную")
    return {
        "name": "Ansible импорт (fusion)",
        "description": "Fused-блок register-цепочки: один exec",
        "commands": [{"type": "ssh", "text": text}],
        "timeout": timeout,
        "params": params,
        "iid": uuid.uuid4().hex,
    }


def _capture_register(steps: list[str], rc: str, outv: str, *, stat_result: bool = False) -> str:
    """Capture the whole module body, keeping heredoc delimiters on their lines."""
    body = _checked_body(steps)
    captured = (
        f"{outv}=$(\n"
        "  {\n"
        f"{_indent_shell(body, '    ')}\n"
        "  } 2>&1\n"
        f"); {rc}=$?\n"
    )
    if stat_result:
        exists = rc.removesuffix("_RC") + "_EXISTS"
        decoder = 'import json,sys; print("true" if json.loads(sys.argv[1])["exists"] else "false")'
        captured += (
            f'if [ "${rc}" -eq 0 ]; then\n'
            f'{exists}=$(python3 -c {_shell_quote(decoder)} "${outv}") || {rc}=$?\nfi\n'
        )
    return captured + f'(exit "${rc}")'


def _checked_body(steps: list[str]) -> str:
    """Stop at the first failed mapper step, without changing a shell module body."""
    return "\n".join(
        "{\n" + _indent_shell(step, "  ") + "\n} || exit $?"
        if any(line.strip() and not line.lstrip().startswith("#") for line in step.splitlines())
        else step for step in steps
    ) + "\n:"


def _indent_shell(text: str, prefix: str) -> str:
    """Indent generated shell while keeping heredoc payloads and delimiters intact."""
    lines = text.splitlines()
    result = []
    heredocs = []
    for line in lines:
        if heredocs:
            result.append(line)
            delimiter, strip_tabs = heredocs[0]
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate == delimiter:
                heredocs.pop(0)
            continue
        result.append(prefix + line if line.strip() else line)
        for match in re.finditer(r"<<(-?)(?:\s*)(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))", line):
            delimiter = next(group for group in match.groups()[1:] if group is not None)
            heredocs.append((delimiter, match.group(1) == "-"))
    return "\n".join(result)


def _module_result(body: str, task: Dict[str, Any], options: ImportOptions) -> str:
    """Report the task's status without exiting a persistent session shell."""
    from .ansible_arguments import boolean

    text = "{\n" + _indent_shell(body, "  ") + "\n}; DNO_TASK_STATUS=$?\n"
    failure = task.get("failed_when")
    if failure is not None:
        if boolean(failure):
            text += "DNO_TASK_STATUS=1\n"
        elif options.soften_errors:
            text += "DNO_TASK_STATUS=0\n"
    if boolean(task.get("ignore_errors", False)) and options.soften_errors:
        text += "DNO_TASK_STATUS=0\n"
    return text + '(exit "$DNO_TASK_STATUS")'


def _runtime_refs(text: str) -> list[str]:
    """Find generated shell variables, including their braced spelling."""
    return list(dict.fromkeys(re.findall(r"\$\{?(DNO_\w+)\b", text)))


def _stat_reference_error(task, previous, internal) -> str:
    for name in re.findall(r"\b([a-zA-Z_]\w*)\.stat\.exists\b", str(task)):
        if name not in internal:
            continue
        producer = next((t for _, _, t in reversed(previous) if t.get("register") == name), None)
        if producer is None or _detect_module(producer)[0].rsplit(".", 1)[-1] != "stat":
            return f"{name}.stat.exists: нет предшествующей задачи stat"
    return ""


def _elevate_block(
    text: str, mode: str, user: str, *, inherit_runtime: bool = False
) -> str:
    """Whole-block elevation: блок целиком, минуя построчный _elevate_typed.

    Оборачивает составные команды и скрипты вместе с комментариями.
    """
    if not mode:
        return text
    refs = _runtime_refs(text) if inherit_runtime else []
    # Pass values as arguments, not environment (sudo normally clears it).
    # Assignments and gates stay in the session shell; only the body is elevated.
    if refs:
        prelude = "\n".join(
            f'{name}="${{{index}}}"' for index, name in enumerate(refs, 1)
        )
        text = prelude + "\n" + text
    delimiter = f"DNO_SCRIPT_{uuid.uuid4().hex}"
    while delimiter in text:
        delimiter = f"DNO_SCRIPT_{uuid.uuid4().hex}"
    # Late textual placeholders cannot escape a shell quote surrounding the
    # script: the quoted heredoc produces one argument without re-evaluation.
    # Unlike `sh <<EOF`, commands in the script retain their original stdin.
    script = f"\"$(cat <<'{delimiter}'\n{text}\n{delimiter}\n)\""
    arguments = " sh" + "".join(f' "${{{name}}}"' for name in refs)
    if mode == "sudo":
        target = f" -u {shlex.quote(user)}" if user and user != "root" else ""
        return f"sudo{target} sh -c {script}{arguments}"
    who = user or "root"
    return f"su -s /bin/sh -c {script} -- {shlex.quote(who)}{arguments}"


def _sq_escape(text: str) -> str:
    """Экранировать текст для одинарных кавычек shell."""
    return text.replace("'", "'\"'\"'")


def _chain_cond(
    t: Dict[str, Any],
    mains: List[Tuple[str, Dict[str, Any], Dict[str, Any]]],
    idx: int,
    params: List[str],
    internal_names: Set[str] | FrozenSet[str],
    normalize_dnf4: bool = True,
) -> Tuple[str | None, bool, str]:
    """Условие when задачи на $DNO_* (общее для fusion/session).

    Возврат (cond|None, changed_approx, причина_отказа).
    cond None без отказа = when нет (безусловно).
    Продюсер С when тоже гейтится (захват внутри if).
    """
    from src.services.commands.ansible_register import session_map_when as _smw
    from src.services.commands.ansible_register import dno_var as _dno_var

    when = t.get("when")
    wtext = (
        " and ".join(str(w) for w in when)
        if isinstance(when, list)
        else (str(when) if when is not None else "")
    )
    if not wtext:
        return None, False, ""
    invalid_stat = _stat_reference_error(t, mains[:idx], internal_names)
    if invalid_stat:
        return "?", False, invalid_stat
    cond, mapped, cvars = _when_to_shell(when, preserve_fields=True, normalize_dnf4=normalize_dnf4)
    if not mapped:
        changed = re.fullmatch(r"\s*([a-zA-Z_]\w*)\s+is\s+(not\s+)?changed\s*", wtext)
        if changed:
            name = changed.group(1)
            if name not in internal_names or not any(
                prior.get("register") == name for _, _, prior in mains[:idx]
            ):
                return "?", False, "when is changed без продюсера"
            rc0, _o0 = _dno_var(name)
            operator = "!=" if changed.group(2) else "="
            return f'[ "${rc0}" {operator} "0" ]', True, ""
        return "?", False, f"when не маппится: {wtext}"
    try:
        cond, ext = _smw(cond, cvars, internal_names)
    except ValueError as exc:
        return "?", False, str(exc)
    for v in ext:
        if v not in params:
            params.append(v)
    for v in sorted(ext, key=len, reverse=True):
        cond = re.sub(
            r"\$" + re.escape(v) + r"(?!\w)",
            f"%param{params.index(v) + 1}%",
            cond,
        )
    return cond, False, ""


def _last_register_before(
    mains: List[Tuple[str, Dict[str, Any], Dict[str, Any]]],
    idx: int,
    internal: Set[str] | FrozenSet[str],
) -> str | None:
    for j in range(idx - 1, -1, -1):
        reg = mains[j][2].get("register")
        if isinstance(reg, str) and reg.strip() and reg.strip() in internal:
            return reg.strip()
    return None


def _try_session(
    collected: List[Tuple[str, Dict[str, Any], Dict[str, Any], str]],
    base_dir: str,
    options: ImportOptions,
    internal_names: Set[str] | FrozenSet[str],
    warnings: List[str],
) -> Dict[str, Any] | None:
    """SessionStrategy-конвертер: ссылки на internal -> ``$DNO_*``.

    Каждая Ansible-задача становится одним шагом с проверкой своего результата,
    продюсер с capture-оберткой, when - shell-маппинг. Цепочка через
    разные команды требует общего состояния -> флаг ``session: true``
    + warning "включен session-режим". Возврат None = оставить комментарий.
    """
    from src.services.commands.ansible_register import dno_var as _dno_var

    mains = [(pn, pc, t) for pn, pc, t, s in collected if s != "handlers"]
    if not mains or not internal_names:
        return None
    plays = {pc.get("_play_id", 0) for _, pc, _ in mains}
    if len(plays) > 1:
        warnings.append("session невозможен (кросс-play) - оставлен комментарий")
        return None
    params: List[str] = []
    commands: List[Dict[str, str]] = []
    n_ssh = 0
    changed_approx = False
    has_cross_command = False
    from src.services.commands.ansible_register import collect_internal as _ci

    prod_index: dict[str, int] = _ci([t for _, _, t in mains])["first_producer"]
    for i, (_pn, pc, t) in enumerate(mains):
        label = str(t.get("name", "") or f"шаг {i + 1}")
        if t.get("until") or t.get("retries"):
            warnings.append("session невозможен (until/retries) - оставлен комментарий")
            return None
        cw = t.get("changed_when")
        if isinstance(cw, str):
            from src.services.commands.ansible_register import bare_hit as _cw_bare2
            from src.services.commands.ansible_register import jinja_bases as _cw_jb2

            if any(b in internal_names for b in _cw_jb2(cw)) or _cw_bare2(
                cw, internal_names
            ):
                warnings.append(
                    "session невозможен (changed_when от register) "
                    "- оставлен комментарий"
                )
                return None
        steps, reason = _fusion_shells_for_task(
            t, base_dir, options, params, internal_names
        )
        if steps is None:
            warnings.append(f"session невозможен ({reason}) - оставлен комментарий")
            return None
        mode, user, _w = _elevation(t, pc)
        if options.privileges != "playbook":
            mode = "" if options.privileges == "none" else options.privileges
            user = ""
        short_module = _detect_module(t)[0].split(".")[-1]
        if mode and short_module != "set_fact":
            steps = [_elevate_block(s, mode, user, inherit_runtime=True) for s in steps]
        reg = t.get("register")
        shaped: List[str] = []
        cond, appr, fail = _chain_cond(t, mains, i, params, internal_names, options.normalize_dnf4)
        if fail:
            warnings.append(f"session невозможен ({fail}) - оставлен комментарий")
            return None
        changed_approx = changed_approx or appr
        if isinstance(reg, str) and reg.strip():
            rc, outv = _dno_var(reg.strip())
            body = _capture_register(steps, rc, outv, stat_result=_detect_module(t)[0].rsplit(".", 1)[-1] == "stat")
        else:
            body = _checked_body(steps)
            if short_module != "set_fact":
                body = "(\n" + _indent_shell(body, "  ") + "\n)"
        body = _module_result(body, t, options)
        if cond is not None:
            shaped.append("if " + cond + "; then\n" + _indent_shell(body, "  ") + "\nfi")
            has_cross_command = True
        else:
            shaped.append(body)
        joined = "\n".join(shaped)
        refs = _runtime_refs(joined)
        if refs:
            # Чтение $DNO_* продюсера из другой команды = общее состояние.
            # Свои capture той же команды не считаются: свои имена выше.
            for _name, pi in prod_index.items():
                if pi < i:
                    rc_n, out_n = _dno_var(_name)
                    if rc_n in refs or out_n in refs:
                        has_cross_command = True
                        break
        for s in shaped:
            commands.append({"type": "ssh", "text": f"# === {label} ===\n{s}"})
            n_ssh += 1
    if changed_approx:
        warnings.append("changed аппроксимировано как rc==0 - проверьте вручную")
    if has_cross_command:
        warnings.append(
            "включен общий shell: переменные цепочки живут между шагами - "
            "задача выполняется в persistent shell"
        )
    timeout = max(DEFAULT_TIMEOUT, n_ssh * DEFAULT_TIMEOUT)
    task: Dict[str, Any] = {
        "name": "Ansible импорт (session)",
        "description": "Session-режим: общий shell на задачу",
        "commands": commands,
        "timeout": timeout,
        "params": params,
        "iid": uuid.uuid4().hex,
    }
    if has_cross_command:
        task["session"] = True
    return task


def _play_warnings(play: Dict[str, Any], play_name: str) -> List[str]:
    """Предупреждения о непереносимых ключах уровня play."""
    label = play_name or "play"
    warns = []
    for key in _PLAY_IGNORED_KEYS:
        if play.get(key) not in (None, False, [], "", 0):
            warns.append(
                f"{label}: {key}={play[key]} не переносится - проверьте вручную"
            )
    return warns


def parse_ansible_yaml(
    text: str, base_dir: str = "", options: ImportOptions | None = None
) -> Dict[str, Any]:
    """Распарсить текст YAML (playbook или список tasks).

    Args:
        text: содержимое вставки из окна импорта.
        base_dir: каталог .yml для резолва локальных файлов
            (copy src, script и т.п.) на машине оператора.
        options: настройки панели (None = дефолты).

    Returns:
        Словарь ``{tasks, warnings, skipped, errors, stats, options}``
        (см. module docstring).
    """
    options = (options or ImportOptions()).normalized()
    result: Dict[str, Any] = {
        "tasks": [],
        "warnings": [],
        "info": [],
        "skipped": [],
        "errors": [],
        "stats": {
            "plays": 0,
            "ansible_tasks": 0,
            "created": 0,
            "steps": 0,
            "skipped": 0,
        },
        "options": asdict(options),
    }
    if yaml is None:
        result["errors"].append("PyYAML не установлен (pip install pyyaml)")
        return result
    if not text or not text.strip():
        result["errors"].append("Вставьте YAML playbook или список tasks")
        return result
    try:
        if len(text) > MAX_YAML_CHARS:
            raise ValueError(f"YAML превышает {MAX_YAML_CHARS} символов")
        data = yaml.load(text, Loader=_ImportLoader)
        _check_yaml_graph(data)
    except Exception as exc:  # noqa: BLE001 - любая ошибка YAML уходит в превью
        result["errors"].append(f"Ошибка YAML: {exc}")
        return result
    if data is None:
        result["errors"].append("Пустой документ")
        return result

    # Собрать плоский список (play_name, play_ctx, ansible_task, section).
    collected: List[Tuple[str, Dict[str, Any], Dict[str, Any], str]] = []
    active_blocks: Set[int] = set()
    visited_tasks = 0
    origins = {}

    def _collect_task_list(
        items: Any,
        play_name: str,
        play_ctx: Dict[str, Any],
        section: str = "",
        inherited: Dict[str, Any] | None = None,
        block_path: tuple[int, ...] = (),
    ) -> None:
        nonlocal visited_tasks
        if not isinstance(items, list):
            result["errors"].append(
                f"{play_name or 'YAML'}: {section or 'tasks'} должен быть списком"
            )
            return
        for position, entry in enumerate(items, 1):
            visited_tasks += 1
            if visited_tasks > MAX_TASKS:
                if not result["errors"]:
                    result["errors"].append(f"Превышен лимит {MAX_TASKS} задач/блоков")
                return
            if not isinstance(entry, dict):
                result["errors"].append("Каждая задача должна быть словарем")
                continue
            if id(entry) in active_blocks or len(active_blocks) >= 64:
                result["errors"].append("Циклический или слишком глубоко вложенный block")
                continue
            effective = dict(inherited or {})
            effective.update(entry)
            # when наследуется с AND, а не перекрывается дочерним when.
            if inherited and "when" in inherited and "when" in entry:

                def conditions(value: Any) -> List[Any]:
                    return value if isinstance(value, list) else [value]

                effective["when"] = conditions(inherited["when"]) + conditions(
                    entry["when"]
                )
            for key in ("environment", "vars"):
                if (
                    inherited
                    and isinstance(inherited.get(key), dict)
                    and isinstance(entry.get(key), dict)
                ):
                    effective[key] = {**inherited[key], **entry[key]}
            if any(key in entry for key in ("block", "rescue", "always")):
                if "rescue" in entry or "always" in entry:
                    result["skipped"].append({
                        "task": str(entry.get("name", "block")),
                        "module": "block",
                        "reason": "block с rescue/always: обработку ошибок нельзя "
                        "заменить последовательностью шагов; перенесите блок вручную",
                    })
                    continue
                directives = {
                    k: v for k, v in effective.items()
                    if k in _NON_MODULE_KEYS
                    and k not in ("name", "block", "rescue", "always")
                }
                active_blocks.add(id(entry))
                try:
                    _collect_task_list(
                        entry["block"], play_name, play_ctx, section, directives,
                        block_path + (position,),
                    )
                finally:
                    active_blocks.remove(id(entry))
                continue
            try:
                _detect_module(effective)
            except (ValueError, TypeError) as exc:
                result["errors"].append(f"{play_name or 'YAML'} / {entry.get('name', '?')}: {exc}")
                continue
            origins[id(effective)] = {
                "play_id": play_ctx.get("_play_id", 0),
                "section": section or "tasks",
                "block_path": block_path + (position,),
                "line": getattr(entry, "line", 0),
            }
            collected.append((play_name, play_ctx, effective, section))

    if _looks_like_play(data):
        plays: List[Any] = [data]
        synthetic: List[Tuple[str, Any]] | None = None
    elif (
        isinstance(data, list)
        and data
        and all(isinstance(i, dict) and _looks_like_play(i) for i in data)
    ):
        plays = data
        synthetic = None
    elif isinstance(data, list):
        plays = []
        synthetic = [("", item) for item in data]
    elif isinstance(data, dict):
        # Одиночная задача {- name: ..., shell: ...}
        plays = []
        synthetic = [("", data)]
    else:
        result["errors"].append("Неизвестная структура YAML")
        return result

    if synthetic is not None:
        _collect_task_list([entry for _, entry in synthetic], "", {})
    else:
        for play_id, play in enumerate(plays, 1):
            if not isinstance(play, dict):
                result["errors"].append("Play должен быть словарем")
                return result
            play_name = str(play.get("name", "") or "")
            if _looks_like_play(play):
                result["stats"]["plays"] += 1
                play_ctx = _play_elevation_ctx(play)
                play_ctx["_play_id"] = play_id
                result["warnings"].extend(_play_warnings(play, play_name))
                for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                    _collect_task_list(
                        play.get(section, []), play_name, play_ctx, section
                    )
            else:
                # Смешанный список - трактовать элементы как tasks.
                _collect_task_list([play], "", {})

    if result["errors"]:
        return result
    result["stats"]["skipped"] = len(result["skipped"])
    if not collected:
        result["errors"].append("Задачи не найдены: нужен playbook или список tasks")
        return result

    if not options.stage_dir.startswith("/"):
        result["warnings"].append(
            f"Временный каталог '{options.stage_dir}' некорректен - используется /tmp"
        )
        options.stage_dir = "/tmp"
        result["options"] = asdict(options)

    # Предпроход register (фаза 0): строго ПОСЛЕ развертки block: во flat-список.
    from src.services.commands.ansible_register import collect_internal as _collect_internal

    # Handlers never enter register chains; retain a diagnostic in every mode.
    executable = []
    for pn, pc, task, section in collected:
        if section == "handlers":
            result["stats"]["ansible_tasks"] += 1
            result["skipped"].append({
                "task": str(task.get("name", "handler")), "module": "handler",
                "reason": "handler: notify/changed не моделируются; перенесите обработчик вручную",
                "source": origins[id(task)],
            })
        else:
            executable.append((pn, pc, task, section))
    collected = executable
    scopes = {}
    for _, pc, task, _ in collected:
        scopes.setdefault(pc.get("_play_id", 0), []).append(task)
    internals_by_play = {}
    for play_id, tasks in scopes.items():
        pre = _collect_internal(tasks)
        internals_by_play[play_id] = frozenset(pre.get("names", set()))
        result["warnings"].extend(pre.get("warnings", []))
    internal_names = frozenset().union(*internals_by_play.values())
    output_chars = 0
    output_steps = 0
    structural_skips = list(result["skipped"])

    for play_name, play_ctx, ansible_task, section in collected:
        result["stats"]["ansible_tasks"] += 1
        local_internal = internals_by_play[play_ctx.get("_play_id", 0)]
        foreign_internal = internal_names - local_internal
        from src.services.commands.ansible_register import when_refs

        foreign_hit = (
            _step_internal_hit(str(ansible_task), foreign_internal)
            or _raw_task_internal_hit(ansible_task, foreign_internal)
            or next((n for n in when_refs(str(ansible_task.get("when", "")))
                     if n in foreign_internal), None)
        )
        if foreign_hit:
            result["skipped"].append({
                "task": str(ansible_task.get("name") or "Задача"),
                "module": _detect_module(ansible_task)[0],
                "reason": f"register/set_fact '{foreign_hit}' из другого play: "
                "между plays нет общей сессии; перенесите задачу вручную",
                "source": origins[id(ansible_task)],
            })
            continue
        try:
            dno_tasks, warns, skipped = _convert_ansible_task(
                ansible_task,
                play_name,
                play_ctx,
                section,
                base_dir,
                options,
                local_internal,
            )
        except (ValueError, TypeError, OverflowError) as exc:
            result["errors"].append(
                f"{ansible_task.get('name') or 'Задача'}: некорректные аргументы: {exc}"
            )
            continue
        output_chars += sum(len(step["text"]) for t in dno_tasks for step in t["commands"])
        output_steps += sum(len(t["commands"]) for t in dno_tasks)
        if output_chars > MAX_RESULT_CHARS or output_steps > MAX_STEPS:
            result["errors"].append(
                f"Результат превышает лимит {MAX_RESULT_CHARS} символов / {MAX_STEPS} шагов"
            )
            result["tasks"] = []
            break
        result["tasks"].extend(dno_tasks)
        origin = origins[id(ansible_task)]
        prefix = f"[play #{origin['play_id']}, строка {origin['line']}] "
        result["warnings"].extend(prefix + warning for warning in warns)
        for skip in skipped:
            skip["source"] = origin
        result["skipped"].extend(skipped)

    if options.privileges != "playbook":
        label = {
            "sudo": "всегда sudo",
            "su": "всегда su",
            "none": "root (без повышения)",
        }[options.privileges]
        result["warnings"].append(f"Привилегии переопределены настройкой: {label}")

    result["stats"]["skipped"] = len(result["skipped"])
    if result["tasks"] and not result["errors"]:
        if options.register_mode == "fusion" and internal_names:
            # FusionStrategy (фаза 1): цепочка - ОДНА command-запись.
            # Неудача - тихо оставить CommentStrategy (warning уже добавлен).
            fused = _try_fusion(
                collected, base_dir, options, internal_names, result["warnings"]
            )
            if fused is not None:
                if sum(len(c["text"]) for c in fused["commands"]) > MAX_RESULT_CHARS:
                    result["errors"].append("Fusion: превышен лимит размера результата")
                    result["tasks"] = []
                    result["stats"]["created"] = result["stats"]["steps"] = 0
                    return result
                result["skipped"] = structural_skips
                result["stats"]["skipped"] = len(structural_skips)
                _drop_stale_comment_warnings(result["warnings"])
                result["tasks"] = [fused]
                result["stats"]["created"] = 1
                result["stats"]["steps"] = 1
                _classify_messages(result)
                return result
        if options.register_mode == "session" and internal_names:
            # SessionStrategy-конвертер (фаза 2-3): $DNO_* + флаг session.
            # Полная сборка - ниже в _try_session; неудача - комментарий.
            sess = _try_session(
                collected, base_dir, options, internal_names, result["warnings"]
            )
            if sess is not None:
                if (sum(len(c["text"]) for c in sess["commands"]) > MAX_RESULT_CHARS
                        or len(sess["commands"]) > MAX_STEPS):
                    result["errors"].append("Session: превышен лимит размера результата")
                    result["tasks"] = []
                    result["stats"]["created"] = result["stats"]["steps"] = 0
                    return result
                result["skipped"] = structural_skips
                result["stats"]["skipped"] = len(structural_skips)
                _drop_stale_comment_warnings(result["warnings"])
                result["tasks"] = [sess]
                result["stats"]["created"] = 1
                result["stats"]["steps"] = len(sess["commands"])
                _classify_messages(result)
                return result
        if options.single_task:
            # Всегда одна задача: слить созданные (handlers - в конец).
            result["tasks"] = [_merge_tasks(result["tasks"], result["warnings"])]
        else:
            # Поштучно: дедуп имен для дерева.
            seen: Dict[str, int] = {}
            for task in result["tasks"]:
                base = task.get("name", "Без имени")
                seen[base] = seen.get(base, 0) + 1
                if seen[base] > 1:
                    task["name"] = f"{base} ({seen[base]})"

    result["stats"]["created"] = len(result["tasks"])
    result["stats"]["steps"] = sum(len(t["commands"]) for t in result["tasks"])
    result["stats"]["skipped"] = len(result["skipped"])
    if not result["tasks"]:
        result["errors"].append(
            "Ни одна задача не преобразована - проверьте список пропущенных"
        )
    _classify_messages(result)
    return result


def _classify_messages(result: Dict[str, Any]) -> None:
    """Move descriptive conversion notes out of actionable warnings."""
    info_markers = (
        "Ansible backend dnf4 преобразован в штатную команду dnf",
        "включен общий shell: переменные цепочки живут между шагами",
    )
    warnings = result.setdefault("warnings", [])
    info = result.setdefault("info", [])
    remaining = []
    for message in warnings:
        if any(marker in message for marker in info_markers):
            if message not in info:
                info.append(message)
        else:
            remaining.append(message)
    warnings[:] = remaining


def _drop_stale_comment_warnings(warnings: List[str]) -> None:
    """Убрать warnings CommentStrategy после успеха fusion/session.

    Цепочка покрывает все задачи (предикат), значения register унесены
    в $DNO_* - пометки "не переносится"/"закомментирован" устарели.
    """
    stale = (
        "задача комментарием",
        "закомментирован",
        "when ссылается на register",
        "условие when не выполняется автоматически",
        "register=",
    )
    warnings[:] = [w for w in warnings if not any(s in w for s in stale)]


def can_import(result: Dict[str, Any]) -> bool:
    """Проверить, что результат парсинга годится для импорта."""
    return bool(result.get("tasks")) and not result.get("errors")
