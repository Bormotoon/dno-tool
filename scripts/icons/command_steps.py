"""Типы шагов команд и независимые режимы SSH, читаемые при размере 24 px."""

from ._base import fi, st

COLORS = {
    "command_ssh": "#3B82F6",
    "command_sftp": "#0D9488",
    "command_local": "#8B5CF6",
    "command_desktop_session": "#0284C7",
    "command_background": "#D97706",
}


def _ink(mode: str, color: str, name: str) -> str:
    """Прозрачным глифам badge оставляем собственный семантический цвет."""
    return COLORS[name] if mode == "badge" else color


def _ssh(mode: str, color: str) -> str:
    """Окно удалённого терминала с крупным приглашением >_."""
    color = _ink(mode, color, "command_ssh")
    return (
        f'<rect x="16" y="25" width="96" height="78" rx="12" {st(color, 8)}/>'
        f'<path d="M16 43 H112" {st(color, 6)}/>'
        f'<path d="M33 59 L47 72 L33 85 M62 85 H86" {st(color, 8)}/>'
    )


def _sftp(mode: str, color: str) -> str:
    """Папка и встречные стрелки передачи файлов."""
    color = _ink(mode, color, "command_sftp")
    return (
        f'<path d="M16 43 V29 H49 L61 43 H112 V101 H16 Z" {st(color, 8)}/>'
        f'<path d="M35 61 H91 M80 51 L91 61 L80 71 '
        f'M93 84 H37 M48 74 L37 84 L48 94" {st(color, 7)}/>'
    )


def _local(mode: str, color: str) -> str:
    """Командная строка на собственном ноутбуке оператора."""
    color = _ink(mode, color, "command_local")
    return (
        f'<rect x="26" y="22" width="76" height="64" rx="8" {st(color, 8)}/>'
        f'<path d="M26 86 L14 103 H114 L102 86 M52 103 H76" {st(color, 8)}/>'
        f'<path d="M41 40 L53 53 L41 66 M67 66 H83" {st(color, 7)}/>'
    )


def _mode_monitor(color: str, symbol: str) -> str:
    """Общие рамка, подставка и видимые границы обоих режимов SSH."""
    return (
        f'<rect x="16" y="20" width="96" height="70" rx="10" {st(color, 8)}/>'
        f'<path d="M64 90 V108 M44 108 H84" {st(color, 8)}/>'
        + symbol
    )


def _desktop(mode: str, color: str) -> str:
    """Монитор и человек: выполнение в сеансе владельца рабочего стола."""
    color = _ink(mode, color, "command_desktop_session")
    return _mode_monitor(
        color,
        f'<circle cx="64" cy="44" r="10" {fi(color)}/>'
        f'<path d="M43 76 V72 C43 55 85 55 85 72 V76 Z" {fi(color)}/>'
    )


def _background(mode: str, color: str) -> str:
    """Тот же монитор со стрелкой запуска самостоятельного приложения."""
    color = _ink(mode, color, "command_background")
    return _mode_monitor(
        color,
        f'<path d="M46 72 L82 38 M60 38 H82 V60" {st(color, 8)}/>'
    )


PAINTERS = {
    "command_ssh": _ssh,
    "command_sftp": _sftp,
    "command_local": _local,
    "command_desktop_session": _desktop,
    "command_background": _background,
}
