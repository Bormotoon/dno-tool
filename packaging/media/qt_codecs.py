"""Проверить общий набор кодеков Qt Multimedia для пакетов МОС."""

from __future__ import annotations

import json
import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtMultimedia import QMediaFormat

EXPECTED = {
    "audio_decode": {
        "AAC",
        "AC3",
        "ALAC",
        "EAC3",
        "FLAC",
        "MP3",
        "Opus",
        "Vorbis",
        "WMA",
        "Wave",
    },
    "audio_encode": {
        "AAC",
        "AC3",
        "ALAC",
        "EAC3",
        "FLAC",
        "MP3",
        "Opus",
        "Vorbis",
        "Wave",
    },
    "video_decode": {
        "AV1",
        "H264",
        "H265",
        "MPEG1",
        "MPEG2",
        "MPEG4",
        "MotionJPEG",
        "VP8",
        "VP9",
        "WMV",
    },
    "video_encode": {
        "AV1",
        "H264",
        "H265",
        "MPEG1",
        "MPEG2",
        "MPEG4",
        "MotionJPEG",
        "VP8",
        "VP9",
    },
}


def main() -> int:
    QCoreApplication(sys.argv)
    media_format = QMediaFormat()
    actual = {
        "audio_decode": {
            codec.name
            for codec in media_format.supportedAudioCodecs(
                QMediaFormat.ConversionMode.Decode
            )
        },
        "audio_encode": {
            codec.name
            for codec in media_format.supportedAudioCodecs(
                QMediaFormat.ConversionMode.Encode
            )
        },
        "video_decode": {
            codec.name
            for codec in media_format.supportedVideoCodecs(
                QMediaFormat.ConversionMode.Decode
            )
        },
        "video_encode": {
            codec.name
            for codec in media_format.supportedVideoCodecs(
                QMediaFormat.ConversionMode.Encode
            )
        },
    }
    print(
        json.dumps(
            {key: sorted(value) for key, value in actual.items()},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if actual != EXPECTED:
        missing = {key: sorted(EXPECTED[key] - actual[key]) for key in EXPECTED}
        extra = {key: sorted(actual[key] - EXPECTED[key]) for key in EXPECTED}
        print(
            f"Набор кодеков Qt Multimedia не совпадает с целевым: недостающие={missing}, лишние={extra}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
