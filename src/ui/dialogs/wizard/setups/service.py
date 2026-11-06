"""Registry-driven options for extension services; no service-specific branches."""

from PySide6.QtWidgets import QFormLayout, QLineEdit
from .base import SetupPage


class ServiceOptionsPage(SetupPage):
    def __init__(self, descriptor, parent=None):
        super().__init__(parent)
        self.descriptor = descriptor
        self.sid = "service_" + descriptor.key
        layout = QFormLayout(self)
        self.fields = {}
        for name, label, default in descriptor.parameters:
            field = QLineEdit(default)
            field.setMaxLength(4096)
            self.fields[name] = field
            layout.addRow(label, field)

    def collect(self, ctx):
        ctx.service_options[self.descriptor.key] = dict(
            self.descriptor.build_parameters(
                {name: field.text() for name, field in self.fields.items()}
            )
        )

    def validate(self, ctx):
        try:
            self.collect(ctx)
            return True, ""
        except ValueError as error:
            return False, str(error)
