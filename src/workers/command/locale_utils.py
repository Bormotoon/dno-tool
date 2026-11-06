"""Утилиты локализации SSH-команд.

Механизм: перед выполнением команды через SSHWorker (PTY, команды из
``commands.json``) экспортируются переменные окружения ``LANG``, ``LC_ALL``,
``LC_CTYPE``. Это нужно для корректного вывода кириллицы и локализованных
сообщений на ROSA Linux (ru_RU.UTF-8).

Проблема: на системах, где локаль ``ru_RU.UTF-8`` не сгенерирована (нет в
``locale -a``), жёсткий экспорт вызывает предупреждение
``bash: line 1: warning: setlocale: LC_ALL: cannot change locale (ru_RU.UTF-8)``.

Решение (по аналогии с sudo): глобальный дефолт ``SSHConfig.locale`` +
per-device override ``DeviceModel.locale``. Пустая строка = не экспортировать
локаль (устраняет предупреждение).

Прямые paramiko-вызовы (``remote_service_installer``, ``device_info``
и др.) локаль НЕ экспортируют намеренно — ``export`` искажает вывод при разделённых
stdout/stderr. См. AGENTS.md (Sudo support).
"""

from typing import Optional


# Стандартные локали для выбора в UI (editable QComboBox). Сортировка: сначала
# наиболее вероятные для целевой аудитории (ROSA/CIS), затем алфавит. Список не
# исчерпывающий — поле редактируемое, пользователь может ввести любую.
STANDARD_LOCALES: tuple[str, ...] = (
    "ru_RU.UTF-8",
    "en_US.UTF-8",
    "C.UTF-8",
    "C",
    "POSIX",
    "ru_UA.UTF-8",
    "uk_UA.UTF-8",
    "be_BY.UTF-8",
    "kk_KZ.UTF-8",
    "ky_KG.UTF-8",
    "uz_UZ.UTF-8",
    "tg_TJ.UTF-8",
    "mn_MN.UTF-8",
    "hy_AM.UTF-8",
    "ka_GE.UTF-8",
    "az_AZ.UTF-8",
    "bg_BG.UTF-8",
    "sr_RS.UTF-8",
    "de_DE.UTF-8",
    "fr_FR.UTF-8",
    "es_ES.UTF-8",
    "pt_BR.UTF-8",
    "it_IT.UTF-8",
    "pl_PL.UTF-8",
    "cs_CZ.UTF-8",
    "nl_NL.UTF-8",
    "da_DK.UTF-8",
    "nb_NO.UTF-8",
    "sv_SE.UTF-8",
    "fi_FI.UTF-8",
    "et_EE.UTF-8",
    "lv_LV.UTF-8",
    "lt_LT.UTF-8",
    "ro_RO.UTF-8",
    "hu_HU.UTF-8",
    "el_GR.UTF-8",
    "tr_TR.UTF-8",
    "he_IL.UTF-8",
    "ar_SA.UTF-8",
    "fa_IR.UTF-8",
    "zh_CN.UTF-8",
    "zh_TW.UTF-8",
    "ja_JP.UTF-8",
    "ko_KR.UTF-8",
    "th_TH.UTF-8",
    "vi_VN.UTF-8",
    "id_ID.UTF-8",
)


def get_locale(device) -> Optional[str]:
    """Определяет локаль для устройства.

    Приоритет:
    1. ``device.locale`` (если не None — явный per-device override, в т.ч. пустая
       строка = явно отключить экспорт локали)
    2. ``config.app.ssh.locale`` (глобальный дефолт)
    3. ``None`` если глобальный дефолт пуст

    Returns:
        Строка локали (например ``ru_RU.UTF-8``), либо ``None`` если локаль
        экспортировать не нужно (пустая строка на любом уровне → ``None``).
    """
    if device is not None and device.locale is not None:
        loc = device.locale.strip()
        return loc if loc else None
    from src.config import config

    loc = (config.app.ssh.locale or "").strip()
    return loc if loc else None


def build_locale_prefix(device) -> str:
    """Формирует префикс ``export LANG=...; export LC_ALL=...; export LC_CTYPE=...; ``.

    Возвращает пустую строку, если локаль для устройства не задана (``None``)
    — в этом случае переменные окружения не экспортируются и предупреждение
    ``setlocale`` не возникает.

    Args:
        device: Устройство (``DeviceModel``) или ``None``.

    Returns:
        Префикс для подстановки перед текстом команды, либо ``""``.
    """
    loc = get_locale(device)
    if not loc:
        return ""
    return f"export LANG={loc}; export LC_ALL={loc}; export LC_CTYPE={loc}; "
