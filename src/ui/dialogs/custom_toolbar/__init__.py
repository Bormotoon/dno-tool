"""Редактор пользовательских панелей команд."""

from .editor import CustomToolbarsDialog
from .icon_picker import IconPickerDialog
from .command_picker import CommandPickerDialog

__all__ = [
    "CustomToolbarsDialog",
    "IconPickerDialog",
    "CommandPickerDialog",
]
