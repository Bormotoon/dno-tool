"""
Command Parameter Replacer - Единая точка замены параметров в командах.

Использование:
    from src.domain.utils.command_params import CommandParamReplacer

    # Для подстановки устройства
    replacer = CommandParamReplacer()
    text = replacer.replace_for_device(command_text, device)

    # Для подстановки custom-параметров
    text = replacer.replace_custom(command_text, {"CUSTOM": "value"})

    # Комбинированная замена
    text = replacer.replace(command_text, device, custom_params)
"""

import base64
import ntpath
import re
from datetime import datetime

from src.config import config


class CommandParamReplacer:
    """
    Утилита для замены плейсхолдеров в командах.

    Поддерживаемые плейсхолдеры устройства:
    - %name% - имя устройства (псевдоним)
    - %login%, %user%, %username% - логин устройства
    - %hostname%, %host%, %ip% - хост устройства
    - %port% - порт устройства или порт по умолчанию
    - %password%, %pass% - пароль устройства
    - %date% - текущая дата (YYYY.MM.DD)
    - %time% - текущее время (HHMMSS)
    - %timestamp% - дата и время (YYYY.MM.DD_HHMMSS)
    - %mac% - MAC адрес устройства
    - %sudo% - префикс `sudo ` если для устройства включено use_sudo, иначе пусто

    Custom плейсхолдеры заменяются из переданного словаря.
    Значения вставляются буквально, без повторного раскрытия в том же проходе.
    %paramN:base64% передаёт UTF-8 значение в base64 для безопасного аргумента shell.
    """

    @staticmethod
    def _replace_tokens(text: str, replacements: dict[str, str]) -> str:
        """Заменить только токены исходного текста, не повторно их значения."""
        if not replacements:
            return text
        pattern = "|".join(re.escape(token) for token in replacements)
        return re.sub(pattern, lambda match: replacements[match.group(0)], text)

    @staticmethod
    def _get_credentials(device) -> tuple[str, str | None]:
        """
        Получить учётные данные из связок учётных данных.

        Приоритет:
        0. Связки учётных данных (device.credential_bundle_iid → default_bundle)
        1. Значение по умолчанию ('root')

        Returns:
            (username, password)
        """
        # Приоритет 0: связки учётных данных
        bundle_login = None
        bundle_password = None
        try:
            from src.di import get_container
            from src.services.configuration.credential_service import CredentialService

            svc = get_container().resolve_optional(CredentialService)
            if svc is not None:
                bundle_login, bundle_password = svc.resolve(device)
        except Exception:
            pass

        username = (bundle_login or "").strip() or "root"
        password = bundle_password or None

        return username, password

    def replace_for_device(self, text: str, device) -> str:
        """
        Замена плейсхолдеров устройства в тексте.

        Args:
            text: Текст с плейсхолдерами
            device: Устройство для подстановки значений

        Returns:
            Текст с заменёнными плейсхолдерами устройства
        """
        if device is None:
            return text

        username, password = self._get_credentials(device)
        now = datetime.now()

        # %sudo%: префикс для команд, требующих root. PTY-expect в SSHWorker
        # автоматически подаёт пароль при появлении prompt sudo.
        from src.workers.command.sudo_utils import should_use_sudo

        sudo_prefix = "sudo " if should_use_sudo(device) else ""

        replacements = {
            "%name%": device.name,
            "%login%": username,
            "%user%": username,
            "%username%": username,
            "%hostname%": device.host,
            "%host%": device.host,
            "%ip%": device.host,
            "%port%": str(device.port or config.app.ssh.port or 22),
            "%password%": password or "",
            "%pass%": password or "",
            "%date%": now.strftime("%Y.%m.%d"),
            "%time%": now.strftime("%H%M%S"),
            "%timestamp%": now.strftime("%Y.%m.%d_%H%M%S"),
            "%mac%": device.mac_address or "unknown",
            "%sudo%": sudo_prefix,
        }

        return self._replace_tokens(text, replacements)

    def replace_custom(self, text: str, params: dict[str, object]) -> str:
        """
        Замена custom-параметров в тексте.

        Args:
            text: Текст с плейсхолдерами
            params: Словарь параметров (ключ без %, значение - подстановка)

        Returns:
            Текст с заменёнными custom-параметрами
        """
        if not params:
            return text

        replacements = {
            f"%{param_name}%": str(param_value)
            for param_name, param_value in params.items()
        }
        replacements.update(
            {
                f"%{name}:base64%": base64.b64encode(str(value).encode("utf-8")).decode(
                    "ascii"
                )
                for name, value in params.items()
            }
        )
        for name, value in params.items():
            filename = ntpath.basename(str(value))
            replacements[f"%{name}:filename%"] = filename
            replacements[f"%{name}:filename:base64%"] = base64.b64encode(
                filename.encode("utf-8")
            ).decode("ascii")
        return self._replace_tokens(text, replacements)

    def replace(
        self, text: str, device=None, custom_params: dict[str, object] | None = None
    ) -> str:
        """
        Комбинированная замена: custom-параметры + устройство.

        Порядок замены:
        1. Custom-параметры, без каскадного раскрытия custom-значений
        2. Плейсхолдеры устройства, включая токены внутри custom-значений

        Args:
            text: Текст с плейсхолдерами
            device: Устройство для подстановки (опционально)
            custom_params: Custom-параметры (опционально)

        Returns:
            Текст с заменёнными всеми параметрами
        """
        result = text

        # Сначала custom-параметры
        if custom_params:
            result = self.replace_custom(result, custom_params)

        # Затем устройство
        if device:
            result = self.replace_for_device(result, device)

        return result


# Глобальный экземпляр для удобства
command_param_replacer = CommandParamReplacer()
