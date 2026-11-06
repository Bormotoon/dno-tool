"""
Точка входа в приложение DNO Tool.

Инициализирует DI-контейнер, регистрирует сервисы, создаёт окно приложения
и запускает главный цикл событий Qt.
"""

import sys

from src.utils.system.qt_multimedia import configure_video_decoding

configure_video_decoding()

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from src import __version__
from src.config import config, get_asset_path
from src.di import get_container
from src.diagnostics import install_crash_report
from src.logger import install_qt_message_handler, is_debug_mode, logger
from src.services import initialize_services
from src.ui.localization import RussianQtTranslator
from src.ui.tray import TrayController
from src.views.main_window import MainWindow


def _set_windows_app_user_model_id() -> None:
    """AppUserModelID - чтобы уведомления и трей имели свою иконку и имя."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DNO.Tool")
    except Exception:
        pass


def _start_background_server(name: str, factory, enabled: bool) -> None:
    """Запустить фоновый сервер (веб-сервер / NodeServer), если включён.

    Фабрика вызывается лениво - модули src.web тянут fastapi/uvicorn,
    которые не нужны, если сервер выключен.
    """
    if not enabled:
        return
    try:
        factory().start()
    except Exception as e:
        logger.error(f"Не удалось запустить {name}: {e}")


def main() -> None:
    """Создаёт приложение Qt, инициализирует сервисы и запускает главный цикл."""
    logger.info("Запуск приложения")

    # Qt-fatal (qFatal -> 0xc0000409, молчаливый вылет) - в лог до создания QApplication
    install_qt_message_handler()

    # Автосбор диагностики при падении (faulthandler + minidump) - только в debug-режиме
    if is_debug_mode():
        install_crash_report()

    # Запрет второй копии - до инициализации сервисов, создания окон
    # и запуска фоновых серверов, чтобы вторая копия ничего не трогала.
    from src.utils.system.single_instance import SingleInstanceGuard

    _guard = SingleInstanceGuard()
    if not _guard.try_lock():
        _info = _guard.lock_info()
        _pid = _info.get("pid", 0)
        _detail = f" (PID {_pid})" if _pid else ""
        # error, не warning: дефолтный уровень лога - ERROR (см. src/logger)
        logger.error(
            f"Программа уже запущена{_detail} (lock: {_guard.lock_path}, "
            f"QLockFile error={_guard.lock_error()}). Выход."
        )
        _app = QApplication.instance() or QApplication(sys.argv)
        _app._qt_translator = RussianQtTranslator(_app)
        _app.setOrganizationName("DNO")
        _app.setApplicationName("DNOTool")
        try:
            QMessageBox.warning(
                None,
                "DNO Tool",
                "Программа уже запущена.",
            )
        except Exception:
            pass
        sys.exit(1)

    # Инициализация сервисов до создания QApplication
    container = get_container()
    initialize_services(container)

    app = QApplication(sys.argv)
    # Qt в Linux меняет числовую локаль процесса, FFmpeg без C-локали не работает.
    from src.utils.system.media_runtime import ensure_c_numeric_locale

    ensure_c_numeric_locale()
    app._qt_translator = RussianQtTranslator(app)
    # Держать QLockFile живым до конца процесса, иначе lock отпустится.
    app._single_instance_guard = _guard
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
    app.setOrganizationName("DNO")
    app.setApplicationName("DNOTool")
    app.setApplicationDisplayName("DNO Tool")
    app.setWindowIcon(QIcon(get_asset_path("icons/favicon.ico")))
    app.setQuitOnLastWindowClosed(False)
    _set_windows_app_user_model_id()

    # Тема оформления из настроек (до создания окон, чтобы сразу рисовались в теме)
    try:
        from src.ui.theme_manager import FALLBACK_THEME_ID, apply_theme

        requested = config.app.theme or FALLBACK_THEME_ID
        effective = apply_theme(app, requested)
        if effective != requested:
            config.app.theme = effective
    except Exception as e:
        logger.error(f"Не удалось применить тему: {e}")

    # Фоновые серверы - импорт ленивый (fastapi/uvicorn тяжелые)
    from src.web import get_web_server
    from src.web.node_server import get_node_server

    _start_background_server("веб-сервер", get_web_server, config.app.web.enabled)
    _start_background_server(
        "NodeServer", get_node_server, config.app.nodes.listen_enabled
    )

    logger.info("Приложение запущено")

    # Главное окно (сервисы инициализирует самостоятельно через DI-контейнер)
    window = MainWindow()
    window.setWindowTitle(f"DNO Tool v{__version__}")
    window.resize(1000, 800)
    # Центрируем относительно экрана
    screen = app.primaryScreen().availableGeometry()
    window.move(
        (screen.width() - window.width()) // 2,
        (screen.height() - window.height()) // 2,
    )
    window.show()

    # Системный трей
    tray_controller = container.resolve(TrayController)
    window.set_tray_controller(tray_controller)
    tray_controller.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
