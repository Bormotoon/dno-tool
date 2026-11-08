from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from src.config.settings import LOGS_PATH
from src.domain.device_info import CategoryResult, DeviceInfoSnapshot, InfoCell, InfoStatus
from src.services.hosts.device_info.compare import compare_snapshots

CRITICAL_HARDWARE_CATEGORIES = (
    "Материнская плата",
    "Процессор",
    "Оперативная память",
    "Видеокарта",
    "Диски и разделы",
)


class HardwareBaselineStore:
    """Персистентное SQLite-хранилище эталонных снимков конфигурации устройств."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._path = Path(db_path) if db_path else Path(LOGS_PATH) / "hardware_baselines.sqlite3"
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self._path), timeout=5.0)

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS baselines (
                    device_key TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    name TEXT NOT NULL,
                    saved_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _key_str(device_key: tuple[str, str]) -> str:
        return f"{device_key[0]}::{device_key[1]}"

    def save_baseline(self, snapshot: DeviceInfoSnapshot) -> None:
        categories_payload: dict[str, Any] = {}
        for cat_name, cat in snapshot.categories.items():
            categories_payload[cat_name] = {
                "category_id": cat.category_id,
                "name": cat.name,
                "columns": list(cat.columns),
                "rows": [[cell.display for cell in row] for row in cat.rows],
                "status": str(cat.status),
                "timestamp": cat.timestamp,
            }
        payload = {
            "device_key": list(snapshot.device_key),
            "name": snapshot.name,
            "host": snapshot.host,
            "owner": snapshot.owner,
            "transport": snapshot.transport,
            "endpoint": snapshot.endpoint,
            "profile": snapshot.profile,
            "started_at": snapshot.started_at,
            "finished_at": snapshot.finished_at,
            "categories": categories_payload,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO baselines (device_key, host, name, saved_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._key_str(snapshot.device_key),
                    snapshot.host,
                    snapshot.name,
                    snapshot.finished_at or snapshot.started_at,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def load_baseline(self, device_key: tuple[str, str]) -> DeviceInfoSnapshot | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM baselines WHERE device_key = ?",
                (self._key_str(device_key),),
            ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        snap = DeviceInfoSnapshot(
            device_key=tuple(data["device_key"]),
            name=data["name"],
            host=data["host"],
            owner=data.get("owner", ""),
            transport=data.get("transport", "ssh"),
            endpoint=data.get("endpoint", data["host"]),
            profile=data.get("profile", "full"),
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""),
        )
        for cat_name, cat_data in data.get("categories", {}).items():
            rows = tuple(
                tuple(InfoCell(display=str(cell), value=str(cell)) for cell in r)
                for r in cat_data.get("rows", [])
            )
            snap.categories[cat_name] = CategoryResult(
                category_id=cat_data.get("category_id", cat_name),
                name=cat_name,
                columns=tuple(cat_data.get("columns", ("Свойство", "Значение"))),
                rows=rows,
                status=InfoStatus(cat_data.get("status", "complete")),
                timestamp=cat_data.get("timestamp", ""),
            )
        return snap

    def diff_against_baseline(
        self, current: DeviceInfoSnapshot, hardware_only: bool = True
    ) -> list[tuple[str, str, str, str, str]]:
        baseline = self.load_baseline(current.device_key)
        if baseline is None:
            return []
        diffs = compare_snapshots(baseline, current)
        if hardware_only:
            diffs = [d for d in diffs if d[0] in CRITICAL_HARDWARE_CATEGORIES]
        return diffs
