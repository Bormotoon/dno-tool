from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Callable, Literal
import uuid

from PySide6.QtCore import QObject, QTimer, Signal

from src.logger import logger
from src.services.base import BaseService


@dataclass
class ScheduleRule:
    iid: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Новое правило"
    enabled: bool = True
    days_of_week: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # 0=Пн .. 6=Вс
    time_hhmm: str = "19:30"
    action_type: Literal["task", "feature"] = "feature"
    action_id: str = "powerdown"  # task_iid или токен фичи (power_on, powerdown, reboot)
    target_folder: str = ""       # Пусто = отмеченные в дереве или список target_iids
    target_iids: list[str] = field(default_factory=list)
    last_run_date: str = ""

    def should_trigger(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        if now.weekday() not in self.days_of_week:
            return False
        today_str = now.strftime("%Y-%m-%d")
        if self.last_run_date == today_str:
            return False
        current_hhmm = now.strftime("%H:%M")
        return current_hhmm == self.time_hhmm


class TaskSchedulerService(BaseService, QObject):
    """Сервис выполнения задач и команд питания по расписанию (по дням недели и времени)."""

    rule_triggered = Signal(object)  # ScheduleRule

    def __init__(self, event_bus=None) -> None:
        BaseService.__init__(self, event_bus=event_bus)
        QObject.__init__(self)
        self._rules: list[ScheduleRule] = []
        self._timer = QTimer(self)
        self._timer.setInterval(15_000)
        self._timer.timeout.connect(self._tick)
        self._executor_callback: Callable[[ScheduleRule], None] | None = None
        self._storage_path = Path.home() / ".dno-tool" / "schedule_rules.json"
        self._load_rules()

    def set_executor(self, callback: Callable[[ScheduleRule], None]) -> None:
        self._executor_callback = callback

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def get_rules(self) -> list[ScheduleRule]:
        return [ScheduleRule(**asdict(r)) for r in self._rules]

    def set_rules(self, rules: list[ScheduleRule]) -> None:
        self._rules = list(rules)
        self._save_rules()

    def _load_rules(self) -> None:
        if self._storage_path.is_file():
            try:
                data = json.loads(self._storage_path.read_text(encoding="utf-8"))
                self._rules = [ScheduleRule(**r) for r in data]
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Не удалось прочитать правила расписания ({exc})")

    def _save_rules(self) -> None:
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            payload = [asdict(r) for r in self._rules]
            self._storage_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Не удалось сохранить правила расписания ({exc})")

    def _tick(self) -> None:
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        for rule in self._rules:
            if rule.should_trigger(now):
                rule.last_run_date = today_str
                self._save_rules()
                logger.info(
                    f"Сработало правило расписания: {rule.name} ({rule.action_type}:{rule.action_id})"
                )
                self.rule_triggered.emit(rule)
                if self._executor_callback is not None:
                    try:
                        self._executor_callback(rule)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            f"Ошибка запуска правила расписания {rule.name}: {exc}"
                        )
