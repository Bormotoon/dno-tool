"""
Модуль конфигурации приложения DNO Tool.

Содержит классы данных для настроек SSH, сети и приложения в целом,
а также глобальный синглтон ``config`` для доступа к конфигурации.
Определяет пути к файлам настроек, хостов, команд и логов,
порты для сканирования и словарь иконок.
"""

import json
import math
import os
import re
import sys
import tempfile
import threading
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid4, uuid5
from dataclasses import asdict, dataclass, field
from os.path import expanduser
from pathlib import Path
from typing import Any, Dict

# Import logger with fallback for compatibility
try:
    from src.logger import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from src.config.web_ldap import WebLdapConfig, normalize_ldap_config


def get_asset_path(filename: str) -> str:
    """
    Получить абсолютный путь к ресурсному файлу в каталоге ``assets/``.

    При запуске из PyInstaller-пакета используется ``sys._MEIPASS``,
    иначе — корень проекта (на два уровня выше ``src/config/``).
    Для Linux-сборки в /opt внешние ассеты (zip иконок рядом с бинарником)
    проверяются первыми: если файл есть во внешнем ``assets/`` - отдать его.

    Args:
        filename: Имя файла в каталоге ``assets/``.

    Returns:
        Абсолютный путь к файлу ресурса.
    """
    if getattr(sys, "frozen", False):  # Если программа собрана в exe
        try:
            from src.config.icon_theme_loader import _external_assets_base
        except Exception:  # noqa: BLE001 - ранний импорт, откат на _MEIPASS
            _external_assets_base = None
        if _external_assets_base is not None:
            try:
                ext = _external_assets_base()
                if ext is not None and (ext / filename).exists():
                    return str(ext / filename)
            except Exception:  # noqa: BLE001
                pass
        base_path = Path(sys._MEIPASS)
    else:  # Если запуск как обычный Python-скрипт
        # Go up two levels from src/config/ to reach project root
        base_path = Path(__file__).parent.parent.parent
    return str(base_path / "assets" / filename)


# Resource paths configuration
_ICON_FILES: Dict[str, str] = {
    "folder": "folder.svg",
    "online": "online.svg",
    "offline": "offline.svg",
    "busy": "busy.svg",
    "scan_ping": "scan_ping.svg",
    "scan_analyze": "scan_analyze.svg",
    "default": "default.svg",
    "shield": "shield.svg",
    "command": "command.svg",
    "command_ssh": "command_ssh.svg",
    "command_sftp": "command_sftp.svg",
    "command_local": "command_local.svg",
    "command_desktop_session": "command_desktop_session.svg",
    "command_background": "command_background.svg",
    "result_success": "result_success.svg",
    "result_failure": "result_failure.svg",
    "result_cancelled": "result_cancelled.svg",
    "result_warning": "result_warning.svg",
    "result_pending": "result_pending.svg",
    "result_executing": "result_executing.svg",
    "result_connection_lost": "result_connection_lost.svg",
    "result_skip": "result_skip.svg",
    "key_exists": "key_exists.svg",
    "key_missing": "key_missing.svg",
    "menu_file": "menu_file.svg",
    "menu_save": "menu_save.svg",
    "menu_import": "menu_import.svg",
    "menu_export": "menu_export.svg",
    "menu_hosts": "menu_hosts.svg",
    "menu_commands": "command.svg",
    "menu_settings": "menu_settings.svg",
    "menu_tools": "menu_tools.svg",
    "menu_terminal": "menu_terminal.svg",
    "menu_sftp": "menu_sftp.svg",
    "menu_scanner": "menu_scanner.svg",
    "menu_import_classroom": "menu_import_classroom.svg",
    "menu_ping": "menu_ping.svg",
    "menu_info": "menu_info.svg",
    "menu_help": "menu_help.svg",
    "menu_delete": "menu_delete.svg",
    "menu_add_folder": "menu_add_folder.svg",
    "menu_add_command": "menu_add_command.svg",
    "menu_add_panel": "menu_add_panel.svg",
    "menu_exit": "menu_exit.svg",
    "menu_kde": "menu_kde.svg",
    "menu_recording": "menu_recording.svg",
    "menu_camera_view": "menu_camera_view.svg",
    "menu_camera_record": "menu_camera_record.svg",
    "menu_camera_stop": "menu_camera_stop.svg",
    "camera_view_list": "camera_view_list.svg",
    "camera_view_list_on": "camera_view_list_on.svg",
    "camera_view_1": "camera_view_1.svg",
    "camera_view_1_on": "camera_view_1_on.svg",
    "camera_view_4": "camera_view_4.svg",
    "camera_view_4_on": "camera_view_4_on.svg",
    "camera_view_6": "camera_view_6.svg",
    "camera_view_6_on": "camera_view_6_on.svg",
    "camera_view_6_focus": "camera_view_6_focus.svg",
    "camera_view_6_focus_on": "camera_view_6_focus_on.svg",
    "camera_view_9": "camera_view_9.svg",
    "camera_view_9_on": "camera_view_9_on.svg",
    "camera_view_16": "camera_view_16.svg",
    "camera_view_16_on": "camera_view_16_on.svg",
    "menu_credentials": "menu_credentials.svg",
    "menu_nodes": "menu_nodes.svg",
    "node_online": "node_online.svg",
    "node_offline": "node_offline.svg",
    "camera": "camera.svg",
    "menu_polkit": "menu_polkit.svg",
    "menu_veyon": "menu_veyon.svg",
    "menu_veyon_view": "menu_veyon_view.svg",
    "menu_results": "menu_results.svg",
    "menu_results_on": "menu_results_on.svg",
    "menu_list_toolbar": "menu_list_toolbar.svg",
    "menu_veyon_control": "menu_veyon_control.svg",
    "menu_vnc": "menu_vnc.svg",
    "menu_krfb": "menu_krfb.svg",
    "menu_diagnostics": "menu_diagnostics.svg",
    "menu_monitoring": "menu_monitoring.svg",
    "veyon_lock": "veyon_lock.svg",
    "veyon_unlock": "veyon_unlock.svg",
    "veyon_powerdown": "veyon_powerdown.svg",
    "veyon_reboot": "veyon_reboot.svg",
    "veyon_restart_service": "veyon_restart_service.svg",
    "veyon_logoff": "veyon_logoff.svg",
    "veyon_lock_input": "veyon_lock_input.svg",
    "veyon_unlock_input": "veyon_unlock_input.svg",
    "veyon_login": "veyon_login.svg",
    "veyon_website": "veyon_website.svg",
    "veyon_start_app": "veyon_start_app.svg",
    "veyon_message": "veyon_message.svg",
    "veyon_power_on": "veyon_power_on.svg",
    "toolbar_online_only": "toolbar_online_only.svg",
    "toolbar_online_only_on": "toolbar_online_only_on.svg",
    "toolbar_thumbnails": "toolbar_thumbnails.svg",
    "toolbar_thumbnails_on": "toolbar_thumbnails_on.svg",
    "toolbar_thumbnail_style": "toolbar_thumbnail_style.svg",
    "toolbar_thumbnail_style_on": "toolbar_thumbnail_style_on.svg",
    "toolbar_host_info": "toolbar_host_info.svg",
    "result_host_name": "result_host_name.svg",
    "result_host_path": "result_host_path.svg",
    "toolbar_expand_all": "toolbar_expand_all.svg",
    "toolbar_collapse_all": "toolbar_collapse_all.svg",
    "toolbar_nodes": "toolbar_nodes.svg",
    "toolbar_nodes_on": "toolbar_nodes_on.svg",
    "thumb_template": "thumb_template.svg",
    "veyon_down": "veyon_down.svg",
    "menu_folder": "folder.svg",
    "menu_import_host": "menu_import.svg",
    "menu_export_host": "menu_export.svg",
    "menu_up": "menu_up.svg",
    "menu_down": "menu_down.svg",
    "menu_back": "menu_back.svg",
    "menu_forward": "menu_forward.svg",
    "menu_home": "menu_home.svg",
    "menu_rename": "menu_rename.svg",
    "menu_new_file": "menu_new_file.svg",
    "menu_copy_path": "menu_copy_path.svg",
    "menu_copy_name": "menu_copy_name.svg",
    "icon": "icon.svg",
    "icon_on": "icon_on.svg",
    "menu_edit_path": "menu_edit_path.svg",
    "menu_edit_file": "menu_edit_file.svg",
    "menu_archive": "menu_archive.svg",
    "menu_extract": "menu_extract.svg",
    "menu_download": "menu_download.svg",
    "menu_upload": "menu_upload.svg",
    "menu_refresh": "menu_refresh.svg",
    "menu_add_ip_range": "menu_add_ip_range.svg",
    "menu_add_device": "menu_add_device.svg",
    "menu_check_all": "menu_check_all.svg",
    "menu_uncheck_all": "menu_uncheck_all.svg",
    "menu_hide": "menu_hide.svg",
    "menu_abort": "menu_abort.svg",
    "menu_sync": "menu_sync.svg",
    "menu_sort": "menu_sort.svg",
    "info_system": "info_system.svg",
    "info_devices": "info_devices.svg",
    "info_network": "info_network.svg",
    "info_users": "info_users.svg",
    "info_storage": "info_storage.svg",
    "info_computer": "info_computer.svg",
    "info_cpu": "info_cpu.svg",
    "info_memory": "info_memory.svg",
    "info_motherboard": "info_motherboard.svg",
    "info_gpu": "info_gpu.svg",
    "info_disk": "info_disk.svg",
    "info_usb": "info_usb.svg",
    "info_sound": "info_sound.svg",
    "info_optical": "info_optical.svg",
    "info_listening": "info_listening.svg",
    "info_connected": "info_connected.svg",
    "info_user": "info_user.svg",
    "info_groups": "info_groups.svg",
    "info_disk_space": "info_disk_space.svg",
    "menu_topology": "menu_topology.svg",
    "topo_network": "topo_network.svg",
    "topo_router": "topo_router.svg",
    "topo_host": "topo_host.svg",
    "topo_local": "topo_local.svg",
    "topo_unknown": "topo_unknown.svg",
    "topo_switch": "topo_switch.svg",
    "topo_ap": "topo_ap.svg",
    "topo_printer": "topo_printer.svg",
    "topo_camera": "topo_camera.svg",
    "topo_nas": "topo_nas.svg",
    "topo_phone": "topo_phone.svg",
    "topo_pc": "topo_pc.svg",
    "topo_linux": "topo_linux.svg",
    "topo_vm": "topo_vm.svg",
    "topo_web": "topo_web.svg",
    "topo_netdev": "topo_netdev.svg",
    "topo_projector": "topo_projector.svg",
    "topo_tv": "topo_tv.svg",
    "topo_media_player": "topo_media_player.svg",
    "topo_network_audio": "topo_network_audio.svg",
    "topo_smartphone": "topo_smartphone.svg",
    "topo_tablet": "topo_tablet.svg",
    "topo_mac": "topo_mac.svg",
    "topo_server": "topo_server.svg",
    "topo_thin_client": "topo_thin_client.svg",
    "topo_ups": "topo_ups.svg",
    "topo_pdu": "topo_pdu.svg",
    "topo_access_control": "topo_access_control.svg",
    "topo_intercom": "topo_intercom.svg",
    "topo_plc": "topo_plc.svg",
    "topo_smart_home": "topo_smart_home.svg",
    "topo_3d_printer": "topo_3d_printer.svg",
    "topo_interactive_panel": "topo_interactive_panel.svg",
    "menu_video_editor": "menu_video_editor.svg",
    "ve_import": "ve_import.svg",
    "ve_export": "ve_export.svg",
    "ve_add_timeline": "ve_add_timeline.svg",
    "ve_fit_project": "ve_fit_project.svg",
    "ve_fit_selection": "ve_fit_selection.svg",
    "ve_view_dual": "ve_view_dual.svg",
    "ve_view_dual_on": "ve_view_dual_on.svg",
    "ve_view_source": "ve_view_source.svg",
    "ve_view_source_on": "ve_view_source_on.svg",
    "ve_view_program": "ve_view_program.svg",
    "ve_view_program_on": "ve_view_program_on.svg",
    "ve_undo": "ve_undo.svg",
    "ve_redo": "ve_redo.svg",
    "ve_play": "ve_play.svg",
    "ve_pause": "ve_pause.svg",
    "ve_stop": "ve_stop.svg",
    "ve_prev": "ve_prev.svg",
    "ve_next": "ve_next.svg",
    "ve_to_start": "ve_to_start.svg",
    "ve_mark_in": "ve_mark_in.svg",
    "ve_mark_out": "ve_mark_out.svg",
    "ve_split": "ve_split.svg",
    "ve_select": "ve_select.svg",
    "ve_select_on": "ve_select_on.svg",
    "ve_blade": "ve_blade.svg",
    "ve_blade_on": "ve_blade_on.svg",
    "ve_snap": "ve_snap.svg",
    "ve_snap_on": "ve_snap_on.svg",
    "ve_ripple_delete": "ve_ripple_delete.svg",
    "ve_delete": "ve_delete.svg",
    "ve_cut_range": "ve_cut_range.svg",
    "ve_keep_range": "ve_keep_range.svg",
    "ve_remove_file": "ve_remove_file.svg",
    "ve_clear_list": "ve_clear_list.svg",
    "ve_volume": "ve_volume.svg",
    "ve_mute": "ve_mute.svg",
    "ve_add_text": "ve_add_text.svg",
    "ve_add_overlay": "ve_add_overlay.svg",
}


# Темы иконок: каждая тема - папка assets/icons/<id>/ с полным набором
# SVG (те же имена файлов). Манифест assets/icons/themes.json.
ICON_THEME_FALLBACK = "flat"
_ICON_THEMES_MANIFEST = "icons/themes.json"

# Текущая тема иконок (меняется через set_icon_theme).
_icon_theme_id: str = ICON_THEME_FALLBACK


def list_icon_themes() -> list:
    """Список тем иконок из манифеста. При битом манифесте - только flat."""
    manifest_path = get_asset_path(_ICON_THEMES_MANIFEST)
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return data
    except (OSError, ValueError) as e:
        logger.error(f"Темы иконок: не читается манифест {manifest_path}: {e}")
    return [{"id": ICON_THEME_FALLBACK, "name": "Flat"}]


def get_icon_theme() -> str:
    """Текущий id темы иконок."""
    return _icon_theme_id


def _themed_icon_path(filename: str, theme_id: str) -> str:
    """Путь к файлу иконки в теме с fallback на дефолтную тему."""
    candidate = get_asset_path(f"icons/{theme_id}/{filename}")
    if os.path.exists(candidate):
        return candidate
    if theme_id != ICON_THEME_FALLBACK:
        return get_asset_path(f"icons/{ICON_THEME_FALLBACK}/{filename}")
    return candidate


# Публичный алиас (используется вне config, например в web/API).
themed_icon_path = _themed_icon_path


# Свойства темы иконок (assets/icons/<id>/theme.json): оформительские
# настройки, не являющиеся иконками. Сейчас - секция "thumbnail" для
# рамки миниатюр Veyon:
#   {"thumbnail": {"viewBox": [x, y, w, h], "hole": [x, y, w, h],
#                  "overlay": "thumb_template.svg", "underlay": null}}
# viewBox/hole - геометрия оверлея в его координатах SVG (hole - отверстие
# экрана, куда вписывается скриншот); overlay - файл рамки поверх скриншота,
# underlay - картинка-подложка под скриншотом (png/jpg/svg из папки темы,
# null - без подложки). У тем без theme.json - пустой словарь, код
# использует встроенную геометрию старого шаблона.
_ICON_THEME_CONFIGS: Dict[str, dict] = {}


def get_themed_icon_bytes(filename: str, theme_id: str = "") -> bytes | None:
    """Байты файла иконки из zip темы (на лету) или папки (fallback).

    Порядок: <theme>.zip -> flat.zip -> папка темы -> папка flat.
    None = файл нигде не найден.
    """
    from src.config.icon_theme_loader import get_icon_bytes

    target = theme_id or _icon_theme_id
    return get_icon_bytes(filename, target, ICON_THEME_FALLBACK)


def get_icon_theme_config(theme_id: str = "") -> dict:
    """Словарь свойств темы иконок (кэш на тему, перечитывается при смене файлов - нет)."""
    target = theme_id or _icon_theme_id
    if target in _ICON_THEME_CONFIGS:
        return _ICON_THEME_CONFIGS[target]
    data: dict = {}
    try:
        from src.config.icon_theme_loader import get_theme_config

        data = get_theme_config(target)
    except Exception:  # noqa: BLE001 - лоадер недоступен, откат на файлы
        data = {}
    if not data:
        try:
            cfg_path = get_asset_path(f"icons/{target}/theme.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
        except (OSError, ValueError) as e:
            logger.error(f"Свойства темы иконок: не читается {target}/theme.json: {e}")
    _ICON_THEME_CONFIGS[target] = data
    return data


def get_thumbnail_layout(theme_id: str = "") -> dict:
    """Параметры рамки миниатюр для темы: viewBox, hole, overlay, underlay.

    Возвращает словарь с ключами viewBox/hole (списки из 4 чисел),
    overlay/underlay (имена файлов в папке темы или None). У тем без
    theme.json - геометрия встроенного старого шаблона (viewBox
    12 12 104x106, отверстие 18/26.125/92/51.75) и оверлей thumb_template.svg.
    Битые значения молча заменяются дефолтными.
    """
    default = {
        "viewBox": [12.0, 12.0, 104.0, 106.0],
        "hole": [18.0, 26.125, 92.0, 51.75],
        "overlay": "thumb_template.svg",
        "underlay": None,
    }
    cfg = get_icon_theme_config(theme_id)
    section = cfg.get("thumbnail")
    if not isinstance(section, dict):
        return default
    layout = dict(default)
    for key in ("viewBox", "hole"):
        val = section.get(key)
        if (
            isinstance(val, (list, tuple))
            and len(val) == 4
            and all(isinstance(v, (int, float)) for v in val)
            and val[2] > 0
            and val[3] > 0
        ):
            layout[key] = [float(v) for v in val]
    for key in ("overlay", "underlay"):
        val = section.get(key)
        if val is None:
            layout[key] = None
        elif isinstance(val, str) and val and "/" not in val and "\\" not in val:
            layout[key] = val
    return layout


def build_icons(theme_id: str) -> Dict[str, str]:
    """Словарь иконок (ключ -> абсолютный путь) для темы."""
    return {
        key: _themed_icon_path(filename, theme_id)
        for key, filename in _ICON_FILES.items()
    }


def set_icon_theme(theme_id: str) -> str:
    """Переключить тему иконок, обновив ICONS in-place. Вернуть итоговый id.

    Неизвестный id откатывается на fallback. Обновление in-place сохраняет
    ссылки модулей, сделавших ``from src.config import ICONS``.
    """
    global _icon_theme_id
    known = {t.get("id") for t in list_icon_themes() if isinstance(t, dict)}
    effective = theme_id if theme_id in known else ICON_THEME_FALLBACK
    _icon_theme_id = effective
    ICONS.clear()
    ICONS.update(build_icons(effective))
    try:
        from src.config.icon_theme_loader import warm_theme

        warm_theme(effective)
    except Exception:  # noqa: BLE001 - прогрев опционален, иконки уже резолвятся
        pass
    return effective


def remap_icon_path(path: str, theme_id: str = "") -> str:
    """Перемаппить путь иконки из assets/icons/ на текущую тему.

    Покрывает файлы как в корне icons/, так и в любой подпапке темы:
    возвращается путь к одноимённому файлу в целевой теме. Чужие пути
    (пользовательские иконки вне assets) возвращаются без изменений.
    """
    if not path:
        return path
    target = theme_id or _icon_theme_id
    filename = path.replace("\\", "/").split("/")[-1]
    if not filename:
        return path
    # Принадлежность assets/icons - по сегментам пути, без relpath: во
    # frozen путь темы (_MEIPASS, напр. C:) и корень assets (внешний,
    # напр. D:) могут лежать на разных дисках - os.path.relpath кидает
    # ValueError и путь ошибочно считался чужим (ремап молча не работал).
    parts = [p for p in re.split(r"[\\/]+", path) if p and p != "."]
    if parts and len(parts[0]) == 2 and parts[0].endswith(":"):
        parts = parts[1:]
    try:
        idx = parts.index("assets")
    except ValueError:
        return path
    tail = parts[idx:]
    if len(tail) < 2 or tail[1] != "icons":
        return path
    # rel-хвост после icons/: [тема/]файл
    rel_tail = tail[2:]
    if not rel_tail or ".." in rel_tail:
        return path
    # Fast-path: путь уже в папке целевой темы - вернуться без stat
    # (идемпотентный ремап при live-переключении темы обходит сотни
    # хостов, каждый stat на Windows дорог).
    if len(rel_tail) == 2 and rel_tail[0] == target:
        return path
    return _themed_icon_path(filename, target)


# Resource paths configuration (обновляется через set_icon_theme in-place).
ICONS: Dict[str, str] = build_icons(ICON_THEME_FALLBACK)

# Default ports (used if nothing stored in settings)
DEFAULT_PORTS = {
    21: True,
    22: True,
    23: False,
    53: True,
    80: True,
    88: False,
    123: False,
    135: False,
    139: False,
    389: False,
    443: True,
    445: False,
    3389: True,
    8080: True,
    8443: True,
    8888: False,
}

_ports_lock = threading.Lock()


class _PortsDict(dict):
    """
    Потокобезопасный словарь для портов сканирования.

    Делегирует мутабельные операции через ``RLock``, чтобы
    ``PORTS`` можно было безопасно обновлять из разных потоков.
    """

    def copy(self):
        with _ports_lock:
            return dict(self)

    def replace(self, ports):
        with _ports_lock:
            super().clear()
            super().update(ports)

    def __setitem__(self, key: int, value: bool) -> None:
        with _ports_lock:
            super().__setitem__(key, value)

    def __delitem__(self, key: int) -> None:
        with _ports_lock:
            super().__delitem__(key)

    def update(self, *args, **kwargs):
        with _ports_lock:
            super().update(*args, **kwargs)

    def clear(self):
        with _ports_lock:
            super().clear()


# Module-level PORTS dict (will be updated from saved config on load)
PORTS = _PortsDict(DEFAULT_PORTS)

config_folder_name = ".dno-tool"


DEFAULT_SETTINGS_FILE = (
    expanduser("~") + os.sep + config_folder_name + os.sep + "settings.json"
)  # 'settings.json'
DEFAULT_COMMANDS_FILE = (
    expanduser("~") + os.sep + config_folder_name + os.sep + "commands.json"
)  #'commands.json'
DEFAULT_HOSTS_FILE = (
    expanduser("~") + os.sep + config_folder_name + os.sep + "hosts.json"
)  #'hosts.json'
DEFAULT_SSH_PRIVATE_KEY_PATH = (
    expanduser("~") + os.sep + ".ssh" + os.sep + "id_ed25519"
)  #'~/.ssh/id_ed25519'
DEFAULT_SSH_PUBLIC_KEY_PATH = (
    expanduser("~") + os.sep + ".ssh" + os.sep + "id_ed25519.pub"
)  #'~/.ssh/id_ed25519.pub'
LOGS_PATH = expanduser("~") + os.sep + config_folder_name + os.sep  #'~/.dno-tool'

SSH_RECV_BUFFER_SIZE = 65536
# Чтение SSH: всё доступное, но не дольше среза, чтобы отмена, таймаут и ввод
# проверялись и при непрерывном выводе; без данных пауза растёт до максимума.
SSH_READ_SLICE_SEC = 0.05
SSH_IDLE_POLL_MIN_SEC = 0.01
SSH_IDLE_POLL_MAX_SEC = 0.1

# Вывод команды: начало и конец сохраняются, середина сверх лимита отбрасывается.
COMMAND_OUTPUT_HEAD_CHARS = 256 * 1024
COMMAND_OUTPUT_TAIL_CHARS = 16 * 1024 * 1024 - COMMAND_OUTPUT_HEAD_CHARS
# Живой вывод на хост: это ограниченное представление, полный результат
# приходит только в итоговом событии.
COMMAND_PROGRESS_INTERVAL_SEC = 0.2
COMMAND_PROGRESS_MAX_CHARS = 16 * 1024

try:
    configs_dir = os.path.dirname(DEFAULT_SETTINGS_FILE)
    if not os.path.exists(configs_dir):
        logger.info(f"Creating {configs_dir} directory")
        os.makedirs(configs_dir)
        from src.utils.filesystem.fs_utils import ensure_user_owned

        ensure_user_owned(configs_dir)
except Exception as e:
    logger.error(f"Error creating {configs_dir} directory: {e}")


@dataclass
class SSHConfig:
    """Настройки SSH-подключения к удалённым устройствам.

    Учётные данные (логин/пароль, в т.ч. для sudo) хранятся ТОЛЬКО в связках
    учётных данных (CredentialsConfig.bundles + DeviceModel.credential_bundle_iid).
    """

    port: int = 22  # SSH-порт
    strict_host_checking: bool = False  # Строгая проверка ключей хоста
    command_timeout: int = 30  # Таймаут выполнения команды (секунды)
    ssh_connect_timeout: int = 10  # Таймаут SSH-соединения (секунды)
    connect_timeout: int = 5  # TCP-таймаут для оффлайн-хостов (секунды)
    max_as_completed_timeout: int = (
        600  # Максимальный таймаут асинхронного выполнения (секунды)
    )
    # При non-root логине выполнять весь обычный SSH-скрипт через sudo и Bash.
    # Пароль берётся из связки учётных данных (per-host или глобальной).
    # Перекрывается per-device через `DeviceModel.use_sudo`.
    # См. src/workers/command/sudo_utils.py
    use_sudo: bool = False
    # Локаль, экспортируемая перед выполнением SSH-команд (LANG/LC_ALL/LC_CTYPE).
    # По умолчанию `ru_RU.UTF-8` для обратной совместимости с ROSA Linux. Пустая строка —
    # не экспортировать локаль (для систем, где ru_RU.UTF-8 не сгенерирована, устраняет
    # предупреждение `setlocale: LC_ALL: cannot change locale`). Перекрывается per-device
    # через `DeviceModel.locale`. См. src/workers/command/locale_utils.py
    locale: str = "ru_RU.UTF-8"


@dataclass
class NetworkConfig:
    """Сетевые настройки (пинг, потоки)."""

    ping_timeout: float = 2.0
    ping_interval: int = 30  # Интервал между пингами (секунды)
    thread_count: int = 8  # Общий лимит статусов и служебных операций (Qt/web/node)
    port_scan_timeout: float = 1.0  # Таймаут connect_ex при сканировании портов (сек)
    ping_timeout_ms: int = (
        300  # Таймаут ICMP-пинга: Windows -w (мс), Linux -W (сек = max(1, //1000))
    )
    scanner_thread_count: int = (
        32  # Количество потоков для сканера хостов (по умолчанию для ползунка)
    )
    scanner_ping_timeout_ms: int = 1000  # Таймаут ICMP только для сканера, 100..10000 мс
    scanner_ping_backend: str = "system"  # system | icmplib, только для сканера IP
    scanner_sort_column: int = (
        1  # Сканер: колонка сортировки (0=Имя, 1=IP, 2=Порты, 3=MAC, 4=Тип, 5=Состояние)
    )
    scanner_sort_order: int = (
        0  # Сканер: направление сортировки (0=ascending, 1=descending)
    )
    pinger_hosts: list = field(
        default_factory=lambda: ["8.8.8.8"]  # Пингер: история хостов (редактор-список)
    )
    pinger_checked: list = field(
        default_factory=list  # Пингер: отмеченные хосты (восстановление отметок)
    )
    pinger_colors: dict = field(
        default_factory=dict  # Пингер: цвет кривой по хосту (host -> #RRGGBB)
    )
    topology_subnets: str = field(
        default=""  # Карта сети: подсети для сканирования (по строкам CIDR)
    )
    topology_max_hosts: int = 254  # Устаревшее поле: сохраняется для совместимости, не ограничивает скан
    topology_max_addresses: int = 65536  # Устаревшее поле: сохраняется для совместимости, не ограничивает скан
    topology_discovery_mode: str = "icmp"  # icmp | icmp_tcp
    topology_tcp_ports: list = field(default_factory=lambda: [22, 80, 443])
    topology_threads: int = 32  # Карта сети: потоков ping-фазы
    topology_trace_threads: int = 8  # Карта сети: потоков traceroute-фазы
    topology_max_hops: int = 20  # Карта сети: лимит хопов traceroute
    topology_trace_timeout: int = 5000  # Карта сети: ожидание хопа, мс
    topology_trace_retries: int = 1  # Карта сети: повторы при молчунах (0 - выкл)
    topology_scan_found_subnets: bool = (
        False  # Карта сети: сканировать найденные подсети
    )
    topology_exact_routes: bool = (
        False  # Карта сети: трассировать каждый хост (точно, дольше)
    )
    topology_merge_unknown: bool = (
        False  # Карта сети: визуальная группировка независимых молчащих хопов
    )
    topology_node_names: dict = field(
        default_factory=dict  # Карта сети: переименованные узлы (id -> подпись)
    )
    topology_params_expanded: bool = True  # Карта сети: панель настроек раскрыта
    topology_layout: str = "circles"  # Карта сети: circles | rings
    topology_edge_style: str = "ants"  # Карта сети: линии (plain) | муравьи (ants)
    topology_spread: int = 100  # Карта сети: разброс 40-150%
    topology_node_style: str = "scheme"  # Карта сети: схема (scheme) | иконки (icons)

    def __post_init__(self) -> None:
        """Нормализовать настройки карты и сканера без записи файла при чтении."""
        from src.utils.network.topology_settings import normalize_settings
        normalize_settings(self)
        if self.scanner_ping_backend not in ("system", "icmplib"):
            self.scanner_ping_backend = "system"


@dataclass
class ExecutionConfig:
    """Shared command execution concurrency for Qt, web and node requests."""

    thread_count: int = 8


THREAD_POOL_RANGE = (1, 64)
# Шаги ползунков числа потоков. Прежние значения вне списка остаются допустимыми.
THREAD_COUNT_STEPS = (1, 2, 4, 8, 16, 32, 64)


def validate_thread_count(value: object) -> int:
    """Accept only integral pool limits in 1..64, never bool or coercion."""
    minimum, maximum = THREAD_POOL_RANGE
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Thread count must be an integer in {minimum}..{maximum}")
    return value


def load_thread_pool_settings(data: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Build detached canonical pool limits without changing or saving data.

    The new execution/thumbnail concurrency keys identify canonical documents.
    Without either key, network.thread_count is a legacy GUI limit and merges
    upward with old web limits. Explicit new keys always win, even if invalid
    (default + warning rather than fallback to a lower-priority legacy key).
    """
    sections: dict[str, dict[str, Any]] = {}
    for name in ("network", "execution", "thumbnails", "web"):
        section = data.get(name, {}) if isinstance(data, dict) else {}
        if not isinstance(section, dict):
            logger.warning(f"Invalid {name} pool settings section; using defaults")
            section = {}
        sections[name] = section
    network, execution, thumbnails, web = (
        sections[name] for name in ("network", "execution", "thumbnails", "web")
    )
    canonical = "thread_count" in execution or "thread_count" in thumbnails

    def legacy_max(candidates: list[tuple[dict[str, Any], str]], default: int) -> int:
        valid = []
        for section, key in candidates:
            if key not in section:
                continue
            try:
                valid.append(validate_thread_count(section[key]))
            except ValueError:
                logger.warning(f"Invalid legacy pool {key}; ignoring value")
        return max(valid, default=default)

    merged_status = legacy_max(
        [(network, "thread_count"), (network, "status_thread_count")]
        + [
            (web, key)
            for key in ("thread_count", "thumb_thread_count", "status_thread_count")
        ],
        8,
    )
    merged_commands = legacy_max(
        [(network, "thread_count"), (web, "command_thread_count")], 8
    )

    def explicit(section: str, key: str, fallback: int, default: int) -> int:
        if key not in sections[section]:
            return fallback
        try:
            return validate_thread_count(sections[section][key])
        except ValueError:
            logger.warning(f"Invalid {section}.{key}; using default")
            return default

    return {
        "network": {
            "thread_count": explicit("network", "thread_count", merged_status, 8)
            if canonical
            else merged_status
        },
        "execution": {
            "thread_count": explicit("execution", "thread_count", merged_commands, 8)
        },
        "thumbnails": {
            "thread_count": explicit("thumbnails", "thread_count", merged_status, 8)
        },
        "web": {"rfb_thread_count": explicit("web", "rfb_thread_count", 4, 4)},
    }


@dataclass
class ThumbnailResourceConfig:
    """Local shared encoded-cache and session-record budgets."""

    cache_bytes: int = 33_554_432
    cache_entries: int = 512
    max_session_records: int = 256
    max_thumbnail_dimension: int = 2048


@dataclass
class ThumbnailVncConfig:
    """Local read-only VNC capture budgets, independent of source selection."""

    max_sessions: int = 32
    memory_budget_bytes: int = 256_000_000
    capture_timeout_sec: float = 10.0
    poll_timeout_sec: float = 0.2
    auth_backoff_sec: float = 30.0
    max_image_variants: int = 4
    max_screen_pixels: int = 16_000_000


# Finite absolute safety bound for any configured thumbnail side maximum.
# Shared by the facade validator, HTTP endpoints and worker-side clamps.
ABSOLUTE_MAX_THUMBNAIL_DIMENSION = 4096

# Finite safety guards, deliberately distinct from operational defaults.
THUMBNAIL_RESOURCE_RANGES = {
    "cache_bytes": (1, 1024**3),
    "cache_entries": (1, 16384),
    "max_session_records": (1, 4096),
    "max_thumbnail_dimension": (1, 4096),
}
THUMBNAIL_VNC_RANGES = {
    "max_sessions": (1, 256),
    "memory_budget_bytes": (1, 4 * 1024**3),
    "capture_timeout_sec": (0.1, 120.0),
    "poll_timeout_sec": (0.01, 120.0),
    "auth_backoff_sec": (0.1, 300.0),
    "max_image_variants": (1, 32),
    "max_screen_pixels": (1, 64_000_000),
}


@dataclass
class ThumbnailConfig:
    """Canonical image and scheduling settings shared by thumbnail backends."""

    backend: str = "veyon"
    interval_sec: int = 5
    stagger_sec: float = 3.0  # Initial capture spread for Qt, web and node hosts
    quality: int = 75
    compression: int = 3
    width: int = 128
    format: str = "png"
    scale: int = 2
    style: str = "monitor"
    require_online: bool = True
    thread_count: int = 8  # Shared Qt/web/node capture workers; restart to apply
    resources: ThumbnailResourceConfig = field(default_factory=ThumbnailResourceConfig)
    vnc: ThumbnailVncConfig = field(default_factory=ThumbnailVncConfig)


_THUMBNAIL_INT_RANGES = {
    "thread_count": THREAD_POOL_RANGE,
    "interval_sec": (1, 60),
    "quality": (0, 100),
    "compression": (0, 9),
    "width": (1, 4096),
    "scale": (1, 4),
}

# Capture snapshots and all thumbnail/VNC writers share this reentrant lock.
# Direct integration saves must replace the dataclass while holding this lock.
THUMBNAIL_CONFIG_LOCK = threading.RLock()


def validate_thumbnail_backend(value: object, allow_none: bool = False) -> str | None:
    """Validate a backend without coercion; None optionally means inheritance."""
    if value is None and allow_none:
        return None
    if isinstance(value, str) and value in ("veyon", "vnc"):
        return value
    raise ValueError("thumbnail_backend must be 'veyon' or 'vnc'")


def normalize_thumbnail_backend(value: object, allow_none: bool = False) -> str | None:
    """Normalize invalid persisted backends to a safe default with a warning."""
    try:
        return validate_thumbnail_backend(value, allow_none=allow_none)
    except ValueError:
        logger.warning("Invalid thumbnail backend; using safe default")
        return None if allow_none else "veyon"


def validate_thumbnail_settings(data: dict) -> dict:
    """Validate partial thumbnail settings, rejecting unknown keys and coercion."""
    if not isinstance(data, dict):
        # ValueError is the shared API validation contract for all bad inputs.
        raise ValueError("Thumbnail settings must be a dictionary")  # noqa: TRY004
    known = ThumbnailConfig.__dataclass_fields__
    for key, value in data.items():
        if key not in known:
            raise ValueError("Unknown thumbnail setting")
        if key == "backend":
            validate_thumbnail_backend(value)
        elif key in ("resources", "vnc"):
            _validate_thumbnail_resource_group(key, value)
        elif key in _THUMBNAIL_INT_RANGES:
            minimum, maximum = _THUMBNAIL_INT_RANGES[key]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(
                    f"Thumbnail {key} must be an integer in {minimum}..{maximum}"
                )
        elif key == "require_online":
            if type(value) is not bool:
                raise ValueError("Thumbnail require_online must be a boolean")
        elif key == "stagger_sec":
            if type(value) not in (int, float) or not 0 <= value <= 30:
                raise ValueError("Thumbnail stagger_sec must be a number in 0..30")
        elif key == "format":
            if not isinstance(value, str) or value not in ("png", "jpeg"):
                raise ValueError("Thumbnail format must be 'png' or 'jpeg'")
        elif key == "style" and (
            not isinstance(value, str) or value not in ("monitor", "plain")
        ):
            raise ValueError("Thumbnail style must be 'monitor' or 'plain'")
    # Return detached groups so callers cannot mutate a validated snapshot.
    return {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in data.items()
    }


def _validate_thumbnail_resource_group(name: str, data: object) -> None:
    if not isinstance(data, dict):
        raise ValueError(f"Thumbnail {name} must be a dictionary")
    ranges = THUMBNAIL_RESOURCE_RANGES if name == "resources" else THUMBNAIL_VNC_RANGES
    for key, value in data.items():
        if key not in ranges:
            raise ValueError(f"Unknown thumbnail {name} setting")
        minimum, maximum = ranges[key]
        numeric = (
            type(value) in (int, float) if key.endswith("_sec") else type(value) is int
        )
        if not numeric or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"Thumbnail {name}.{key} must be in {minimum}..{maximum}")


def merge_thumbnail_settings(current: ThumbnailConfig, data: dict) -> ThumbnailConfig:
    """Merge a partial update and validate relationships on the complete snapshot."""
    partial = validate_thumbnail_settings(data)
    values = asdict(current)
    for key, value in partial.items():
        if key in ("resources", "vnc"):
            values[key].update(value)
        else:
            values[key] = value
    validate_thumbnail_settings(values)
    if values["width"] > values["resources"]["max_thumbnail_dimension"]:
        raise ValueError("Thumbnail width exceeds max_thumbnail_dimension")
    if values["vnc"]["max_sessions"] > values["resources"]["max_session_records"]:
        raise ValueError("VNC max_sessions exceeds max_session_records")
    if values["vnc"]["poll_timeout_sec"] > values["vnc"]["capture_timeout_sec"]:
        raise ValueError("VNC poll_timeout_sec exceeds capture_timeout_sec")
    values["resources"] = ThumbnailResourceConfig(**values["resources"])
    values["vnc"] = ThumbnailVncConfig(**values["vnc"])
    return ThumbnailConfig(**values)


def load_thumbnail_config(data: dict) -> ThumbnailConfig:
    """Migrate per key: thumbnails, legacy Veyon/web setting, default.

    Invalid explicitly supplied values use defaults, not lower-priority values.
    This only builds an in-memory configuration and never writes the source file.
    """
    if not isinstance(data, dict):
        logger.warning("Invalid thumbnail settings document; using defaults")
        return ThumbnailConfig()
    new = data.get("thumbnails", {})
    if not isinstance(new, dict):
        logger.warning("Invalid thumbnails section; migrating legacy settings")
        new = {}
    legacy = data.get("veyon", {})
    if not isinstance(legacy, dict):
        legacy = {}
    web = data.get("web", {})
    if not isinstance(web, dict):
        web = {}
    values = asdict(ThumbnailConfig())
    values["thread_count"] = load_thread_pool_settings(data)["thumbnails"][
        "thread_count"
    ]
    for key in values:
        if key == "thread_count":
            continue
        if key in ("resources", "vnc"):
            group = new.get(key, {})
            if not isinstance(group, dict):
                logger.warning(f"Invalid thumbnail {key} section; using defaults")
                continue
            for name, value in group.items():
                try:
                    _validate_thumbnail_resource_group(key, {name: value})
                    values[key][name] = value
                except ValueError:
                    logger.warning(f"Invalid thumbnail {key}.{name}; using default")
            continue
        if key in new:
            value = new[key]
        elif key == "stagger_sec" and "thumbnail_stagger_sec" in web:
            value = web["thumbnail_stagger_sec"]
        elif key != "backend" and f"thumbnail_{key}" in legacy:
            value = legacy[f"thumbnail_{key}"]
        else:
            continue
        try:
            values[key] = validate_thumbnail_settings({key: value})[key]
        except ValueError:
            logger.warning(f"Invalid thumbnail {key}; using default")
    for key in new:
        if key not in values:
            logger.warning("Ignoring unknown thumbnail setting")
    # On load, controlling limits win: clamp dependents, keeping valid neighbors.
    for group, dependent, limit in (
        (values, "width", values["resources"]["max_thumbnail_dimension"]),
        (values["vnc"], "max_sessions", values["resources"]["max_session_records"]),
        (values["vnc"], "poll_timeout_sec", values["vnc"]["capture_timeout_sec"]),
    ):
        if group[dependent] > limit:
            logger.warning(f"Conflicting thumbnail {dependent}; clamping to limit")
            group[dependent] = limit
    return merge_thumbnail_settings(ThumbnailConfig(), values)


# VNC-серверы, которые ставит мастер настройки на хосты.
VNC_SERVERS = ("kmsvnc", "x11vnc", "krfb")
# Сервер по умолчанию: kmsvnc работает и в X11, и в Wayland, показывает
# экран входа. NVIDIA захватывается через EGL DMA-BUF.
DEFAULT_VNC_SERVER = "kmsvnc"


@dataclass
class VeyonConfig:
    """Настройки интеграции Veyon WebAPI."""

    webapi_url: str = "http://localhost:11080/api/v1/"  # URL WebAPI сервера Veyon
    key_name: str = "teacher"  # Имя ключа аутентификации
    server_port: int = 11100  # Порт Veyon Server (RFB proxy) на удалённых хостах
    rfb_bridge_enabled: bool = (
        True  # Разрешить браузерный RFB-доступ к хостам через Veyon
    )
    vnc_port: int = 5900  # Общий VNC-порт (x11vnc, kmsvnc и krfb на хостах)
    vnc_password: str = ""  # VNC-пароль (глобальный; пусто = без пароля)
    # Выбранный VNC-сервер для режима VNC: один из VNC_SERVERS.
    # Определяет, какой сервер ставит мастер настройки, какой вьювер
    # открывает доступ (веб/кrdc) и какой пароль использует диагностика.
    vnc_server: str = DEFAULT_VNC_SERVER
    webapi_connection_limit: int = (
        32  # Лимит WebAPI/ConnectionLimit (степень 2: 32/64/128/256/512/1024)
    )
    feature_transport: str = "webapi"  # Транспорт фичей: "webapi" (PUT /feature/{uid}) | "cli" (veyon-cli subprocess)


@dataclass
class WebUser:
    """Учётная запись пользователя веб-сервера (многопользовательский режим).

    Если список ``WebServerConfig.users`` непуст — аутентификация идёт по нему,
    каждый пользователь видит только свои задания (owner = username).
    Если список пуст — fallback на единые username/password (как раньше),
    но вкладки браузера изолируются по session_id.
    """

    username: str
    password: str = ""


@dataclass
class CameraConfig:
    """Настройки интеграции камер видеонаблюдения ЕЦХД.

    Структура ``cameras`` — иерархия ``{address: {cabinet: {camera_name: ip}}}``
    (как в config.json echd_qt6). Адрес — IP/имя с необязательными портом
    и путём, например ``10.161.27.201:554/onvif1``.

    Учётные данные RTSP — связки ``bundles`` (как у SSH): связка камеры
    ``camera_bundles[iid камеры]`` → связка по умолчанию → без авторизации.
    """

    bundles: list = field(default_factory=list)  # list[CredentialBundle]
    default_bundle_iid: str = ""  # Связка по умолчанию для всех камер
    camera_bundles: dict = field(default_factory=dict)  # {iid камеры: iid связки}
    player: str = "vlc"  # vlc / mpv / ffplay / системный
    preview_backend: str = "ffmpeg"  # ffmpeg / qt — встроенный просмотр
    preview_transport: str = "tcp"  # Транспорт RTSP встроенного Qt-просмотра
    preview_decoding: str = "gpu"  # gpu / cpu; фиксируется при запуске приложения
    preview_fps: int = 25
    preview_resolution: str = "1920x1080"  # Потолок размера кадра, без увеличения исходника
    timeout: int = 2  # Таймаут TCP-пинга RTSP-порта (секунды)
    ping_interval: int = (
        20  # Интервал опроса камер (секунды) — default DEFAULT_PING_INTERVAL
    )
    output: str = ""  # Каталог записи (пусто = домашняя папка); те же %токены%
    path_mask: str = ""  # Шаблон пути: %addr%/%cabinet%/%camera%/%year%/%month%/%day%/%hour%/%min%/%sec%
    file_mask: str = ""  # Шаблон имени: те же %токены% + strftime (%Y_%m_%d__%H_%M_%S)
    max_record_hours: float = 0.0  # Авт-останов записи через N часов (0 = без лимита)
    min_free_gb: float = 1.0  # Минимум свободного места на диске для старта (ГБ)
    cameras: dict = field(default_factory=dict)
    camera_ids: dict = field(default_factory=dict)
    max_simultaneous: int = 32
    ping_workers: int = 8
    network_timeout: int = 10
    stale_timeout: int = 30
    reconnect_grace: int = 120  # Сколько секунд переподключаться внутри текущего файла
    record_video: str = "copy"  # copy (из потока) | h264 | h265
    record_encoder: str = "cpu"  # cpu | gpu (аппаратный, с откатом на cpu)
    record_crf: int = 23  # Качество при перекодировании (меньше = лучше)
    record_audio: str = "auto"  # auto (из потока) | aac | opus | flac | pcm | none
    record_preset: str = "fast"  # fast | balanced | quality — скорость кодера
    record_resolution: str = "source"  # source | потолок размера кадра WxH
    record_gop: int = 2  # Интервал ключевых кадров, секунды
    record_max_fps: int = 0  # Потолок частоты кадров (0 = как в потоке)
    record_video_bitrate: int = 0  # Целевой битрейт видео, кбит/с (0 = по CRF)
    record_audio_bitrate: int = 128  # Битрейт AAC/Opus, кбит/с
    record_audio_rate: int = 0  # Частота дискретизации, Гц (0 = как в потоке)
    record_audio_channels: int = 0  # 0 = как в потоке, 1 = моно, 2 = стерео
    offline_failures: int = 3
    retry_max: int = 60
    segment_minutes: int = 60
    segment_mb: int = 1024
    emergency_free_gb: float = 0.5


# Настройки прежних версий: в старых settings.json они остаются и не считаются ошибкой.
# Общие login/password заменены связками без переноса значений.
_REMOVED_CAMERA_SETTINGS = frozenset({"retention_days", "login", "password"})
_CAMERA_BUNDLE_KEYS = frozenset({"iid", "description", "login", "password"})


def _normalize_camera_bundles(source, invalid) -> list:
    """Проверить связки учётных данных камер; повтор iid — ошибка."""
    if not isinstance(source, list):
        invalid("Связки камер должны быть списком")
        return []
    result = []
    used = set()
    for entry in source:
        if isinstance(entry, CredentialBundle):
            entry = asdict(entry)
        if not isinstance(entry, dict) or set(entry) - _CAMERA_BUNDLE_KEYS:
            invalid("Некорректная связка учётных данных камер")
            continue
        values = {key: entry.get(key, "") for key in _CAMERA_BUNDLE_KEYS}
        if not all(isinstance(value, str) for value in values.values()):
            invalid("Поля связки учётных данных камер должны быть строками")
            continue
        iid = values["iid"].strip() or uuid4().hex
        if iid in used:
            invalid("Идентификатор связки камер повторяется")
            continue
        used.add(iid)
        result.append(
            CredentialBundle(
                iid=iid,
                description=values["description"].strip(),
                login=values["login"].strip(),
                password=values["password"],
            )
        )
    return result


def normalize_camera_config(data: dict, *, strict: bool = True) -> CameraConfig:
    """Проверить настройки камер; при чтении восстановить исправные поля."""
    defaults = CameraConfig()
    result = deepcopy(defaults)
    if not isinstance(data, dict):
        if strict:
            raise ValueError("Настройки камер должны быть объектом")
        return result

    def invalid(message: str) -> None:
        if strict:
            raise ValueError(message)
        logger.warning("Настройки камер: %s", message)

    unknown = set(data) - set(defaults.__dataclass_fields__) - _REMOVED_CAMERA_SETTINGS
    if unknown:
        invalid("Неизвестные поля настроек камер")
    for key in ("player", "output", "path_mask", "file_mask", "default_bundle_iid"):
        value = data.get(key, getattr(defaults, key))
        if not isinstance(value, str):
            invalid(f"Поле {key} должно быть строкой")
        else:
            setattr(result, key, value)
    backend = data.get("preview_backend", defaults.preview_backend)
    if not isinstance(backend, str) or backend not in ("ffmpeg", "qt"):
        invalid("Поле preview_backend: требуется ffmpeg или qt")
    else:
        result.preview_backend = backend
    transport = data.get("preview_transport", defaults.preview_transport)
    if not isinstance(transport, str) or transport not in ("tcp", "udp"):
        invalid("Поле preview_transport: требуется tcp или udp")
    else:
        result.preview_transport = transport
    for key, choices in (
        ("preview_decoding", ("gpu", "cpu")),
        ("preview_resolution", ("320x180", "640x360", "1280x720", "1920x1080")),
        ("preview_fps", (1, 3, 12, 25)),
        ("record_video", ("copy", "h264", "h265")),
        ("record_encoder", ("cpu", "gpu")),
        ("record_audio", ("auto", "aac", "opus", "flac", "pcm", "none")),
        ("record_preset", ("fast", "balanced", "quality")),
        (
            "record_resolution",
            ("source", "3840x2160", "2560x1440", "1920x1080", "1280x720", "854x480", "640x360"),
        ),
        ("record_audio_rate", (0, 16000, 24000, 32000, 44100, 48000)),
        ("record_audio_channels", (0, 1, 2)),
    ):
        value = data.get(key, getattr(defaults, key))
        expected_type = (
            int
            if key in ("preview_fps", "record_audio_rate", "record_audio_channels")
            else str
        )
        if type(value) is not expected_type or value not in choices:
            invalid(f"Поле {key}: допустимы {', '.join(map(str, choices))}")
        else:
            setattr(result, key, value)
    ranges = {
        "timeout": (1, 10),
        "ping_interval": (5, 300),
        "max_simultaneous": (1, 256),
        "ping_workers": (1, 32),
        "network_timeout": (1, 120),
        "stale_timeout": (5, 600),
        "reconnect_grace": (0, 3600),
        "record_crf": (15, 40),
        "record_gop": (1, 10),
        "record_max_fps": (0, 60),
        "record_video_bitrate": (0, 20000),
        "record_audio_bitrate": (32, 320),
        "offline_failures": (1, 10),
        "retry_max": (3, 600),
        "segment_minutes": (0, 1440),
        "segment_mb": (0, 1048576),
    }
    for key, (low, high) in ranges.items():
        value = data.get(key, getattr(defaults, key))
        if type(value) is not int or not low <= value <= high:
            invalid(f"Поле {key}: требуется целое число {low}..{high}")
            if not strict and type(value) is int:
                setattr(result, key, value)
        else:
            setattr(result, key, value)
    for key, high in (
        ("max_record_hours", 720),
        ("min_free_gb", 100000),
        ("emergency_free_gb", 100000),
    ):
        value = data.get(key, getattr(defaults, key))
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 <= value <= high
        ):
            invalid(f"Поле {key}: требуется число 0..{high}")
            if not strict and type(value) in (int, float) and math.isfinite(value):
                setattr(result, key, value)
        else:
            setattr(result, key, float(value))
    source = data.get("cameras", {})
    ids = data.get("camera_ids", {})
    if not isinstance(ids, dict):
        raise ValueError("Некорректный реестр идентификаторов камер")
    used = set()
    if not isinstance(source, dict):
        raise ValueError("Список камер должен быть объектом")
    for addr, cabs in source.items():
        if (
            not isinstance(addr, str)
            or not isinstance(cabs, dict)
        ):
            raise ValueError("Некорректный адрес в дереве камер")
        result.cameras[addr] = {}
        for cab, cams in cabs.items():
            if (
                not isinstance(cab, str)
                or not isinstance(cams, dict)
            ):
                raise ValueError("Некорректный кабинет в дереве камер")
            result.cameras[addr][cab] = {}
            for name, host in cams.items():
                if (
                    not isinstance(name, str)
                    or not isinstance(host, str)
                ):
                    raise ValueError("Некорректная камера в дереве")
                key = f"{addr}::{cab}::{name}"
                iid = ids.get(key)
                if (
                    not isinstance(iid, str)
                    or re.fullmatch(r"[0-9a-f]{32}", iid) is None
                ):
                    iid = uuid5(NAMESPACE_URL, "dno-tool/camera/" + key).hex
                if iid in used:
                    invalid("Идентификатор камеры повторяется")
                    iid = uuid4().hex
                used.add(iid)
                result.camera_ids[key] = iid
                result.cameras.setdefault(addr, {}).setdefault(cab, {})[name] = host
                if strict:
                    from src.domain.models.camera import CameraModel

                    model = CameraModel(
                        addr,
                        cab,
                        name,
                        host,
                        output_path=result.output,
                        path_mask=result.path_mask,
                        file_mask=result.file_mask,
                        iid=iid,
                    )
                    model.record_file()
    result.bundles = _normalize_camera_bundles(
        data.get("bundles", []), invalid
    )
    # Ссылки на удалённую связку сохраняются: при подключении камера
    # сообщит о необходимости заполнить данные для входа.
    links = data.get("camera_bundles", {})
    if not isinstance(links, dict):
        invalid("Связки камер по идентификаторам должны быть объектом")
        links = {}
    known = set(result.camera_ids.values())
    for camera_iid, bundle_iid in links.items():
        if not isinstance(camera_iid, str) or not isinstance(bundle_iid, str):
            invalid("Некорректная ссылка камеры на связку")
        elif camera_iid in known and bundle_iid:
            result.camera_bundles[camera_iid] = bundle_iid
    if strict:
        from src.domain.models.camera import CameraModel

        sample = CameraModel(
            "Адрес",
            "Кабинет",
            "Камера",
            "192.0.2.1",
            output_path=result.output,
            path_mask=result.path_mask,
            file_mask=result.file_mask,
        )
        sample.record_file()
    return result


@dataclass
class WebServerConfig:
    """Настройки встроенного веб-сервера управления."""

    enabled: bool = False  # Запускать ли сервер при старте приложения
    host: str = "0.0.0.0"  # Адрес привязки
    port: int = 8765  # Порт прослушивания
    public_url: str = ""  # Внешний HTTPS URL reverse proxy; пусто — прямой адрес
    # HTTPS самого сервера: off, generated (выпущен в настройках) или custom (свой).
    tls_mode: str = "off"
    tls_cert_path: str = ""  # Только для custom; файл перечитывается при запуске
    tls_key_path: str = ""  # Только для custom; ключ без пароля
    tls_names: list = field(default_factory=list)  # Имена для выпуска сертификата
    username: str = (
        "admin"  # Логин для базовой аутентификации (fallback, если users пуст)
    )
    password: str = ""  # Пароль (пустая строка = сгенерировать при первом запуске)
    users: list = field(
        default_factory=list
    )  # list[WebUser]; пусто = single-user fallback
    # Вход через каталог LDAP; пароль служебной записи на диске зашифрован.
    ldap: WebLdapConfig = field(default_factory=WebLdapConfig)
    # Время жизни завершённых заданий в реестре JobManager (минуты).
    # 0 = хранить вечно (до перезапуска сервера). Активные (running) задания
    # не удаляются по TTL — только finished/aborted/error.
    job_ttl_minutes: int = 30
    # Constructor compatibility only. Runtime pools use canonical sections;
    # Config.save() omits these legacy fields, ConfigService forwards aliases.
    thread_count: int = 8
    command_thread_count: int = 8
    rfb_thread_count: int = 4  # RFB bridge workers, 1..64; restart to apply


@dataclass
class CredentialBundle:
    """Связка учётных данных (логин/пароль) для SSH-подключений.

    Хранится в settings.json (CredentialsConfig.bundles). Пароль шифруется
    AES (src.utils.security.credential_crypto) при сохранении и расшифровывается при
    загрузке — в памяти всегда plaintext.

    iid — стабильный идентификатор (uuid4().hex), по нему ссылаются
    DeviceModel.credential_bundle_iid и CredentialsConfig.default_bundle_iid.
    """

    iid: str = ""
    description: str = ""
    login: str = ""
    password: str = ""  # plaintext в памяти; ciphertext (base64) в JSON


@dataclass
class CredentialsConfig:
    """Настройки связок учётных данных (login/password bundles).

    Связки хранятся в settings.json под ключом ``credentials``. Пароли
    шифруются при сохранении (Config.save) и расшифровываются при загрузке
    (Config.load). В памяти — всегда plaintext.

    ``default_bundle_iid`` — связка по умолчанию (глобальная). Per-host
    override — DeviceModel.credential_bundle_iid.
    """

    bundles: list = field(default_factory=list)  # list[CredentialBundle]
    default_bundle_iid: str = ""  # iid связки по умолчанию (глобальная)


@dataclass
class NodeRecord:
    """Нода федерации, к которой подключается эта копия.

    Хранится в settings.json под ``nodes.nodes``. Доступ односторонний:
    запись получается из ключа доступа удалённой ноды
    (src/utils/security/node_crypto.py). ``pubkey`` — открытый ключ TLS
    ноды, сверяется до отправки HTTP; ``secret`` — секрет из её ключа
    доступа (в памяти открыт, на диске зашифрован). Пустой ``secret``
    означает подключение служебным мастер-ключом.

    ``iid`` — стабильный идентификатор записи (uuid4().hex), присваивается
    при добавлении. Используется для связи секций дерева/списка с нодой.
    """

    iid: str = ""
    name: str = ""  # Отображаемое имя ноды (секция в дереве)
    host: str = ""  # IP/hostname
    port: int = 8801  # Порт NodeServer удалённой ноды
    pubkey: str = ""  # Открытый ключ TLS ноды (base64 raw 32 байта)
    secret: str = ""  # Секрет ключа доступа (base64); пусто = мастер-ключ
    # autoconnect — видимость/опрос у себя («Отображение» в настройках нод):
    # False = секции нет, клиент не запущен, опрос не идёт; запись сохраняется.
    autoconnect: bool = True


@dataclass
class NodesConfig:
    """Настройки федерации нод (связка инстансов dno-tool в единую сеть).

    Каждый инстанс может одновременно: (а) слушать входящие подключения
    других нод (сервер на ``listen_port``) и (б) подключаться к другим
    нодам из списка ``nodes``.

    ``private_key`` — служебный ключ Ed25519 сервера (подпись TLS), не
    показывается. ``access_secret`` — секрет ключа доступа к этой ноде:
    перегенерация отзывает доступ у всех, кому ключ был передан.
    Закрытая часть служебного мастер-ключа здесь не хранится: она живёт
    только в памяти ``NodeService``. Секреты на диске зашифрованы, в памяти
    открыты.
    ``instance_id`` — случайный идентификатор этой копии для сессий на
    удалённых нодах.
    """

    listen_enabled: bool = True  # Принимать подключения других нод (вкл. по умолчанию)
    listen_port: int = 8801  # Порт прослушивания NodeServer
    show_in_tree: bool = True  # Показывать секции нод в дереве устройств
    poll_interval_sec: int = 5  # Такт клиента; дерево проверяется каждые 5 тактов, статусы — отдельно
    private_key: str = ""  # Служебный ключ сервера (base64 raw 32 байта)
    access_secret: str = ""  # Секрет ключа доступа (base64 32 байта)
    instance_id: str = ""  # uuid4().hex этой копии
    nodes: list = field(default_factory=list)  # list[NodeRecord]


@dataclass
class CustomToolbarButton:
    """Кнопка пользовательской панели (ссылка на команду).

    Хранится в settings.json под ``custom_toolbar``. Привязка к команде
    по ``task_iid`` (стабильный iid задачи из commands.json).
    ``icon``: ``theme:<key>`` | ``pack:<basename>`` | ``color:<basename>`` |
    ``file:<путь>``.
    """

    iid: str = ""
    name: str = ""
    task_iid: str = ""
    icon: str = ""


@dataclass
class CustomToolbarPanel:
    """Именованная пользовательская панель (набор кнопок).

    ``icon_size`` - размер иконок кнопок всей панели в px (16..64,
    default 32 - как у обычных панелей). ``show_name`` - рисовать ли
    nameLabel с названием панели в начале тулбара.
    """

    iid: str = ""
    name: str = ""
    buttons: list = field(default_factory=list)  # list[CustomToolbarButton]
    icon_size: int = 32
    show_name: bool = True


@dataclass
class CustomToolbarConfig:
    """Конфиг пользовательских панелей (settings.json, ключ ``custom_toolbar``)."""

    panels: list = field(default_factory=list)  # list[CustomToolbarPanel]


def _custom_panel_icon_size(value) -> int:
    """Санитизация размера иконок панели: int в 16..64, иначе 32."""
    try:
        size = int(value)
    except (TypeError, ValueError):
        return 32
    return max(16, min(64, size))


@dataclass
class AppConfig:
    """Корневая конфигурация приложения."""

    ssh: SSHConfig = field(default_factory=SSHConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    veyon: VeyonConfig = field(default_factory=VeyonConfig)
    thumbnails: ThumbnailConfig = field(default_factory=ThumbnailConfig)
    web: WebServerConfig = field(default_factory=WebServerConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    credentials: CredentialsConfig = field(default_factory=CredentialsConfig)
    nodes: NodesConfig = field(default_factory=NodesConfig)
    custom_toolbar: CustomToolbarConfig = field(default_factory=CustomToolbarConfig)
    expand: bool = False
    kaomoji_enabled: bool = True
    theme: str = "github-dark"  # id темы оформления (assets/themes), system = нативная
    icon_theme: str = "flat"  # id темы иконок (assets/icons/<id>/)


class Config:
    """
    Менеджер конфигурации приложения.

    Загружает настройки из JSON-файла при инициализации и позволяет
    сохранять изменения обратно. Синглтон-экземпляр доступен как ``config``.
    """

    # Runtime-only generations never enter the settings JSON.
    _thumbnail_backend_revision: int = 0
    _thumbnail_image_revision: int = 0
    _vnc_password_revision: int = 0
    _vnc_port_revision: int = 0

    def __init__(self) -> None:
        self.app = AppConfig()
        self.config_file = DEFAULT_SETTINGS_FILE
        self.load()

    def load(self) -> None:
        """Загрузить конфигурацию из файла ``self.config_file``."""
        with THUMBNAIL_CONFIG_LOCK:
            from src.utils.filesystem.file_transaction import recover_file_transaction
            recover_file_transaction(
                os.path.join(os.path.dirname(self.config_file), ".setup-wizard-transaction.json"),
                (self.config_file, DEFAULT_HOSTS_FILE, DEFAULT_COMMANDS_FILE),
            )
            self._load_locked()

    def _load_locked(self) -> None:
        """Load settings while capture snapshots and writers are excluded."""
        self.camera_load_error: str | None = None
        self._settings_load_error: str | None = None
        self._camera_backup_source: bytes | None = None
        try:
            if os.path.exists(self.config_file):
                logger.info(f"Loading config from {self.config_file}")
                with open(self.config_file, "rb") as f:
                    original_bytes = f.read()
                    data = json.loads(original_bytes.decode("utf-8"))
                    self.app.thumbnails = load_thumbnail_config(data)
                    pool_settings = load_thread_pool_settings(data)
                    self.app.execution = ExecutionConfig(**pool_settings["execution"])
                    self.app.network.thread_count = pool_settings["network"][
                        "thread_count"
                    ]
                    self.app.web.rfb_thread_count = pool_settings["web"][
                        "rfb_thread_count"
                    ]
                    if "ssh" in data:
                        # Устаревшие ключи username/password (legacy учётные данные,
                        # теперь только связки) выбрасываются, чтобы SSHConfig(**…)
                        # не упал на неизвестном поле.
                        ssh_data = {
                            k: v
                            for k, v in dict(data["ssh"]).items()
                            if k not in ("username", "password")
                        }
                        self.app.ssh = SSHConfig(**ssh_data)
                    if "network" in data:
                        # Separate 'ports' (stored under network) from the rest before creating NetworkConfig
                        network_data = dict(data["network"])
                        ports = network_data.pop("ports", None)
                        network_data.pop("status_thread_count", None)
                        network_data.update(pool_settings["network"])
                        try:
                            self.app.network = NetworkConfig(**network_data)
                            # Санитизация pinger_hosts: только непустые строки
                            try:
                                ph = getattr(self.app.network, "pinger_hosts", None)
                                if not isinstance(ph, list):
                                    self.app.network.pinger_hosts = ["8.8.8.8"]
                                else:
                                    clean = [
                                        str(h).strip()
                                        for h in ph
                                        if str(h or "").strip()
                                    ]
                                    self.app.network.pinger_hosts = clean or ["8.8.8.8"]
                                # Санитизация pinger_checked: непустые строки,
                                # пересечение с pinger_hosts
                                pc = getattr(self.app.network, "pinger_checked", None)
                                if not isinstance(pc, list):
                                    self.app.network.pinger_checked = []
                                else:
                                    hosts_set = set(self.app.network.pinger_hosts)
                                    self.app.network.pinger_checked = [
                                        str(h).strip()
                                        for h in pc
                                        if str(h or "").strip()
                                        and str(h).strip() in hosts_set
                                    ]
                                # Санитизация pinger_colors: host -> #RRGGBB
                                pcolors = getattr(
                                    self.app.network, "pinger_colors", None
                                )
                                if not isinstance(pcolors, dict):
                                    self.app.network.pinger_colors = {}
                                else:
                                    hosts_set = set(self.app.network.pinger_hosts)
                                    clean_colors = {}
                                    import re as _re

                                    for h, c in pcolors.items():
                                        hs = str(h or "").strip()
                                        cs = str(c or "").strip()
                                        if hs in hosts_set and _re.fullmatch(
                                            r"#[0-9A-Fa-f]{6}", cs
                                        ):
                                            clean_colors[hs] = cs
                                    self.app.network.pinger_colors = clean_colors
                            except Exception:
                                pass
                        except Exception as e:
                            logger.error(f"Error loading network config: {e}")

                        # Load ports if present (stored under 'network' -> 'ports')
                        if ports is not None:
                            try:
                                loaded_ports = {}
                                for k, v in ports.items():
                                    try:
                                        loaded_ports[int(k)] = bool(v)
                                    except Exception:
                                        continue
                                PORTS.clear()
                                PORTS.update(loaded_ports)
                            except Exception as e:
                                logger.error(f"Error loading ports from config: {e}")
                    if "expand" in data:
                        self.app.expand = bool(data["expand"])
                    if "kaomoji_enabled" in data:
                        self.app.kaomoji_enabled = bool(data["kaomoji_enabled"])
                    if "theme" in data and isinstance(data["theme"], str):
                        self.app.theme = data["theme"] or "github-dark"
                    if "icon_theme" in data and isinstance(data["icon_theme"], str):
                        self.app.icon_theme = set_icon_theme(
                            data["icon_theme"] or ICON_THEME_FALLBACK
                        )
                    else:
                        set_icon_theme(self.app.icon_theme)
                    if "veyon" in data:
                        try:
                            veyon_data = dict(data["veyon"])
                            valid_fields = {
                                f.name
                                for f in __import__("dataclasses").fields(VeyonConfig)
                            }
                            veyon_data = {
                                k: v for k, v in veyon_data.items() if k in valid_fields
                            }
                            self.app.veyon = VeyonConfig(**veyon_data)
                        except Exception as e:
                            logger.error(f"Error loading veyon config: {e}")
                    if "web" in data:
                        try:
                            web_data = dict(data["web"])
                            for key in (
                                "thread_count",
                                "command_thread_count",
                                "thumb_thread_count",
                                "status_thread_count",
                            ):
                                web_data.pop(key, None)
                            web_data.update(pool_settings["web"])
                            # Миграция users: list[dict] -> list[WebUser]
                            users_data = web_data.pop("users", None)
                            if users_data is not None and isinstance(users_data, list):
                                web_data["users"] = [
                                    WebUser(**u) if isinstance(u, dict) else u
                                    for u in users_data
                                ]
                            # Миграция job_ttl_minutes: int (по умолчанию 30)
                            if "job_ttl_minutes" in web_data:
                                try:
                                    web_data["job_ttl_minutes"] = int(
                                        web_data["job_ttl_minutes"]
                                    )
                                except Exception:
                                    web_data.pop("job_ttl_minutes", None)
                            # Scheduling now belongs to the shared thumbnail section.
                            web_data.pop("thumbnail_stagger_sec", None)
                            from src.utils.network.urls import normalize_public_web_url

                            try:
                                web_data["public_url"] = normalize_public_web_url(web_data.get("public_url", ""))
                            except ValueError:
                                logger.warning("Некорректный внешний URL веб-сервера сброшен")
                                web_data["public_url"] = ""
                            if not isinstance(web_data.get("tls_names", []), list):
                                web_data.pop("tls_names", None)
                            web_data["ldap"] = self._load_web_ldap(web_data.pop("ldap", None))
                            self.app.web = WebServerConfig(**web_data)
                        except Exception as e:
                            logger.error(f"Error loading web config: {e}")
                    if "camera" in data:
                        try:
                            if not isinstance(data["camera"], dict):
                                raise ValueError("Настройки камер должны быть объектом")
                            camera_data = deepcopy(data["camera"])
                            from src.utils.security.credential_crypto import decrypt_password
                            plaintext = False
                            bundles_data = camera_data.get("bundles", [])
                            for bundle in bundles_data if isinstance(bundles_data, list) else ():
                                secret = bundle.get("password", "") if isinstance(bundle, dict) else ""
                                if isinstance(secret, str) and secret.startswith("enc:v1:"):
                                    bundle["password"] = decrypt_password(secret[7:], strict=True)
                                elif secret:
                                    plaintext = True
                            loaded_camera = normalize_camera_config(camera_data, strict=False)
                            if asdict(loaded_camera) != camera_data or plaintext:
                                self._camera_backup_source = original_bytes
                            self.app.camera = loaded_camera
                        except Exception as e:
                            self.camera_load_error = (
                                f"Не удалось загрузить настройки камер: {e}. "
                                "Сохранение заблокировано. Исходный файл сохранён."
                            )
                            logger.error(f"Error loading camera config: {e}")
                    if "credentials" in data:
                        try:
                            cred_data = dict(data["credentials"])
                            # Миграция bundles: list[dict] -> list[CredentialBundle]
                            bundles_data = cred_data.pop("bundles", None)
                            if bundles_data is not None and isinstance(
                                bundles_data, list
                            ):
                                from src.utils.security.credential_crypto import decrypt_password

                                bundles_list = []
                                for b in bundles_data:
                                    if not isinstance(b, dict):
                                        continue
                                    # Расшифровываем пароль из JSON
                                    pw = b.get("password", "")
                                    pw_plain = decrypt_password(pw) if pw else ""
                                    bundles_list.append(
                                        CredentialBundle(
                                            iid=b.get("iid", ""),
                                            description=b.get("description", ""),
                                            login=b.get("login", ""),
                                            password=pw_plain,
                                        )
                                    )
                                cred_data["bundles"] = bundles_list
                            self.app.credentials = CredentialsConfig(**cred_data)
                        except Exception as e:
                            logger.error(f"Error loading credentials config: {e}")
                    if "nodes" in data:
                        try:
                            nodes_data = dict(data["nodes"])
                            # Миграция: list[dict] -> list[NodeRecord]
                            recs_data = nodes_data.pop("nodes", None)
                            if recs_data is not None and isinstance(recs_data, list):
                                valid_fields = {
                                    f.name
                                    for f in __import__("dataclasses").fields(
                                        NodeRecord
                                    )
                                }
                                nodes_data["nodes"] = [
                                    NodeRecord(
                                        **{
                                            k: v
                                            for k, v in r.items()
                                            if k in valid_fields
                                        }
                                    )
                                    if isinstance(r, dict)
                                    else r
                                    for r in recs_data
                                ]
                            from src.utils.security.credential_crypto import (
                                decrypt_password,
                            )

                            valid_config = {
                                f.name
                                for f in __import__("dataclasses").fields(NodesConfig)
                            }
                            nodes_data = {
                                k: v for k, v in nodes_data.items() if k in valid_config
                            }
                            # Секреты на диске зашифрованы, в памяти открыты.
                            for key in ("access_secret",):
                                stored = nodes_data.get(key) or ""
                                nodes_data[key] = decrypt_password(stored) if stored else ""
                                if stored and not nodes_data[key]:
                                    logger.error(f"Nodes config: не удалось расшифровать {key}")
                            for rec in nodes_data.get("nodes") or []:
                                if isinstance(rec, NodeRecord) and rec.secret:
                                    rec.secret = decrypt_password(rec.secret)
                            self.app.nodes = NodesConfig(**nodes_data)
                        except Exception as e:
                            logger.error(f"Error loading nodes config: {e}")
                    if "custom_toolbar" in data:
                        try:
                            ct_data = dict(data["custom_toolbar"])
                            panels_data = ct_data.pop("panels", None)
                            if panels_data is not None and isinstance(
                                panels_data, list
                            ):
                                panels = []
                                for p in panels_data:
                                    if not isinstance(p, dict):
                                        continue
                                    btns = []
                                    for b in p.get("buttons", []) or []:
                                        if not isinstance(b, dict):
                                            continue
                                        btns.append(
                                            CustomToolbarButton(
                                                iid=str(b.get("iid", "") or ""),
                                                name=str(b.get("name", "") or ""),
                                                task_iid=str(
                                                    b.get("task_iid", "") or ""
                                                ),
                                                icon=str(b.get("icon", "") or ""),
                                            )
                                        )
                                    panels.append(
                                        CustomToolbarPanel(
                                            iid=str(p.get("iid", "") or ""),
                                            name=str(p.get("name", "") or ""),
                                            buttons=btns,
                                            icon_size=_custom_panel_icon_size(
                                                p.get("icon_size", 32)
                                            ),
                                            show_name=bool(p.get("show_name", True)),
                                        )
                                    )
                                ct_data["panels"] = panels
                            self.app.custom_toolbar = CustomToolbarConfig(**ct_data)
                        except Exception as e:
                            logger.error(f"Error loading custom_toolbar config: {e}")
                    # Миграция: старого ключа нет = пустой конфиг панелей.
        except Exception as e:
            self._settings_load_error = "Не удалось загрузить настройки; сохранение заблокировано"
            logger.error(f"Error loading config: {e}")

    @staticmethod
    def _load_web_ldap(raw: Any) -> WebLdapConfig:
        """Настройка LDAP из файла: испорченное поле не мешает остальным настройкам веба."""
        if not isinstance(raw, dict):
            return normalize_ldap_config(raw, strict=False)
        data = dict(raw)
        secret = data.get("bind_password", "")
        if isinstance(secret, str) and secret.startswith("enc:v1:"):
            from src.utils.security.credential_crypto import decrypt_password

            try:
                data["bind_password"] = decrypt_password(secret[7:], strict=True)
            except ValueError:
                logger.warning("Настройки LDAP: не удалось расшифровать пароль служебной записи")
                data["bind_password"] = ""
        return normalize_ldap_config(data, strict=False)

    def save(self) -> None:
        """Save canonical settings atomically; propagate failures without data loss."""
        with THUMBNAIL_CONFIG_LOCK:
            self._save_locked()

    def _save_locked(self) -> None:
        """Serialize a coherent configuration and atomically replace its file."""
        temporary_file = None
        try:
            if self._settings_load_error or self.camera_load_error:
                raise ValueError(self._settings_load_error or self.camera_load_error)
            validate_thread_count(self.app.network.thread_count)
            validate_thread_count(self.app.execution.thread_count)
            validate_thread_count(self.app.web.rfb_thread_count)
            data: dict[str, Any] = {
                "ssh": asdict(self.app.ssh),
                "network": asdict(self.app.network),
                "execution": asdict(self.app.execution),
                "veyon": asdict(self.app.veyon),
                "thumbnails": validate_thumbnail_settings(asdict(self.app.thumbnails)),
                "web": asdict(self.app.web),
                "camera": asdict(self.app.camera),
                "expand": self.app.expand,
                "kaomoji_enabled": self.app.kaomoji_enabled,
                "theme": self.app.theme,
                "icon_theme": self.app.icon_theme,
            }
            data["web"].pop("thread_count", None)
            from src.utils.security.credential_crypto import encrypt_password
            for bundle in data["camera"]["bundles"]:
                camera_secret = bundle.get("password", "")
                encrypted_camera_secret = encrypt_password(camera_secret)
                if camera_secret and not encrypted_camera_secret:
                    raise ValueError("Не удалось зашифровать пароль связки камер")
                bundle["password"] = (
                    "enc:v1:" + encrypted_camera_secret if camera_secret else ""
                )
            data["web"].pop("command_thread_count", None)
            ldap_secret = self.app.web.ldap.bind_password
            encrypted_ldap_secret = encrypt_password(ldap_secret)
            if ldap_secret and not encrypted_ldap_secret:
                raise ValueError("Не удалось зашифровать пароль служебной записи LDAP")
            data["web"]["ldap"]["bind_password"] = (
                "enc:v1:" + encrypted_ldap_secret if ldap_secret else ""
            )
            # Credentials: шифруем пароли перед сохранением
            try:
                from src.utils.security.credential_crypto import encrypt_password

                bundles_out = []
                for b in self.app.credentials.bundles or []:
                    if not isinstance(b, CredentialBundle):
                        continue
                    bundles_out.append(
                        {
                            "iid": b.iid,
                            "description": b.description,
                            "login": b.login,
                            "password": encrypt_password(b.password or ""),
                        }
                    )
                data["credentials"] = {
                    "bundles": bundles_out,
                    "default_bundle_iid": self.app.credentials.default_bundle_iid,
                }
            except Exception as e:
                logger.error(f"Error serializing credentials for save: {e}")
                raise
            # Nodes: asdict + нормализация списка NodeRecord
            try:
                nodes_out = asdict(self.app.nodes)
                nodes_out["nodes"] = [
                    asdict(r)
                    if isinstance(r, NodeRecord)
                    else (r if isinstance(r, dict) else None)
                    for r in (self.app.nodes.nodes or [])
                ]
                nodes_out["nodes"] = [r for r in nodes_out["nodes"] if r]
                from src.utils.security.credential_crypto import encrypt_password

                for key in ("access_secret",):
                    nodes_out[key] = encrypt_password(nodes_out.get(key) or "")
                for rec in nodes_out["nodes"]:
                    rec["secret"] = encrypt_password(rec.get("secret") or "")
                data["nodes"] = nodes_out
            except Exception as e:
                logger.error(f"Error serializing nodes for save: {e}")
                raise
            # Custom toolbars: plain dicts (iid/name/task_iid/icon)
            try:
                data["custom_toolbar"] = asdict(self.app.custom_toolbar)
            except Exception as e:
                logger.error(f"Error serializing custom_toolbar for save: {e}")
                raise
            # Save ports under network -> ports (keys as strings for JSON)
            try:
                data["network"]["ports"] = {str(k): v for k, v in PORTS.items()}
            except Exception as e:
                logger.error(f"Error serializing ports for save: {e}")
                raise
            logger.info(f"Saving config to {self.config_file}")
            from src.utils.filesystem.fs_utils import ensure_user_owned

            directory = os.path.dirname(os.path.abspath(self.config_file))
            if not os.path.exists(directory):
                os.makedirs(directory, exist_ok=True)
                ensure_user_owned(directory)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=directory,
                prefix=".settings-",
                suffix=".tmp",
                delete=False,
            ) as f:
                temporary_file = f.name
                json.dump(data, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            ensure_user_owned(temporary_file)
            if self._camera_backup_source is not None:
                backup_file = f"{self.config_file}.camera-backup-{uuid4().hex}.bak"
                try:
                    with open(backup_file, "xb") as backup:
                        backup.write(self._camera_backup_source)
                        backup.flush()
                        os.fsync(backup.fileno())
                    ensure_user_owned(backup_file)
                except Exception:
                    if os.path.exists(backup_file):
                        os.unlink(backup_file)
                    raise
            os.replace(temporary_file, self.config_file)
            temporary_file = None
            self._camera_backup_source = None
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            raise
        finally:
            if temporary_file is not None:
                try:
                    os.unlink(temporary_file)
                except OSError:
                    logger.warning("Could not remove temporary settings file")


# Глобальный экземпляр конфигурации
config: Config = Config()
