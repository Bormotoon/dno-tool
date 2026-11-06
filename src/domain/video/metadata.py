"""Контракты медиадвижка и ограниченный кэш неизменённых метаданных между заданиями."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from copy import deepcopy
from typing import ClassVar, TypedDict

SourceFingerprint = tuple[str, int, int, int, int]
MetadataKey = tuple[SourceFingerprint, str, int, int]


class StreamSideData(TypedDict, total=False):
    rotation: int


class FormatMetadata(TypedDict, total=False):
    format_name: str
    duration: str
    start_time: str


class RawProbeMetadata(TypedDict, total=False):
    streams: list[StreamMetadata]
    format: FormatMetadata


class StreamMetadata(TypedDict, total=False):
    codec_type: str
    codec_name: str
    codec_tag_string: str
    profile: str
    level: int
    pix_fmt: str
    color_space: str
    color_range: str
    field_order: str
    r_frame_rate: str
    sample_fmt: str
    ch_layout: str
    frame_size: int
    extradata_size: int
    extradata_hash: str
    tags: dict[str, str]
    width: int
    height: int
    channels: int
    channel_layout: str
    sample_rate: str
    sample_aspect_ratio: str
    avg_frame_rate: str
    start_time: str
    duration: str
    time_base: str
    side_data_list: list[StreamSideData]
    index: int
    color_primaries: int
    color_transfer: int
    chroma_location: int


class ProbeMetadata(TypedDict, total=False):
    path: str
    duration: float
    width: int
    height: int
    fps: float
    has_video: bool
    has_audio: bool
    video_streams: list[StreamMetadata]
    audio_streams: list[StreamMetadata]
    start_time: float
    vstart: float
    astart: float
    copy_signature: str
    vcodec: str
    acodec: str
    channels: int
    channel_layout: str
    stream_counts: dict[str, int]
    _source_fingerprint: SourceFingerprint
    weird: list[str]
    duration_known: bool


class MetadataCache:
    MAX_ENTRIES = 128
    MAX_BYTES = 8 * 1024**2
    _lock = threading.Lock()
    _entries: ClassVar[OrderedDict[MetadataKey, tuple[ProbeMetadata, int]]] = (
        OrderedDict()
    )
    _bytes = 0

    @classmethod
    def key(cls, path: str, binary: str) -> MetadataKey:
        from src.domain.video.media_cache import MediaDiskCache

        return (MediaDiskCache.fingerprint(path), binary, 0, 0)

    @classmethod
    def get(cls, key: MetadataKey) -> ProbeMetadata | None:
        with cls._lock:
            item = cls._entries.get(key)
            if item is None:
                return None
            cls._entries.move_to_end(key)
            return deepcopy(item[0])

    @classmethod
    def put(cls, key: MetadataKey, metadata: ProbeMetadata) -> None:
        size = len(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
        if size > cls.MAX_BYTES:
            return
        with cls._lock:
            for old in list(cls._entries):
                if old[0][0] == key[0][0]:
                    cls._bytes -= cls._entries.pop(old)[1]
            while cls._entries and (
                cls._bytes + size > cls.MAX_BYTES
                or len(cls._entries) >= cls.MAX_ENTRIES
            ):
                cls._bytes -= cls._entries.popitem(last=False)[1][1]
            cls._entries[key] = deepcopy(metadata), size
            cls._bytes += size

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._entries.clear()
            cls._bytes = 0
