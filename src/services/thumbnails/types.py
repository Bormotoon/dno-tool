"""Immutable thumbnail requests and safe results shared by all consumers."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class ThumbnailStatus(StrEnum):
    OK = "ok"
    OFFLINE = "offline"
    UNAVAILABLE = "unavailable"
    AUTH_FAILED = "auth_failed"
    TIMEOUT = "timeout"
    PROTOCOL_ERROR = "protocol_error"
    UNSUPPORTED = "unsupported"
    BUSY = "busy"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ThumbnailRequest:
    width: int
    height: int
    format: str
    quality: int
    compression: int
    backend: str
    host: str
    port: int
    password: str = field(repr=False)
    session_key: tuple
    config_version: int
    interval_sec: int

    @property
    def image_key(self) -> tuple:
        return (self.width, self.height, self.format, self.quality, self.compression)


@dataclass(frozen=True)
class ThumbnailResult:
    data: bytes | None = field(default=None, repr=False)
    mime: str | None = None
    backend: str = "veyon"
    status: ThumbnailStatus = ThumbnailStatus.UNAVAILABLE
    from_cache: bool = False
    frame_time: float | None = None
    config_version: int = 0
    error_code: str | None = None

    @property
    def backend_ok(self) -> bool | None:
        if self.status in (ThumbnailStatus.BUSY, ThumbnailStatus.CANCELLED):
            return None
        return self.status == ThumbnailStatus.OK


class ThumbnailBackend(Protocol):
    def capture(self, device: object, request: ThumbnailRequest) -> ThumbnailResult: ...

    def release(self, session_key: tuple) -> None: ...

    def close_idle_sessions(self, now: float) -> None: ...

    def get_metrics(self) -> dict: ...

    def shutdown(self) -> None: ...

    def is_available(self) -> bool: ...
