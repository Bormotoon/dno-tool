"""Категории ошибок и диагностический отчёт без путей и текста титров."""

from __future__ import annotations

import errno
import json
import re
from dataclasses import asdict, dataclass

_URL = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s'\"]+", re.IGNORECASE)
_QUOTED_PATH = re.compile(r"""(['"])(?:[A-Za-z]:[\\/]|\\\\|[\\/]|\.{1,2}[\\/])(?:(?!\1).)*\1""")
_WINDOWS_PATH = re.compile(r"(?:\b[A-Za-z]:[\\/]|\\\\)[^\s'\",;]*")
_UNIX_PATH = re.compile(r"(?<![\w.])/(?:[^\s/'\",;:]+/)*[^\s/'\",;:]+")
_SECRET = re.compile(
    r"\b(password|passwd|pass|pwd|token|secret|apikey|key)=[^\s:,;]+", re.IGNORECASE
)
_TITLE = re.compile(r"\btext=(?:'[^']*'|[^\s:,;]+)")
DETAIL_LIMIT = 500


def sanitize_detail(text: object) -> str:
    """Убрать из нативной ошибки адреса, пути, пароли и текст титров."""
    value = str(text or "")
    value = _URL.sub("<адрес>", value)
    value = _QUOTED_PATH.sub("<путь>", value)
    value = _WINDOWS_PATH.sub("<путь>", value)
    value = _UNIX_PATH.sub("<путь>", value)
    value = _SECRET.sub(lambda match: f"{match[1]}=<скрыто>", value)
    value = _TITLE.sub("text=<скрыто>", value)
    return " ".join(value.split())[:DETAIL_LIMIT]


def error_detail(error: BaseException) -> str:
    """Очищенный настоящий текст ошибки: сервис хранит его отдельно от сообщения."""
    media = getattr(error, "error", None)
    return getattr(media, "detail", "") or sanitize_detail(error)


@dataclass(frozen=True)
class VideoFailure:
    category: str
    title: str
    action: str
    stage: str
    return_code: int | None = None
    detail: str = ""

    @classmethod
    def classify(
        cls, message: str, stage: str = "export", error: Exception | None = None
    ) -> VideoFailure:
        detail = error_detail(error) if error is not None else ""
        text = f"{message} {detail}".casefold()
        code = re.search(r"(?:exit|код|returncode)\s*[=: ]\s*(-?\d+)", text)
        category, title, action = (
            "unknown",
            "Не удалось выполнить операцию",
            "Проверьте исходники и параметры. Откройте диагностический отчёт.",
        )
        patterns = (
            ("cancelled", "Операция отменена", "Можно повторить операцию.", ("отмен",)),
            (
                "disk_full",
                "Недостаточно места",
                "Освободите место на диске результата и временных файлов.",
                ("no space left", "disk full", "недостаточно места", "enospc"),
            ),
            (
                "permission",
                "Нет доступа к файлу",
                "Выберите доступную папку или закройте программу, использующую файл.",
                (
                    "permission denied",
                    "access is denied",
                    "отказано в доступе",
                    "нет доступа",
                    "занят другим",
                ),
            ),
            (
                "encoder_busy",
                "Кодировщик или устройство недоступны",
                "Освободите GPU, проверьте драйвер или явно выберите libx264 в настройках экспорта.",
                (
                    "no capable devices",
                    "cannot load nvcuda",
                    "device busy",
                    "cannot init",
                    "no device",
                    "openencodesession",
                    "gpu",
                    "устройств",
                ),
            ),
            (
                "missing_filter",
                "Нет нужного фильтра FFmpeg",
                "Проверьте полноту медиабиблиотек в поставке приложения.",
                (
                    "no such filter",
                    "filter not found",
                    "отсутствует drawtext",
                    "нет фильтра",
                ),
            ),
            (
                "missing_codec",
                "Кодек недоступен",
                "Выберите доступный кодировщик или проверьте поставку медиабиблиотек.",
                (
                    "unknown encoder",
                    "encoder not found",
                    "unknown decoder",
                    "не найден кодек",
                ),
            ),
            (
                "source",
                "Исходник недоступен или повреждён",
                "Перепривяжите исходник или создайте отдельную исправленную копию.",
                (
                    "invalid data found",
                    "moov atom not found",
                    "no such file",
                    "исходник изменился",
                    "не удалось проверить медиаисточник",
                    "отсутствует",
                ),
            ),
            (
                "container",
                "Потоки несовместимы с контейнером",
                "Выберите MKV или перекодирование.",
                (
                    "not supported in container",
                    "could not find tag",
                    "muxer",
                    "несовместим",
                    "контейнер",
                ),
            ),
        )
        for key, heading, suggestion, terms in patterns:
            if any(term in text for term in terms):
                category, title, action = key, heading, suggestion
                break
        if isinstance(error, OSError):
            if error.errno == errno.ENOSPC:
                category, title, action = patterns[1][:3]
            elif error.errno in (errno.EACCES, errno.EPERM):
                category, title, action = patterns[2][:3]
        return cls(
            category, title, action, stage, int(code[1]) if code else None, detail
        )

    def report(self, parameters: dict) -> str:
        # Белый список исключает пути, drawtext/filtergraph и подписи даже
        # при добавлении новых параметров вызывающей стороной.
        allowed = {
            "mode",
            "vcodec",
            "acodec",
            "width",
            "height",
            "fps",
            "container",
            "sources",
            "clips",
            "ffmpeg_version",
            "qt",
            "platform",
        }
        safe = {
            key: value
            for key, value in parameters.items()
            if key in allowed and isinstance(value, (str, int, float, bool, type(None)))
        }
        return json.dumps(
            {"schema": 1, "failure": asdict(self), "parameters": safe},
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
