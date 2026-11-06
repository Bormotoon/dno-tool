"""Immutable scanner messages and protocol-qualified evidence (no Qt)."""

from dataclasses import dataclass, field
import time
from typing import Literal

ProbeState = Literal["open", "closed", "timeout", "unreachable", "error", "cancelled", "invalid", "reply", "no_reply"]


@dataclass(frozen=True)
class ProbeResult:
    host: str
    port: int
    protocol: Literal["tcp", "udp", "icmp"]
    state: ProbeState
    elapsed: float = 0.0
    diagnostic: str = ""
    ttl: int | None = None
    rtt_ms: float | None = None

    def __bool__(self):
        return self.state == "open"


@dataclass(frozen=True)
class HostScanResult:
    scan_id: str
    host: str
    hostname: str
    online: bool
    ports: tuple[int | str, ...] = ()
    mac_address: str = ""
    device_type: str = ""
    type_reason: str = ""
    state: str = "done"
    probes: tuple[ProbeResult, ...] = ()
    observed_at: float = field(default_factory=time.monotonic)

    def legacy_args(self):
        return (self.host, self.hostname, self.online, list(self.ports),
                self.mac_address, self.device_type, self.type_reason, self.state)


@dataclass(frozen=True)
class ScanOutcome:
    scan_id: str
    status: Literal["success", "cancelled", "error"]
    error: str = ""
    diagnostics: tuple[tuple[str, int], ...] = ()
