"""Small, Qt-free normalizer for Ansible's compact module argument syntax."""
import re
from typing import Any

_RAW_MODULES = {"command", "shell", "raw", "script", "win_command", "win_shell"}
_RAW_KEYS = {"chdir", "creates", "removes", "executable", "warn", "stdin",
             "stdin_add_newline", "strip_empty_ends", "expand_argument_vars"}
_BOOL_KEYS = {"enabled", "global", "update_cache", "purge", "autoremove", "recurse",
              "remote_src", "backup", "create", "backrefs", "firstmatch", "follow",
              "get_checksum", "persistent", "immediate", "permanent", "append", "force",
              "stdin_add_newline", "strip_empty_ends", "expand_argument_vars"}


def boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, int)):
        text = str(value).lower()
        if text in ("true", "yes", "on", "1"):
            return True
        if text in ("false", "no", "off", "0"):
            return False
    raise ValueError(f"ожидается логическое значение, получено {value!r}")


def mode_argument(value: Any) -> str:
    """YAML integer modes are permission bits, not decimal chmod strings."""
    if isinstance(value, bool):
        raise ValueError("mode не может быть bool")
    if isinstance(value, int):
        if not 0 <= value <= 0o7777:
            raise ValueError("mode вне диапазона 0000..7777")
        return format(value, "04o")
    if not isinstance(value, str) or not value:
        raise ValueError("mode должен быть числом или строкой chmod")
    if re.fullmatch(r"[0-7]{1,4}", value):
        return value.zfill(4)
    if re.fullmatch(r"(?:[ugoa]*[+=-][rwxXstugo]*)(?:,[ugoa]*[+=-][rwxXstugo]*)*", value):
        return value
    if "{{" in value:
        return value
    raise ValueError(f"неверный mode: {value!r}")


def _tokens(text: str) -> list[tuple[int, int]]:
    """Keep source spans, shell quotes and whole Jinja expressions intact."""
    spans = []
    start = None
    quote = ""
    jinja_quote = ""
    closer = ""
    escaped = False
    index = 0
    while index < len(text):
        ch = text[index]
        if start is None and not ch.isspace():
            start = index
        if escaped:
            escaped = False
        elif ch == "\\" and (closer or quote != "'"):
            escaped = True
        elif closer:
            if jinja_quote:
                if ch == jinja_quote:
                    jinja_quote = ""
            elif text.startswith(closer, index):
                closer = ""
                index += 1
            elif ch in "\"'":
                jinja_quote = ch
        elif text[index:index + 2] in ("{{", "{%", "{#"):
            closer = {"{{": "}}", "{%": "%}", "{#": "#}"}[text[index:index + 2]]
            index += 1
        elif quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch.isspace() and start is not None:
            spans.append((start, index))
            start = None
        index += 1
    if quote or closer or escaped:
        raise ValueError("несбалансированные кавычки/Jinja в аргументах")
    if start is not None:
        spans.append((start, len(text)))
    return spans


def _unquote(value: str) -> str:
    # Compact values are not shell words: preserve regex backslashes and Jinja.
    escapes = {'n': '\n', 'r': '\r', 't': '\t', 'a': '\a', 'b': '\b',
               'f': '\f', 'v': '\v', '\\': '\\', "'": "'", '"': '"'}
    result = []
    index = 0
    while index < len(value):
        if value[index] == '\\' and index + 1 < len(value):
            code = value[index + 1]
            if code in escapes:
                result.append(escapes[code])
                index += 2
                continue
            digits = {'x': 2, 'u': 4, 'U': 8}.get(code)
            if digits and re.fullmatch(r'[a-fA-F0-9]{' + str(digits) + '}', value[index + 2:index + 2 + digits]):
                result.append(chr(int(value[index + 2:index + 2 + digits], 16)))
                index += digits + 2
                continue
        result.append(value[index])
        index += 1
    decoded = ''.join(result)
    if len(decoded) >= 2 and decoded[0] == decoded[-1] and decoded[0] in "\"'":
        decoded = decoded[1:-1]
    return decoded


def normalize_args(value: Any, extra_args: Any = None, module: str = "") -> dict[str, Any]:
    result = dict(extra_args) if isinstance(extra_args, dict) else {}
    short = module.rsplit(".", 1)[-1]
    if value is None:
        pass
    elif isinstance(value, dict):
        supplied = dict(value)
        raw = supplied.pop("_raw", None)
        if raw is not None:
            supplied = {**normalize_args(raw, module=module), **supplied}
        result.update(supplied)
    elif isinstance(value, str):
        free_form = not module or short in _RAW_MODULES
        raw_spans = []
        for start, end in _tokens(value):
            token = value[start:end]
            match = re.match(r"^([a-zA-Z_]\w*)=(.*)$", token, flags=re.S)
            if match and (not free_form or match.group(1) in _RAW_KEYS):
                result[match.group(1)] = _unquote(match.group(2))
            else:
                raw_spans.append((start, end))
        if raw_spans:
            if not free_form and short not in {
                'include', 'include_tasks', 'import_tasks', 'import_playbook', 'include_role',
                'import_role', 'include_vars', 'meta',
            }:
                raise ValueError("ожидаются именованные аргументы модуля key=value")
            # Preserve newlines and shell quoting between retained free-form tokens.
            pieces = []
            for index, (start, end) in enumerate(raw_spans):
                if index:
                    previous_end = raw_spans[index - 1][1]
                    gap = value[previous_end:start]
                    pieces.append(gap if not gap.strip() else " ")
                pieces.append(value[start:end])
            result["_raw"] = "".join(pieces)
    else:
        raise ValueError("аргументы модуля должны быть строкой или словарём")
    for key in _BOOL_KEYS & result.keys():
        if isinstance(result[key], str) and "{{" in result[key]:
            continue
        result[key] = boolean(result[key])
    if "mode" in result and result["mode"] is not None:
        result["mode"] = mode_argument(result["mode"])
    return result
