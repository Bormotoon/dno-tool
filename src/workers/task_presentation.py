"""Общие понятные названия операций для Qt- и веб-монитора."""

from __future__ import annotations

from typing import Any

_TYPE_LABELS = {
    "host_ping": "Проверка доступности",
    "thumbnail": "Миниатюра",
    "veyon_thumbnail": "Миниатюра",
    "node_thumbnail": "Миниатюра ноды",
    "command": "Команда",
    "diagnostics": "Диагностика",
    "key_install": "SSH-ключ",
    "vnc_install": "Установка VNC",
    "service_install": "Установка сервиса",
    "veyon_feature": "Действие Veyon",
    # Сканер хостов (ip_scanner: фазы + плоские пробы портов).
    "ping": "Пинг (скан)",
    "names": "Имена",
    "ports": "Пробы портов",
    "finish": "Анализ",
    "port_probe": "Проба порта",
    # Ноды (federation).
    "node_veyon_feature": "Действие Veyon (нода)",
    "node_host_crud": "Хост на ноде",
    "node_status": "Статус ноды",
    "node_release": "Освобождение миниатюр",
    "node_info": "Сведения о хосте (нода)",
    "host_info": "Сведения о хосте",
    "node_owner_status": "Проверка доступности",
    "node_feature": "Действие Veyon",
    "host_info_ping": "Проверка доступности",
    "node_command_control": "Команда на ноде",
    "node_vnc_url": "Удалённый экран ноды",
    "vnc_screens": "Мониторы хоста для окна VNC",
    "veyon_status": "Статус Veyon",
    "camera_ping": "Проверка камеры",
    "thumbnail_release": "Освобождение миниатюр",
    "thumbnail_housekeeping": "Обслуживание миниатюр",
    "media_probe": "Анализ медиафайла",
    "ansible_preview": "Разбор Ansible",
    "web_server_settings": "Управление веб-сервером",
    "web_ldap_check": "Проверка входа LDAP",
    "node_server_settings": "Управление сервером нод",
}


_TYPE_LABELS_WEB = {
    "web_scan": "Сканирование TCP-портов",
    "web_ping": "Проверка доступности (ICMP)",
    "web_camera_preview": "Просмотр камер",
    "web_camera_snapshot": "Получение кадра камеры",
    "web_resources": "Мониторинг ресурсов",
    "web_veyon_restart": "Перезапуск сервиса Veyon",
    "web_device_info": "Сбор сведений об устройстве",
    "web_terminal": "SSH-терминал",
    "web_files": "Файловая операция SFTP",
    "web_files_transfer": "Передача файла SFTP",
    "web_highlight": "Подсветка команд",
    "web_command": "Команда",
    "web_thumbnail": "Миниатюра",
    "web_status": "Проверка доступности",
    "web_power": "Управление питанием",
    "web_veyon_feature": "Действие Veyon",
    "web_job": "Задание",
    "web_rfb": "Удалённый экран",
    "node_thumbnail": "Миниатюра ноды",
}

_FEATURE_LABELS = {
    "lock": "Блокировка экрана",
    "unlock": "Разблокировка экрана",
    "lock_input": "Блокировка ввода",
    "unlock_input": "Разблокировка ввода",
    "ssh_lock": "Блокировка экрана (SSH)",
    "ssh_unlock": "Разблокировка экрана (SSH)",
    "ssh_lock_input": "Блокировка ввода (SSH)",
    "ssh_unlock_input": "Разблокировка ввода (SSH)",
    "power_on": "Включение",
    "powerdown": "Выключение",
    "reboot": "Перезагрузка",
    "logoff": "Завершение сеанса",
    "logoff_webapi": "Завершение сеанса (Veyon)",
    "login": "Вход в систему",
    "login_ssh": "Вход в систему (SSH)",
    "website": "Открытие сайта",
    "start_app": "Запуск приложения",
    "message": "Отправка сообщения",
}


def task_label(fn: str, metadata: dict[str, Any]) -> str:
    """Действие без адресата: хост и нода выводятся в отдельной колонке."""
    mtype = str(metadata.get("type") or "")
    feature = str(metadata.get("feature") or "")
    fallback = {
        "_resources": "Мониторинг ресурсов",
        "_camera_snapshot": "Получение кадра камеры",
        "system_probe": "Проверка доступности (ICMP)",
        "tcp_probe": "Проверка TCP-порта",
        "_decode": "Просмотр камер",
    }.get(fn.rsplit(".", 1)[-1], "Фоновая операция")
    type_label = _TYPE_LABELS.get(mtype, _TYPE_LABELS_WEB.get(mtype, fallback))
    if mtype == "port_probe":
        port = metadata.get("port")
        if port is not None:
            return f"{type_label} · {port}"
    if mtype == "node_release":
        count = metadata.get("count")
        if count is not None:
            return f"{type_label} ({count})"
    action = metadata.get("action")
    prefix = (
        action
        if isinstance(action, str) and action
        else _FEATURE_LABELS.get(feature, type_label)
    )
    backend = metadata.get("backend")
    if backend in ("veyon", "vnc"):
        prefix += f" [{backend.upper()}]"
    return prefix
