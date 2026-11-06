"""Ограниченные proxy-диапазоны предпросмотра; оригиналы остаются в проекте."""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
from dataclasses import replace
from pathlib import Path

from src.domain.video.media_cache import MediaDiskCache
from src.domain.video.render_plan import RenderPlan
from .capabilities import FFmpegCapabilities
from .ffmpeg_utils import probe_media
from src.services.media import get_media_service, OperationContext
from src.services.media.render_types import MediaInput, RenderGraphPlan
from fractions import Fraction


class ProxyCache(MediaDiskCache):
    MAX_BYTES = 2 * 1024**3
    MAX_ENTRIES = 128
    MAX_ARTIFACT = 128 * 1024**2
    VERSION = 3

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(root or Path("~/.dno-tool/video-proxy").expanduser())

    def path(self, key: str) -> Path:
        return super().path(key).with_suffix(".mp4")

    def validate_artifact(self, staging: Path) -> None:
        with staging.open("rb") as source:
            if source.read(8)[4:] != b"ftyp":
                raise ValueError("Некорректный контейнер proxy.")


class PreviewProxy:
    """Аренды живут до завершения рендера; ошибка proxy не подменяет исходник."""

    def __init__(
        self, cancelled: threading.Event, cache: ProxyCache | None = None
    ) -> None:
        self.cancelled = cancelled
        self.cache = cache or ProxyCache()
        self.owner = uuid.uuid4().hex
        self.tokens: list[str] = []

    def close(self) -> None:
        self.cache.release(owner=self.owner)

    def prepare(self, plan: RenderPlan) -> RenderPlan:
        control = OperationContext(cancelled=self.cancelled)
        clips = []
        engine_identity = FFmpegCapabilities.identity()
        for clip in plan.clips:
            control.check()
            if clip.kind != "video":
                clips.append(clip)
                continue
            fingerprint = self.cache.fingerprint(clip.path)
            key = self.cache.key(
                fingerprint,
                "video",
                clip.src_in,
                {
                    "out": clip.src_out,
                    "stream": clip.stream,
                    "width": 1280,
                    "height": 720,
                },
                engine_identity,
            )
            token = uuid.uuid4().hex
            self.tokens.append(token)
            path = self.cache.borrow(key, token, self.owner)
            if path is None:
                with tempfile.TemporaryDirectory(
                    prefix=f".job-{os.getpid()}-{token}-", dir=self.cache.root
                ) as directory:
                    staging = Path(directory) / "proxy.mp4"
                    source_meta = probe_media(clip.path, cancelled=self.cancelled)
                    from src.services.media.render_plan import RenderCommand

                    source_stream = source_meta["video_streams"][clip.stream]
                    rate = Fraction(
                        str(source_stream.get("avg_frame_rate", "25/1"))
                    ) or Fraction(25)
                    sar = Fraction(
                        str(source_stream.get("sample_aspect_ratio", "1:1")).replace(
                            ":", "/"
                        )
                    )
                    width, height = (
                        int(source_stream["width"] * sar),
                        source_stream["height"],
                    )
                    rotate = RenderCommand.autorotate(source_stream)
                    if "transpose" in rotate:
                        width, height = height, width
                    scale = min(1, 1280 / width, 720 / height)
                    width, height = max(2, int(width * scale) // 2 * 2), max(
                        2, int(height * scale) // 2 * 2
                    )
                    duration = clip.src_out - clip.src_in
                    origin = float(source_stream.get("start_time", 0) or 0)
                    description = f"[0:v:0]setpts=PTS-{origin:.9f}/TB,trim=start={clip.src_in:.9f}:end={clip.src_out:.9f},setpts=PTS-STARTPTS,{rotate}scale={width}:{height},setsar=1,format=yuv420p[vout]"
                    graph = RenderGraphPlan(
                        (
                            MediaInput(
                                "0:v:0",
                                clip.path,
                                "video",
                                clip.stream,
                                clip.src_in,
                                clip.src_out,
                            ),
                        ),
                        description,
                        width,
                        height,
                        rate,
                        duration,
                        "libx264",
                        None,
                        {"preset": "ultrafast", "crf": "23"},
                        None,
                        None,
                        False,
                        threads=2,
                    )
                    get_media_service().render(
                        graph,
                        str(staging),
                        OperationContext.with_timeout(120, self.cancelled),
                        max_bytes=self.cache.MAX_ARTIFACT,
                    )
                    meta = probe_media(str(staging), cancelled=self.cancelled)
                    if (
                        not meta.get("has_video")
                        or abs(
                            float(meta.get("duration", 0))
                            - (clip.src_out - clip.src_in)
                        )
                        > 0.15
                    ):
                        raise ValueError("Proxy не сохранил длительность диапазона.")
                    if self.cache.fingerprint(clip.path) != fingerprint:
                        raise ValueError("Исходник изменился при подготовке proxy.")
                    control.check()
                    path = self.cache.publish(key, staging, token, self.owner)
            clips.append(
                replace(
                    clip,
                    path=path,
                    src_out=clip.src_out - clip.src_in,
                    src_in=0,
                    stream=0,
                )
            )
        return replace(plan, clips=tuple(clips))
