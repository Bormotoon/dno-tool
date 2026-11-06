"""Эвристическая классификация типа оборудования по совокупности признаков.

Общая классификация для столбца «Тип» в сканере хостов. Используется
двухступенчатая evidence-based модель:

  Ступень 1 — категория устройства (роутер, принтер, NAS, камера, телефон…).
  Сигналы: MAC OUI, категорийные баннеры (Dropbear/embedded-HTTP/вендоры),
  портовые сигнатуры, hostname, TTL≥129 (слабый сигнал «сетевого железа»).

  Ступень 2 — ОС-уточнение (Windows/Linux): TTL (~128 → Windows, ≤64 →
  Unix-семейство), NetBIOS-имя (Windows/Samba), OpenSSH/IIS-баннеры,
  hostname-паттерны Linux. Применяется только если категория не определена
  или обобщённая («Компьютер», «Сетевое устройство», «Веб-устройство»).

Семантика: TTL/NetBIOS/SSH определяют ОС, а не категорию — роутер на
OpenWRT (TTL=64, Dropbear) остаётся «Роутером», NAS с SMB/NFS — «NAS»,
и только для нераспознанной категории TTL решает Windows/Linux.

Веса сигналов: OUI (3.0) > баннеры/TTL/категорийные порты (2.0) >
NetBIOS/обобщённые порты (1.5) > hostname/TTL≥129 (1.0). Побеждает тип с
максимальной суммой весов (при равенстве — приоритет из ``_TIE_PRIORITY``).

Портовые сигнатуры опциональны (чекбокс «Учитывать портовые сигнатуры»).
Для точек доступа дополнительно проверяется сочетание портов 22/80/443/11100
и баннер SSH (см. ``classify_ap_by_ports_and_banner``) — вызывающий код
применяет его до evidence-модели.

Публичный API: ``classify_device`` (только тип, обратно совместим) и
``classify_device_detailed`` (тип + причина для tooltip).
"""

from __future__ import annotations

import re
from typing import Collection, Mapping, Optional

# ---------------------------------------------------------------------------
# Канонические метки типов (публичный API)
# ---------------------------------------------------------------------------
TYPE_UNKNOWN = "Неизвестно"
TYPE_ROUTER = "Роутер"
TYPE_AP = "Точка доступа"
TYPE_AP_CISCO = "Точка доступа (Cisco)"
TYPE_AP_HUAWEI = "Точка доступа (Huawei)"
TYPE_SWITCH = "Коммутатор"
TYPE_PRINTER = "Принтер"
TYPE_CAMERA = "Камера / DVR"
TYPE_NAS = "NAS / Хранилище"
TYPE_WINDOWS = "Компьютер (Windows)"
TYPE_LINUX = "Компьютер (Linux)"
TYPE_IP_PHONE = "IP-телефон"
TYPE_COMPUTER = "Компьютер"
TYPE_WEB_DEVICE = "Веб-устройство"
TYPE_NETWORK_DEVICE = "Сетевое устройство"
TYPE_VM = "Виртуальная машина"
TYPE_INTERACTIVE_PANEL = "Интерактивная панель"
TYPE_PROJECTOR = "Проектор"
TYPE_TV = "Телевизор"
TYPE_MEDIA_PLAYER = "Медиаплеер / ТВ-приставка"
TYPE_NETWORK_AUDIO = "Сетевое аудио"
TYPE_SMARTPHONE = "Смартфон"
TYPE_TABLET = "Планшет"
TYPE_MAC = "Компьютер (macOS)"
TYPE_SERVER = "Сервер"
TYPE_THIN_CLIENT = "Тонкий клиент"
TYPE_UPS = "ИБП"
TYPE_PDU = "Блок распределения питания (PDU)"
TYPE_ACCESS_CONTROL = "Контроллер СКУД"
TYPE_INTERCOM = "IP-домофон"
TYPE_PLC = "Промышленный контроллер (PLC)"
TYPE_SMART_HOME = "Контроллер умного дома / IoT"
TYPE_3D_PRINTER = "3D-принтер"

# ---------------------------------------------------------------------------
# Иконки типов (ключи ICONS, файлы topo_*.svg без подложки).
# Используют сканер хостов (строки результатов) и карта сети (узлы).
# Камера едина для сканера, карты сети и инструмента камер.
# ---------------------------------------------------------------------------
TYPE_ICON_KEYS = {
    TYPE_ROUTER: "topo_router",
    TYPE_AP: "topo_ap",
    TYPE_AP_CISCO: "topo_ap",
    TYPE_AP_HUAWEI: "topo_ap",
    TYPE_SWITCH: "topo_switch",
    TYPE_PRINTER: "topo_printer",
    TYPE_CAMERA: "topo_camera",
    TYPE_NAS: "topo_nas",
    TYPE_IP_PHONE: "topo_phone",
    TYPE_COMPUTER: "topo_host",
    TYPE_WINDOWS: "topo_pc",
    TYPE_LINUX: "topo_linux",
    TYPE_VM: "topo_vm",
    TYPE_INTERACTIVE_PANEL: "topo_interactive_panel",
    TYPE_PROJECTOR: "topo_projector",
    TYPE_TV: "topo_tv",
    TYPE_MEDIA_PLAYER: "topo_media_player",
    TYPE_NETWORK_AUDIO: "topo_network_audio",
    TYPE_SMARTPHONE: "topo_smartphone",
    TYPE_TABLET: "topo_tablet",
    TYPE_MAC: "topo_mac",
    TYPE_SERVER: "topo_server",
    TYPE_THIN_CLIENT: "topo_thin_client",
    TYPE_UPS: "topo_ups",
    TYPE_PDU: "topo_pdu",
    TYPE_ACCESS_CONTROL: "topo_access_control",
    TYPE_INTERCOM: "topo_intercom",
    TYPE_PLC: "topo_plc",
    TYPE_SMART_HOME: "topo_smart_home",
    TYPE_3D_PRINTER: "topo_3d_printer",
    TYPE_WEB_DEVICE: "topo_web",
    TYPE_NETWORK_DEVICE: "topo_netdev",
    TYPE_UNKNOWN: "topo_unknown",
}


def device_type_icon_key(device_type: str | None) -> str:
    """Ключ ICONS; для пустой или незнакомой метки возвращается пустая строка."""
    if not device_type:
        return ""
    return TYPE_ICON_KEYS.get(device_type.strip(), "")


def scanner_state_icon_key(
    is_online: bool, state: str = "done", device_type: str = ""
) -> str:
    """Единый значок этапа и типа устройства для строк Qt и веб-сканера."""
    if state == "scan":
        return "scan_ping"
    if state == "scan2":
        return "scan_analyze"
    if is_online and state == "done":
        type_key = device_type_icon_key(device_type)
        if type_key:
            return type_key
    return "online" if is_online else "offline"


# ---------------------------------------------------------------------------
# Портовые сигнатуры (используются только если use_port_signatures=True)
# ---------------------------------------------------------------------------
_PRINTER_PORTS = {515, 631, 9100, 9101, 9102, 9110}
_CAMERA_PORTS = {554, 8554, 8000, 8001, 37777, 37778, 8899, 10554}
_NAS_PORTS = {2049, 5000, 5001}
_IP_PHONE_PORTS = {5060, 5061, 1720}
_WINDOWS_PORTS = {135, 139, 445, 3389}
_SNMP_PORT = 161
_DNS_PORT = 53
_TELNET_PORT = 23
_SSH_PORT = 22
_HTTP_PORTS = {80, 8080, 8888, 8443, 443}
_MIKROTIK_PORTS = {8291, 8728, 8729}
_VNC_PORT = 5900
_VEYON_PORT = 11100

# Проверено по реестру Wireshark manuf и документации производителей.
# Общие OUI (APC, Siemens, Roku) дают тип только вместе с другими признаками.
# Источники и ограничения: docs/device-classifier-equipment.md.
_EQUIPMENT_MAC_HINTS = {
    "00:0E:58": ("Sonos", TYPE_NETWORK_AUDIO, {1400, 1443}),
    "38:42:0B": ("Sonos", TYPE_NETWORK_AUDIO, {1400, 1443}),
    "54:2A:1B": ("Sonos", TYPE_NETWORK_AUDIO, {1400, 1443}),
    "74:CA:60": ("Sonos", TYPE_NETWORK_AUDIO, {1400, 1443}),
    "78:28:CA": ("Sonos", TYPE_NETWORK_AUDIO, {1400, 1443}),
    "B8:E9:37": ("Sonos", TYPE_NETWORK_AUDIO, {1400, 1443}),
    "C4:38:75": ("Sonos", TYPE_NETWORK_AUDIO, {1400, 1443}),
    "F0:F6:C1": ("Sonos", TYPE_NETWORK_AUDIO, {1400, 1443}),
    "F8:5C:24": ("Sonos", TYPE_NETWORK_AUDIO, {1400, 1443}),
    "84:00:EC": ("Shelly", TYPE_SMART_HOME, {80}),
    "08:00:06": ("Siemens", TYPE_PLC, {102, 502}),
    "00:0E:8C": ("Siemens", TYPE_PLC, {102, 502}),
}
_AMBIGUOUS_MAC_HINTS = {
    "00:C0:B7": "APC (ИБП/PDU/другое оборудование)",
    "28:29:86": "APC (ИБП/PDU/другое оборудование)",
    "D4:E2:2F": "Roku (телевизор или медиаплеер)",
    "B0:A7:37": "Roku (телевизор или медиаплеер)",
    "00:0D:5D": "Raritan (PDU/KVM/другое оборудование)",
}
_EQUIPMENT_PORT_HINTS = {
    102: "TCP/102: ISO-on-TCP/S7 (PLC/HMI/сервер)",
    502: "TCP/502: Modbus (контроллер/шлюз/счётчик/ИБП)",
    548: "TCP/548: AFP (Mac/NAS/сервер)",
    1400: "TCP/1400: возможен Sonos",
    3493: "TCP/3493: возможен NUT (сервер мониторинга ИБП/PDU)",
    4352: "TCP/4352: возможен PJLink (проектор/дисплей)",
    4370: "TCP/4370: возможен ZKTeco (СКУД/учёт посещаемости)",
    5000: "TCP/5000: DSM/OctoPrint/другой веб-сервис",
    7000: "TCP/7000: возможен AirPlay (Mac/ТВ/аудио)",
    8060: "TCP/8060: возможен Roku ECP (ТВ/медиаплеер)",
    8123: "TCP/8123: возможен Home Assistant",
}

# Каждая сигнатура: (тип, множество портов, минимальный порог совпадений).
# Если несколько сигнатур совпадают, выигрывает та, у которой больше доля
# совпавших портов от её сигнатуры (а при равенстве — больше абсолютных
# совпадений). Это позволяет NVR с SMB (554+445) остаться «Камера», а Windows
# с RTSP-прокси (445+554) — «Компьютер (Windows)» (445 входит в обе, но
# _WINDOWS_PORTS шире и даёт больший score).
_PORT_SIGNATURES: list[tuple[str, frozenset[int], int]] = [
    (TYPE_PRINTER, frozenset(_PRINTER_PORTS), 1),
    (TYPE_CAMERA, frozenset(_CAMERA_PORTS), 1),
    (TYPE_IP_PHONE, frozenset(_IP_PHONE_PORTS), 1),
    (TYPE_WINDOWS, frozenset(_WINDOWS_PORTS), 1),
]

# Явные роли и названия продуктов: общие бренды и сетевые порты сюда не входят.
# Границы слов учитывают дефисы, подчёркивания и номера кабинетов/устройств.
# Эти правила общие для hostname, HTTP Server и SNMP sysDescr.
_EQUIPMENT_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"3d[-_ ]?printer|octoprint|prusalink|bambu[-_ ]?lab", re.I), TYPE_3D_PRINTER),
    (re.compile(r"interactive[-_ ]?(?:panel|display|board)|интерактивн\w*[-_ ]панел", re.I), TYPE_INTERACTIVE_PANEL),
    (re.compile(r"projector|проектор", re.I), TYPE_PROJECTOR),
    (re.compile(r"chromecast|apple[-_ ]?tv|android[-_ ]?tv|fire[-_ ]?tv|roku|media[-_ ]?player", re.I), TYPE_MEDIA_PLAYER),
    (re.compile(r"smart[-_ ]?tv|bravia|webos[-_ ]?tv|(?<![a-z])tv(?=[\d._ -]|$)|телевизор", re.I), TYPE_TV),
    (re.compile(r"sonos|wiim|heos|network[-_ ]?(?:audio|speaker)|сетев\w*[-_ ]аудио", re.I), TYPE_NETWORK_AUDIO),
    (re.compile(r"iphone|smartphone|смартфон", re.I), TYPE_SMARTPHONE),
    (re.compile(r"ipad|tablet|планшет", re.I), TYPE_TABLET),
    (re.compile(r"macbook|imac|mac[-_ ]?(?:mini|studio|pro)(?=[\d._ -]|$)", re.I), TYPE_MAC),
    (re.compile(r"thin[-_ ]?client|wyse|тонк\w*[-_ ]клиент", re.I), TYPE_THIN_CLIENT),
    (re.compile(r"(?<![a-z])(?:pdu|rpdu)(?=[\d._ -]|$)|power[-_ ]?distribution[-_ ]?unit", re.I), TYPE_PDU),
    (re.compile(r"(?<![a-z])ups(?=[\d._ -]|$)|\bибп\b|uninterruptible|smart[-_ ]?ups|symmetra", re.I), TYPE_UPS),
    (re.compile(r"access[-_ ]?control|\bскуд\b|turnstile|турникет", re.I), TYPE_ACCESS_CONTROL),
    (re.compile(r"intercom|doorphone|домофон|axis[-_ ]?i8016", re.I), TYPE_INTERCOM),
    (re.compile(r"(?<![a-z])plc(?=[\d._ -]|$)|simatic|modicon|промышленн\w*[-_ ]контроллер", re.I), TYPE_PLC),
    (re.compile(r"home[-_ ]?assistant|home[-_ ]?automation|openhab|zigbee2mqtt|shelly|tasmota|esphome", re.I), TYPE_SMART_HOME),
    (re.compile(r"(?<![a-z])(?:server|srv)(?=[\d._ -]|$)|proliant|poweredge|сервер", re.I), TYPE_SERVER),
]


def _match_equipment(value: str | None) -> str | None:
    """Найти явный признак специализированного устройства в описании."""
    if value:
        for pattern, category in _EQUIPMENT_RULES:
            if pattern.search(value):
                return category
    return None


# Хостнейм-паттерны — порядок важен (узкие → общие).
_HOSTNAME_RULES: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"print|printer|hp-|canon|epson|kyocera|xerox|brother|lexmark|ricoh|sharp|konica",
            re.IGNORECASE,
        ),
        TYPE_PRINTER,
    ),
    (
        re.compile(
            r"cam|camera|hikvision|dahua|dvr|nvr|axis|uniview|reolink", re.IGNORECASE
        ),
        TYPE_CAMERA,
    ),
    (
        re.compile(
            r"phone|sip|yealink|grandstream|polycom|cisco.*phone|voip", re.IGNORECASE
        ),
        TYPE_IP_PHONE,
    ),
    (
        re.compile(
            r"\bap\b|access[_-]point|unifi|aruba|ruckus|tplink.*ap|cisco.*ap|mikrotik.*ap|cap-",
            re.IGNORECASE,
        ),
        TYPE_AP,
    ),
    (re.compile(r"switch|sw-|catalyst|des-|dgs-|sgs", re.IGNORECASE), TYPE_SWITCH),
    (
        re.compile(r"\bnas\b|synology|qnap|truenas|storage|diskstation", re.IGNORECASE),
        TYPE_NAS,
    ),
    (
        re.compile(
            r"router|gateway|mikrotik|gw-|keenetic|zyxel|huawei.*router|openwrt",
            re.IGNORECASE,
        ),
        TYPE_ROUTER,
    ),
    # Linux / рабочие станции — характерные имена в MOS/ROSA сетях
    (
        re.compile(
            r"astra|mos[-_]|rosa|alt\s*linux|debian|ubuntu|\blinux\b|teacher|student|cab[-_\d]|class|ws[-_\d]",
            re.IGNORECASE,
        ),
        TYPE_LINUX,
    ),
    (
        re.compile(r"\bpc\b|desktop|laptop|workstation|computer", re.IGNORECASE),
        TYPE_COMPUTER,
    ),
]

# ---------------------------------------------------------------------------
# OUI → тип. Сгруппировано по вендору для удобства расширения.
# Префиксы в верхнем регистре, формат "XX:XX:XX".
# ---------------------------------------------------------------------------
_OUI_BY_VENDOR: dict[str, tuple[str, ...]] = {
    # OPS МосТех OPS6725-SHA: подтверждено по SSH/DMI на 12 хостах.
    # Это OEM-префиксы, а не уникальные идентификаторы бренда или модели.
    # Основание и ограничения: docs/device-classifier-panels.md.
    "Shiyuan Interactive Panel": ("B8:41:D9", "3C:BE:8E"),
    # --- Роутеры / шлюзы ---
    "MikroTik": (
        "48:8F:5A",
        "E4:8D:8C",
        "DC:2C:6E",
        "6C:3B:6B",
        "2C:56:DC",
        "00:25:9E",
        "B0:69:E4",
        "D4:CA:6D",
        "74:4D:28",
        "4C:5E:0C",
    ),
    "Keenetic": ("FC:EC:DA", "C8:3A:35", "D4:6F:45"),
    "TP-Link Router": ("50:C7:BF", "14:CC:20", "60:32:B1", "AC:84:C6"),
    "Zyxel": ("B0:4E:26", "04:D9:F5"),
    "Huawei Router": ("CC:2D:E0", "C8:1C:5C", "F8:1E:8E"),
    "Tenda": ("C8:3A:35", "60:45:CB"),
    "ZTE Router": ("C8:1C:5C", "F8:1E:8E", "04:46:08"),
    "D-Link Router": ("B0:C5:54", "14:CC:20"),
    "Asus Router": ("AC:9E:17", "38:2C:4A", "1C:B7:2C"),
    "Xiaomi Router": ("64:09:80", "34:CE:00"),
    # --- Точки доступа ---
    "Ubiquiti UniFi": ("00:27:22", "04:18:D6", "24:5A:4C", "68:72:51", "78:8A:20"),
    "Aruba AP": ("24:5A:4C", "94:B4:0F", "20:4C:03", "6C:F3:7F"),
    "Ruckus AP": ("28:28:5D", "84:83:46", "FC:82:90"),
    "Cisco AP": ("00:1A:1E", "58:B6:33", "00:1C:58", "00:0D:29"),
    "TP-Link AP": ("50:C7:BF", "A0:F3:C1"),
    "Mikrotik AP": ("48:8F:5A", "6C:3B:6B"),
    "Generic AP": ("FC:EC:DA", "78:8A:20", "00:1A:1E", "58:B6:33", "00:1C:58"),
    # --- Коммутаторы ---
    "Cisco Switch": ("00:1C:58", "84:B8:02", "70:10:6F", "1C:7E:E5", "28:10:7B"),
    "D-Link Switch": ("14:D6:4D", "1C:7E:E5", "C8:D3:A3", "34:27:92"),
    "TP-Link Switch": ("50:C7:BF", "A0:F3:C1"),
    "ZTE Switch": ("C8:1C:5C", "04:46:08"),
    "Mellanox Switch": ("EC:0D:9A", "F4:52:14"),
    "Generic Switch": (
        "00:1C:58",
        "84:B8:02",
        "70:10:6F",
        "1C:7E:E5",
        "28:10:7B",
        "C8:D3:A3",
    ),
    # --- Принтеры / МФУ ---
    "HP Printer": ("3C:D9:2B", "2C:55:91", "00:1B:A9", "00:17:C8", "00:00:74"),
    "Canon Printer": ("00:1E:8F", "00:00:74", "3C:6A:2C"),
    "Epson Printer": ("00:00:48", "00:40:AE"),
    "Brother Printer": ("00:80:77", "00:1B:A9"),
    "Xerox Printer": ("00:00:AA", "00:1E:8F"),
    "Kyocera Printer": ("00:17:C8", "00:1B:40"),
    "Ricoh Printer": ("00:1E:8F", "00:26:AB"),
    "Lexmark Printer": ("00:21:5B", "00:1B:A9"),
    "Generic Printer": (
        "3C:D9:2B",
        "40:B0:34",
        "94:57:A5",
        "B4:99:BA",
        "00:1E:8F",
        "00:BB:C1",
        "00:26:AB",
        "00:1B:A9",
        "00:17:C8",
        "00:00:74",
    ),
    # --- Камеры / DVR / NVR ---
    "Hikvision": (
        "BC:CF:CC",
        "18:68:CB",
        "44:19:B6",
        "C0:56:E3",
        "54:C4:15",
        "4C:BD:8F",
    ),
    "Dahua": ("3C:EF:8C", "4C:11:BF", "90:02:A9", "AC:CC:8E"),
    "Axis": ("00:40:8C", "70:28:03", "00:08:A0"),
    "Uniview": ("B8:A4:4F", "00:40:8C"),
    "Reolink": ("EC:71:DB", "B8:A4:4F"),
    "Generic Camera": (
        "BC:CF:CC",
        "44:19:B6",
        "C0:56:E3",
        "54:C4:15",
        "4C:BD:8F",
        "3C:EF:8C",
        "90:02:A9",
        "AC:CC:8E",
        "B8:A4:4F",
        "00:40:8C",
        "28:57:BE",
        "EC:71:DB",
    ),
    # --- NAS ---
    "Synology": ("00:11:32", "90:8D:78"),
    "QNAP": ("00:08:9B", "5C:44:AF"),
    "TrueNAS/ixSystems": ("00:25:90", "00:1B:44"),
    "Generic NAS": ("00:11:32", "00:08:9B", "00:25:90"),
    # --- IP-телефоны ---
    "Yealink": ("5C:8D:CC", "80:25:A3", "00:15:65"),
    "Grandstream": ("C0:F9:45", "38:3A:21"),
    "Polycom": ("00:04:F2", "64:16:8D"),
    "Cisco Phone": ("00:0B:82", "00:1F:9E"),
    "Generic IP Phone": (
        "00:15:65",
        "80:5E:C0",
        "80:5E:0C",
        "00:0B:82",
        "64:16:8D",
        "00:04:F2",
    ),
    # --- Серверы / рабочие станции ---
    "Dell": ("F4:8E:38", "14:FE:F5", "D4:AE:52"),
    "HP ProLiant": ("3C:D9:2B", "C8:CB:B8", "1C:69:7A"),
    "Lenovo": ("00:25:9E", "30:F9:ED", "F4:8E:38"),
    "Supermicro": ("0C:C4:7A", "3C:EC:EF", "00:25:90"),
    "Intel NUC": ("B8:AE:1D", "00:07:32"),
    "Apple Desktop": (
        "F0:18:98",
        "AC:BC:32",
        "A4:5E:60",
        "D0:03:4B",
        "3C:22:FB",
    ),
    "Raspberry Pi": ("B8:27:EB", "DC:A6:32", "E4:5F:01", "D8:3A:DD", "2C:CF:67"),
    "Orange Pi": ("34:C3:D3",),  # Orange Pi / ROCK Pi
    # --- Виртуальные машины ---
    "VMware": ("00:50:56", "00:0C:29", "00:05:69", "00:1C:14"),
    "VirtualBox": ("08:00:27", "0A:00:27"),
    "QEMU/KVM": ("52:54:00",),
    "Microsoft Hyper-V": ("00:15:5D",),
    "Parallels": ("00:1C:42",),
    "Docker/containers": ("02:42:AC",),  # docker bridge MACs 02:42:...
    # --- Дополнительные роутеры / SOHO-оборудование ---
    "Netgear Router": (
        "A0:40:A0",
        "B0:39:56",
        "9C:3D:CF",
        "20:E5:2A",
        "C4:04:15",
        "E4:F4:C6",
    ),
    "D-Link Extra": ("34:08:04", "C8:D3:A3", "28:10:7B"),
    "TP-Link Extra": ("98:DA:C4", "F4:F2:6D", "A4:2B:B0", "C0:25:E9", "1C:FA:68"),
    "Xiaomi Extra": ("0C:80:63", "78:11:DC", "F4:80:2C", "EC:D0:9F"),
    "Asus Extra": ("04:D4:C4", "D8:50:E6", "F0:79:59", "70:4D:7B"),
    "Hikvision Router": ("44:19:B6",),  # тоже в камерах, но Hikvision и роутеры делает
    "Fortinet": ("90:6C:AC", "00:09:0F", "70:4C:A5"),
    "SonicWall": ("00:17:C5",),
    "Check Point": ("00:1C:7F",),
    # --- Российские вендоры (MOS/ROSA-инфраструктура) ---
    "Eltex": (
        "A8:F9:4B",
        "BC:0E:63",
        "E8:28:C1",
        "0C:7A:15",
        "38:61:9A",
        "80:ED:2C",
    ),
    "QTECH": ("40:07:56", "4C:1F:CC", "00:1E:8C"),
    "NSG": ("00:1E:8C",),
    "RDP.ru": ("00:1E:8C",),
    "Kraftway": ("5C:6C:96",),
    "Aquarius": ("00:01:AC",),  # legacy
    # --- Cisco-телефоны (расширение) ---
    "Cisco Phone Extra": ("00:1E:13", "00:1D:70", "64:D9:89", "00:21:A0"),
    # --- Доп. принтеры ---
    "Pantum": ("00:24:22",),
    "Delhi/DELL Printer": ("00:24:22",),  # конфликтует с Pantum - generic fallback
    # --- Доп. камеры ---
    "Trassir/DSSL": ("00:5C:04", "E8:6A:64"),
    "Beward": ("00:C0:65",),
    "RVi": ("3C:8C:F8",),
    # --- IP-телефоны (расширение) ---
    "Fanvil": ("00:8E:73",),
    "Escene": ("00:C1:EE",),
    "Generic Computer Extra": (
        "00:01:AC",
        "5C:6C:96",
        "F0:18:98",
        "AC:BC:32",
        "A4:5E:60",
        "D0:03:4B",
        "3C:22:FB",
        "B8:27:EB",
        "DC:A6:32",
    ),
}

# Обратный индекс: OUI-префикс → тип. Конфликты (один префикс в нескольких
# вендорах) разрешаются в пользу более специфичного вендора — порядок в
# _OUI_BY_VENDOR важен: generic-записи идут после специфичных и перетирают
# только пустые слоты (см. построение ниже).
_OUI_MAP: dict[str, str] = {}
for _vendor, _prefixes in _OUI_BY_VENDOR.items():
    # Эвристика: имя вендора содержит "Generic" → ставим только если префикс
    # ещё не занят специфичным типом.
    is_generic = _vendor.startswith("Generic")
    for _prefix in _prefixes:
        if is_generic and _prefix in _OUI_MAP:
            continue
        # Извлекаем тип из имени вендора (до первой скобки/пробела-разделителя).
        # Здесь мы храним тип явно, поэтому используем второй элемент кортежа
        # через _OUI_TYPE_BY_VENDOR ниже.
        pass

# Явная карта вендор → тип (расшифровка имён из _OUI_BY_VENDOR).
_OUI_TYPE_BY_VENDOR: dict[str, str] = {
    "Shiyuan Interactive Panel": TYPE_INTERACTIVE_PANEL,
    "MikroTik": TYPE_ROUTER,
    "Keenetic": TYPE_ROUTER,
    "TP-Link Router": TYPE_ROUTER,
    "Zyxel": TYPE_ROUTER,
    "Huawei Router": TYPE_ROUTER,
    "Tenda": TYPE_ROUTER,
    "ZTE Router": TYPE_ROUTER,
    "D-Link Router": TYPE_ROUTER,
    "Asus Router": TYPE_ROUTER,
    "Xiaomi Router": TYPE_ROUTER,
    "Ubiquiti UniFi": TYPE_AP,
    "Aruba AP": TYPE_AP,
    "Ruckus AP": TYPE_AP,
    "Cisco AP": TYPE_AP_CISCO,
    "TP-Link AP": TYPE_AP,
    "Mikrotik AP": TYPE_AP,
    "Generic AP": TYPE_AP,
    "Cisco Switch": TYPE_SWITCH,
    "D-Link Switch": TYPE_SWITCH,
    "TP-Link Switch": TYPE_SWITCH,
    "ZTE Switch": TYPE_SWITCH,
    "Mellanox Switch": TYPE_SWITCH,
    "Generic Switch": TYPE_SWITCH,
    "HP Printer": TYPE_PRINTER,
    "Canon Printer": TYPE_PRINTER,
    "Epson Printer": TYPE_PRINTER,
    "Brother Printer": TYPE_PRINTER,
    "Xerox Printer": TYPE_PRINTER,
    "Kyocera Printer": TYPE_PRINTER,
    "Ricoh Printer": TYPE_PRINTER,
    "Lexmark Printer": TYPE_PRINTER,
    "Generic Printer": TYPE_PRINTER,
    "Hikvision": TYPE_CAMERA,
    "Dahua": TYPE_CAMERA,
    "Axis": TYPE_CAMERA,
    "Uniview": TYPE_CAMERA,
    "Reolink": TYPE_CAMERA,
    "Generic Camera": TYPE_CAMERA,
    "Synology": TYPE_NAS,
    "QNAP": TYPE_NAS,
    "TrueNAS/ixSystems": TYPE_NAS,
    "Generic NAS": TYPE_NAS,
    "Yealink": TYPE_IP_PHONE,
    "Grandstream": TYPE_IP_PHONE,
    "Polycom": TYPE_IP_PHONE,
    "Cisco Phone": TYPE_IP_PHONE,
    "Generic IP Phone": TYPE_IP_PHONE,
    "Dell": TYPE_COMPUTER,
    "HP ProLiant": TYPE_COMPUTER,
    "Lenovo": TYPE_COMPUTER,
    "Supermicro": TYPE_COMPUTER,
    "Intel NUC": TYPE_COMPUTER,
    "Generic Computer": TYPE_COMPUTER,
    "Apple Desktop": TYPE_COMPUTER,
    "Raspberry Pi": TYPE_LINUX,
    "Orange Pi": TYPE_LINUX,
    "VMware": TYPE_VM,
    "VirtualBox": TYPE_VM,
    "QEMU/KVM": TYPE_VM,
    "Microsoft Hyper-V": TYPE_VM,
    "Parallels": TYPE_VM,
    "Docker/containers": TYPE_VM,
    "Netgear Router": TYPE_ROUTER,
    "D-Link Extra": TYPE_ROUTER,
    "TP-Link Extra": TYPE_ROUTER,
    "Xiaomi Extra": TYPE_ROUTER,
    "Asus Extra": TYPE_ROUTER,
    "Hikvision Router": TYPE_ROUTER,
    "Fortinet": TYPE_ROUTER,
    "SonicWall": TYPE_ROUTER,
    "Check Point": TYPE_ROUTER,
    "Eltex": TYPE_SWITCH,
    "QTECH": TYPE_SWITCH,
    "NSG": TYPE_ROUTER,
    "RDP.ru": TYPE_ROUTER,
    "Kraftway": TYPE_COMPUTER,
    "Aquarius": TYPE_COMPUTER,
    "Cisco Phone Extra": TYPE_IP_PHONE,
    "Pantum": TYPE_PRINTER,
    "Delhi/DELL Printer": TYPE_PRINTER,
    "Trassir/DSSL": TYPE_CAMERA,
    "Beward": TYPE_CAMERA,
    "RVi": TYPE_CAMERA,
    "Fanvil": TYPE_IP_PHONE,
    "Escene": TYPE_IP_PHONE,
    "Generic Computer Extra": TYPE_COMPUTER,
}

# Финальная сборка _OUI_MAP с разрешением конфликтов: специфичные вендоры
# идут раньше generic, поэтому generic не перетирают уже занятые префиксы.
_OUI_MAP = {}
for _vendor, _prefixes in _OUI_BY_VENDOR.items():
    _typ = _OUI_TYPE_BY_VENDOR.get(_vendor, TYPE_UNKNOWN)
    is_generic = _vendor.startswith("Generic")
    for _prefix in _prefixes:
        if is_generic and _prefix in _OUI_MAP:
            continue
        _OUI_MAP[_prefix] = _typ
del _vendor, _prefixes, _typ, is_generic  # чистим имена цикла из модульного namespace


def _match_hostname(hostname: Optional[str]) -> Optional[str]:
    if not hostname:
        return None
    h = hostname.strip()
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", h):
        return None
    equipment = _match_equipment(h)
    if equipment:
        return equipment
    for pat, typ in _HOSTNAME_RULES:
        if pat.search(h):
            return typ
    return None


_MAC_OUI_RE = re.compile(r"([0-9A-Fa-f]{2})[-:]([0-9A-Fa-f]{2})[-:]([0-9A-Fa-f]{2})")


def _mac_prefix(mac: Optional[str]) -> Optional[str]:
    """Извлечь OUI-префикс (3 октета) из MAC-адреса.

    Корректно обрабатывает форматы ``XX:XX:XX:...``, ``XX-XX-XX-...``,
    ``XXXX.XXXX.XXXX`` (Cisco) и обрезает мусор после 3-го октета.
    Возвращает ``"XX:XX:XX"`` в верхнем регистре или ``None``.
    """
    if not mac:
        return None
    normalized = mac.strip()
    if re.fullmatch(r"[0-9a-fA-F]{4}(?:\.[0-9a-fA-F]{4}){2}", normalized):
        compact = normalized.replace(".", "")
        normalized = ":".join(compact[i:i + 2] for i in range(0, 12, 2))
    m = _MAC_OUI_RE.match(normalized)
    if not m:
        return None
    return ":".join(octet.upper().zfill(2) for octet in m.groups())


def _norm_ports(ports: Optional[Collection]) -> set[int]:
    if not ports:
        return set()
    out: set[int] = set()
    for p in ports:
        if p == "icmp":
            continue
        try:
            out.add(int(p))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Детект точек доступа (порт-паттерн + баннер)
# ---------------------------------------------------------------------------
_HUAWEI_BANNER_PATTERNS = [
    re.compile(r"Huawei.*WAP.*sshd", re.IGNORECASE),
    re.compile(r"Huawei.*WAP", re.IGNORECASE),
    re.compile(r"Huawei.*AP", re.IGNORECASE),
    re.compile(r"ssh.*Huawei", re.IGNORECASE),
]
_CISCO_BANNER = re.compile(r"Cisco", re.IGNORECASE)
_CISCO_AP_BANNER_PATTERNS = [
    re.compile(r"Aironet", re.IGNORECASE),
    re.compile(r"\bAP\b", re.IGNORECASE),
    re.compile(r"Access.?Point", re.IGNORECASE),
    # Явные маркеры из nmap-практики (ap_scan): заголовки HTTP/SSL.
    # Применяются к http_server отдельно - свободный "\bAP\b" к HTTP не
    # применяется (Server: "AP-xyz" дал бы ложный Cisco AP).
    re.compile(r"Cisco\s+Systems", re.IGNORECASE),
    re.compile(r"AIR-AP", re.IGNORECASE),
    re.compile(r"Cisco.*WAP", re.IGNORECASE),
    re.compile(r"Cisco.*Wireless", re.IGNORECASE),
]
_CISCO_ROUTER_BANNER_PATTERNS = [
    re.compile(r"\brouter\b", re.IGNORECASE),
    re.compile(r"Catalyst", re.IGNORECASE),
    re.compile(r"\bISR\b", re.IGNORECASE),
]


def classify_ap_by_ports_and_banner(
    ap_ports: dict,
    ssh_banner: Optional[str] = None,
    nmap_os_output: Optional[str] = None,
    http_server: Optional[str] = None,
) -> Optional[str]:
    """Классифицировать точку доступа по портам и баннеру.

    Для Cisco дополнительно отделяет маршрутизатор от AP:
    * AP: Aironet/AP в баннере
    * Router: router/Catalyst/ISR в баннере или открытые 53/23/161

    http_server - значение HTTP-заголовка ``Server:`` (порт 80). Участвует
    в Huawei-детекте наравне с SSH-баннером; в Cisco-детекте применяется
    только через явные маркеры (Cisco Systems / AIR-AP / WAP / Wireless) -
    свободный ``\\bAP\\b`` к HTTP не применяется, чтобы ``Server: AP-xyz``
    не давал ложный AP.
    """
    p22 = bool(ap_ports.get(22))
    p80 = bool(ap_ports.get(80))
    p443 = bool(ap_ports.get(443))
    p11100 = bool(ap_ports.get(11100))
    if p22 and p80 and p443:
        haystack = " ".join(
            filter(None, [ssh_banner or "", http_server or "", nmap_os_output or ""])
        )
        if haystack and any(p.search(haystack) for p in _HUAWEI_BANNER_PATTERNS):
            return TYPE_AP_HUAWEI
        return None
    if p22 and not p443 and not p11100:
        haystack = " ".join(filter(None, [ssh_banner or "", nmap_os_output or ""]))
        if haystack and _CISCO_BANNER.search(haystack):
            # Явные маркеры в баннере
            if any(p.search(haystack) for p in _CISCO_AP_BANNER_PATTERNS):
                return TYPE_AP_CISCO
            if any(p.search(haystack) for p in _CISCO_ROUTER_BANNER_PATTERNS):
                return None  # это маршрутизатор, не AP
            # Порт-признаки маршрутизатора: DNS / Telnet / SNMP
            if ap_ports.get(53) or ap_ports.get(23) or ap_ports.get(161):
                return None
            return TYPE_AP_CISCO
        # SSH-баннер не дал ответа - пробуем HTTP-заголовок со строгими
        # маркерами (кейс ap_scan: "80/tcp open http Cisco Systems",
        # "AIR-AP" - модель Cisco и без слова "Cisco" в заголовке).
        http_haystack = http_server or ""
        if http_haystack and (
            _CISCO_BANNER.search(http_haystack)
            or re.search(r"AIR-AP", http_haystack, re.IGNORECASE)
        ):
            strict = _CISCO_AP_BANNER_PATTERNS[3:]  # Cisco Systems/AIR-AP/WAP/Wireless
            if any(p.search(http_haystack) for p in strict):
                return TYPE_AP_CISCO
        return None
    return None


def _classify_by_ports(port_set: set[int]) -> Optional[str]:
    """Портовая классификация с весами.

    Сначала проверяем «комбинированные» правила (требующие нескольких
    портов) — они дают более уверенный ответ. Затем взвешенные сигнатуры
    из ``_PORT_SIGNATURES``. Наконец, одиночные портовые эвристики
    (роутер/switch/Linux/веб-устройство).
    """
    if not port_set:
        return None

    # --- Комбинированные правила (мульти-портовые) ---
    # RTSP (554/8554) без RPC (135) и RDP (3389) → камера/NVR: RTSP слушают
    # камеры и видеорегистраторы. SMB (445) у многих камер тоже открыт, а
    # Windows с RTSP-прокси почти всегда держит 135/3389 — поэтому наличие
    # 135/3389 отдает приоритет Windows-сигнатуре ниже.
    if (554 in port_set or 8554 in port_set) and (
        135 not in port_set and 3389 not in port_set
    ):
        return TYPE_CAMERA
    # NAS: SMB (445) + NFS/Synology/QNAP, без RDP
    if 445 in port_set and (port_set & _NAS_PORTS or 2049 in port_set):
        if 3389 not in port_set:
            return TYPE_NAS
    # Synology DSM: веб-пара 5000 (HTTP) + 5001 (HTTPS) — маркер DSM
    # даже без SMB/NFS.
    if 5000 in port_set and 5001 in port_set:
        if 3389 not in port_set:
            return TYPE_NAS

    # --- Взвешенные сигнатуры из _PORT_SIGNATURES ---
    best_type: Optional[str] = None
    best_score: float = -1.0
    best_abs: int = -1
    for typ, sig, threshold in _PORT_SIGNATURES:
        hits = port_set & sig
        n_hits = len(hits)
        if n_hits < threshold:
            continue
        # Доля совпавших портов от сигнатуры (0..1). Сигнатуры с меньшим
        # числом портов и полным совпадением получают score=1.0.
        score = n_hits / len(sig)
        # Абсолютное число совпадений — тай-брейкер.
        if score > best_score or (score == best_score and n_hits > best_abs):
            best_type = typ
            best_score = score
            best_abs = n_hits
    if best_type is not None:
        return best_type

    # --- Одиночные портовые эвристики (порядок: узкие → общие) ---
    # Маршрутизатор — до generic-эвристик, чтобы Cisco-роутер с 22+53/161
    # не ушёл в generic.
    if _DNS_PORT in port_set and (port_set & _HTTP_PORTS):
        if 88 not in port_set and 389 not in port_set:
            return TYPE_ROUTER
    if _TELNET_PORT in port_set and (port_set & (_HTTP_PORTS | {_DNS_PORT})):
        return TYPE_ROUTER
    if port_set & _MIKROTIK_PORTS:
        return TYPE_ROUTER
    if _SNMP_PORT in port_set and (port_set & _HTTP_PORTS):
        if _DNS_PORT in port_set:
            return TYPE_ROUTER
        return TYPE_SWITCH
    if _SNMP_PORT in port_set and not (port_set & _HTTP_PORTS):
        return TYPE_SWITCH
    # SSH (22) НЕ мапится на Linux: OpenWRT-роутеры, NAS, принтеры и телефоны
    # тоже держат SSH. ОС по SSH/TTL уточняется на ступени 2
    # (classify_device_detailed). Здесь SSH → обобщённое сетевое устройство,
    # которое ступень 1 перекроет категорийными сигналами, а ступень 2
    # уточнит ОС при их отсутствии.
    if _SSH_PORT in port_set:
        if port_set & _MIKROTIK_PORTS:
            return TYPE_ROUTER
        return None
    # Veyon (11100) / VNC (5900) — кроссплатформенны (Veyon ставится и на
    # Windows), без доп. признаков ОС не определить — generic Компьютер.
    if _VNC_PORT in port_set or _VEYON_PORT in port_set:
        return TYPE_COMPUTER
    if port_set and port_set <= _HTTP_PORTS:
        return TYPE_WEB_DEVICE
    if port_set:
        return TYPE_NETWORK_DEVICE
    return None


# ---------------------------------------------------------------------------
# Двухступенчатая evidence-based классификация
# ---------------------------------------------------------------------------
# Веса сигналов ступени 1 (категория) и ступени 2 (ОС).
_W_OUI = 3.0
_W_BANNER = 2.0
_W_TTL_OS = 2.0
_W_PORTS_CAT = 2.0  # категорийные портовые сигнатуры (принтер/камера/NAS…)
_W_NETBIOS = 1.5
_W_PORTS_GENERIC = 1.5  # обобщённые портовые эвристики
_W_HOSTNAME = 1.0
_W_TTL_NETWORK = 1.0  # TTL≥129 — слабый сигнал «сетевого железа»

# Категории, которые ступень 2 (ОС-уточнение) вправе превратить в
# «Компьютер (Windows/Linux)». Всё остальное (роутер, принтер, NAS…) —
# финальный результат ступени 1, ОС-сигналы для них игнорируются.
_OS_REFINABLE_CATEGORIES = frozenset(
    {TYPE_COMPUTER, TYPE_WEB_DEVICE, TYPE_NETWORK_DEVICE}
)

# Приоритет при равной сумме весов (меньше = приоритетнее).
_TIE_PRIORITY = {
    **{category: 1 for _, category in _EQUIPMENT_RULES},
    TYPE_AP: 0,
    TYPE_AP_CISCO: 0,
    TYPE_AP_HUAWEI: 0,
    TYPE_ROUTER: 1,
    TYPE_PRINTER: 2,
    TYPE_CAMERA: 2,
    TYPE_NAS: 2,
    TYPE_IP_PHONE: 2,
    TYPE_INTERACTIVE_PANEL: 2,
    TYPE_SWITCH: 3,
    TYPE_WINDOWS: 3,
    TYPE_LINUX: 3,
    TYPE_VM: 3,
    TYPE_COMPUTER: 4,
    TYPE_WEB_DEVICE: 5,
    TYPE_NETWORK_DEVICE: 5,
    TYPE_UNKNOWN: 9,
}

# TTL-эвристика: типичные начальные значения.
#   ~128 (Windows)  — Windows с небольшим декрементом на пути.
#   ~64  (Linux/Unix) — Unix-семейство. ВАЖНО: вся embedded-техника
#     (OpenWRT, NAS, принтеры, телефоны) работает на Linux-ядре и тоже
#     отвечает TTL=64, поэтому TTL — сигнал ОС, а НЕ категории.
#   ~255 (сетевое)  — часто у роутеров/коммутаторов/AP/принтеров; слабый
#     сигнал «не-компьютера».
_TTL_UNIX_MAX = 64
_TTL_WINDOWS_MAX = 128
_TTL_NETWORK_MIN = 129

# --- Баннеры ступени 1: категория ---
# SSH-баннер → (тип, причина). Dropbear — embedded SSH (OpenWRT/роутеры/AP).
_SSH_BANNER_RULES: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(r"Huawei.*WAP", re.IGNORECASE),
        TYPE_AP_HUAWEI,
        "SSH-баннер Huawei WAP",
    ),
    (re.compile(r"Cisco", re.IGNORECASE), TYPE_AP_CISCO, "SSH-баннер Cisco"),
    (
        re.compile(r"dropbear", re.IGNORECASE),
        TYPE_ROUTER,
        "SSH-баннер Dropbear (embedded)",
    ),
]

# --- Баннеры ступени 2: ОС (применяются только к уточняемым категориям) ---
_SSH_OS_BANNER_RULES: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(r"OpenSSH", re.IGNORECASE),
        TYPE_LINUX,
        "SSH-баннер OpenSSH",
    ),
]

# HTTP Server: ступень 1 — категория (вендоры embedded и специфичной техники).
_HTTP_SERVER_RULES: list[tuple[re.Pattern, str, str]] = [
    # Категорийные вендоры — до embedded-эвристик.
    (
        re.compile(
            r"Hikvision|Dahua|Webcam|Network.?Camera|Axis|Beward", re.IGNORECASE
        ),
        TYPE_CAMERA,
        "HTTP Server: камера",
    ),
    (
        re.compile(r"Synology|QNAP|TrueNAS|DiskStation", re.IGNORECASE),
        TYPE_NAS,
        "HTTP Server: NAS",
    ),
    (
        re.compile(
            r"HP-JetDirect|HP-HTTP|Epson|Canon|Kyocera|Xerox|Brother|Lexmark|Ricoh",
            re.IGNORECASE,
        ),
        TYPE_PRINTER,
        "HTTP Server: принтер",
    ),
    (
        re.compile(r"Yealink|Grandstream|Polycom|Fanvil|SIP-Phone", re.IGNORECASE),
        TYPE_IP_PHONE,
        "HTTP Server: IP-телефон",
    ),
    (
        re.compile(r"Keenetic", re.IGNORECASE),
        TYPE_ROUTER,
        "HTTP Server: Keenetic",
    ),
    # Embedded web-серверы: OpenWRT (uhttpd/lighttpd), роутеры (GoAhead/Boa),
    # BusyBox httpd (всё встроенное).
    (
        re.compile(
            r"GoAhead|Boa|lighttpd|mini_httpd|uhttpd|Allegro-Software|BusyBox httpd|miniupnpd",
            re.IGNORECASE,
        ),
        TYPE_ROUTER,
        "HTTP Server: embedded web (роутер/AP)",
    ),
]

# HTTP Server: ступень 2 — ОС.
_HTTP_OS_SERVER_RULES: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(r"IIS|Microsoft-HTTPAPI", re.IGNORECASE),
        TYPE_WINDOWS,
        "HTTP Server: IIS/Microsoft",
    ),
]

# Hostname-паттерны ступени 2: ОС (Linux-имена рабочих станций MOS/ROSA).
_HOSTNAME_OS_RULES: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"astra|mos[-_]|rosa|alt\s*linux|debian|ubuntu|\blinux\b|teacher|student|cab[-_\d]|class|ws[-_\d]",
            re.IGNORECASE,
        ),
        TYPE_LINUX,
    ),
    (
        re.compile(r"\bpc\b|desktop|laptop|workstation|computer", re.IGNORECASE),
        TYPE_COMPUTER,
    ),
]


def classify_device_detailed(
    ports: Optional[Collection] = None,
    hostname: Optional[str] = None,
    mac_address: Optional[str] = None,
    *,
    use_port_signatures: bool = True,
    ttl: Optional[int] = None,
    netbios_name: Optional[str] = None,
    netbios_suffix: Optional[int] = None,
    ssh_banner: Optional[str] = None,
    http_server: Optional[str] = None,
    nmap_os_output: Optional[str] = None,
    snmp_description: Optional[str] = None,
    service_banners: Mapping[int, str] | None = None,
) -> tuple[str, str]:
    """Определить тип оборудования с причиной классификации.

    Двухступенчатая модель:

      Ступень 1 — категория (роутер/принтер/NAS/камера/телефон…) по OUI,
      категорийным баннерам (Dropbear/embedded-HTTP/вендоры), портовым
      сигнатурам, hostname и TTL≥129 (слабый).

      Ступень 2 — ОС-уточнение (Windows/Linux) по TTL/NetBIOS/OpenSSH/IIS
      и hostname-паттернам ОС. Применяется только если ступень 1 дала
      неопределённый или обобщённый результат.

    Args:
        ports: открытые порты (игнорируются при use_port_signatures=False).
        hostname: reverse-DNS или IP-адрес хоста.
        mac_address: MAC-адрес хоста.
        use_port_signatures: учитывать ли портовые сигнатуры.
        ttl: TTL из ICMP-ответа (Windows ~128, Unix ≤64, сетевое ≥129).
        netbios_name: имя из NetBIOS Name Service (UDP 137).
        netbios_suffix: NetBIOS-суффикс (0x00 workstation, 0x20 server).
        ssh_banner: баннер SSH-сервера ("SSH-2.0-OpenSSH_8.4").
        http_server: значение HTTP-заголовка ``Server:``.
        nmap_os_output: зарезервировано для будущей интеграции.
        snmp_description: sysDescr из проверенного SNMP UDP-ответа.
        service_banners: проверяемые ответы специализированных сервисов.

    Returns:
        (тип, причина); при TYPE_UNKNOWN возможны неоднозначные признаки.
    """
    port_set = _norm_ports(ports) if use_port_signatures else set()
    evidence: dict[str, tuple[float, list[str]]] = {}

    def _add(typ: str, weight: float, reason: str) -> None:
        if not typ:
            return
        if typ in evidence:
            w, reasons = evidence[typ]
            evidence[typ] = (w + weight, reasons + [reason])
        else:
            evidence[typ] = (weight, [reason])

    def _pick() -> tuple[str, float, str]:
        if not evidence:
            return TYPE_UNKNOWN, 0.0, ""
        best_type, (best_weight, reasons) = max(
            evidence.items(), key=lambda kv: (kv[1][0], -_TIE_PRIORITY.get(kv[0], 5))
        )
        return best_type, best_weight, ", ".join(reasons)

    ttl_int: Optional[int] = None
    if ttl is not None:
        try:
            ttl_int = int(ttl)
        except Exception:
            ttl_int = None

    # ------------------------------------------------------------------
    # Ступень 1: категория устройства
    # ------------------------------------------------------------------

    # --- OUI (самый сильный сигнал) ---
    prefix = _mac_prefix(mac_address)
    if prefix and prefix in _OUI_MAP:
        oui_type = _OUI_MAP[prefix]
        if oui_type == TYPE_INTERACTIVE_PANEL:
            reason = (
                f"MAC-префикс {prefix} (вероятно интерактивная панель; "
                "наблюдался у OPS МосТех OPS6725-SHA; бренд по OUI не определяется)"
            )
        else:
            reason = f"MAC-префикс {prefix} (известный вендор)"
        # Общий OUI компьютера/одноплатника не опровергает явную роль:
        # тот же адаптер бывает у сервера, Mac и контроллера умного дома.
        explicit_role = any(
            _match_equipment(value)
            for value in (hostname, http_server, snmp_description if use_port_signatures else None)
        )
        weight = _W_OUI
        if explicit_role and oui_type in (_OS_REFINABLE_CATEGORIES | {TYPE_LINUX, TYPE_WINDOWS}):
            weight = _W_PORTS_GENERIC
        _add(oui_type, weight, reason)

    if use_port_signatures:
        if prefix in _EQUIPMENT_MAC_HINTS:
            vendor, category, expected_ports = _EQUIPMENT_MAC_HINTS[prefix]
            matches = sorted(port_set & expected_ports)
            if matches:
                _add(category, _W_OUI, f"MAC {vendor} + TCP {matches} (вероятный тип)")
        # Приветствие читается без команд управления и без аутентификации.
        pjlink = (service_banners or {}).get(4352, "")
        if 4352 in port_set and re.fullmatch(
            r"PJLINK (?:0|1 [0-9a-fA-F]{8})\r?\n?", pjlink
        ):
            _add(TYPE_PROJECTOR, 4.0, "ответ PJLink TCP/4352 (проектор/дисплей)")

    # --- TTL≥129: слабый сигнал «не-компьютера» ---
    # У роутеров/коммутаторов/AP/принтеров часто 255. Но это НЕ признак
    # именно роутера — Linux-принтер тоже отвечает 64, а Cisco — 255.
    if ttl_int is not None and ttl_int >= _TTL_NETWORK_MIN:
        _add(TYPE_NETWORK_DEVICE, _W_TTL_NETWORK, f"TTL={ttl_int} (сетевое железо)")

    # --- Баннеры SSH / HTTP (категорийные) ---
    if ssh_banner:
        for pat, typ, reason in _SSH_BANNER_RULES:
            if pat.search(ssh_banner):
                _add(typ, _W_BANNER, reason)
                break
    if http_server:
        equipment = _match_equipment(http_server)
        if equipment:
            _add(equipment, _W_BANNER, "HTTP Server: явный тип устройства")
        for pat, typ, reason in (() if equipment else _HTTP_SERVER_RULES):
            if pat.search(http_server):
                _add(typ, _W_BANNER, reason)
                break

    if use_port_signatures and snmp_description:
        # Проверенный ответ UDP/161 не означает открытый TCP-порт;
        # само наличие SNMP не определяет категорию устройства.
        category = _match_hostname(snmp_description)
        if category and category not in (TYPE_COMPUTER, TYPE_LINUX, TYPE_WINDOWS):
            _add(category, _W_BANNER, "SNMP sysDescr (UDP/161)")
        else:
            _add(TYPE_NETWORK_DEVICE, _W_PORTS_GENERIC, "SNMP sysDescr (UDP/161)")

    # --- Порты ---
    if use_port_signatures and port_set:
        port_type = _classify_by_ports(port_set)
        if port_type == TYPE_WINDOWS and (135 not in port_set and 3389 not in port_set):
            # Одиночный SMB (445/139) без RPC (135) и RDP (3389) — слабый
            # признак Windows: SMB есть и у Samba на Linux (Synology,
            # NAS, MOS-машины). ОС решается на ступени 2 по TTL/NetBIOS.
            _add(
                TYPE_COMPUTER,
                _W_PORTS_GENERIC,
                "SMB-порт (445/139) — Windows или Samba",
            )
        elif port_type:
            if port_type in (
                TYPE_PRINTER,
                TYPE_CAMERA,
                TYPE_IP_PHONE,
                TYPE_WINDOWS,
                TYPE_NAS,
            ):
                # Категорийные сигнатуры — сильный сигнал ступени 1.
                cat_reasons = {
                    TYPE_PRINTER: "порты печати",
                    TYPE_CAMERA: "RTSP/DVR-порты",
                    TYPE_IP_PHONE: "SIP/H.323-порты",
                    TYPE_WINDOWS: "Windows-порты (135/139/445/3389)",
                    TYPE_NAS: "NAS-порты (SMB+NFS/DSM)",
                }
                _add(port_type, _W_PORTS_CAT, cat_reasons[port_type])
            else:
                # Обобщённые эвристики (роутер/коммутатор/веб/сетевое) — слабее.
                gen_reasons = {
                    TYPE_ROUTER: "порты управления (DNS/Telnet/SNMP+web)",
                    TYPE_SWITCH: "SNMP",
                    TYPE_COMPUTER: "Veyon/VNC",
                    TYPE_WEB_DEVICE: "только HTTP-порты",
                    TYPE_NETWORK_DEVICE: "неспецифичные порты",
                }
                _add(
                    port_type,
                    _W_PORTS_GENERIC,
                    gen_reasons.get(port_type, f"порты {sorted(port_set)}"),
                )

    # --- Hostname (категорийные паттерны) ---
    hn_type = _match_hostname(hostname)
    if hn_type and hn_type not in (TYPE_LINUX, TYPE_COMPUTER):
        weight = _W_BANNER if _match_equipment(hostname) else _W_HOSTNAME
        _add(hn_type, weight, f"hostname «{(hostname or '').strip()}»")

    # Порт подтверждает уже найденную роль, но сам по себе её не устанавливает.
    role_ports = {
        TYPE_PROJECTOR: {4352}, TYPE_MEDIA_PLAYER: {8060},
        TYPE_TV: {8060}, TYPE_NETWORK_AUDIO: {1400, 1443, 7000},
        TYPE_SMART_HOME: {8123, 80}, TYPE_PLC: {102, 502},
        TYPE_ACCESS_CONTROL: {4370}, TYPE_INTERCOM: {5060, 5061, 554},
        TYPE_3D_PRINTER: {5000}, TYPE_MAC: {548, 5900},
    }
    if use_port_signatures:
        for category, expected in role_ports.items():
            matches = sorted(port_set & expected)
            if category in evidence and matches:
                _add(category, 1.5, f"порты роли TCP {matches}")

    # В подсказку попадают также неоднозначные признаки без повышения веса.
    hints = []
    if prefix in _AMBIGUOUS_MAC_HINTS:
        hints.append(f"MAC {prefix}: {_AMBIGUOUS_MAC_HINTS[prefix]}")
    if use_port_signatures:
        hints.extend(text for port, text in _EQUIPMENT_PORT_HINTS.items() if port in port_set)
    if ttl_int is not None and 1 <= ttl_int <= 255:
        hints.append(f"ICMP Echo TTL={ttl_int} (сам по себе не определяет тип)")

    def _result(category: str, reason: str) -> tuple[str, str]:
        return category, "; ".join(filter(None, [reason, *hints]))

    cat_type, cat_weight, cat_reasons = _pick()
    cat_reasons_text = cat_reasons

    # ------------------------------------------------------------------
    # Ступень 2: ОС-уточнение
    # ------------------------------------------------------------------
    # Применяется только если категория не определена или обобщённая
    # («Компьютер», «Веб-устройство», «Сетевое устройство»). Для
    # специфичных категорий (роутер, принтер, NAS…) ОС-сигналы игнорируются:
    # TTL=64 у OpenWRT-роутера не делает его «Компьютером (Linux)».
    os_applies = (
        cat_type == TYPE_UNKNOWN
        or cat_weight < 1.0
        or cat_type in _OS_REFINABLE_CATEGORIES
    )

    if os_applies:
        os_evidence: dict[str, tuple[float, list[str]]] = {}

        def _add_os(typ: str, weight: float, reason: str) -> None:
            if not typ:
                return
            if typ in os_evidence:
                w, reasons = os_evidence[typ]
                os_evidence[typ] = (w + weight, reasons + [reason])
            else:
                os_evidence[typ] = (weight, [reason])

        # --- TTL: Windows ~128, Unix ≤64 ---
        if ttl_int is not None:
            if ttl_int <= _TTL_UNIX_MAX:
                _add_os(TYPE_LINUX, _W_TTL_OS, f"TTL={ttl_int} (Unix-семейство)")
            elif ttl_int <= _TTL_WINDOWS_MAX:
                _add_os(TYPE_WINDOWS, _W_TTL_OS, f"TTL={ttl_int} (Windows)")

        # --- NetBIOS: Windows или Samba на Linux (уточняется по TTL) ---
        if netbios_name:
            nb_desc = f"NetBIOS-имя «{netbios_name}»"
            if ttl_int is not None and ttl_int <= _TTL_UNIX_MAX:
                _add_os(TYPE_LINUX, _W_NETBIOS, f"{nb_desc} (Samba на Unix)")
            elif ttl_int is not None and ttl_int <= _TTL_WINDOWS_MAX:
                _add_os(TYPE_WINDOWS, _W_NETBIOS, f"{nb_desc} (Windows)")
            else:
                _add_os(TYPE_WINDOWS, _W_NETBIOS, f"{nb_desc} (Windows/Samba)")

        # --- Баннеры ОС ---
        if ssh_banner:
            for pat, typ, reason in _SSH_OS_BANNER_RULES:
                if pat.search(ssh_banner):
                    _add_os(typ, _W_BANNER, reason)
                    break
        if http_server:
            for pat, typ, reason in _HTTP_OS_SERVER_RULES:
                if pat.search(http_server):
                    _add_os(typ, _W_BANNER, reason)
                    break

        # --- Hostname-паттерны ОС ---
        for pat, typ in _HOSTNAME_OS_RULES:
            if hostname and pat.search(hostname):
                _add_os(typ, _W_HOSTNAME, f"hostname «{(hostname or '').strip()}»")
                break

        if os_evidence:
            os_type, (os_weight, os_reasons) = max(
                os_evidence.items(),
                key=lambda kv: (kv[1][0], -_TIE_PRIORITY.get(kv[0], 5)),
            )
            if os_weight >= 1.0:
                os_reasons_text = ", ".join(os_reasons)
                if cat_type not in (TYPE_UNKNOWN, "") and cat_weight >= 1.0:
                    # Обобщённая категория + ОС: «Компьютер (Linux)».
                    merged = ", ".join(
                        filter(None, [cat_reasons_text, os_reasons_text])
                    )
                    return _result(os_type, merged)
                # Категории не было — чистый ОС-результат.
                return _result(os_type, os_reasons_text)

    # Ступень 2 не сработала — возвращаем результат ступени 1.
    if cat_type == TYPE_UNKNOWN or cat_weight < 1.0:
        return _result(TYPE_UNKNOWN, cat_reasons_text)
    # Обобщённая категория без ОС-сигналов остаётся как есть.
    if cat_type == TYPE_LINUX:
        # Защита: hostname-правила Linux в ступени 1 уже отфильтрованы выше.
        return _result(TYPE_COMPUTER, cat_reasons_text)
    return _result(cat_type, cat_reasons_text)


def classify_device(
    ports: Optional[Collection] = None,
    hostname: Optional[str] = None,
    mac_address: Optional[str] = None,
    *,
    use_port_signatures: bool = True,
    nmap_os_output: Optional[str] = None,
    ttl: Optional[int] = None,
    netbios_name: Optional[str] = None,
    netbios_suffix: Optional[int] = None,
    ssh_banner: Optional[str] = None,
    http_server: Optional[str] = None,
) -> str:
    """Определить тип оборудования (совместимая обёртка).

    Сигналы ступени 1 (категория): OUI (3.0) > баннеры/категорийные порты (2.0)
    > обобщённые порты/NetBIOS (1.5) > hostname (1.0). Ступень 2 — ОС.

    Args:
        ports: открытые порты (игнорируются если ``use_port_signatures=False``
            или ``ports`` пустой/None).
        hostname: reverse-DNS или IP-адрес хоста.
        mac_address: MAC-адрес хоста.
        use_port_signatures: учитывать ли портовые сигнатуры.
        nmap_os_output: опциональный вывод ``nmap -O`` (зарезервировано).
        ttl: TTL из ICMP-ответа.
        netbios_name: имя из NetBIOS Name Service.
        netbios_suffix: NetBIOS-суффикс.
        ssh_banner: баннер SSH-сервера.
        http_server: значение HTTP-заголовка ``Server:``.

    Returns:
        Метка типа (например ``"Компьютер (Windows)"``).
    """
    typ, _reason = classify_device_detailed(
        ports=ports,
        hostname=hostname,
        mac_address=mac_address,
        use_port_signatures=use_port_signatures,
        ttl=ttl,
        netbios_name=netbios_name,
        netbios_suffix=netbios_suffix,
        ssh_banner=ssh_banner,
        http_server=http_server,
        nmap_os_output=nmap_os_output,
    )
    return typ


__all__ = [
    "classify_device",
    "classify_device_detailed",
    "classify_ap_by_ports_and_banner",
    "device_type_icon_key",
    "scanner_state_icon_key",
    "TYPE_ICON_KEYS",
    "TYPE_UNKNOWN",
    "TYPE_ROUTER",
    "TYPE_AP",
    "TYPE_AP_CISCO",
    "TYPE_AP_HUAWEI",
    "TYPE_SWITCH",
    "TYPE_PRINTER",
    "TYPE_CAMERA",
    "TYPE_NAS",
    "TYPE_IP_PHONE",
    "TYPE_COMPUTER",
    "TYPE_WINDOWS",
    "TYPE_LINUX",
    "TYPE_VM",
    "TYPE_INTERACTIVE_PANEL",
    "TYPE_PROJECTOR",
    "TYPE_TV",
    "TYPE_MEDIA_PLAYER",
    "TYPE_NETWORK_AUDIO",
    "TYPE_SMARTPHONE",
    "TYPE_TABLET",
    "TYPE_MAC",
    "TYPE_SERVER",
    "TYPE_THIN_CLIENT",
    "TYPE_UPS",
    "TYPE_PDU",
    "TYPE_ACCESS_CONTROL",
    "TYPE_INTERCOM",
    "TYPE_PLC",
    "TYPE_SMART_HOME",
    "TYPE_3D_PRINTER",
    "TYPE_WEB_DEVICE",
    "TYPE_NETWORK_DEVICE",
]
