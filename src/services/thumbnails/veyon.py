"""Thumbnail adapter for the existing shared Veyon WebAPI connection pool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types import ThumbnailRequest, ThumbnailResult, ThumbnailStatus

if TYPE_CHECKING:
    from src.services.remote.veyon_service import VeyonService


class VeyonBackend:
    """Delegate capture and let the service coordinate feature-safe cleanup."""

    def __init__(self, service: VeyonService) -> None:
        self._service = service
        self._closed = False

    def capture(self, device: object, request: ThumbnailRequest) -> ThumbnailResult:
        if self._closed:
            return ThumbnailResult(
                backend="veyon",
                status=ThumbnailStatus.CANCELLED,
                config_version=request.config_version,
                error_code="veyon_shutdown",
            )
        return self._service.get_thumbnail_result(request.host, request)

    def release(self, session_key: tuple) -> None:
        """Queue release only; safe even if the caller is the GUI thread."""
        self._service.release_thumbnail_session(session_key)

    def close_idle_sessions(self, now: float) -> None:
        """Perform network cleanup in the facade's cleanup worker, not the GUI."""
        self._service.sweep_idle_connections(now=now)

    def get_metrics(self) -> dict:
        metrics = self._service.get_thumb_metrics()
        metrics["sessions"] = self._service.get_webapi_client_count()
        return metrics

    def shutdown(self) -> None:
        """Close this backend's shared Veyon pool only at process shutdown."""
        if self._closed:
            return
        self._closed = True
        self._service.shutdown_thumbnail_backend()

    def is_available(self) -> bool:
        return not self._closed and self._service.is_thumbnails_available()
