"""
Command Service - Сервис для управления командами.

Предоставляет бизнес-логику для операций с командами:
- чтение дерева задач и поиск задачи по iid;
- замена всего дерева с проверкой формата;
- импорт/экспорт
- Публикация событий об изменениях
"""

from typing import List, Dict, Optional, Tuple
from src.architecture import EventType
from src.data import datastore
from src.services.base import BaseService


class CommandService(BaseService):
    """
    Сервис для управления командами.
    
    Инкапсулирует всю бизнес-логику работы с командами,
    предоставляя UI компонентам простой интерфейс.
    """

    def __init__(self, event_bus: Optional['EventBus'] = None) -> None:
        """
        Инициализация сервиса команд.
        
        Args:
            event_bus: Экземпляр EventBus (опционально)
        """
        super().__init__(event_bus)
        self._datastore = datastore

    # ========== ЧТЕНИЕ ДАННЫХ ==========

    def get_all_commands(self) -> List[Dict]:
        """
        Получить все команды.
        
        Returns:
            Список всех команд
        """
        return self._datastore.get_commands_data()

    def get_command_by_iid(self, iid: str) -> Optional[Dict]:
        """
        Найти задачу по iid (первичный метод).
        
        Args:
            iid: Уникальный идентификатор задачи
        
        Returns:
            Данные задачи или None
        """
        return self._datastore.find_task_by_iid(iid)

    def get_commands_count(self) -> int:
        """
        Получить количество команд.

        Returns:
            Количество команд
        """
        return self._datastore.get_commands_count()

    def get_tasks_count(self) -> int:
        """
        Подсчитать общее количество задач (Task) во всей иерархии команд.

        В отличие от ``get_commands_count`` возвращает число реальных задач
        (с полями ``name`` и ``commands``) на всех уровнях вложенности, а не
        количество элементов верхнего уровня.

        Returns:
            Количество задач
        """
        return self._datastore.get_tasks_count()

    # ========== МАССОВЫЕ ОПЕРАЦИИ ==========

    def save_commands(self, commands_data: List[Dict]) -> Tuple[bool, str]:
        """
        Заменить все задачи после проверки формата.

        Args:
            commands_data: Новое дерево задач.

        Returns:
            (успех, текст ошибки для пользователя)
        """
        success = self._datastore.set_commands_data(commands_data)
        if not success:
            return False, self._datastore.commands_error or "Неверный формат задач"

        self._logger.info(f"CommandService: Saved {len(commands_data)} commands")
        self._event_bus.publish_typed(
            EventType.DATA_SAVED,
            source='CommandService',
            data={'type': 'commands', 'count': len(commands_data)}
        )
        return True, ""

    def import_commands_from_file(
        self, file_path: str, mode: str = "merge", resolution: Optional[Dict] = None
    ) -> Tuple[bool, str, Optional[Dict]]:
        """
        Импортировать команды из файла.

        Args:
            file_path: Путь к файлу.
            mode: "merge" (обновить, по умолчанию) или "replace" (заменить все).
            resolution: Решение конфликтов {"name": "mine"|"theirs",
                "parent": "mine"|"theirs"} (только для merge).

        Returns:
            (success, message, stats)
        """
        import json

        from src.services.commands.command_import import format_stats_message

        try:
            if mode not in ("merge", "replace"):
                return False, f"Неизвестный режим импорта: {mode}", None

            if mode == "replace":
                success = self._datastore.load_commands_from_file(file_path)
                if success:
                    count = self.get_tasks_count()
                    self._logger.info(
                        f"CommandService: Imported commands from {file_path} ({count} tasks, replace)"
                    )
                    if self._event_bus:
                        self._event_bus.publish_typed(
                            EventType.DATA_LOADED,
                            source="CommandService",
                            data={
                                "type": "commands",
                                "file": file_path,
                                "count": count,
                                "mode": "replace",
                            },
                        )
                    return (
                        True,
                        f"Импортировано команд: {count}",
                        {"mode": "replace", "total_imported": count},
                    )
                return False, self._datastore.commands_error or "Ошибка при загрузке файла", None

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    imported_data = json.load(f)
            except FileNotFoundError:
                return False, f"Файл не найден: {file_path}", None
            except Exception as e:
                return False, f"Ошибка чтения файла: {e}", None

            if not isinstance(imported_data, list):
                return False, "Файл должен содержать список команд", None

            success, stats = self._datastore.merge_commands_data(
                imported_data, resolution=resolution
            )
            if success:
                stats = {**stats, "mode": "merge"}
                if resolution:
                    stats["conflicts_resolved"] = (
                        "mine" if resolution.get("name") == "mine" else "theirs"
                    )
                message = format_stats_message(stats, "merge")
                count = self.get_tasks_count()
                self._logger.info(
                    f"CommandService: Merged commands from {file_path} ({stats}, total={count})"
                )
                if self._event_bus:
                    self._event_bus.publish_typed(
                        EventType.DATA_LOADED,
                        source="CommandService",
                        data={
                            "type": "commands",
                            "file": file_path,
                            "count": count,
                            "mode": "merge",
                            "stats": stats,
                        },
                    )
                return True, message, stats
            return False, "Ошибка при слиянии команд", None

        except Exception as e:
            self._logger.error(f"CommandService: Error importing commands: {e}")
            return False, str(e), None

    def preview_import_from_file(
        self, file_path: str
    ) -> Tuple[bool, str, Optional[Dict]]:
        """
        Посчитать статистику merge без изменения данных.

        Returns:
            (success, message, stats)
        """
        import copy
        import json

        from src.services.commands.command_import import preview_merge

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                imported_data = json.load(f)
            if not isinstance(imported_data, list):
                return False, "Файл должен содержать список команд", None
            existing = copy.deepcopy(self._datastore.get_commands_data())
            stats = preview_merge(existing, imported_data)
            return True, "Превью готово", stats
        except FileNotFoundError:
            return False, f"Файл не найден: {file_path}", None
        except Exception as e:
            self._logger.error(f"CommandService: Error previewing import: {e}")
            return False, str(e), None

    def find_import_conflicts(self, file_path: str) -> List[Dict]:
        """Найти конфликты импорта без изменения данных.

        Конфликт: тот же iid, но другое имя или другая категория.

        Returns:
            Список конфликтов (пустой если расхождений нет).
        """
        import json

        from src.services.commands.command_import import find_conflicts

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                imported_data = json.load(f)
            if not isinstance(imported_data, list):
                return []
            existing = self._datastore.get_commands_data()
            return find_conflicts(existing, imported_data)
        except Exception as e:
            self._logger.error(f"CommandService: Error finding conflicts: {e}")
            return []

    def export_commands_to_file(self, file_path: str) -> Tuple[bool, str]:
        """
        Экспортировать команды в файл.
        
        Args:
            file_path: Путь к файлу
        
        Returns:
            (success, message)
        """
        try:
            success = self._datastore.save_commands_to_file(file_path)
            
            if success:
                count = self.get_tasks_count()
                self._logger.info(f"CommandService: Exported commands to {file_path} ({count} tasks)")
                
                return True, f"Экспортировано команд: {count}"
            else:
                return False, self._datastore.commands_error or "Ошибка при сохранении файла"
                
        except Exception as e:
            self._logger.error(f"CommandService: Error exporting commands: {e}")
            return False, str(e)
