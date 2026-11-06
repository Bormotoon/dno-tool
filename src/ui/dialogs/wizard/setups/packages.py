"""Шаг выбора дополнительных пакетов (sid="remote_packages")."""

import re
from typing import Dict, List

from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from src.config import EXTRA_PACKAGES, PACKAGE_DEPENDENCIES

from ..core.context import WizardContext
from .base import SetupPage, hint_label


class PackagesPage(SetupPage):
    """Выбор дополнительных пакетов для установки на хосты."""

    sid = "remote_packages"

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        hint = hint_label(
            "Выберите пакеты для установки на хосты. "
            "Можно добавить свои пакеты в текстовом поле ниже "
            "(по одному на строку или через пробел/запятую)."
        )
        layout.addWidget(hint)

        builtin_label = QLabel("Будут установлены следующие пакеты:")
        builtin_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(builtin_label)

        self._pkg_checkboxes: Dict[str, QCheckBox] = {}
        for pkg in EXTRA_PACKAGES:
            cb = QCheckBox(pkg)
            cb.setChecked(True)
            layout.addWidget(cb)
            self._pkg_checkboxes[pkg] = cb

            deps = PACKAGE_DEPENDENCIES.get(pkg, [])
            if deps:
                dep_label = QLabel(f"Будут установлены: {', '.join(deps)}")
                dep_label.setStyleSheet("color: gray; padding-left: 20px;")
                layout.addWidget(dep_label)

        if not EXTRA_PACKAGES:
            empty = QLabel("(нет встроенных пакетов)")
            empty.setStyleSheet("color: gray; padding-left: 20px;")
            layout.addWidget(empty)

        custom_label = QLabel("Дополнительные пакеты (ручной ввод):")
        custom_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(custom_label)

        self._pkg_custom_edit = QPlainTextEdit()
        self._pkg_custom_edit.setPlaceholderText(
            "Например: qt5-qttools\nили: qt5-qttools polkit qdbus-qt5"
        )
        self._pkg_custom_edit.setMaximumHeight(80)
        layout.addWidget(self._pkg_custom_edit)

        layout.addStretch()

    def collect(self, ctx: WizardContext) -> None:
        ctx.packages = self.collect_selected_packages()

    def collect_selected_packages(self) -> List[str]:
        """Собрать список пакетов для установки.

        Объединяет отмеченные чекбоксами встроенные пакеты (EXTRA_PACKAGES)
        с их зависимостями (PACKAGE_DEPENDENCIES) и пакеты из текстового поля
        ручного ввода (разделители - пробел, запятая, перенос строки).
        Дубликаты удаляются с сохранением порядка.
        """
        packages: List[str] = []
        seen = set()

        for pkg, cb in self._pkg_checkboxes.items():
            if cb.isChecked() and pkg not in seen:
                packages.append(pkg)
                seen.add(pkg)
                for dep in PACKAGE_DEPENDENCIES.get(pkg, []):
                    if dep not in seen:
                        packages.append(dep)
                        seen.add(dep)

        if self._pkg_custom_edit is not None:
            raw = self._pkg_custom_edit.toPlainText().strip()
            if raw:
                parts = re.split(r"[\s,]+", raw)
                for p in parts:
                    p = p.strip()
                    if p and p not in seen:
                        packages.append(p)
                        seen.add(p)

        return packages

    def validate(self, ctx) -> tuple:
        from src.workers.setup.worker import RemoteServiceInstallerWorker

        try:
            RemoteServiceInstallerWorker._validate_package_names(ctx.packages)
        except ValueError:
            return False, "Имена пакетов содержат недопустимые символы."
        return True, ""

    def summary_html(self, ctx) -> str:
        n = len(getattr(ctx, "packages", []) or [])
        return f"<b>Пакеты:</b> {n}"
