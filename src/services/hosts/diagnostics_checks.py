"""Общие хелперы проверок для диалога «Диагностика».

Вынесено из src/ui/dialogs/common/vnc_settings.py, чтобы переиспользовать
в DiagnosticsWorker и других модулях. Содержит:
  - иконки результатов (ICON_* — строковые ключи в ICONS);
  - русификацию systemctl (ACTIVE_MAP, ACTIVE_STATUS_ICON);
  - tcp_ping(host, port) → (bool, latency_ms|None);
  - check_rfb_banner(host, port) → (bool, banner_str);
  - vnc_auth_check(host, vnc_port, password) → (icon_key, text);
  - read_service_status_ssh(ssh, service) → active_raw;
  - run_ssh_cmd(ssh, cmd, timeout) → (exit_code, stdout_str).

Все функции безопасны к вызову из worker-потоков (не трогают Qt).
"""

from __future__ import annotations

import socket
import struct
import time
from typing import Optional, Tuple

from src.services.hosts.diagnostics_state import Exchange, OperationControl

# ================================================================
#  Иконки результатов (строковые ключи в ICONS)
# ================================================================
ICON_OK = "result_success"
ICON_ERR = "result_failure"
ICON_WARN = "result_warning"
ICON_SKIP = "result_skip"
ICON_EXECUTING = "result_executing"


# ================================================================
#  Маппинги русификации systemctl
# ================================================================

# Текстовые подписи статусов (без эмодзи — иконку определяет ACTIVE_STATUS_ICON).
ACTIVE_MAP = {
    "active": "Активен",
    "inactive": "Неактивен",
    "failed": "Сбой",
    "activating": "Запускается",
    "deactivating": "Останавливается",
    "unknown": "Неизвестно",
    "?": "?",
}

# Иконка для каждого raw-статуса active.
ACTIVE_STATUS_ICON = {
    "active": ICON_OK,
    "inactive": ICON_WARN,
    "failed": ICON_ERR,
    "activating": ICON_EXECUTING,
    "deactivating": ICON_EXECUTING,
    "unknown": ICON_ERR,
    "?": ICON_ERR,
}


# ================================================================
#  TCP-ping
# ================================================================


def tcp_ping(
    host: str, port: int, timeout: float = 2.0
) -> Tuple[bool, Optional[float]]:
    """TCP-подключение к host:port с замером задержки.

    Returns:
        (ok, latency_ms) — ok=True если соединение установлено,
        latency_ms — время установления соединения в мс (или None при ошибке).
    """
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            latency = (time.monotonic() - t0) * 1000.0
            return True, latency
    except Exception:
        return False, None


def tcp_probe(
    host: str, port: int, timeout: float = 2.0, control: OperationControl | None = None
) -> tuple[bool, float | None, str]:
    """TCP-задержка с различением сетевых ошибок и отменой своего сокета."""
    start = time.monotonic()
    try:
        addresses = socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False, None, "Ошибка разрешения имени"
    reason = "Порт недоступен"
    for family, kind, proto, _, address in addresses:
        remaining = timeout - (time.monotonic() - start)
        if remaining <= 0:
            return False, None, "Таймаут подключения"
        sock = socket.socket(family, kind, proto)
        exchange = None
        try:
            exchange = Exchange(sock, remaining, control)
            sock.settimeout(remaining)
            sock.connect(address)
            return True, (time.monotonic() - start) * 1000, "Открыт"
        except ConnectionRefusedError:
            reason = "В подключении отказано"
        except (socket.timeout, TimeoutError):
            reason = "Таймаут подключения"
        except InterruptedError:
            return False, None, "Отменено"
        except OSError:
            reason = "Сетевая ошибка"
        finally:
            exchange.finish() if exchange else sock.close()
    return False, None, reason


# ================================================================
#  RFB banner check
# ================================================================


def check_rfb_banner(
    host: str, port: int, timeout: float = 5.0, control: OperationControl | None = None
) -> Tuple[bool, str]:
    """Прочитать первые 12 байт с RFB-сервера (ожидается 'RFB 003.008').

    Returns:
        (ok, banner) — ok=True если ответ начинается с 'RFB ',
        banner — прочитанная строка (или сообщение об ошибке).
    """
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.settimeout(timeout)
        try:
            exchange = Exchange(sock, timeout, control)
            data = exchange.read(12)
        finally:
            try:
                exchange.finish()
            except Exception:
                pass
        if data in (b"RFB 003.003\n", b"RFB 003.007\n", b"RFB 003.008\n"):
            return True, data.decode("ascii", errors="replace").strip()
        return False, f"Неожиданный ответ: {data[:20]!r}"
    except socket.timeout:
        return False, "Таймаут (нет ответа RFB)"
    except ConnectionRefusedError:
        return False, "В подключении отказано"
    except Exception as e:
        return False, str(e)


# ================================================================
#  VNC Authentication (security type 2)
# ================================================================


def vnc_auth_check(
    host: str, vnc_port: int, password: str, control: OperationControl | None = None
) -> Tuple[str, str]:
    """Проверка VNC-аутентификации (security type 2) до SecurityResult.

    См. подробное описание в src/ui/dialogs/common/vnc_settings.py
    (функция _vnc_auth_check) — здесь тот же алгоритм, вынесенный в общий модуль.

    Returns:
        (icon_key, text), где icon_key — строковый ключ в ICONS:
        (ICON_OK, «ОК») — пароль принят (SecurityResult=0)
        (ICON_ERR, «неверный пароль») — SecurityResult=1
        (ICON_WARN, «без пароля») — сервер предложил только None auth (type 1)
        (ICON_ERR, «нет VNC Auth») — сервер не предложил type 2 и type 1
        (ICON_ERR, «{ошибка}») — таймаут/отказ/иная ошибка
        (ICON_SKIP, «—») — пароль не задан (проверка пропущена)
    """
    if not password:
        return ICON_SKIP, "Пароль не задан"

    try:
        sock = socket.create_connection((host, vnc_port), timeout=5.0)
        sock.settimeout(5.0)
    except socket.timeout:
        return ICON_ERR, "Таймаут подключения"
    except ConnectionRefusedError:
        return ICON_ERR, "В подключении отказано"
    except Exception as e:
        return ICON_ERR, str(e)

    try:
        exchange = Exchange(sock, 10.0, control)
        server_ver = exchange.read(12)
        versions = {b"RFB 003.003\n": 3, b"RFB 003.007\n": 7, b"RFB 003.008\n": 8}
        version = versions.get(server_ver)
        if version is None:
            return ICON_ERR, "Неподдерживаемая версия RFB"
        sock.sendall(server_ver)
        if version == 3:
            security = struct.unpack(">I", exchange.read(4))[0]
            types = [security] if security else []
        else:
            count = exchange.read(1)[0]
            types = exchange.read(count) if count else []
        if not types:
            reason_len = struct.unpack(">I", exchange.read(4))[0]
            if reason_len > 65536:
                return ICON_ERR, "Слишком длинная причина отказа RFB"
            exchange.read(reason_len)
            return ICON_ERR, "Сервер отклонил соединение"
        if 2 in types:
            if version != 3:
                sock.sendall(bytes([2]))
        elif 1 in types:
            if version != 3:
                sock.sendall(bytes([1]))
            if version == 8 and struct.unpack(">I", exchange.read(4))[0] != 0:
                return ICON_ERR, "None auth отклонён"
            return ICON_WARN, "Без пароля"
        else:
            return ICON_ERR, "Нет VNC Auth"

        # 4. VNC Auth: 16-байтный challenge
        challenge = exchange.read(16)

        # 5. Подготовка ключа: пароль → UTF-8 → обрезка/дополнение до 8 байт
        pwd_bytes = password.encode("utf-8")[:8]
        pwd_bytes = pwd_bytes.ljust(8, b"\x00")
        key_bytes = bytes(int(f"{b:08b}"[::-1], 2) for b in pwd_bytes)
        key_24 = key_bytes * 3

        from cryptography.hazmat.primitives.ciphers import Cipher, modes

        try:
            from cryptography.hazmat.decrepit.ciphers.algorithms import (
                TripleDES as _DES,
            )
        except ImportError:
            from cryptography.hazmat.primitives.ciphers.algorithms import (
                TripleDES as _DES,
            )
        cipher = Cipher(_DES(key_24), modes.ECB())
        encryptor = cipher.encryptor()
        response = encryptor.update(challenge) + encryptor.finalize()

        # 6. Отправка ответа
        sock.sendall(response)

        # 7. SecurityResult: 4 байта BE uint32 (0 = OK, 1 = failed)
        result = struct.unpack(">I", exchange.read(4))[0]

        if result == 0:
            return ICON_OK, "ОК"
        if result == 1:
            return ICON_ERR, "Неверный пароль"
        return ICON_ERR, f"Код {result}"
    except socket.timeout:
        return ICON_ERR, "Таймаут (нет ответа RFB)"
    except ConnectionRefusedError:
        return ICON_ERR, "В подключении отказано"
    except Exception as e:
        return ICON_ERR, str(e)
    finally:
        try:
            exchange.finish() if "exchange" in locals() else sock.close()
        except Exception:
            pass


# ================================================================
#  SSH helpers
# ================================================================


def run_ssh_cmd(
    ssh, cmd: str, timeout: int = 10, stdin_data: str | None = None
) -> Tuple[int, str]:
    """Выполнить команду с дедлайном, дренированием stderr и секретом в stdin."""
    channel = None
    deadline = time.monotonic() + timeout
    try:
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        channel = stdout.channel
        if stdin_data is not None:
            stdin.write(stdin_data)
            stdin.flush()
        stdin.channel.shutdown_write()
        output = bytearray()
        errors = bytearray()
        while True:
            if time.monotonic() >= deadline:
                return -1, "Таймаут команды SSH"
            while channel.recv_ready():
                output.extend(channel.recv(32768))
                if len(output) > 1048576:
                    return -1, "Слишком большой ответ SSH"
            while channel.recv_stderr_ready():
                errors.extend(channel.recv_stderr(32768))
                if len(errors) > 1048576:
                    return -1, "Слишком большой stderr SSH"
            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                break
            if channel.closed:
                return -1, "SSH-канал закрыт"
            time.sleep(0.01)
        code = channel.recv_exit_status()
        text = (
            bytes(output + (b"\n" + errors if errors else b""))
            .decode("utf-8", errors="replace")
            .strip()
        )
        if stdin_data:
            text = text.replace(stdin_data.rstrip("\n"), "[скрыто]")
        return code, text
    except Exception as error:
        return -1, f"Ошибка команды SSH: {type(error).__name__}"
    finally:
        if channel is not None:
            channel.close()


def read_service_status_ssh(ssh, service: str) -> str:
    """Через SSH получить active-статус systemd-сервиса.

    Возвращает сырой ключ systemctl (например «active», «inactive»,
    «failed»). Для русификации используйте ACTIVE_MAP, для иконки —
    ACTIVE_STATUS_ICON.
    """
    _, active_raw = run_ssh_cmd(ssh, f"systemctl is-active {service} 2>/dev/null")
    active_raw = (active_raw or "unknown").strip()
    return active_raw
