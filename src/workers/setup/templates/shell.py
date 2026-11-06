"""Remote shell scripts and templates."""


def _as_user_func() -> str:
    """Определение shell-функции dnoasuser для смены пользователя на хосте.

    Основной механизм - setpriv --reuid/--regid (util-linux, есть на всех
    целевых системах): смена uid БЕЗ PAM. runuser проходит PAM-сессию и
    падает на учётках с лимитами (pam_limits maxlogins): "There were too
    many logins for <user>" / "cannot open session" - типично для
    школьных guest-аккаунтов. Функция готовит окружение X-сессии: HOME
    (из getent), XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS, PATH.

    Использование: dnoasuser <user> <command...>
    Определение от user не зависит, повторная дефиниция безопасна; в
    циклах - dnoasuser "$u" ... (определение один раз перед циклом).

    Fallback - прежний runuser (если setpriv на хосте отсутствует).
    """
    return (
        'dnoasuser() { U="$1"; shift; '
        # setpriv в PATH? (входит в util-linux; смена uid без PAM).
        "if command -v setpriv >/dev/null 2>&1; then "
        'setpriv --reuid="$(id -u "$U")" --regid="$(id -g "$U")" '
        "--init-groups "
        'env PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" '
        'HOME="$(getent passwd "$U" | cut -d: -f6)" '
        'XDG_RUNTIME_DIR="/run/user/$(id -u "$U")" '
        'DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "$U")/bus" '
        '"$@"; '
        # Fallback: runuser (PAM); XDG_RUNTIME_DIR нужен systemctl --user.
        "else "
        'runuser -u "$U" -- env '
        'XDG_RUNTIME_DIR="/run/user/$(id -u "$U")" '
        'DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "$U")/bus" '
        '"$@"; fi; }'
    )


def _as_user_call(user: str) -> str:
    """Готовый вызов dnoasuser: '<funcdef>; dnoasuser <user>'.

    Args:
        user: имя пользователя (литеральное, экранируется).
    """
    return _as_user_func() + "; dnoasuser " + _shell_quote_bare(user)


def _shell_quote_bare(s: str) -> str:
    """Экранирование имени пользователя для shell (без кавычек в конце)."""
    s = (s or "").strip()
    if not s or any(ch in s for ch in " ;&|<>()$`\\\"'!*?[]#~=%\n\r\t"):
        return "'" + s.replace("'", "'\\''") + "'"
    return s
