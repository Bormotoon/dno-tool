"""Управление временем жизни двух направлений сетевого прокси."""

import asyncio
from collections.abc import Coroutine


async def run_duplex(
    outbound: Coroutine[None, None, None],
    inbound: Coroutine[None, None, None],
) -> None:
    """Завершает оба направления при разрыве одного или отмене прокси."""
    tasks = (asyncio.create_task(outbound), asyncio.create_task(inbound))
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Флаг закрытия не прерывает ожидающий sock_recv/receive_bytes.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
