"""Выбор сервера захвата Veyon для активной графической сессии."""

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from src.workers.setup.phases import CommandRunner

HEADLESS_PLUGIN = "f626f759-7691-45c0-bd4a-37171d98d219"
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_SESSION_COMMAND = (
    "sessions=$(loginctl list-sessions --no-legend) || exit 1; "
    'for sid in $(printf "%s\\n" "$sessions" | awk \'{print $1}\'); do '
    'state=$(loginctl show-session "$sid" -p State --value) || exit 1; '
    '[ "$state" = active ] || continue; '
    'class=$(loginctl show-session "$sid" -p Class --value) || exit 1; '
    'case "$class" in user|user-early) ;; *) continue;; esac; '
    'kind=$(loginctl show-session "$sid" -p Type --value) || exit 1; '
    'case "$kind" in x11|wayland) printf "%s\\n" "$kind";; esac; done'
)


class CaptureConfigurationError(RuntimeError):
    """Настройки не позволяют подтвердить захват рабочего стола."""


@dataclass(frozen=True)
class CaptureTarget:
    """Обнаруженная сессия и подходящий установленный модуль."""

    session: str | None
    name: str
    uid: str
    current_uid: str


def normalize_plugin(value: str) -> str:
    """Нормализовать UUID из вывода veyon-cli без принятия лишнего текста."""
    value = value.strip().strip('"').strip("{}").lower()
    return value if _UUID.fullmatch(value) else ""


def capture_target(run: CommandRunner) -> CaptureTarget:
    """Определить модуль по активной пользовательской сессии и списку плагинов."""
    code, output, _ = run(_SESSION_COMMAND, timeout=10)
    if code:
        raise CaptureConfigurationError(
            "Не удалось определить графическую сессию Veyon"
        )
    kinds = set(output.splitlines())
    if kinds - {"x11", "wayland"} or len(kinds) > 1:
        raise CaptureConfigurationError(
            "Неоднозначная графическая сессия: нельзя выбрать один модуль захвата Veyon"
        )
    session = next(iter(kinds), None)
    code, current, _ = run("veyon-cli config get VncServer/Plugin", timeout=10)
    current_uid = normalize_plugin(current) if code == 0 else ""
    code, output, _ = run("veyon-cli plugin show", timeout=10)
    if code:
        raise CaptureConfigurationError("Не удалось получить список модулей Veyon")
    plugins: dict[str, str] = {}
    for line in output.splitlines():
        columns = [part.strip() for part in line.split("|")]
        if len(columns) >= 6 and _UUID.fullmatch(columns[4]):
            plugins[columns[1]] = columns[4].lower()
    if session == "wayland":
        name = "PipeWireVncServer"
    elif session == "x11":
        name = "BuiltinX11VncServer"
    else:
        name = (
            "PipeWireVncServer"
            if current_uid and current_uid == plugins.get("PipeWireVncServer")
            else "BuiltinX11VncServer"
        )
    uid = plugins.get(name)
    if uid is None:
        hint = (
            "Для Wayland нужен Veyon 4.11+ с модулем PipeWireVncServer; "
            "установите подходящий пакет или войдите в Plasma (X11)"
            if session == "wayland"
            else f"В установленном Veyon отсутствует модуль {name}"
        )
        raise CaptureConfigurationError(hint)
    return CaptureTarget(session, name, uid, current_uid)


def configure_capture(run: CommandRunner, log: Callable[[str], None]) -> CaptureTarget:
    """Выбрать модуль и подтвердить фактически сохранённый UUID."""
    target = capture_target(run)
    if target.current_uid == HEADLESS_PLUGIN:
        log(
            "Обнаружен HeadlessVncServer: он выдаёт искусственный экран без захвата рабочего стола"
        )
    log(
        f"Графическая сессия: {target.session or 'нет активной пользовательской сессии'}"
    )
    if target.current_uid != target.uid:
        code, _, _ = run(
            "veyon-cli config set VncServer/Plugin "
            + shlex.quote("{" + target.uid + "}"),
            timeout=10,
        )
        if code:
            raise CaptureConfigurationError(f"Не удалось выбрать модуль {target.name}")
    code, actual, _ = run("veyon-cli config get VncServer/Plugin", timeout=10)
    if code or normalize_plugin(actual) != target.uid:
        raise CaptureConfigurationError(
            "Модуль захвата Veyon не подтверждён после сохранения"
        )
    log(f"Модуль захвата: {target.name} (настройка подтверждена)")
    return target
