"""NodeCommandWorker — выполнение команд на хостах удалённой ноды через её API.

Совместим с конвейером MainWindow.execute_command: сигналы
``device_started/progress_update/result_ready/input_requested`` и методы
``abort()``/``send_input()`` зеркалят CommandWorker. На стороне оператора
создаёт по одному ThreadPool на ноду (параллелизм ограничен числом нод),
внутри каждого — один запрос POST /api/node/execute на все устройства ноды
(сервер сам параллелит по хостам). Результаты стримятся polling'ом
(``GET /api/node/execute/{job_id}?last_seq=...`` каждые ~0.3с).
"""

import threading
import uuid
from typing import Any, Dict, List

from PySide6.QtCore import QObject, Signal

from src.domain.models.device import DeviceModel
from src.logger import logger
from src.workers.application_pools import get_application_pool


class NodeCommandWorker(QObject):
    """Оркестратор выполнения команд на хостах одной или нескольких нод."""

    # Зеркало сигналов CommandWorker
    started = Signal()
    finished = Signal()
    error = Signal(str)
    device_started = Signal(DeviceModel)
    progress_update = Signal(DeviceModel, str)
    result_ready = Signal(DeviceModel, bool, object)
    input_requested = Signal(DeviceModel)

    def __init__(
        self,
        devices: List[DeviceModel],
        commands: List,
        timeout: int = 30,
        params: dict | None = None,
        node_service=None,
        parent=None,
        session: bool = False,
        desktop_session: bool = False,
    ):
        super().__init__(parent)
        self._devices = list(devices)
        self._commands = list(commands)
        self._timeout = int(timeout or 30)
        self._params = dict(params or {})
        self._node_service = node_service
        # Флаг задачи session (D2): пробрасывается в payload ноды.
        self._session = bool(session)
        if type(desktop_session) is not bool:
            raise ValueError("desktop_session должен быть boolean")
        self._desktop_session = desktop_session
        self._abort_event = threading.Event()

    # ---------- управление ----------

    def abort(self) -> None:
        self._abort_event.set()

    @property
    def is_aborting(self) -> bool:
        return self._abort_event.is_set()

    def send_input(self, device_iid: str, text: str) -> bool:
        """Интерактивный ввод в PTY на ноде (не поддерживается — задел)."""
        logger.debug(
            f"NodeCommandWorker: send_input для нод не реализован ({device_iid})"
        )
        return False

    # ---------- выполнение ----------

    def _group_devices_by_node(self) -> Dict[str, List[DeviceModel]]:
        """Сгруппировать устройства по node_iid (порядок сохраняется)."""
        groups: Dict[str, List[DeviceModel]] = {}
        for d in self._devices:
            nid = getattr(d, "node_iid", None)
            if nid and nid != "local":
                groups.setdefault(nid, []).append(d)
        return groups

    def _commands_payload(self) -> List[Dict[str, Any]]:
        """Передать исходные шаблоны в порядке выполнения, включая повторы."""
        out = []
        for cmd in self._commands:
            # Тип команды: CommandType -> строка api ("ssh"/"sftp"/"local")
            t = getattr(cmd, "commandType", None)
            t_str = t.value if hasattr(t, "value") else str(t or "ssh")
            out.append({**cmd.to_dict(), "type": t_str})
        return out

    def execute(self) -> None:
        """Выполнить команды на всех устройствах нод (в текущем потоке QThread).

        Вызывается WorkerBridge после thread.started.
        """
        self.started.emit()
        try:
            if self._node_service is None:
                self.error.emit("NodeService недоступен")
                return

            commands_payload = self._commands_payload()
            if not commands_payload:
                self.error.emit("Нет команд для выполнения")
                return
            use_session = self._session and any(
                command["type"] == "ssh" for command in commands_payload
            )

            groups = self._group_devices_by_node()
            if not groups:
                self.error.emit("Нет устройств нод")
                return

            # Устройства → device_start: сразу (всем), чтобы таблица ожила.
            for dev in self._devices:
                if self._abort_event.is_set():
                    break
                self.device_started.emit(dev)

            # Поток на ноду: выполнение (внутри — polling результатов)
            pool = get_application_pool("services").lease(source="application")
            futures = []
            batch_id = uuid.uuid4().hex

            def run_for_node(nid: str, devices: List[DeviceModel]) -> None:
                results_map = {d.iid: d for d in devices}
                if self._abort_event.is_set():
                    for dev in devices:
                        self.result_ready.emit(dev, False, "Отменено")
                    return
                # Capability-гейт session (D3): проверка ДО отправки.
                # Без capability - честный отказ, выполнения без состояния нет.
                if use_session:
                    supports = False
                    try:
                        supports = bool(self._node_service.supports_session(nid))
                    except Exception:
                        supports = False
                    if not supports:
                        try:
                            name = self._node_service.get_display_name(nid)
                        except Exception:
                            name = nid[:8]
                        msg = f"нода {name} не поддерживает общий shell - обновите ноду"
                        for d in devices:
                            self.result_ready.emit(d, False, msg)
                        return
                if (
                    any("desktop_session" in command for command in commands_payload)
                    and not self._node_service.supports_desktop_session_steps(nid)
                ):
                    for dev in devices:
                        self.result_ready.emit(dev, False, "Нода не поддерживает выбор окружения SSH-шага — обновите ноду")
                    return
                if (
                    self._desktop_session
                    and not self._node_service.supports_desktop_session(nid)
                ):
                    for dev in devices:
                        self.result_ready.emit(dev, False, "Нода не поддерживает окружение рабочего стола — обновите ноду")
                    return
                if (
                    any(command.get("background") is True for command in commands_payload)
                    and not self._node_service.supports_background_commands(nid)
                ):
                    for dev in devices:
                        self.result_ready.emit(dev, False, "Нода не поддерживает фоновый запуск — обновите ноду")
                    return
                client = self._node_service.get_client(nid)
                if client is None:
                    for d in devices:
                        self.result_ready.emit(d, False, "Нода недоступна")
                    return
                iids = [d.iid for d in devices]
                # Устройства ноды, чей финальный результат УЖЕ застримен
                # через on_event (device_result). Финальный цикл после
                # execute_commands эмитит только остаточных (401/таймаут/
                # abort/exception) — иначе «нет результата» перезаписал бы
                # уже показанный результат, а статусы всех хостов ноды
                # появлялись бы одновременно в самом конце задания.
                done: set = set()

                def on_event(evt: dict) -> None:
                    diid = evt.get("device_iid")
                    dev = results_map.get(diid)
                    if dev is None:
                        return
                    t = evt.get("type")
                    if t == "progress":
                        chunk = evt.get("chunk", "")
                        if chunk:
                            self.progress_update.emit(dev, chunk)
                    elif t == "device_result":
                        done.add(diid)
                        out = evt.get("output") or ""
                        self.result_ready.emit(dev, bool(evt.get("success")), out)

                def abort_check() -> bool:
                    return self._abort_event.is_set()

                try:
                    result = client.execute_commands(
                        commands_payload,
                        iids,
                        self._timeout,
                        params=self._params,
                        on_event=on_event,
                        abort_check=abort_check,
                        session=use_session,
                        **({"desktop_session": True} if self._desktop_session else {}),
                    )
                except Exception as e:
                    logger.debug(f"NodeCommandWorker: execute on node {nid}: {e}")
                    result = {iid: (False, str(e)) for iid in iids}
                for d in devices:
                    if d.iid in done:
                        continue
                    ok, out = result.get(d.iid, (False, "нет результата"))
                    self.result_ready.emit(d, bool(ok), out or "")

            for nid, devs in groups.items():
                if self._abort_event.is_set():
                    # Отмечаем оставшихся как отменённые
                    for d in devs:
                        self.result_ready.emit(d, False, "Отменено")
                    continue
                try:
                    future = pool.submit(
                        run_for_node,
                        nid,
                        devs,
                        metadata={
                            "type": "node_command_control",
                            "node_iid": nid,
                            "batch_id": batch_id,
                        },
                    )
                except RuntimeError as error:
                    for dev in devs:
                        self.result_ready.emit(dev, False, str(error))
                else:
                    futures.append(future)

            try:
                for future in futures:
                    future.result()
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        except Exception as e:
            logger.error(f"NodeCommandWorker: {e}")
            self.error.emit(str(e))
        finally:
            self.finished.emit()
