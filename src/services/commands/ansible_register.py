"""Register-стратегии импорта Ansible (план register sessions).

Qt-free. Общий предпроход + три стратегии с единым интерфейсом.

- internal-имя: значение из ``register:``/``set_fact:`` самого playbook,
  появляется только во время прогона. Никогда не спрашивать у оператора.
- external-имя: ``vars:``, факты, окружение. Значение известно до прогона,
  уходит в ``params`` (``%paramN%``).

Фаза 0: предпроход ``collect_internal`` + ``CommentStrategy``.
Фаза 1: ``FusionStrategy`` (предикаты + генератор fused-блока).
Фаза 2-3: ``SessionStrategy`` (маппинг на ``$DNO_*`` + флаг ``session``).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set, Tuple

#: Префикс shell-переменных fused/session-блоков. Зарезервирован.
DNO_PREFIX = "DNO_"

_JINJA_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}", re.DOTALL)
_IDENT_RE = re.compile(r"^[a-zA-Z_]\w*$")


def jinja_bases(text: str) -> List[str]:
    """Базы ``{{ ... }}`` в порядке появления (``a.b`` -> ``a``)."""
    bases: List[str] = []
    for match in _JINJA_RE.finditer(text or ""):
        inner = match.group(1).strip()
        if not inner:
            continue
        base = inner.split("|")[0].strip().split()[0] if inner else ""
        base = base.split(".")[0].split("[")[0]
        if base and _IDENT_RE.match(base) and base not in bases:
            bases.append(base)
    return bases


def when_refs(when_text: str) -> List[str]:
    """Имена переменных в ``when`` (без ``{{ }}``, базу до точки)."""
    text = (when_text or "").replace("{{", " ").replace("}}", " ").strip()
    text = re.sub(r"'[^'\\]*(?:\\.[^'\\]*)*'|\"[^\"\\]*(?:\\.[^\"\\]*)*\"", " ", text)
    if not text:
        return []
    refs: List[str] = []
    for tok in re.findall(r"[a-zA-Z_]\w*", text):
        if tok in (
            "and",
            "or",
            "not",
            "is",
            "defined",
            "changed",
            "true",
            "false",
            "yes",
            "no",
            "none",
            "null",
        ):
            continue
        if tok not in refs:
            refs.append(tok)
    return refs


def collect_internal(flat_tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Предпроход по flat-списку задач после развертки ``block:``.

    Args:
        flat_tasks: список raw Ansible-задач (dict) в порядке следования.

    Returns:
        ``{names, first_producer, producers, consumers, warnings}``:
        - names: множество internal-имен (register + set_fact ключи).
        - first_producer: имя -> индекс первого продюсера.
        - consumers: список ``(индекс, вид, деталь)``.
        - warnings: предпроходные предупреждения.
    """
    names: Set[str] = set()
    first_producer: Dict[str, int] = {}
    producers: Dict[str, List[int]] = {}
    for idx, task in enumerate(flat_tasks):
        if not isinstance(task, dict):
            continue
        reg = task.get("register")
        if isinstance(reg, str) and reg.strip() and _IDENT_RE.match(reg.strip()):
            name = reg.strip()
            names.add(name)
            producers.setdefault(name, []).append(idx)
            first_producer.setdefault(name, idx)
        # set_fact ключи - тоже internal.
        for key in ("set_fact", "ansible.builtin.set_fact"):
            val = task.get(key)
            if isinstance(val, dict):
                for var in val:
                    if isinstance(var, str) and _IDENT_RE.match(var):
                        names.add(var)
                        producers.setdefault(var, []).append(idx)
                        first_producer.setdefault(var, idx)
    consumers: List[Tuple[int, str, str]] = []
    warnings: List[str] = []
    _BARE_RE = re.compile(
        r"\b([a-zA-Z_]\w*)\.(?:rc|stdout(?:_lines)?|stderr|changed)\b"
    )
    for idx, task in enumerate(flat_tasks):
        if not isinstance(task, dict):
            continue
        label = str(task.get("name", "") or f"задача {idx + 1}")
        # Jinja-ссылки во всей задаче (модуль, loop, debug, assert...).
        try:
            blob = str(task)
        except Exception:  # noqa: BLE001 - диагностический проход
            blob = ""
        for base in jinja_bases(blob):
            if base in names:
                consumers.append((idx, "jinja", base))
                prod = first_producer.get(base, -1)
                if prod == idx:
                    warnings.append(
                        f"{label}: self-ссылка на register '{base}' - "
                        "значение еще не существует"
                    )
                elif prod > idx:
                    warnings.append(
                        f"{label}: ссылка на '{base}' раньше продюсера "
                        "(задача {}) - проверьте порядок".format(prod + 1)
                    )
        # Голые ссылки без скобок (assert that: r.rc == 0, changed_when).
        seen_here = {(i, b) for i, _, b in consumers if i == idx}
        for base in _BARE_RE.findall(blob):
            if base in names and (idx, base) not in seen_here:
                consumers.append((idx, "bare", base))
                seen_here.add((idx, base))
                prod = first_producer.get(base, -1)
                if prod >= idx:
                    warnings.append(
                        f"{label}: ссылка на '{base}' "
                        "без продюсера выше - проверьте порядок"
                    )
        # when-ссылки.
        when = task.get("when")
        if when:
            text = (
                " and ".join(str(w) for w in when)
                if isinstance(when, list)
                else str(when)
            )
            for ref in when_refs(text):
                if ref in names:
                    consumers.append((idx, "when", ref))
                    prod = first_producer.get(ref, -1)
                    if prod >= idx:
                        warnings.append(
                            f"{label}: when ссылается на '{ref}' "
                            "без продюсера выше - проверьте порядок"
                        )
        # loop от register.
        loop = task.get("loop", task.get("with_items"))
        if isinstance(loop, str):
            for base in jinja_bases(loop):
                if base in names and (idx, "loop", base) not in consumers:
                    consumers.append((idx, "loop", base))
    # Продюсер вне play (нет консьюмеров вообще) - не warning, молча.
    return {
        "names": names,
        "first_producer": first_producer,
        "producers": producers,
        "consumers": consumers,
        "warnings": warnings,
    }


def filter_external(vars_list: List[str], internal: Set[str] | frozenset) -> List[str]:
    """Только external-имена (internal никогда не в params, D1)."""
    return [v for v in vars_list if v not in internal]


def step_has_internal(text: str, internal: Set[str] | frozenset) -> str | None:
    """Первое internal-имя в ``{{ }}`` шага или None."""
    for base in jinja_bases(text or ""):
        if base in internal:
            return base
    return None


def comment_step(task_label: str, reg_name: str, reason: str, text: str) -> str:
    """Весь шаг в комментарий (CommentStrategy)."""
    lines = [f"# {task_label}: шаг требует register '{reg_name}' - {reason}"]
    for line in (text or "").splitlines() or [""]:
        lines.append(f"# {line}" if line.strip() else "#")
    return "\n".join(lines)


# --- FusionStrategy: предикаты и генератор (фаза 1) ---


def dno_var(name: str) -> Tuple[str, str]:
    """Shell-переменные fused-блока для internal-имени."""
    clean = re.sub(r"\W", "_", name).upper() or "X"
    return f"{DNO_PREFIX}{clean}_RC", f"{DNO_PREFIX}{clean}_OUT"


def fusion_predicates(
    prod_idx: int,
    cons_idx: List[int],
    typed_kinds: List[str],
    elev_modes: List[str],
    raw_tasks: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Предикаты фьюзабельности F1-F7. Возврат (ok, причина)."""
    if not cons_idx:
        return False, "нет консьюмеров"
    if min(cons_idx) < prod_idx:
        return False, "ссылка раньше продюсера (F1)"
    span = range(prod_idx, max(cons_idx) + 1)
    for i in span:
        if i < len(typed_kinds) and typed_kinds[i] != "ssh":
            return False, "разрыв sftp/local в цепочке (F2)"
    if len({m for m in elev_modes if m is not None}) > 1:
        return False, "разный elevation в цепочке (F3)"
    for i in span:
        if i < len(raw_tasks):
            t = raw_tasks[i] or {}
            if t.get("until") or t.get("retries"):
                return False, "until/retries в цепочке (F4)"
            cw = t.get("changed_when")
            if isinstance(cw, str) and ("{{" in cw or "}}" in cw):
                return False, "Jinja-register в changed_when (F5)"
            if (
                isinstance(cw, str)
                and any(k in cw for k in ("register", "stdout", "stdout_lines", "rc"))
                and "{{" not in str(cw)
            ):
                pass  # шаблон без Jinja - можно, проверит генератор
            for mod in (
                "copy",
                "template",
                "ansible.builtin.copy",
                "ansible.builtin.template",
            ):
                val = t.get(mod)
                if isinstance(val, dict) and isinstance(val.get("content"), str):
                    if "{{" in val["content"] and step_has_internal(
                        val["content"],
                        set(raw_tasks and [] or []),
                    ):
                        return False, "copy/content от register (F6)"
    return True, ""


def build_fused_block(
    segments: List[Tuple[str, str, str]],
    changed_approx: bool = False,
) -> Tuple[str, List[str]]:
    """Собрать ОДИН fused-блок из сегментов (label, kind, shell).

    kinds: run (захват rc/out), cond (when-обертка уже в shell),
    echo (debug), guard (assert/fail), assign (set_fact).
    Возврат (текст_блока, warnings).
    """
    warns: List[str] = []
    out: List[str] = ["# fused-блок register-цепочки: один exec, переменные живы"]
    for label, kind, shell in segments:
        out.append(f"# === {label} ===")
        out.append(shell)
    if changed_approx:
        warns.append("changed аппроксимировано как rc==0 - проверьте вручную")
    return "\n".join(out), warns


# --- SessionStrategy: маппинг (фазы 2-3) ---


def _runtime_name(expr: str, internal: set[str] | frozenset[str]) -> str | None:
    """Map only supported scalar/register expressions, without discarding filters."""
    expr = expr.strip()
    match = re.match(r"[a-zA-Z_]\w*", expr)
    head = match.group(0) if match else ""
    if head not in internal:
        return None
    if not re.fullmatch(r"[a-zA-Z_]\w*(?:\.\w+)*", expr):
        raise ValueError(f"неподдерживаемое выражение register: {expr}")
    _, _, field = expr.partition(".")
    rc, outv = dno_var(head)
    if field == "stat.exists":
        return rc.removesuffix("_RC") + "_EXISTS"
    if not field or field in ("rc", "changed"):
        return rc
    if field in ("stdout", "stdout_lines", "stderr"):
        return outv
    raise ValueError(f"неподдерживаемое выражение register: {expr}")


def _heredoc_delimiter(text: str, position: int) -> tuple[str, bool, int] | None:
    """Read a heredoc word with shell quote removal, not shlex's escape rules."""
    position += 2
    strip_tabs = text[position : position + 1] == "-"
    if strip_tabs:
        position += 1
    while position < len(text):
        if text[position] in " \t":
            position += 1
        elif text.startswith("\\\n", position):
            position += 2
        else:
            break
    word: list[str] = []
    quote = ""
    started = False
    while position < len(text):
        char = text[position]
        if char == "\\" and quote != "'":
            if position + 1 >= len(text):
                return None
            following = text[position + 1]
            if not quote or following in '$`"\\\n':
                if following != "\n":
                    word.append(following)
                    started = True
                position += 2
                continue
        if char in ("'", '"') and (not quote or char == quote):
            quote = "" if quote else char
            started = True
        elif not quote and (char.isspace() or char in ";|&()<>"):
            break
        else:
            word.append(char)
            started = True
        position += 1
    if not started or quote:
        return None
    return "".join(word), strip_tabs, position


def _quote_context(text: str) -> str:
    """Find the quote enclosing a generated argument's interpolation."""
    quote = ""
    escaped = False
    comment = False
    token_start = True
    pending_docs: list[tuple[str, bool]] = []
    docs: list[tuple[str, bool]] = []
    position = 0
    arithmetic = 0
    while position < len(text):
        if docs:
            end = text.find("\n", position)
            if end < 0:
                break
            delimiter, strip_tabs = docs[0]
            line = text[position:end]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                docs.pop(0)
            position = end + 1
            continue
        char = text[position]
        if char == "\n" and not quote and not escaped:
            comment = False
            token_start = True
            docs, pending_docs = pending_docs, []
            position += 1
            continue
        if not quote and not escaped and not comment:
            if text.startswith("((", position):
                arithmetic += 1
            elif arithmetic and text.startswith("))", position):
                arithmetic -= 1
            if (
                not arithmetic
                and text.startswith("<<", position)
                and not text.startswith("<<<", position)
                and (position == 0 or text[position - 1] != "<")
            ):
                heredoc = _heredoc_delimiter(text, position)
                if heredoc:
                    word, strip_tabs, position = heredoc
                    pending_docs.append((word, strip_tabs))
                    token_start = False
                    continue
        position += 1
        if comment:
            if char == "\n":
                comment = False
                token_start = True
            continue
        if escaped:
            escaped = False
            if char != "\n":
                token_start = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif not quote and char == "#" and token_start:
            comment = True
        elif char in ("'", '"'):
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            token_start = False
        elif not quote:
            token_start = char.isspace() or char in ";|&()"
    return quote


def _quoted_runtime_reference(text: str, position: int, var: str) -> str:
    """Expand a runtime value without unquoting the surrounding literal."""
    expanded = f"${{{var}}}"
    quote = _quote_context(text[:position])
    if quote == "'":
        return f"'\"{expanded}\"'"
    if quote == '"':
        return expanded
    return f'"{expanded}"'


def session_map_text(
    text: str,
    internal: set[str] | frozenset[str],
    *,
    quote_variables: bool = False,
    runtime_names: dict[str, str] | None = None,
) -> str:
    """``{{ r.stdout }}`` -> ``$DNO_R_OUT``, ``r.rc`` в when -> ``$DNO_R_RC``."""

    def _repl(match: "re.Match[str]") -> str:
        inner = match.group(1).strip()
        var = (runtime_names or {}).get(inner)
        if var is None:
            var = _runtime_name(inner, internal)
        if var is None:
            return match.group(0)
        if not quote_variables:
            # Raw shell keeps unquoted expansion, but existing single quotes
            # must be reopened around the value. Braces preserve suffixes.
            if _quote_context(text[: match.start()]) == "'":
                return _quoted_runtime_reference(text, match.start(), var)
            return f"${{{var}}}"
        return _quoted_runtime_reference(text, match.start(), var)

    return _JINJA_RE.sub(_repl, text or "")


def session_map_when(
    cond_shell: str, cond_vars: List[str], internal: Set[str] | frozenset
) -> Tuple[str, List[str]]:
    """Shell-условие на ``$DNO_*`` для internal, остальное как есть."""
    mapped = cond_shell or ""
    external: List[str] = []
    for var in cond_vars:
        runtime = _runtime_name(var, internal)
        if runtime is not None:
            replacement = f"${runtime}"
        else:
            # Keep legacy external base-name prompts, not a Jinja evaluator.
            base = var.split(".")[0]
            if base not in external:
                external.append(base)
            replacement = f"${base}"
        mapped = re.sub(r"\$" + re.escape(var) + r"(?![\w.])", replacement, mapped)
        mapped = re.sub(r"\$\{" + re.escape(var) + r"\}", replacement, mapped)
    return mapped, external


def map_bare_refs(
    text: str, internal: set[str] | frozenset[str], *, quote_variables: bool = False
) -> str:
    """Голые ``r.rc``/``r.stdout`` (assert that без скобок) -> ``$DNO_*``.

    Применяется поверх шагов fusion/session: ``_map_simple`` для assert
    переносит ``that`` дословно, скобок там нет. Возврат текста с заменами.
    """

    def _replace(match: re.Match[str]) -> str:
        quote = _quote_context(text[: match.start()])
        if quote == "'" or (quote and not match.group(0).startswith("$")):
            return match.group(0)
        var = _runtime_name(match.group(1), internal)
        if var is None:
            return match.group(0)
        if quote_variables:
            return _quoted_runtime_reference(text, match.start(), var)
        return f"${var}"

    return re.sub(
        r"(?<!\w)\$?([a-zA-Z_]\w*\.(?:rc|stdout(?:_lines)?|stderr))\b",
        _replace,
        text or "",
    )


def bare_hit(text: str, internal: Set[str] | frozenset) -> str | None:
    """Первое internal-имя в голой (без скобок) позиции или None."""
    for name in sorted(internal, key=len, reverse=True):
        if re.search(
            r"\b"
            + re.escape(name)
            + r"(?:\.(?:rc|stdout(?:_lines)?|stderr|changed))?\b",
            text or "",
        ):
            return name
    return None
