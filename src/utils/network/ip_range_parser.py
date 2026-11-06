"""Общий парсер диапазонов IP-адресов.

Поддерживаемые форматы:
  - CIDR:          192.168.1.0/24
  - Короткий:      192.168.1.1-15   (IPv4 только)
  - Полный:        192.168.1.1-192.168.1.254  (IPv4 + IPv6)
  - Одиночный IP:  192.168.1.1

Используется диалогом «Добавить диапазон» (src/ui/dialogs/common/ip_range.py)
и Сканером сети (src/ui/dialogs/tools/ip_scanner.py), чтобы ввод диапазона
был единообразным в обоих местах.

Функция parse_ip_range возвращает список строк IP-адресов либо None при ошибке.
Ошибки парсинга отображаются через QMessageBox (если show_error=True).
Параметр max_addresses ограничивает максимальное количество адресов
(по умолчанию 1024); None — без ограничения (для сканера).
"""

import ipaddress
from typing import List, Optional

from PySide6.QtWidgets import QMessageBox


def parse_ip_range(
    text: str,
    *,
    show_error: bool = True,
    parent=None,
    max_addresses: Optional[int] = 1024,
) -> Optional[List[str]]:
    """Разобрать строку с диапазоном IP в список адресов.

    Args:
        text: строка от пользователя (CIDR / dash / single IP).
        show_error: показывать ли QMessageBox при ошибках.
        parent: родитель для QMessageBox.
        max_addresses: лимит количества адресов; None — без лимита.

    Returns:
        Список строк IP либо None при ошибке/пустом вводе.
    """
    if not text:
        if show_error:
            QMessageBox.warning(parent, "Ошибка", "Введите IP-адрес, подсеть или диапазон.")
        return None

    if '/' in text:
        return _parse_cidr(text, show_error, parent, max_addresses)
    if '-' in text:
        return _parse_dash(text, show_error, parent, max_addresses)
    return _parse_single(text, show_error, parent)


def _parse_cidr(text, show_error, parent, max_addresses):
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError as e:
        if show_error:
            QMessageBox.warning(parent, "Ошибка парсинга", f"Неверный формат подсети:\n{e}")
        return None

    if network.prefixlen == 32:
        return [str(network.network_address)]
    if network.prefixlen == 31:
        return [str(network.network_address), str(network.broadcast_address)]

    num_hosts = network.num_addresses - 2
    if max_addresses is not None and num_hosts > max_addresses:
        if show_error:
            QMessageBox.warning(
                parent, "Слишком много адресов",
                f"Подсеть содержит {num_hosts} адресов.\n"
                f"Максимум — {max_addresses}. Укажите более узкую подсеть."
            )
        return None

    return [str(h) for h in network.hosts()]


def _parse_dash(text, show_error, parent, max_addresses):
    parts = text.split('-', 1)
    left = parts[0].strip()
    right = parts[1].strip()

    if '.' in right:
        try:
            start_ip = ipaddress.ip_address(left)
            end_ip = ipaddress.ip_address(right)
        except ValueError as e:
            if show_error:
                QMessageBox.warning(parent, "Ошибка парсинга", f"Неверный формат IP:\n{e}")
            return None

        if start_ip.version != end_ip.version:
            if show_error:
                QMessageBox.warning(parent, "Ошибка", "IP-адреса должны быть одной версии.")
            return None
        if start_ip > end_ip:
            if show_error:
                QMessageBox.warning(parent, "Ошибка", "Начальный IP не может быть больше конечного.")
            return None

        count = int(end_ip) - int(start_ip) + 1
        if max_addresses is not None and count > max_addresses:
            if show_error:
                QMessageBox.warning(
                    parent, "Слишком много адресов",
                    f"Диапазон содержит {count} адресов.\nМаксимум — {max_addresses}."
                )
            return None
        return [str(ipaddress.ip_address(i)) for i in range(int(start_ip), int(end_ip) + 1)]

    try:
        end_octet = int(right)
    except ValueError:
        if show_error:
            QMessageBox.warning(
                parent, "Ошибка парсинга",
                f"После дефиса ожидается число или полный IP-адрес:\n{text}"
            )
        return None

    if not (0 <= end_octet <= 255):
        if show_error:
            QMessageBox.warning(parent, "Ошибка", f"Октет должен быть от 0 до 255, получено: {end_octet}")
        return None

    try:
        base_ip = ipaddress.ip_address(left)
    except ValueError as e:
        if show_error:
            QMessageBox.warning(parent, "Ошибка парсинга", f"Неверный формат IP:\n{e}")
        return None

    if base_ip.version != 4:
        if show_error:
            QMessageBox.warning(parent, "Ошибка", "Короткий формат (N-M) поддерживается только для IPv4.")
        return None

    octets = left.split('.')
    base_octet = int(octets[3])
    start_val = int(base_ip)
    end_val = start_val - base_octet + end_octet

    if end_octet < base_octet:
        if show_error:
            QMessageBox.warning(
                parent, "Ошибка",
                f"Конечный октет ({end_octet}) должен быть >= начального ({base_octet})."
            )
        return None

    count = end_val - start_val + 1
    if max_addresses is not None and count > max_addresses:
        if show_error:
            QMessageBox.warning(
                parent, "Слишком много адресов",
                f"Диапазон содержит {count} адресов.\nМаксимум — {max_addresses}."
            )
        return None
    return [str(ipaddress.ip_address(i)) for i in range(start_val, end_val + 1)]


def _parse_single(text, show_error, parent):
    try:
        ipaddress.ip_address(text)
        return [text]
    except ValueError:
        if show_error:
            QMessageBox.warning(
                parent, "Ошибка парсинга",
                "Неверный формат IP-адреса.\n\n"
                "Используйте форматы:\n"
                "  192.168.1.0/24\n"
                "  192.168.1.1-15\n"
                "  192.168.1.1-192.168.1.254"
            )
        return None