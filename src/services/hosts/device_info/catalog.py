"""Каталог команд и чистые парсеры системной информации Linux."""

import json
import re
from typing import Any

INFO_CATEGORIES: dict[str, Any] = {
    "Компьютер": {
        "order": 0,
        "commands": {
            "hostname": "hostname",
            "os": "cat /etc/os-release",
            "kernel": "uname -r",
            "arch": "uname -m",
            "uptime": "uptime -p 2>/dev/null || uptime",
            "hostname_fqdn": "hostname -f 2>/dev/null || hostname",
            "loadavg": "cat /proc/loadavg",
            "proc_count": "ps -e --no-headers | wc -l",
            "boot_time": "who -b 2>/dev/null || uptime -s 2>/dev/null",
            "timezone": "timedatectl show 2>/dev/null || cat /etc/timezone 2>/dev/null || readlink /etc/localtime 2>/dev/null",
            "last_reboot": "last reboot 2>/dev/null | head -5",
            "services_count": "systemctl list-units --type=service --state=running --no-legend 2>/dev/null | wc -l",
            "users_count": "who | wc -l",
        },
        "parse": {
            "hostname": lambda out: [("Имя хоста", out.strip())] if out.strip() else [],
            "os": lambda out: _parse_os_release(out),
            "kernel": lambda out: [("Ядро", out.strip())] if out.strip() else [],
            "arch": lambda out: [("Архитектура", out.strip())] if out.strip() else [],
            "uptime": lambda out: (
                [("Время работы", out.strip())] if out.strip() else []
            ),
            "hostname_fqdn": lambda out: [("FQDN", out.strip())] if out.strip() else [],
            "loadavg": lambda out: _parse_loadavg(out),
            "proc_count": lambda out: (
                [("Процессов", out.strip())]
                if out.strip() and out.strip().isdigit()
                else []
            ),
            "boot_time": lambda out: (
                [("Время загрузки", out.strip())] if out.strip() else []
            ),
            "timezone": lambda out: _parse_timezone(out),
            "last_reboot": lambda out: (
                [("Последние перезагрузки", "\n".join(out.strip().split("\n")[:5]))]
                if out.strip()
                else []
            ),
            "services_count": lambda out: (
                [("Служб запущено", out.strip())] if out.strip() else []
            ),
            "users_count": lambda out: (
                [("Пользователей онлайн", out.strip())]
                if out.strip() and out.strip().isdigit()
                else []
            ),
        },
        "fields_order": [
            "Имя хоста",
            "FQDN",
            "Операционная система",
            "Версия ОС",
            "ID ОС",
            "Ядро",
            "Архитектура",
            "Время работы",
            "Время загрузки",
            "Часовой пояс",
            "Нагрузка (1/5/15 мин)",
            "Процессов",
            "Служб запущено",
            "Пользователей онлайн",
            "Последние перезагрузки",
        ],
    },
    "Материнская плата": {
        "order": 1,
        "commands": {
            "board": "cat /sys/devices/virtual/dmi/id/board_vendor 2>/dev/null; cat /sys/devices/virtual/dmi/id/board_name 2>/dev/null; cat /sys/devices/virtual/dmi/id/board_version 2>/dev/null",
            "bios": "cat /sys/devices/virtual/dmi/id/bios_vendor 2>/dev/null; cat /sys/devices/virtual/dmi/id/bios_version 2>/dev/null; cat /sys/devices/virtual/dmi/id/bios_date 2>/dev/null",
            "sys_vendor": "cat /sys/devices/virtual/dmi/id/sys_vendor 2>/dev/null",
            "product": "cat /sys/devices/virtual/dmi/id/product_name 2>/dev/null; cat /sys/devices/virtual/dmi/id/product_version 2>/dev/null",
            "serial": "cat /sys/devices/virtual/dmi/id/product_serial 2>/dev/null; cat /sys/devices/virtual/dmi/id/board_serial 2>/dev/null",
        },
        "parse": {
            "board": lambda out: _parse_lines(
                out, ["Производитель платы", "Модель платы", "Версия платы"]
            ),
            "bios": lambda out: _parse_lines(
                out, ["Производитель BIOS", "Версия BIOS", "Дата BIOS"]
            ),
            "sys_vendor": lambda out: (
                [("Производитель системы", out.strip())] if out.strip() else []
            ),
            "product": lambda out: _parse_lines(out, ["Продукт", "Версия продукта"]),
            "serial": lambda out: _parse_lines(
                out, ["Серийный номер продукта", "Серийный номер платы"]
            ),
        },
        "fields_order": [
            "Производитель системы",
            "Продукт",
            "Версия продукта",
            "Производитель платы",
            "Модель платы",
            "Версия платы",
            "Производитель BIOS",
            "Версия BIOS",
            "Дата BIOS",
            "Серийный номер продукта",
            "Серийный номер платы",
        ],
    },
    "Процессор": {
        "order": 2,
        "commands": {
            "cpuinfo": "cat /proc/cpuinfo",
            "lscpu": "lscpu",
        },
        "parse": {
            "cpuinfo": lambda out: _parse_cpuinfo(out),
            "lscpu": lambda out: _parse_lscpu(out),
        },
        "fields_order": [
            "Модель процессора",
            "Сокет",
            "Ядра",
            "Потоки",
            "Частота (МГц)",
            "Макс. частота (МГц)",
            "Мин. частота (МГц)",
            "Архитектура",
            "Гипервизор",
            "L1d кэш",
            "L1i кэш",
            "L2 кэш",
            "L3 кэш",
            "Побочная нагрузка (BogoMIPS)",
            "Флаги CPU",
        ],
    },
    "Память": {
        "order": 3,
        "commands": {
            "meminfo": "cat /proc/meminfo",
            "dimm": "dmidecode -t memory 2>/dev/null || echo 'NO_ACCESS'",
        },
        # Команды, требующие sudo (выполняются от root; dmidecode читает SMBIOS
        # через /dev/mem, доступный только root). При should_use_sudo(device)
        # обёртываются в `sudo -S -p '' -E -- bash -c '...'` + пароль через stdin.
        # Без sudo — fallback `|| echo 'NO_ACCESS'` (текущее поведение).
        "sudo_commands": ["dimm"],
        "parse": {
            "meminfo": lambda out: _parse_meminfo(out),
            "dimm": lambda out: _parse_dimm(out),
        },
        "fields_order": [
            "Общая память",
            "Доступная память",
            "Буферы",
            "Кэш",
            "Swap всего",
            "Swap свободно",
        ],
    },
    "Видеокарта": {
        "order": 4,
        "commands": {
            "gpu": "lspci -v -s $(lspci | grep -i vga | cut -d' ' -f1) 2>/dev/null || lspci | grep -i vga",
            "gpu_all": "lspci | grep -iE 'vga|3d|display'",
        },
        "parse": {
            "gpu": lambda out: _parse_lspci_vga(out),
            "gpu_all": lambda out: (
                [("Все видеоустройства", out.strip())] if out.strip() else []
            ),
        },
        "fields_order": ["Видеокарта", "Драйвер", "Все видеоустройства"],
    },
    "Накопители": {
        "order": 5,
        "commands": {
            "lsblk": "lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL -n -b",
            "smart": "ls -1 /dev/sd* /dev/nvme* 2>/dev/null | head -20",
            "df": "df -h --output=source,size,used,avail,pcent,target 2>/dev/null | grep -v tmpfs | grep -v devtmpfs",
        },
        "parse": {
            "lsblk": lambda out: _parse_lsblk(out),
            "smart": lambda out: [("Устройства", out.strip())] if out.strip() else [],
            "df": lambda out: _parse_df(out),
        },
        "fields_order": [],
    },
    "Сеть": {
        "order": 6,
        "commands": {
            "interfaces": "ip -br addr",
            "ip_addr": "ip addr show",
            "routes": "ip route",
            "dns": "cat /etc/resolv.conf 2>/dev/null",
            "hostname_domain": "hostname -d 2>/dev/null || echo ''",
        },
        "parse": {
            "interfaces": lambda out: _parse_ip_br(out),
            "ip_addr": lambda out: _parse_ip_addr(out),
            "routes": lambda out: [("Маршруты", out.strip())] if out.strip() else [],
            "dns": lambda out: _parse_resolv_conf(out),
            "hostname_domain": lambda out: (
                [("Домен", out.strip())] if out.strip() else []
            ),
        },
        "fields_order": [],
    },
    "Оптические приводы": {
        "order": 7,
        "commands": {
            "cdrom": "ls -1 /dev/cd* /dev/dvd* /dev/sr* 2>/dev/null || echo 'NO_OPTICAL'",
            "cdrom_info": "lspci | grep -i 'cdrom\\|dvd\\|ATAPI' 2>/dev/null || echo ''",
        },
        "parse": {
            "cdrom": lambda out: [("Устройства", out.strip())] if out.strip() else [],
            "cdrom_info": lambda out: (
                [("PCI устройство", out.strip())] if out.strip() else []
            ),
        },
        "fields_order": [],
    },
    "USB": {
        "order": 8,
        "commands": {
            "usb": "lsusb 2>/dev/null || cat /sys/kernel/debug/usb/devices 2>/dev/null || echo 'NO_USB_INFO'",
            "usb_controllers": "lspci | grep -i usb",
            "usb_tree": "lsusb -t 2>/dev/null || echo 'NO_USB_TREE'",
            "usb_devices_detail": "usb-devices 2>/dev/null || echo 'NO_USB_DETAIL'",
        },
        "parse": {
            "usb": lambda out: _parse_lsusb(out),
            "usb_controllers": lambda out: (
                [("USB контроллеры", out.strip())] if out.strip() else []
            ),
            "usb_tree": lambda out: (
                [("USB дерево", out.strip())]
                if out.strip() and "NO_USB_TREE" not in out
                else []
            ),
            "usb_devices_detail": lambda out: _parse_usb_devices(out),
        },
        "fields_order": [],
    },
    "Звук": {
        "order": 9,
        "commands": {
            "audio": "lspci | grep -i audio",
            "alsa": "cat /proc/asound/cards 2>/dev/null || echo ''",
        },
        "parse": {
            "audio": lambda out: _parse_lspci_audio(out),
            "alsa": lambda out: _parse_alsa_cards(out),
        },
        "fields_order": [],
    },
    "Прослушиваемые порты": {
        "order": 10,
        "is_table": True,
        "table_columns": [
            "Состояние",
            "Recv-Q",
            "Send-Q",
            "Локальный адрес",
            "Удалённый адрес",
            "Процесс",
        ],
        "commands": {
            "listening": "ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null",
        },
        "parse": {
            "listening": lambda out: _parse_ss_listening(out),
        },
        "fields_order": [],
    },
    "Установленные соединения": {
        "order": 11,
        "is_table": True,
        "table_columns": [
            "Состояние",
            "Recv-Q",
            "Send-Q",
            "Локальный адрес",
            "Удалённый адрес",
            "Процесс",
        ],
        "commands": {
            "established": "ss -tnp state established 2>/dev/null || netstat -tnp 2>/dev/null | grep ESTABLISHED",
        },
        "parse": {
            "established": lambda out: _parse_ss_established(out),
        },
        "fields_order": [],
    },
    "Сводка пользователей": {
        "order": 11,
        "commands": {
            "user_count": "cat /etc/passwd | wc -l",
            "sys_user_count": "awk -F: '$3 < 1000 {count++} END {print count+0}' /etc/passwd",
            "sudo_users": "getent group sudo wheel adm 2>/dev/null",
            "logged_in_count": "who | wc -l",
        },
        "parse": {
            "user_count": lambda out: (
                [("Учетных записей NSS", out.strip())] if out.strip() else []
            ),
            "sys_user_count": lambda out: (
                [("Локальных UID < 1000 (эвристика)", out.strip())]
                if out.strip()
                else []
            ),
            "sudo_users": lambda out: (
                [("Членство в группах sudo/wheel/adm", out.strip())]
                if out.strip()
                else []
            ),
            "logged_in_count": lambda out: (
                [("Активных сеансов", out.strip())] if out.strip() else []
            ),
        },
        "fields_order": [
            "Учетных записей NSS",
            "Локальных UID < 1000 (эвристика)",
            "Активных сеансов",
            "Членство в группах sudo/wheel/adm",
        ],
    },
    "Все пользователи": {
        "order": 12,
        "is_table": True,
        "table_columns": [
            "Пользователь",
            "UID",
            "GID",
            "Домашняя папка",
            "Shell",
            "Тип",
        ],
        "commands": {
            "all_users_table": "getent passwd",
        },
        "parse": {
            "all_users_table": lambda out: _parse_passwd_table(out),
        },
        "fields_order": [],
    },
    "Активные сеансы": {
        "order": 13,
        "is_table": True,
        "table_columns": ["Пользователь", "Терминал", "Дата", "Время", "Откуда"],
        "commands": {
            "who_table": "who",
        },
        "parse": {
            "who_table": lambda out: _parse_who_table(out),
        },
        "fields_order": [],
    },
    "Последние входы": {
        "order": 14,
        "is_table": True,
        "table_columns": ["Пользователь", "Терминал", "Хост", "Дата/время", "Статус"],
        "commands": {
            "last_table": "last -n 20 2>/dev/null || echo 'NO_LAST'",
        },
        "parse": {
            "last_table": lambda out: (
                _parse_last_table(out) if "NO_LAST" not in out else []
            ),
        },
        "fields_order": [],
    },
    "Неудачные входы": {
        "order": 15,
        "is_table": True,
        "table_columns": ["Пользователь", "Терминал", "Хост", "Дата/время", "Статус"],
        "commands": {
            "lastb_table": "lastb -n 20 2>/dev/null || echo 'NO_LASTB'",
        },
        "parse": {
            "lastb_table": lambda out: (
                _parse_last_table(out) if "NO_LASTB" not in out else []
            ),
        },
        "fields_order": [],
    },
    "Папки пользователей": {
        "order": 16,
        "is_table": True,
        "table_columns": ["Пользователь", "Размер"],
        "size_columns": {1: "bytes"},
        "commands": {
            "home_dirs_table": "du -sb /home/* 2>/dev/null | sort -rn",
        },
        "parse": {
            "home_dirs_table": lambda out: _parse_home_dirs_table(out),
        },
        "fields_order": [],
    },
    "Сводка групп": {
        "order": 17,
        "commands": {
            "group_count": "cat /etc/group | wc -l",
            "sys_groups": "awk -F: '$3 < 1000 {count++} END {print count+0}' /etc/group",
            "user_priv_groups": "getent group sudo wheel adm root 2>/dev/null",
        },
        "parse": {
            "group_count": lambda out: (
                [("Всего групп", out.strip())] if out.strip() else []
            ),
            "sys_groups": lambda out: (
                [("Локальных GID < 1000 (эвристика)", out.strip())]
                if out.strip()
                else []
            ),
            "user_priv_groups": lambda out: (
                [("Членство в группах sudo/wheel/adm", out.strip())]
                if out.strip()
                else []
            ),
        },
        "fields_order": [
            "Всего групп",
            "Локальных GID < 1000 (эвристика)",
            "Членство в группах sudo/wheel/adm",
        ],
    },
    "Все группы": {
        "order": 18,
        "is_table": True,
        "table_columns": [
            "Группа",
            "GID",
            "Дополнительные участники (без primary group)",
        ],
        "commands": {
            "group_table": "getent group",
        },
        "parse": {
            "group_table": lambda out: _parse_group_table(out),
        },
        "fields_order": [],
    },
    "Дисковое пространство": {
        "order": 13,
        "is_table": True,
        "table_columns": [
            "Файловая система",
            "Размер",
            "Использовано",
            "Свободно",
            "Исп.",
            "Точка монтирования",
        ],
        "size_columns": {1: "kb", 2: "kb", 3: "kb", 4: "pct"},
        "commands": {
            "df_all": "df --total 2>/dev/null | tail -n +2 | awk '{print $1,$2,$3,$4,$5,$6}' || df | tail -n +2 | awk '{print $1,$2,$3,$4,$5,$6}'",
        },
        "parse": {
            "df_all": lambda out: _parse_df_table(out),
        },
        "fields_order": [],
    },
}


TREE_GROUPS = {
    "Система": ["Компьютер", "Процессор", "Память", "Материнская плата"],
    "Устройства": ["Накопители", "Видеокарта", "USB", "Звук", "Оптические приводы"],
    "Сеть": ["Сеть", "Прослушиваемые порты", "Установленные соединения"],
    "Пользователи и безопасность": [
        "Сводка пользователей",
        "Все пользователи",
        "Активные сеансы",
        "Последние входы",
        "Неудачные входы",
        "Папки пользователей",
        "Сводка групп",
        "Все группы",
    ],
    "Хранение": ["Дисковое пространство"],
}

CATEGORY_ICONS = {
    "Система": "info_system",
    "Устройства": "info_devices",
    "Сеть": "info_network",
    "Пользователи и безопасность": "info_users",
    "Хранение": "info_storage",
    "Компьютер": "info_computer",
    "Процессор": "info_cpu",
    "Память": "info_memory",
    "Материнская плата": "info_motherboard",
    "Накопители": "info_disk",
    "Видеокарта": "info_gpu",
    "USB": "info_usb",
    "Звук": "info_sound",
    "Оптические приводы": "info_optical",
    "Прослушиваемые порты": "info_listening",
    "Установленные соединения": "info_connected",
    "Пользователи": "info_user",
    "Сводка пользователей": "info_users",
    "Все пользователи": "info_user",
    "Активные сеансы": "info_user",
    "Последние входы": "info_user",
    "Неудачные входы": "info_user",
    "Папки пользователей": "info_disk_space",
    "Группы": "info_groups",
    "Сводка групп": "info_groups",
    "Все группы": "info_groups",
    "Дисковое пространство": "info_disk_space",
}


def _parse_lines(output: str, labels: list[str]) -> list[tuple[str, str]]:
    lines = output.rstrip("\n").split("\n")
    return [
        (
            label,
            lines[i].strip() if i < len(lines) and lines[i].strip() else "Нет данных",
        )
        for i, label in enumerate(labels)
    ]


def _parse_os_release(output: str) -> list[tuple[str, str]]:
    result = []
    data = {}
    for line in output.strip().split("\n"):
        if "=" in line:
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip().strip('"')
    if "PRETTY_NAME" in data:
        result.append(("Операционная система", data["PRETTY_NAME"]))
    elif "NAME" in data:
        result.append(("Операционная система", data["NAME"]))
    if "VERSION" in data:
        result.append(("Версия ОС", data["VERSION"]))
    if "ID" in data:
        result.append(("ID ОС", data["ID"]))
    return result


def _parse_cpuinfo(output: str) -> list[tuple[str, str]]:
    records = []
    for block in re.split(r"\n\s*\n", output.strip()):
        record = {
            k.strip(): v.strip()
            for line in block.splitlines()
            for k, sep, v in [line.partition(":")]
            if sep
        }
        if "processor" in record:
            records.append(record)
    result = []
    models = list(
        dict.fromkeys(
            r.get("model name") or r.get("Processor") or r.get("Hardware")
            for r in records
        )
    )
    models = [m for m in models if m]
    if models:
        result.append(("Модель процессора", "; ".join(str(model) for model in models)))
    sockets = {r["physical id"] for r in records if "physical id" in r}
    cores = {
        (r["physical id"], r["core id"])
        for r in records
        if "physical id" in r and "core id" in r
    }
    if sockets:
        result.append(("Сокет", str(len(sockets))))
    if records and all("physical id" in r and "core id" in r for r in records):
        result.append(("Ядра", str(len(cores))))
    else:
        result.append(("Ядра", "Топология недоступна"))
    if records:
        result.append(("Потоки", str(len({r["processor"] for r in records}))))
    return result


def _parse_lscpu(output: str) -> list[tuple[str, str]]:
    result = []
    mapping = {
        "Architecture": "Архитектура",
        "CPU max MHz": "Макс. частота (МГц)",
        "CPU min MHz": "Мин. частота (МГц)",
        "CPU MHz": "Частота (МГц)",
        "Hypervisor vendor": "Гипервизор",
        "L1d cache": "L1d кэш",
        "L1i cache": "L1i кэш",
        "L2 cache": "L2 кэш",
        "L3 cache": "L3 кэш",
        "Flags": "Флаги CPU",
    }
    for line in output.split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            if key in mapping:
                result.append((mapping[key], value.strip()))
    return result


def _parse_meminfo(output: str) -> list[tuple[str, str]]:
    result = []
    mapping = {
        "MemTotal": "Общая память",
        "MemAvailable": "Доступная память",
        "Buffers": "Буферы",
        "Cached": "Кэш",
        "SwapTotal": "Swap всего",
        "SwapFree": "Swap свободно",
    }
    for line in output.split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            if key in mapping:
                val = value.strip()
                try:
                    kb = int(val.split()[0])
                    val = str(kb * 1024)
                except (ValueError, IndexError):
                    pass
                result.append((mapping[key], val))
    return result


def _parse_dimm(output: str) -> list[tuple[str, str]]:
    if "NO_ACCESS" in output:
        return []
    result = []
    devices = output.split("\n\n")
    idx = 1
    for dev in devices:
        dev = dev.strip()
        if not dev:
            continue
        loc = ""
        size = ""
        speed = ""
        typ = ""
        manu = ""
        for line in dev.split("\n"):
            line = line.strip()
            if line.startswith("Locator:"):
                loc = line.split(":", 1)[1].strip()
            elif line.startswith("Size:"):
                size = line.split(":", 1)[1].strip()
            elif line.startswith("Speed:"):
                speed = line.split(":", 1)[1].strip()
            elif line.startswith("Type:"):
                typ = line.split(":", 1)[1].strip()
            elif line.startswith("Manufacturer:"):
                manu = line.split(":", 1)[1].strip()
            elif line.startswith("Part Number:"):
                line.split(":", 1)[1].strip()
        if size and size != "No Module Installed":
            label = f"Модуль RAM {idx}"
            value_parts = []
            if loc:
                value_parts.append(loc)
            if size:
                value_parts.append(size)
            if typ:
                value_parts.append(typ)
            if speed:
                value_parts.append(speed)
            if manu and manu != "Unknown":
                value_parts.append(manu)
            result.append((label, " | ".join(value_parts)))
            idx += 1
    return result


def _parse_lspci_vga(output: str) -> list[tuple[str, str]]:
    result = []
    for block in re.split(r"\n(?=\S)", output.strip()):
        lines = block.splitlines()
        if not lines or not re.search(
            r"VGA|3D controller|Display controller", lines[0], re.IGNORECASE
        ):
            continue
        slot, _, description = lines[0].partition(" ")
        result.append((f"GPU {slot}", description))
        for line in lines[1:]:
            if "Kernel driver in use:" in line:
                result.append(
                    (
                        f"Драйвер {slot}",
                        line.partition("Kernel driver in use:")[2].strip(),
                    )
                )
    return result


def _parse_lspci_audio(output: str) -> list[tuple[str, str]]:
    result = []
    for line in output.splitlines():
        if line and not line[0].isspace() and "audio" in line.lower():
            slot, _, desc = line.partition(" ")
            result.append((slot, desc.strip()))
    return result


def _parse_alsa_cards(output: str) -> list[tuple[str, str]]:
    if not output.strip():
        return []
    result = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s+\[([^\]]*)\s*\]\s*:\s*(.+)$", line)
        if m:
            idx = m.group(1)
            short = m.group(2).strip()
            full = m.group(3).strip()
            result.append((f"Карта {idx}", f"{short} — {full}" if short else full))
        else:
            parts = line.split(None, 1)
            if len(parts) == 2:
                result.append((parts[0], parts[1]))
            elif len(parts) == 1 and parts[0]:
                result.append(("", parts[0]))
    return result


def _parse_lsblk(output: str) -> list[tuple[str, str]]:
    legacy = not output.lstrip().startswith("{")
    if legacy:
        devices = []
        for line in output.splitlines():
            values = {}
            for key, value in re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', line):
                values[key.lower()] = re.sub(
                    rb"\\x([0-9a-fA-F]{2})",
                    lambda match: bytes([int(match[1], 16)]),
                    value.encode(),
                ).decode("utf-8", "replace")
            if values:
                devices.append(values)
        if not devices and output.strip():
            raise ValueError("Неверный формат lsblk pairs")
        data = {"blockdevices": devices}
    else:
        data = json.loads(output)
    if not isinstance(data, dict) or not isinstance(data.get("blockdevices"), list):
        raise TypeError("Неверная схема lsblk")
    result = []

    def visit(devices: list[dict[str, Any]]) -> None:
        for device in devices:
            name = device.get("path") or device.get("name") or "?"
            if not name.startswith("/"):
                name = "/dev/" + name
            size = device.get("size")
            values = [
                f"{size} bytes" if size is not None else "Размер неизвестен",
                str(device.get("type") or "?"),
                str(device.get("fstype") or ""),
                str(device.get("model") or "").strip(),
            ]
            mounts = device.get("mountpoints") or [device.get("mountpoint")]
            values.extend(str(m) for m in mounts if m is not None)
            if device.get("rm") in (True, 1, "1"):
                values.append("Съёмный")
            result.append((name, " | ".join(v for v in values if v)))
            visit(device.get("children") or [])

    visit(data.get("blockdevices") or [])
    if legacy:
        result.append(
            (
                "Ограничение lsblk",
                "Старый формат pairs: доступна одна точка монтирования на устройство",
            )
        )
    return result


def _parse_df(output: str) -> list[tuple[str, str]]:
    result = []
    for line in output.strip().split("\n"):
        parts = line.strip().split()
        if len(parts) >= 6:
            result.append(
                (parts[0], f"{parts[1]} (использовано {parts[4]}) -> {parts[5]}")
            )
    return result


def _parse_ip_br(output: str) -> list[tuple[str, str]]:
    result = []
    for line in output.strip().split("\n"):
        parts = line.strip().split()
        if len(parts) >= 2:
            iface = parts[0]
            status = parts[1]
            addr = parts[2] if len(parts) > 2 else ""
            result.append((iface, f"{status} {addr}".strip()))
    return result


def _parse_ip_addr(output: str) -> list[tuple[str, str]]:
    result = []
    current_iface = ""
    for line in output.split("\n"):
        line = line.strip()
        if re.match(r"^\d+:", line):
            match = re.match(r"^\d+:\s*(\S+):", line)
            if match:
                current_iface = match.group(1)
        elif "inet " in line:
            parts = line.strip().split()
            for i, p in enumerate(parts):
                if p == "inet" and i + 1 < len(parts):
                    result.append((f"  {current_iface} IPv4", parts[i + 1]))
        elif "inet6 " in line:
            parts = line.strip().split()
            for i, p in enumerate(parts):
                if p == "inet6" and i + 1 < len(parts):
                    result.append((f"  {current_iface} IPv6", parts[i + 1]))
    return result


def _parse_resolv_conf(output: str) -> list[tuple[str, str]]:
    result = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if line.startswith("nameserver"):
            result.append(
                ("DNS сервер", line.split(None, 1)[1] if len(line.split()) > 1 else "")
            )
        elif line.startswith("search"):
            result.append(
                (
                    "Поисковый домен",
                    line.split(None, 1)[1] if len(line.split()) > 1 else "",
                )
            )
    return result


def _parse_loadavg(output: str) -> list[tuple[str, str]]:
    parts = output.strip().split()
    if len(parts) >= 3:
        return [("Нагрузка (1/5/15 мин)", f"{parts[0]} / {parts[1]} / {parts[2]}")]
    return []


def _parse_timezone(output: str) -> list[tuple[str, str]]:
    result = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if line.startswith("Timezone="):
            tz = line.split("=", 1)[1].strip()
            result.append(("Часовой пояс", tz))
            return result
        elif line.startswith("LocalRTC="):
            pass
        elif line and "=" not in line and "/" in line:
            result.append(("Часовой пояс", line))
            return result
    raw = output.strip()
    if raw:
        result.append(("Часовой пояс", raw))
    return result


def _parse_lsusb(output: str) -> list[tuple[str, str]]:
    if "NO_USB_INFO" in output:
        return []
    result = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        result.append(("USB устройство", line))
    return result


def _parse_usb_devices(output: str) -> list[tuple[str, str]]:
    """Сохранять каждое устройство и драйверы всех USB-интерфейсов."""
    rows = []
    for block in re.split(r"\n\s*\n", output.strip()):
        address = re.search(r"T:.*?Bus=\s*(\d+).*?Dev#=\s*(\d+)", block)
        if not address:
            continue
        product = re.search(r"S:\s*Product=(.*)", block)
        ids = re.search(r"P:.*?Vendor=(\w+).*?ProdID=(\w+)", block)
        drivers = re.findall(r"I:.*?Driver=(\S+)", block)
        values = ([product[1]] if product else []) + (
            [f"VID:PID={ids[1]}:{ids[2]}"] if ids else []
        )
        values.extend(dict.fromkeys(drivers))
        rows.append((f"USB {address[1]}:{address[2]}", " | ".join(values)))
    return rows


def _parse_passwd_table(output: str) -> list[tuple[str, ...]]:
    """Разобрать учетные записи NSS в строки таблицы."""
    rows: list[tuple[str, ...]] = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) < 7:
            continue
        username, _passwd, uid, gid, _gecos, home, shell = (
            parts[0],
            parts[1],
            parts[2],
            parts[3],
            parts[4],
            parts[5],
            parts[6],
        )
        try:
            uid_int = int(uid)
        except (ValueError, TypeError):
            uid_int = -1
        if uid_int == 0:
            user_type = "root"
        elif uid_int < 1000:
            user_type = "UID < 1000 (эвристика)"
        elif uid_int >= 1000 and uid_int < 65534:
            user_type = "UID 1000–65533 (эвристика)"
        else:
            user_type = "Специальный/доменный UID (эвристика)"
        rows.append((username, uid, gid, home, shell, user_type))
    return rows


def _parse_who_table(output: str) -> list[tuple[str, ...]]:
    """Сохранить дату who целиком в ISO-формате и варианте C с месяцем/днём."""
    rows: list[tuple[str, ...]] = []
    pattern = re.compile(
        r"^\s*(\S+)\s+(\S+)\s+(\d{4}-\d{2}-\d{2}|[A-Za-z]{3}\s+\d{1,2})"
        r"\s+(\d{2}:\d{2}(?::\d{2})?)(?:\s+(.*))?\s*$"
    )
    for line in output.splitlines():
        match = pattern.match(line)
        if match:
            rows.append(
                (
                    match[1],
                    match[2],
                    " ".join(match[3].split()),
                    match[4],
                    match[5] or "Локальный",
                )
            )
    return rows


def _parse_last_table(output: str) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    date_re = re.compile(
        r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+\s+\d{2}:\d{2}(?::\d{2})?(?:\s+\d{4})?"
    )
    for line in output.splitlines():
        if not line.strip() or line.split()[0] in {
            "wtmp",
            "btmp",
            "reboot",
            "shutdown",
            "runlevel",
        }:
            continue
        match = date_re.search(line)
        if not match:
            continue
        prefix = line[: match.start()].split()
        if len(prefix) < 2:
            continue
        rows.append(
            (
                prefix[0],
                prefix[1],
                " ".join(prefix[2:]),
                match.group(),
                line[match.end() :].strip(),
            )
        )
    return rows


def _parse_home_dirs_table(output: str) -> list[tuple[str, ...]]:
    """Разобрать размеры домашних каталогов."""
    rows: list[tuple[str, ...]] = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            size = parts[0].strip()
            path = parts[1].strip()
        else:
            sp = line.split(None, 1)
            if len(sp) >= 2:
                size = sp[0].strip()
                path = sp[1].strip()
            else:
                continue
        user = path.split("/")[-1]
        rows.append((user, size))
    return rows


def _parse_group_table(output: str) -> list[tuple[str, ...]]:
    """Разобрать группы NSS и явно перечисленных участников."""
    rows: list[tuple[str, ...]] = []
    for line in output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) < 4:
            continue
        group_name = parts[0]
        gid = parts[2]
        members = parts[3]
        rows.append((group_name, gid, members))
    return rows


def _parse_df_table(output: str) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for line in output.splitlines():
        parts = line.strip().split(None, 5)
        if len(parts) == 6 and parts[1].isdigit():
            rows.append(tuple(parts))
    return rows


def _parse_ss_row(line: str) -> list[str] | None:
    parts = line.strip().split()
    if not parts:
        return None
    if parts[0] in {"tcp", "tcp6"}:
        if len(parts) < 6 or not parts[1].isdigit():
            return None
        return [
            parts[5],
            parts[1],
            parts[2],
            parts[3],
            parts[4],
            " ".join(parts[6:]) or "Недоступен",
        ]
    if len(parts) < 5 or not parts[1].isdigit() or not parts[2].isdigit():
        return None
    return parts[:5] + [" ".join(parts[5:]) or "Недоступен (проверьте права)"]


def _parse_ss_listening(output: str) -> list[tuple[str, ...]]:
    return [tuple(row) for line in output.splitlines() if (row := _parse_ss_row(line))]


def _parse_ss_established(output: str) -> list[tuple[str, ...]]:
    return [tuple(row) for line in output.splitlines() if (row := _parse_ss_row(line))]


def _parse_login_sessions(output: str) -> list[tuple[str, ...]]:
    """Разобрать свойства сеансов без подмены даты типом или состоянием."""
    sessions: dict[str, tuple[str, ...]] = {}
    for block in re.split(r"\n\s*\n", output.strip()):
        props = {
            key: value
            for line in block.splitlines()
            for key, sep, value in [line.partition("=")]
            if sep
        }
        session = props.get("Id", "")
        if not session or props.get("Class") not in {"user", "user-early"}:
            continue
        state = props.get("State", "")
        if state == "closing":
            continue
        terminal, display = props.get("TTY", ""), props.get("Display", "")
        location = (
            f"{terminal} ({display})"
            if terminal and display
            else terminal or display or "—"
        )
        timestamp = re.sub(r"^[A-Za-z]{3}\s+(?=\d{4}-)", "", props.get("Timestamp", ""))
        remote_host = props.get("RemoteHost", "")
        origin = remote_host or (
            "Удалённый (адрес неизвестен)"
            if props.get("Remote") == "yes"
            else "Локальный"
        )
        sessions[session] = (
            props.get("Name") or props.get("User", "—"),
            session,
            location,
            {
                "x11": "X11",
                "wayland": "Wayland",
                "tty": "TTY",
                "unspecified": "Не указан",
            }.get(props.get("Type", ""), props.get("Type") or "Неизвестен"),
            {"active": "Активен", "online": "Подключён"}.get(
                state, state or "Неизвестно"
            ),
            timestamp or "Неизвестно",
            origin,
        )
    return list(sessions.values())


def _parse_active_sessions(output: str) -> list[tuple[str, ...]]:
    """Использовать один авторитетный источник; who — только ограниченный fallback."""
    source, separator, body = output.partition("\n")
    if not separator:
        raise ValueError("Не указан источник сеансов")
    if source == "DNOSESSIONS|loginctl":
        return _parse_login_sessions(body)
    if source == "DNOSESSIONS|who":
        rows: list[tuple[str, ...]] = []
        for user, terminal, date, started, origin in _parse_who_table(body):
            local_display = origin.startswith("(:") and origin.endswith(")")
            location = f"{terminal} {origin}" if local_display else terminal
            rows.append(
                (
                    user,
                    "—",
                    location,
                    "Неизвестен",
                    "Неизвестно (who)",
                    f"{date} {started}"
                    + (" (год неизвестен)" if not re.match(r"\d{4}-", date) else ""),
                    "Локальный" if local_display else origin.strip("()"),
                )
            )
        return rows
    raise ValueError("Неизвестный источник сеансов")


# Стабильные идентификаторы не зависят от порядка разделов и языка интерфейса.
CATEGORY_IDS = dict(
    zip(
        INFO_CATEGORIES,
        (
            "computer",
            "board",
            "cpu",
            "memory",
            "gpu",
            "storage",
            "network",
            "optical",
            "usb",
            "audio",
            "tcp_listen",
            "tcp_connections",
            "users_summary",
            "users",
            "sessions",
            "logins",
            "failed_logins",
            "homes",
            "groups_summary",
            "groups",
            "filesystems",
        ),
    )
)
BRIEF_CATEGORIES = (
    "Компьютер",
    "Процессор",
    "Память",
    "Накопители",
    "Сеть",
    "Дисковое пространство",
)
HEAVY_CATEGORIES = {"Папки пользователей"}
HARDWARE_CATEGORIES = {"Материнская плата", "Процессор", "Видеокарта"}


def _configure_commands() -> None:
    """Выбирать машинные форматы и не маскировать отказы sentinel-строками."""
    board = INFO_CATEGORIES["Материнская плата"]
    INFO_CATEGORIES["Процессор"][
        "description"
    ] = "Топология по видимым записям /proc/cpuinfo: отключённые CPU могут отсутствовать; число ядер без socket/core не вычисляется."
    fields = {
        "board_vendor": "Производитель платы",
        "board_name": "Модель платы",
        "board_version": "Версия платы",
        "bios_vendor": "Производитель BIOS",
        "bios_version": "Версия BIOS",
        "bios_date": "Дата BIOS",
        "sys_vendor": "Производитель системы",
        "product_name": "Продукт",
        "product_version": "Версия продукта",
        "product_serial": "Серийный номер продукта",
        "board_serial": "Серийный номер платы",
    }
    board["commands"] = {
        key: f"cat /sys/devices/virtual/dmi/id/{key}" for key in fields
    }
    board["parse"] = {
        key: (lambda out, label=label: [(label, out.strip())] if out.strip() else [])
        for key, label in fields.items()
    }
    board["sudo_commands"] = ["product_serial", "board_serial"]
    INFO_CATEGORIES["Память"]["commands"]["dimm"] = "dmidecode -t memory"
    INFO_CATEGORIES["Память"]["field_units"] = {
        name: "bytes"
        for name in (
            "Общая память",
            "Доступная память",
            "Буферы",
            "Кэш",
            "Swap всего",
            "Swap свободно",
        )
    }
    INFO_CATEGORIES["Видеокарта"]["commands"] = {"gpu": "lspci -nnk"}
    INFO_CATEGORIES["Накопители"]["commands"] = {
        "lsblk": "H=$(lsblk --help) || exit; if printf '%s' \"$H\" | grep -q -- '--json'; then if printf '%s' \"$H\" | grep -qw MOUNTPOINTS; then lsblk -J -b -p -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,RM; else lsblk -J -b -p -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,RM; fi; else lsblk -P -b -p -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,RM; fi",
    }
    INFO_CATEGORIES["Оптические приводы"]["commands"] = {
        "cdrom": "find /dev -maxdepth 1 \\( -name 'sr[0-9]*' -o -name 'cdrom*' -o -name 'dvd*' \\) -print",
    }
    INFO_CATEGORIES["USB"]["commands"] = {
        "usb": "lsusb",
        "usb_tree": "lsusb -t",
        "usb_devices_detail": "usb-devices",
    }
    INFO_CATEGORIES["USB"]["parse"]["usb_devices_detail"] = _parse_usb_devices
    INFO_CATEGORIES["Звук"]["commands"] = {
        "audio": "lspci -nnk",
        "alsa": "cat /proc/asound/cards",
    }
    INFO_CATEGORIES["Прослушиваемые порты"]["commands"][
        "listening"
    ] = "if command -v ss >/dev/null; then ss -H -tlnp; else netstat -tlnp; fi"
    INFO_CATEGORIES["Установленные соединения"]["commands"][
        "established"
    ] = "if command -v ss >/dev/null; then ss -H -tnp | awk '$1 == \"ESTAB\"'; else netstat -tnp | awk '$6 == \"ESTABLISHED\"'; fi"
    for name in ("Прослушиваемые порты", "Установленные соединения"):
        INFO_CATEGORIES[name]["sudo_commands"] = list(INFO_CATEGORIES[name]["commands"])
        INFO_CATEGORIES[name][
            "description"
        ] = "Только TCP; видимость PID зависит от прав владельца SSH-сеанса."
    INFO_CATEGORIES["Активные сеансы"]["commands"] = {
        "sessions": (
            "if command -v loginctl >/dev/null && "
            "DNO_SESSIONS=$(SYSTEMD_COLORS=0 loginctl list-sessions --no-legend --no-pager 2>/dev/null); then "
            'DNO_CURRENT=${XDG_SESSION_ID:-}; if [ -z "$DNO_CURRENT" ]; then '
            "DNO_CURRENT=$(loginctl show-session self -p Id 2>/dev/null | awk -F= '$1==\"Id\"{print $2}') || DNO_CURRENT=; fi; "
            "printf 'DNOSESSIONS|loginctl\\n'; "
            "for s in $(printf '%s\\n' \"$DNO_SESSIONS\" | awk '{print $1}'); do "
            '[ "$s" != "$DNO_CURRENT" ] || continue; '
            'if loginctl show-session "$s" --no-pager -p Id -p Name -p User -p Type -p Class -p State -p TTY -p Display -p Timestamp -p Remote -p RemoteHost; '
            "then printf '\\n'; else exit 1; fi; done; "
            "else printf 'DNOSESSIONS|who\\n'; printf 'loginctl unavailable; who provides limited session information\\n' >&2; who; fi"
        )
    }
    INFO_CATEGORIES["Активные сеансы"]["parse"] = {"sessions": _parse_active_sessions}
    INFO_CATEGORIES["Активные сеансы"]["allow_empty_parsed"] = ["sessions"]
    INFO_CATEGORIES["Активные сеансы"][
        "description"
    ] = "Пользовательские сеансы loginctl; собственное SSH-подключение сборщика не учитывается. При недоступном loginctl используется ограниченный вывод who."
    INFO_CATEGORIES["Активные сеансы"]["table_columns"] = [
        "Пользователь",
        "Сеанс",
        "Терминал / дисплей",
        "Тип",
        "Состояние",
        "Начало",
        "Откуда",
    ]
    INFO_CATEGORIES["Последние входы"]["commands"]["last_table"] = "last -F -w -n 20"
    INFO_CATEGORIES["Неудачные входы"]["commands"]["lastb_table"] = "lastb -F -w -n 20"
    INFO_CATEGORIES["Неудачные входы"]["sudo_commands"] = ["lastb_table"]
    INFO_CATEGORIES["Папки пользователей"]["commands"][
        "home_dirs_table"
    ] = "du -s -B1 -x /home/*"
    INFO_CATEGORIES["Папки пользователей"]["sudo_commands"] = ["home_dirs_table"]
    INFO_CATEGORIES["Дисковое пространство"]["commands"][
        "df_all"
    ] = "df -k --output=source,size,used,avail,pcent,target"
    INFO_CATEGORIES["Сводка пользователей"]["commands"][
        "user_count"
    ] = "getent passwd | wc -l"
    for name in ("Сводка пользователей", "Сводка групп"):
        commands = INFO_CATEGORIES[name]["commands"]
        for key in ("sudo_users", "user_priv_groups"):
            if key in commands:
                commands[key] = (
                    'getent group | awk -F: \'$1 == "sudo" || $1 == "wheel" || $1 == "adm"\''
                )
    computer = INFO_CATEGORIES["Компьютер"]["commands"]
    computer["uptime"] = "uptime -p"
    computer["boot_time"] = "uptime -s"
    computer["last_reboot"] = "last -F -w -n 5 reboot"
    computer["timezone"] = (
        "if command -v timedatectl >/dev/null; then timedatectl show -p Timezone; else readlink /etc/localtime; fi"
    )
    computer["services_count"] = (
        "systemctl list-units --type=service --state=running --no-legend | wc -l"
    )
    computer["users_count"] = INFO_CATEGORIES["Активные сеансы"]["commands"]["sessions"]
    INFO_CATEGORIES["Компьютер"]["parse_empty_commands"] = ["users_count"]
    INFO_CATEGORIES["Компьютер"]["parse"]["users_count"] = lambda out: [
        (
            "Пользователей онлайн",
            str(len({row[0] for row in _parse_active_sessions(out)})),
        )
    ]
    users = INFO_CATEGORIES["Сводка пользователей"]
    users["commands"]["user_count"] = "getent passwd"
    users["parse_empty_commands"] = ["user_count", "logged_in_count"]
    users["parse"]["user_count"] = lambda out: [
        ("Учетных записей NSS", str(len(out.splitlines())))
    ]
    users["commands"]["logged_in_count"] = INFO_CATEGORIES["Активные сеансы"][
        "commands"
    ]["sessions"]
    users["parse"]["logged_in_count"] = lambda out: [
        ("Активных сеансов", str(len(_parse_active_sessions(out))))
    ]
    groups = INFO_CATEGORIES["Сводка групп"]
    groups["commands"]["group_count"] = "getent group"
    groups["parse_empty_commands"] = ["group_count"]
    groups["parse"]["group_count"] = lambda out: [
        ("Всего групп", str(len(out.splitlines())))
    ]
    for name, commands in {
        "Видеокарта": ["gpu"],
        "Звук": ["audio"],
        "Накопители": ["lsblk"],
        "Последние входы": ["last_table"],
        "Неудачные входы": ["lastb_table"],
        "Прослушиваемые порты": ["listening"],
        "Установленные соединения": ["established"],
        "Память": ["dimm"],
    }.items():
        INFO_CATEGORIES[name]["allow_empty_parsed"] = commands
    for category in INFO_CATEGORIES.values():
        category["commands"] = {
            key: "export LC_ALL=C LANG=C; " + value
            for key, value in category["commands"].items()
        }


_configure_commands()
