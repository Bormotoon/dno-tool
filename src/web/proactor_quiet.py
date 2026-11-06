# -*- coding: utf-8 -*-
"""Гашение известного шума ProactorEventLoop на Windows.

CPython gh-88964 (bpo-39010): при обрыве TCP-соединения клиентом (RST -
закрытие вкладки / жесткое обновление браузера с открытыми SSE/WebSocket)
``ProactorBasePipeTransport._call_connection_lost()`` вызывает
``sock.shutdown(SHUT_RDWR)`` и получает ``ConnectionResetError``
(WinError 10054); asyncio печатает traceback «Exception in callback»
в stderr. Соединение уже закрывается, на работу серверов это не влияет,
поэтому такие события логируются на DEBUG вместо системного ERROR.
"""

import sys

from src.logger.logger import get_logger

logger = get_logger("dno-tool.proactor")

_CONNECTION_LOST = "_call_connection_lost"
_IGNORED_EXC = (ConnectionResetError, ConnectionAbortedError)


def install_proactor_quiet_handler(loop) -> bool:
    """Установить фильтрующий обработчик исключений event loop.

    Подавляет только известный шум: ConnectionReset/Aborted на Handle
    ``_call_connection_lost``. Все остальные контексты уходят в предыдущий
    обработчик цикла (или стандартный default_exception_handler) прозрачным
    образом - реальные ошибки не скрываются.

    Args:
        loop: активный asyncio event loop (на Windows - ProactorEventLoop).

    Returns:
        True - обработчик установлен, False - не требуется (не Windows).
    """
    if sys.platform != "win32":
        return False

    previous = loop.get_exception_handler()
    default_handler = loop.default_exception_handler

    def _handler(_loop, context):
        try:
            exc = context.get("exception")
            handle = context.get("handle")
            hit_type = isinstance(exc, _IGNORED_EXC)
            hit_origin = (
                _CONNECTION_LOST in repr(handle)
                if handle is not None
                else _CONNECTION_LOST in str(context.get("message") or "")
            )
            if hit_type and hit_origin:
                logger.debug(
                    "Proactor: соединение сброшено клиентом при закрытии транспорта (%s)",
                    exc,
                )
                return
        except Exception:
            # Сам фильтр не должен ронять обработку исключений цикла.
            pass
        if previous is not None:
            try:
                previous(_loop, context)
            except Exception:
                default_handler(context)
        else:
            default_handler(context)

    loop.set_exception_handler(_handler)
    return True
