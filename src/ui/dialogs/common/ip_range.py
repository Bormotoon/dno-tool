"""Диалог добавления диапазона IP-адресов с поддержкой CIDR и диапазонов."""

import ipaddress
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QLineEdit,
    QSpinBox,
    QDialogButtonBox,
    QFormLayout,
    QMessageBox,
)
from PySide6.QtCore import Qt
from src.config import config
from src.domain.models.device import DeviceModel
from typing import List, Tuple


class IpRangeDialog(QDialog):
    """Диалог для добавления диапазона IP-адресов."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setWindowTitle("Добавить диапазон IP")
        self.setMinimumWidth(400)
        self._ips_cache = None
        self._setup_ui()

    def _setup_ui(self):
        form = QFormLayout(self)

        self._ip_edit = QLineEdit()
        self._ip_edit.setPlaceholderText(
            "192.168.1.0/24 · 192.168.1.1-15 · 192.168.1.1-192.168.1.254"
        )
        self._ip_edit.setToolTip(
            "Подсеть: 192.168.1.0/24\nКороткий диапазон: 192.168.1.1-15\nПолный диапазон: 192.168.1.1-192.168.1.254"
        )
        form.addRow("Диапазон:", self._ip_edit)

        self._name_prefix_edit = QLineEdit()
        self._name_prefix_edit.setPlaceholderText("host")
        self._name_prefix_edit.setText("host")
        self._name_prefix_edit.setToolTip(
            "Префикс для имени каждого устройства.\nИмена будут вида: префикс-1, префикс-2, ..."
        )
        form.addRow("Префикс имени:", self._name_prefix_edit)

        self._port_edit = QSpinBox()
        self._port_edit.setRange(1, 65535)
        self._port_edit.setValue(config.app.ssh.port)
        self._port_edit.setToolTip(
            f"SSH-порт по умолчанию для всех устройств (текущий: {config.app.ssh.port})"
        )
        form.addRow("Порт:", self._port_edit)

        # Связка учётных данных для всех устройств в диапазоне
        from src.ui.widgets.bundle_selector import BundleSelector

        self._bundle_selector = BundleSelector(
            allow_none=True, none_label="(наследовать глобальную)"
        )
        self._bundle_selector.setToolTip(
            "Связка учётных данных (логин/пароль) для SSH-подключения\n"
            "ко всем устройствам в диапазоне.\n\n"
            "(наследовать глобальную) — использовать связку по умолчанию\n"
            "из настроек.\n\n"
            "Кнопка справа открывает окно управления связками."
        )
        form.addRow("Связка:", self._bundle_selector)

        self._count_label = QLabel("")
        form.addRow(self._count_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._ip_edit.textChanged.connect(self._update_count)

    def _update_count(self):
        text = self._ip_edit.text().strip()
        if not text:
            self._count_label.setText("")
            return
        count = self._count_ips(text)
        if count is None:
            self._count_label.setText("")
        else:
            self._count_label.setText(f"Будет добавлено устройств: {count}")

    def _count_ips(self, text):
        """Быстрый подсчёт количества IP без генерации списка."""
        text = text.strip()
        if not text:
            return None

        if "/" in text:
            try:
                network = ipaddress.ip_network(text, strict=False)
            except ValueError:
                return None
            if network.prefixlen == 32:
                return 1
            if network.prefixlen == 31:
                return 2
            n = network.num_addresses - 2
            if n <= 0:
                return 0
            return n if n <= 1024 else None

        if "-" in text:
            parts = text.split("-", 1)
            left = parts[0].strip()
            right = parts[1].strip()

            if "." in right:
                try:
                    start_ip = ipaddress.ip_address(left)
                    end_ip = ipaddress.ip_address(right)
                except ValueError:
                    return None
                if start_ip.version != end_ip.version or start_ip > end_ip:
                    return None
                n = int(end_ip) - int(start_ip) + 1
                return n if n <= 1024 else None
            else:
                try:
                    end_octet = int(right)
                except ValueError:
                    return None
                if not (0 <= end_octet <= 255):
                    return None
                try:
                    base_ip = ipaddress.ip_address(left)
                except ValueError:
                    return None
                if base_ip.version != 4:
                    return None
                octets = left.split(".")
                base_octet = int(octets[3])
                if end_octet < base_octet:
                    return None
                n = end_octet - base_octet + 1
                return n if n <= 1024 else None

        try:
            ipaddress.ip_address(text)
            return 1
        except ValueError:
            return None

    def _on_accept(self):
        devices = self.get_devices()
        if devices is None:
            return
        self.accept()

    def get_devices(self) -> List[DeviceModel] | None:
        prefix = self._name_prefix_edit.text().strip() or "host"
        port = self._port_edit.value()
        bundle_iid = self._bundle_selector.current_iid()

        ips = self._parse_input()
        if ips is None:
            return None

        if len(ips) == 0:
            QMessageBox.warning(
                self, "Ошибка", "Нет допустимых IP-адресов в указанном диапазоне."
            )
            return None

        if len(ips) > 1024:
            QMessageBox.warning(
                self,
                "Слишком много адресов",
                f"Диапазон содержит {len(ips)} адресов.\n"
                "Максимум — 1024. Укажите более узкую подсеть.",
            )
            return None

        devices = []
        for idx, ip in enumerate(ips, start=1):
            device = DeviceModel(
                {
                    "name": f"{prefix}-{idx}",
                    "host": ip,
                    "port": port if port != config.app.ssh.port else None,
                    "credential_bundle_iid": bundle_iid,
                }
            )
            devices.append(device)

        return devices

    def _parse_input(self, show_error=True) -> List[str] | None:
        from src.utils.network.ip_range_parser import parse_ip_range

        return parse_ip_range(
            self._ip_edit.text().strip(),
            show_error=show_error,
            parent=self,
            max_addresses=1024,
        )

    @staticmethod
    def add_ip_range(parent=None) -> Tuple[bool, List[DeviceModel] | None]:
        dialog = IpRangeDialog(parent)
        if dialog.exec():
            devices = dialog.get_devices()
            if devices is not None:
                return True, devices
        return False, None
