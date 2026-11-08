"""Утилиты sudo: определение необходимости и PTY-expect для пароля.

Механизм: при ``should_use_sudo(device) == True`` и наличии ``sudo`` в тексте команды,
SSHWorker включает PTY-expect — перехватывает prompt sudo в выводе и автоматически
отправляет пароль через ``channel.send()``. ``sudo`` читает пароль из ``/dev/tty``
(сам PTY), поэтому пайпы, heredoc и ``sudo -u`` не конфликтуют с паролем.

Кэш tty: ``sudo`` запоминает аутентификацию на tty (по умолчанию ``timestamp_type=tty``,
``timestamp_timeout=5 мин``). paramiko открывает один PTY на ``exec_command`` →
многострочные команды и ``sudo cmd1 && sudo cmd2`` аутентифицируются один раз.

Безопасность: при NOPASSWD sudo (или root-логине) prompt не появляется → пароль
не отправляется → нет утечки в stdin команды.
"""

import re
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.domain.models.device import DeviceModel

# Регулярка обнаружения `sudo` как отдельного слова в тексте команды.
# Не срабатывает на `sudoedit`, комментарии `# sudo`, и т.п. — но это допустимо:
# лучше перестраховаться (false-positive = отправить пароль, который не нужен,
# чем пропустить). sudoedit всё равно требует sudo-прав.
_SUDO_RE = re.compile(r"\bsudo\b")


def has_sudo(command_text: str) -> bool:
    """Возвращает True, если в тексте команды встречается `sudo` как отдельное слово."""
    return bool(_SUDO_RE.search(command_text))


# Prompt sudo: ``[sudo] password for user:`` (англ.) или ``[sudo] пароль для user:``
# (русск.). Локализация зависит от ``LANG``. SSHWorker экспортирует локаль перед
# командой через ``build_locale_prefix`` (по умолчанию ru_RU.UTF-8, но может быть
# отключена — см. ``locale_utils.py``), поэтому при пустом/неполном окружении sudo
# может выдать англ. или перевод на другой язык. Regex покрывает:
# - Чистые локали: ``[sudo] password for user:`` / ``[sudo] пароль для user:``
# - Смешанные локали (когда LANG частично сбит): ``[sudo] password для user:``
#   / ``[sudo] пароль for user:`` — слово и предлог могут быть из разных языков.
# - Без указания пользователя: ``[sudo] password:`` / ``[sudo] пароль:``.
# - Без скобок (редко, при перенаправлении/обёртке): ``sudo: password for user:``.
# Универсальный fallback: ключевое слово ``password``/``пароль`` после ``[sudo]`` или
# ``sudo:`` с опциональным ``for``/``для`` + именем пользователя.
_SUDO_PROMPT_RE = re.compile(
    r"(?:\[sudo\]\s+|sudo:\s*)(?:password|пароль)(?:\s+(?:for|для)\s+\S+)?\s*:",
    re.IGNORECASE,
)


def is_sudo_password_prompt(buffer: str) -> bool:
    """Возвращает True, если buffer содержит prompt пароля sudo.

    Проверяет хвост buffer (последние 512 символов): prompt может появиться
    после большого вывода предыдущих команд в многострочном скрипте.
    """
    return bool(_SUDO_PROMPT_RE.search(buffer[-512:]))


def build_masked_sudo_prompt(token_hex: str) -> tuple[str, str]:
    """Вернуть (ожидаемый_промпт, shell_аргумент_для_sudo_p).
    Разделение кавычек гарантирует, что PTY-эхо самой команды никогда не
    будет содержать склеенную строку маркера до реального выполнения sudo.
    """
    expected = f"__DNO_SUDO_{token_hex}__:"
    shell_arg = "'__DNO_SUDO_'" + shlex.quote(f"{token_hex}__:")
    return expected, shell_arg


def should_use_sudo(device: "DeviceModel | None") -> bool:
    """Определяет, нужно ли использовать sudo для устройства.

    Приоритет:
    1. ``device.use_sudo`` (если не None — явный per-device override)
    2. ``config.app.ssh.use_sudo`` (глобальный дефолт)
    """
    if device is None:
        return False
    if device.use_sudo is not None:
        return bool(device.use_sudo)
    from src.config import config

    return bool(config.app.ssh.use_sudo)


def ensure_sudo_prefix(
    command_text: str,
    device: "DeviceModel | None",
    username: str,
    prompt_arg: str = "",
) -> str:
    """Выполнить весь SSH-скрипт в root Bash при автоматическом sudo.

    Цепочки, пайпы и перенаправления получают одинаковые права. Явные sudo,
    включая sudo -u, остаются внутри скрипта без изменения их аргументов.
    Пустой текст и комментарии не требуют повышения прав.

    Args:
        command_text: Текст команды (с уже раскрытыми плейсхолдерами).
        device: Устройство.
        username: Логин пользователя (для проверки root).
        prompt_arg: Строка аргумента для -p (если задана).

    Returns:
        Скрипт в повышенной оболочке либо исходный текст для root/без sudo.
    """
    if not should_use_sudo(device):
        return command_text
    if username == "root":
        return command_text
    for line in command_text.splitlines(keepends=True):
        effective = line.lstrip()
        if effective and not effective.startswith("#"):
            break
    else:
        return command_text
    p_flag = f"-p {prompt_arg} " if prompt_arg else ""
    return f"sudo {p_flag}-- bash -c {shlex.quote(command_text)}"


def sudo_password_error(password: str | None, sent: bool) -> str:
    """Причина отказа автоматической аутентификации без повторной отправки."""
    if sent:
        return "sudo: пароль не принят или требуется повторная аутентификация — проверьте связку и настройки sudo"
    if not password:
        return "sudo требует пароль — задайте пароль в связке или настройте NOPASSWD"
    if "\n" in password or "\r" in password:
        return (
            "sudo: пароль содержит перевод строки и не может быть передан автоматически"
        )
    return ""


# ================================================================
# Хелперы для прямого paramiko exec_command (без SSHWorker)
# ================================================================
#
# Используются в модулях, которые работают с paramiko.SSHClient напрямую
# (polkit_editor, kde_config_dialog, device_info), когда SSHWorker неприменим
# (нет PTY-expect, нужен разделённый stdout/stderr) или нежелателен
# (locale_cmd искажает вывод).
#
# Паттерн: ``sudo -S -p '' -E -- bash -c '...'`` + пароль первой строкой
# через stdin. ``-S`` — пароль из stdin, ``-p ''`` — пустой prompt,
# ``-E`` — сохранить окружение (HOME, DISPLAY, XAUTHORITY), ``bash -c`` —
# вся цепочка (с &&, |, ;) выполняется под sudo. Без PTY — stdout/stderr
# разделены, нет эха пароля. См. RemoteServiceInstallerWorker.run_cmd.


def wrap_sudo_cmd(cmd: str, use_sudo: bool) -> str:
    """Обернуть команду в ``sudo -S -p '' -E -- bash -c '...'`` при ``use_sudo``.

    ``bash -c`` гарантирует, что вся цепочка (с ``&&``, ``|``, ``;``)
    выполняется под sudo, а не только первая команда до оператора.
    Одинарные кавычки экранируются (``'`` → ``'\\''``).

    Args:
        cmd: Текст команды (без sudo).
        use_sudo: Флаг — выполнять ли под sudo.

    Returns:
        Обёрнутая команда, либо исходная ``cmd`` если ``use_sudo=False``.
    """
    if not use_sudo:
        return cmd
    escaped = cmd.replace("'", "'\\''")
    return f"sudo -S -p '' -E -- bash -c '{escaped}'"
