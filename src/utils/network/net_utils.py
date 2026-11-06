"""Сетевые утилиты без зависимостей от Qt/async.

Чистые функции, переиспользуемые как веб-сервером, так и GUI-воркерами.
"""

import ipaddress
import socket
from typing import Optional


def tcp_ping(host: str, port: int, timeout: float = 2.0) -> bool:
    """Проверить доступность TCP-порта через connect_ex.

    Возвращает True, если порт принимает соединение; False — иначе
    (включая любые сетевые ошибки). Не бросает исключений.

    Используется веб-сервером (проверка VNC/Veyon/SSH-порта) и GUI-пингом
    хостов (HostPingTimer) — единая реализация, ранее дублировавшаяся.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, int(port))) == 0
    except Exception:
        return False
    finally:
        sock.close()


def get_broadcast_addresses() -> list:
    """Все directed broadcast-адреса активных IPv4-интерфейсов.

    Перебирает NIC через psutil.net_if_addrs(), для каждого IPv4 адреса
    с маской считает network broadcast (ip | ~netmask). Пропускает
    loopback (127.x), link-local (169.254.x), и адреса без маски.

    Возвращает уникальные directed broadcast-адреса (например
    ['192.168.1.255', '10.0.0.255']). При ошибке или отсутствии psutil
    возвращает пустой список — caller должен сделать fallback на
    ограниченный broadcast 255.255.255.255.
    """
    try:
        import psutil
    except Exception:
        return []

    broadcasts = set()
    try:
        for addrs in psutil.net_if_addrs().values():
            for snic in addrs:
                if snic.family != socket.AF_INET:
                    continue
                addr = snic.address
                netmask = snic.netmask
                if not addr or not netmask:
                    continue
                if addr.startswith("127.") or addr.startswith("169.254."):
                    continue
                try:
                    iface = ipaddress.IPv4Interface(f"{addr}/{netmask}")
                    bcast = str(iface.network.broadcast_address)
                    if bcast == addr:
                        continue
                    broadcasts.add(bcast)
                except (ValueError, ipaddress.AddressValueError):
                    continue
    except Exception:
        return []

    return list(broadcasts)


def _default_route_source_ip() -> Optional[str]:
    """Src-IP маршрута по умолчанию (UDP-trick, без реальных пакетов).

    UDP-сокет connect() на внешний адрес не отправляет данных, но ОС
    выполняет выбор маршрута и интерфейса: getsockname() возвращает
    локальный адрес, с которого пошёл бы трафик. Учитывает VPN/Wi-Fi/
    Ethernet ровно так, как реальный роутинг.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0)
        try:
            sock.connect(("8.8.8.8", 80))
        except OSError:
            return None
        addr = sock.getsockname()[0]
        return addr or None
    except Exception:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def source_ip_for_target(target: str, *, fallback: bool = True) -> Optional[str]:
    """Локальный IP, с которого пошел бы трафик к target.

    UDP-сокет connect() не отправляет пакетов, но ОС выполняет выбор
    маршрута и интерфейса: getsockname() возвращает src-IP именно для
    этого назначения (учитывает LAN/VPN/несколько NIC). Работает даже
    для недоступных хостов. Fallback - src маршрута по умолчанию;
    fallback=False сохраняет неизвестный источник как None.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0)
        try:
            sock.connect((target, 80))
        except OSError:
            return _default_route_source_ip() if fallback else None
        addr = sock.getsockname()[0]
        return addr or (_default_route_source_ip() if fallback else None)
    except Exception:
        return _default_route_source_ip() if fallback else None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _iter_ipv4_nics() -> list:
    """Все IPv4-адреса NIC (address, netmask) без loopback/link-local."""
    nics = []
    try:
        import psutil
    except Exception:
        return nics
    try:
        for addrs in psutil.net_if_addrs().values():
            for snic in addrs:
                if snic.family != socket.AF_INET:
                    continue
                addr = snic.address
                netmask = snic.netmask
                if not addr or not netmask:
                    continue
                if addr.startswith("127.") or addr.startswith("169.254."):
                    continue
                nics.append((addr, netmask))
    except Exception:
        return []
    return nics


def _is_rfc1918(ip_str: str) -> bool:
    """Приватный диапазон IPv4 (RFC1918)."""
    try:
        return ipaddress.ip_address(ip_str).is_private
    except (ValueError, ipaddress.AddressValueError):
        return False


def get_default_ipv4_interface() -> Optional[ipaddress.IPv4Interface]:
    """IPv4-интерфейс основного маршрута: IP + маска локальной сети.

    Порядок:
      1. UDP-trick - src-IP маршрута по умолчанию, затем его маска из
         psutil.net_if_addrs() (интерфейс выбирает ОС: VPN/Wi-Fi/Ethernet).
      2. Fallback: первый не-loopback не-link-local IPv4 с маской,
         предпочитая RFC1918-адреса (офлайн-машина с LAN).
    Возвращает ipaddress.IPv4Interface или None (нет psutil/адресов).
    """
    src_ip = _default_route_source_ip()
    if src_ip:
        for addr, netmask in _iter_ipv4_nics():
            if addr == src_ip:
                try:
                    return ipaddress.IPv4Interface(f"{addr}/{netmask}")
                except (ValueError, ipaddress.AddressValueError):
                    break

    best = None
    for addr, netmask in _iter_ipv4_nics():
        try:
            iface = ipaddress.IPv4Interface(f"{addr}/{netmask}")
        except (ValueError, ipaddress.AddressValueError):
            continue
        if best is None or (_is_rfc1918(addr) and not _is_rfc1918(best.ip)):
            best = iface
    return best
