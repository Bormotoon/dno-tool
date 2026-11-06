"""Версии запросов, видимые приоритеты и бюджет GUI-медиакэша."""

from __future__ import annotations

import time
from collections import OrderedDict

from PySide6.QtCore import QTimer, QFileSystemWatcher
from PySide6.QtGui import QPixmap

from src.domain.video.media_cache import MediaDiskCache
from src.workers.video_editor.thumbnail_worker import MediaRequest, STRIP_N


class VideoMediaMixin:
    MEDIA_RAM_BYTES = 32 * 1024 * 1024

    def _init_media_results(self) -> None:
        self._media_ram: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._media_ram_bytes = 0
        self._media_backoff: dict[tuple[str, str], tuple[float, int]] = {}
        self._retired_media_pools = []
        self._visible_asset_iids: set[str] = set()
        self._visible_video_clips: set[str] = set()
        self._visible_audio_assets: set[str] = set()
        self._media_retry_timer = QTimer(self)
        self._media_retry_timer.setSingleShot(True)
        self._media_retry_timer.setInterval(500)
        self._media_retry_timer.timeout.connect(self._retry_media_requests)
        self._source_watcher = QFileSystemWatcher(self)
        self._source_watcher.fileChanged.connect(self._source_file_changed)
        self._source_changes: set[str] = set()
        self._source_timer = QTimer(self)
        self._source_timer.setSingleShot(True)
        self._source_timer.setInterval(200)
        self._source_timer.timeout.connect(self._apply_source_changes)

    def _sync_source_watches(self) -> None:
        wanted = {asset.path for asset in self.project.assets}
        current = set(self._source_watcher.files())
        if current - wanted:
            self._source_watcher.removePaths(list(current - wanted))
        if wanted - current:
            self._source_watcher.addPaths(list(wanted - current))

    def _source_file_changed(self, path: str) -> None:
        if not self._closing:
            self._source_changes.add(path)
            self._source_timer.start()

    def _apply_source_changes(self) -> None:
        paths, self._source_changes = self._source_changes, set()
        if self._closing:
            return
        changed = False
        for asset in self.project.assets:
            if asset.path in paths:
                self._forget_media_cache(asset.iid)
                asset.probe_error = (
                    "Исходник изменён. Повторите анализ или перепривяжите файл."
                )
                self.panel.update_asset(asset.iid)
                changed = True
        if changed:
            self._sync_source_watches()
            self.preview.set_render_plan(None, None)
            self.refresh_preview()
            self._schedule_media_retry()

    def _connect_media_visibility(self) -> None:
        for signal in (
            self.panel.list.verticalScrollBar().valueChanged,
            self.timeline.view.horizontalScrollBar().valueChanged,
            self.timeline.view.verticalScrollBar().valueChanged,
            self.timeline.zoom.valueChanged,
        ):
            signal.connect(self._schedule_media_retry)

    def _schedule_media_retry(self, *_: object) -> None:
        if not self._closing and not self._media_retry_timer.isActive():
            self._media_retry_timer.start()

    def _update_media_visibility(self) -> None:
        self._visible_asset_iids = self.panel.visible_iids()
        visible = self.timeline.view.mapToScene(
            self.timeline.view.viewport().rect()
        ).boundingRect()
        self._visible_video_clips = set()
        self._visible_audio_assets = set()
        for track in self.project.tracks:
            for clip in track.clips:
                item = self.timeline._items.get(clip.iid)
                if item is None or not item.sceneBoundingRect().intersects(visible):
                    continue
                if (
                    clip.duration * self.timeline._cur_px < 24
                    and clip.iid != self._sel_clip
                ):
                    continue
                if track.kind == "video":
                    self._visible_video_clips.add(clip.iid)
                else:
                    self._visible_audio_assets.add(clip.asset_iid)

    def _retry_media_requests(self) -> None:
        if self._closing:
            return
        self._retire_media_pools()
        self._update_media_visibility()
        for iid in self._visible_asset_iids:
            asset = self._assets.get(iid)
            if asset and asset.has_video and asset.duration > 0:
                self._request_thumb(iid)
        for iid in self._visible_audio_assets:
            self._request_wave(iid)
        for track in self.project.video_tracks():
            for clip in track.clips:
                if clip.iid in self._visible_video_clips:
                    self._request_strip(clip, self._assets.get(clip.asset_iid))

    def _retire_media_pools(self) -> None:
        for pool in list(self._retired_media_pools):
            if pool.pending_count == 0:
                self._retired_media_pools.remove(pool)
                pool.deleteLater()

    def _media_allowed(self, mode: str, key: str) -> bool:
        retry = self._media_backoff.get((mode, key))
        if retry and time.monotonic() < retry[0]:
            self._schedule_media_retry()
            return False
        return True

    def _media_failed(self, mode: str, key: str) -> None:
        previous = self._media_backoff.get((mode, key), (0, 0))[1]
        count = min(6, previous + 1)
        self._media_backoff[(mode, key)] = (time.monotonic() + min(60, 2**count), count)
        self._schedule_media_retry()

    def _media_request_is_current(self, request: MediaRequest) -> bool:
        if request.mode == "strip":
            clip = next(
                (
                    clip
                    for track in self.project.tracks
                    for clip in track.clips
                    if clip.iid == request.key
                ),
                None,
            )
            if clip is None:
                return False
            asset = self._assets.get(clip.asset_iid)
            expected = [
                clip.src_in + (index + 0.5) / STRIP_N * clip.duration
                for index in range(STRIP_N)
            ]
            if request.extra.get("times") != expected:
                return False
        else:
            asset = self._assets.get(request.key)
        if asset is None or asset.path != request.source:
            return False
        stream = asset.audio_stream if request.mode == "wave" else asset.video_stream
        if request.extra.get("stream", 0) != stream:
            return False
        try:
            return request.fingerprint == MediaDiskCache.fingerprint(asset.path)
        except OSError:
            return False

    def _media_request_discarded(self, request: MediaRequest) -> None:
        pending = {
            "frame": self._thumb_pending,
            "wave": self._wave_pending,
            "strip": self._strip_pending,
        }[request.mode]
        pending.discard(request.key)
        if request.mode == "strip":
            self._strip_bounds.pop(request.key, None)
        self._schedule_media_retry()

    def _remember_media_pixmap(self, mode: str, key: str, pixmap: QPixmap) -> None:
        identity = (mode, key)
        self._media_ram_bytes -= self._media_ram.pop(identity, 0)
        size = pixmap.width() * pixmap.height() * 8
        self._media_ram[identity] = size
        self._media_ram_bytes += size
        self._media_backoff.pop(identity, None)
        while self._media_ram_bytes > self.MEDIA_RAM_BYTES and self._media_ram:
            (kind, iid), amount = self._media_ram.popitem(last=False)
            self._media_ram_bytes -= amount
            if kind == "frame":
                self._thumbs.pop(iid, None)
                self.panel.remove_thumb(iid)
            elif kind == "wave":
                self._waves.pop(iid, None)
                for track in self.project.audio_tracks():
                    for clip in track.clips:
                        item = self.timeline._items.get(clip.iid)
                        if item is not None and clip.asset_iid == iid:
                            item._wave = None
                            item.update()
            else:
                self._strip_pix.pop(iid, None)
                self._strip_bounds.pop(iid, None)
                item = self.timeline._items.get(iid)
                if item is not None:
                    item._strip = None
                    item.update()

    def _touch_media(self, mode: str, key: str) -> None:
        identity = (mode, key)
        if identity in self._media_ram:
            self._media_ram.move_to_end(identity)

    def _forget_media_ram(self, key: str) -> None:
        for identity in [
            identity for identity in self._media_ram if identity[1] == key
        ]:
            self._media_ram_bytes -= self._media_ram.pop(identity)
            self._media_backoff.pop(identity, None)
        self._thumb_pool.invalidate(key)
        self.panel.remove_thumb(key)

    def _clear_media_ram(self) -> None:
        self._media_ram.clear()
        self._media_ram_bytes = 0
        self._media_backoff.clear()
