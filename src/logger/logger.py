"""Модуль логирования DNO Tool. Настраивает ротируемый файловый обработчик с автоматическим изменением владельца файла логов при запуске от sudo/pkexec."""

from logging.handlers import RotatingFileHandler
import logging
import os
from os import path, makedirs
import sys


class UserOwnedRotatingFileHandler(RotatingFileHandler):
    """Ротируемый обработчик логов с автоматической сменой владельца файла."""

    def doRollover(self) -> None:
        """Выполнить ротацию лога и сменить владельца нового файла."""
        super().doRollover()
        try:
            from src.utils.filesystem.fs_utils import ensure_user_owned

            ensure_user_owned(self.baseFilename)
        except Exception:
            pass

    def _open(self):
        """Открыть файл лога и сменить владельца."""
        stream = super()._open()
        try:
            from src.utils.filesystem.fs_utils import ensure_user_owned

            ensure_user_owned(self.baseFilename)
        except Exception:
            pass
        return stream


def get_log_file_path() -> str:
    """Вернуть путь к файлу логов (~/.dno-tool/dno-tool.log)."""
    return path.join(path.expanduser("~"), ".dno-tool", "dno-tool.log")


log_file = get_log_file_path()


if not path.exists(path.dirname(log_file)):
    makedirs(path.dirname(log_file))
    try:
        from src.utils.filesystem.fs_utils import ensure_user_owned

        ensure_user_owned(path.dirname(log_file))
    except Exception:
        pass


CRITICAL = logging.CRITICAL
FATAL = logging.FATAL
ERROR = logging.ERROR
WARNING = logging.WARN
WARN = logging.WARN
INFO = logging.INFO
DEBUG = logging.DEBUG
NOTSET = logging.NOTSET

DEFAULT_LOG_LEVEL = ERROR


def _env_level() -> int:
    """Уровень логов из окружения: DNO_TOOL_LOG_LEVEL (DEBUG/INFO/.../10..50).

    Дефолт — DEFAULT_LOG_LEVEL: полные диагностические следы (фазы сканера, пулы,
    Veyon-таймеры) нужны для разбора падений; crash-репортёр и минидампы
    читают этот лог для корреляции. Временно включить ERROR можно через
    set DNO_TOOL_LOG_LEVEL=ERROR.
    """
    raw = os.environ.get("DNO_TOOL_LOG_LEVEL", "").strip().upper()
    if not raw:
        return DEFAULT_LOG_LEVEL
    level = logging.getLevelName(raw)
    if isinstance(level, int):
        return level
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_LOG_LEVEL


def is_debug_mode() -> bool:
    """Режим отладки: уровень логов DEBUG или числовой <= DEBUG (10).

    Единственный источник истины для гейтов отладочных файлов
    (crash-дампы и т.п.): без DNO_TOOL_LOG_LEVEL=DEBUG приложение
    считает себя релизным и не создаёт отладочные артефакты.
    """
    return _env_level() <= logging.DEBUG


def get_logger(name: str, level: int = None) -> logging.Logger:
    """Создать и настроить логгер с ротируемым файловым обработчиком.

    ``level`` = None → из окружения (DNO_TOOL_LOG_LEVEL), дефолт DEFAULT_LOG_LEVEL.
    """
    logfile = path.join("logs", log_file)
    log_format = f"%(asctime)s - [%(levelname)s] - %(name)s - (%(filename)s).%(funcName)s(%(lineno)d) - %(message)s"

    handler = UserOwnedRotatingFileHandler(
        logfile, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
    )

    handler.setFormatter(logging.Formatter(log_format))

    log = logging.getLogger(name)
    log.setLevel(_env_level() if level is None else level)
    log.addHandler(handler)

    return log


logger = get_logger(
    "dno-tool"
)  # основной логгер, используется в __main__.py и других модулях


def install_qt_message_handler() -> None:
    """Перехватить сообщения Qt (warnings/critical/fatal) в файл логов.

    Qt пишет фатальные ошибки (qFatal → код выхода 0xc0000409, молчаливый
    вылет без Python-traceback) только в stderr — в dno-tool.log они не
    попадают и причина падения теряется. Хендлер дублирует все Qt-сообщения
    в лог; QtFatalMsg дополнительно сбрасывает буферы перед смертью
    процесса. Потокобезопасен (только logging + stderr, без вызовов Qt).

    Вызывать до создания QApplication (см. __main__.main).
    """
    try:
        from PySide6.QtCore import (
            qInstallMessageHandler,
            QtMsgType,
            QMessageLogContext,
        )
    except Exception:
        return

    _level_by_type = {
        QtMsgType.QtDebugMsg: DEBUG,
        QtMsgType.QtInfoMsg: INFO,
        QtMsgType.QtWarningMsg: WARNING,
        QtMsgType.QtCriticalMsg: ERROR,
        QtMsgType.QtFatalMsg: CRITICAL,
    }

    def _handler(mode: QtMsgType, context: QMessageLogContext, message: str) -> None:
        try:
            try:
                where = ""
                if context is not None and getattr(context, "file", None):
                    where = f" ({context.file}:{context.line} {context.function or ''})"
                logger.log(
                    _level_by_type.get(mode, ERROR),
                    "Qt[%s]%s: %s",
                    getattr(mode, "name", mode),
                    where,
                    message,
                )
            except Exception:
                pass
            # Сохраняем дефолтную видимость Qt: fatal/critical дублируем
            # в консоль (иначе при запуске без консоли их никто не увидит).
            if mode in (QtMsgType.QtFatalMsg, QtMsgType.QtCriticalMsg):
                try:
                    sys.stderr.write(f"Qt[{getattr(mode, 'name', mode)}]: {message}\n")
                    sys.stderr.flush()
                except Exception:
                    pass
        except Exception:
            pass

    try:
        qInstallMessageHandler(_handler)
    except Exception:
        pass
