"""Русский перевод стандартных элементов интерфейса Qt."""

from PySide6.QtCore import QLibraryInfo, QTranslator
from PySide6.QtWidgets import QApplication

from src.logger import logger


class RussianQtTranslator(QTranslator):
    """Загружает перевод Qt и живёт вместе с приложением."""

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        translations_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if not self.load("qtbase_ru", translations_path):
            logger.warning("Не удалось загрузить русский перевод Qt: %s", translations_path)
            return
        app.installTranslator(self)
