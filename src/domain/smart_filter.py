from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.domain.models.device import DeviceModel
    from src.services.hosts.host_info_service import HostInfoSnapshot


@dataclass(frozen=True)
class SmartHostQuery:
    text_terms: tuple[str, ...] = field(default_factory=tuple)
    predicates: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @classmethod
    def parse(cls, raw_query: str) -> "SmartHostQuery":
        terms: list[str] = []
        preds: list[tuple[str, str]] = []
        for token in (raw_query or "").strip().split():
            if ":" in token:
                key, val = token.split(":", 1)
                key_low = key.casefold()
                if key_low in ("is", "status", "sudo", "mac", "os", "user", "port", "backend"):
                    preds.append((key_low, val.casefold()))
                    continue
            terms.append(token.casefold())
        return cls(text_terms=tuple(terms), predicates=tuple(preds))

    def matches_device(
        self,
        device: "DeviceModel",
        info: "HostInfoSnapshot | None" = None,
    ) -> bool:
        haystack = f"{device.name or ''} {device.host or ''} {device.mac_address or ''}".casefold()
        for term in self.text_terms:
            if term not in haystack:
                return False

        for key, val in self.predicates:
            if key in ("is", "status"):
                if val in ("online", "on", "онлайн") and not device.is_online:
                    return False
                if val in ("offline", "off", "офлайн") and device.is_online:
                    return False
            elif key == "sudo":
                if val in ("yes", "true", "да", "1") and not device.use_sudo:
                    return False
                if val in ("no", "false", "нет", "0") and device.use_sudo is not False:
                    return False
            elif key == "mac":
                has_mac = bool((device.mac_address or "").strip())
                if val in ("empty", "none", "нет", "пусто") and has_mac:
                    return False
                if val in ("set", "yes", "есть") and not has_mac:
                    return False
            elif key == "port":
                if str(device.port or 22) != val:
                    return False
            elif key == "backend":
                if (device.thumbnail_backend or "default").casefold() != val:
                    return False
            elif key == "os":
                os_str = (info.os if info else "").casefold()
                if val not in os_str:
                    return False
            elif key == "user":
                user_str = (info.user if info else "").casefold()
                if val not in user_str:
                    return False

        return True
