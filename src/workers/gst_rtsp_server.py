#!/usr/bin/env python3
"""GStreamer RTSP Server — загружается на удалённый хост и запускает RTSP-стрим."""

import json
import os
import sys
from fractions import Fraction

_typelib_paths = "/usr/lib64/girepository-1.0:/usr/lib/girepository-1.0"
existing = os.environ.get("GI_TYPELIB_PATH", "")
if existing:
    _typelib_paths = _typelib_paths + ":" + existing
os.environ["GI_TYPELIB_PATH"] = _typelib_paths

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstRtsp", "1.0")
gi.require_version("GstRtspServer", "1.0")

from gi.repository import GLib, Gst, GstRtspServer


def make_caps(caps_str):
    return Gst.Caps.from_string(caps_str)


def el(factory_name):
    element = Gst.ElementFactory.make(factory_name, None)
    if element is None:
        raise RuntimeError(f"Недоступен элемент GStreamer: {factory_name}")
    return element


def build_pipeline(config):
    """Создаёт Gst.Pipeline с видео-пайплайном. pay0 — обязательное имя для RTSP payloader."""
    pipeline = Gst.Pipeline.new(None)
    capture_type = config.get("capture_type", "x11grab")
    capture_input = config.get("capture_input", "")
    input_format = config.get("input_format", "")
    width = config["width"]
    height = config["height"]
    rate = Fraction(str(config["fps"])).limit_denominator(1001)
    fps = f"{rate.numerator}/{rate.denominator}"

    # --- Источник ---
    if capture_type == "x11grab":
        src = el("ximagesrc")
        src.set_property("use-damage", False)
        display = capture_input.split(".")[0] if capture_input else ":0"
        src.set_property("display-name", display)
        src_caps = el("capsfilter")
        src_caps.set_property("caps", make_caps(f"video/x-raw,framerate={fps}"))

        crop_left = config.get("crop_left")
        crop_top = config.get("crop_top")
        crop_right = config.get("crop_right")
        crop_bottom = config.get("crop_bottom")

        has_crop = crop_left is not None
        if has_crop:
            videocrop = el("videocrop")
            videocrop.set_property("left", int(crop_left))
            videocrop.set_property("top", int(crop_top))
            videocrop.set_property("right", int(crop_right))
            videocrop.set_property("bottom", int(crop_bottom))
            crop_convert = el("videoconvert")

        videoscale = el("videoscale")
        scale_caps = el("capsfilter")
        scale_caps.set_property(
            "caps", make_caps(f"video/x-raw,width={width},height={height}")
        )
        videoconvert = el("videoconvert")

        if has_crop:
            pre_encoder = [
                src,
                src_caps,
                videocrop,
                crop_convert,
                videoscale,
                scale_caps,
                videoconvert,
            ]
        else:
            pre_encoder = [src, src_caps, videoscale, scale_caps, videoconvert]

    elif capture_type == "v4l2":
        src = el("v4l2src")
        if capture_input:
            src.set_property("device", capture_input)
        if input_format == "mjpeg":
            src_caps = el("capsfilter")
            src_caps.set_property(
                "caps",
                make_caps(f"image/jpeg,framerate={fps},width={width},height={height}"),
            )
            jpegdec = el("jpegdec")
            videoconvert = el("videoconvert")
            pre_encoder = [src, src_caps, jpegdec, videoconvert]
        elif input_format in ("yuyv422", "yuyv"):
            src_caps = el("capsfilter")
            src_caps.set_property(
                "caps",
                make_caps(
                    f"video/x-raw,format=YUY2,framerate={fps},width={width},height={height}"
                ),
            )
            videoconvert = el("videoconvert")
            pre_encoder = [src, src_caps, videoconvert]
        elif input_format == "h264":
            src_caps = el("capsfilter")
            src_caps.set_property(
                "caps",
                make_caps(
                    f"video/x-h264,framerate={fps},width={width},height={height}"
                ),
            )
            pre_encoder = [
                src,
                src_caps,
                el("h264parse"),
                el("avdec_h264"),
                el("videoconvert"),
            ]
        else:
            src_caps = el("capsfilter")
            raw_format = {
                "nv12": ",format=NV12",
                "yuv420p": ",format=I420",
                "rgb24": ",format=RGB",
                "bgr24": ",format=BGR",
                "uyvy422": ",format=UYVY",
            }.get(input_format)
            if input_format and raw_format is None:
                raise ValueError(f"Неподдерживаемый формат камеры: {input_format}")
            src_caps.set_property(
                "caps",
                make_caps(
                    f"video/x-raw{raw_format or ''},framerate={fps},width={width},height={height}"
                ),
            )
            videoconvert = el("videoconvert")
            pre_encoder = [src, src_caps, videoconvert]
    else:
        print(f"Unsupported capture_type: {capture_type}", file=sys.stderr, flush=True)
        return None

    # --- Общий caps перед энкодером ---
    conv_caps = el("capsfilter")
    conv_caps.set_property(
        "caps",
        make_caps(
            f"video/x-raw,format=I420,width={width},height={height},framerate={fps}"
        ),
    )
    queue = el("queue")
    queue.set_property("max-size-buffers", 3)
    queue.set_property("max-size-time", 0)
    queue.set_property("max-size-bytes", 0)
    queue.set_property("leaky", 2)

    codec = config.get("codec", "libx264")
    bitrate = config.get("bitrate_kbps", 2000)

    if codec in ("libx264", "h264"):
        encoder = el("x264enc")
        encoder.set_property("tune", 0x00000004)
        encoder.set_property("speed-preset", 1)
        encoder.set_property("bitrate", bitrate)
        enc_caps = el("capsfilter")
        enc_caps.set_property(
            "caps", make_caps("video/x-h264,stream-format=byte-stream")
        )
        payloader = Gst.ElementFactory.make("rtph264pay", "pay0")
        elements = pre_encoder + [conv_caps, queue, encoder, enc_caps, payloader]

    elif codec in ("libx265", "h265", "hevc"):
        encoder = el("x265enc")
        encoder.set_property("tune", 0x00000004)
        encoder.set_property("bitrate", bitrate)
        enc_caps = el("capsfilter")
        enc_caps.set_property(
            "caps", make_caps("video/x-h265,stream-format=byte-stream")
        )
        payloader = Gst.ElementFactory.make("rtph265pay", "pay0")
        elements = pre_encoder + [conv_caps, queue, encoder, enc_caps, payloader]

    elif codec == "mpeg2video":
        videorate = el("videorate")
        encoder = el("avenc_mpeg2video")
        encoder.set_property("bitrate", bitrate * 1000)
        payloader = Gst.ElementFactory.make("rtpmpvpay", "pay0")
        elements = pre_encoder + [conv_caps, queue, videorate, encoder, payloader]

    elif codec == "mpeg4":
        videorate = el("videorate")
        encoder = el("avenc_mpeg4")
        encoder.set_property("bitrate", bitrate * 1000)
        payloader = Gst.ElementFactory.make("rtpmp4vpay", "pay0")
        elements = pre_encoder + [conv_caps, queue, videorate, encoder, payloader]

    else:
        print(f"Unsupported codec: {codec}", file=sys.stderr, flush=True)
        return None

    for e in elements:
        if e is None:
            raise RuntimeError("Недоступен элемент видеопотока GStreamer")
        pipeline.add(e)
    for i in range(len(elements) - 1):
        if not elements[i].link(elements[i + 1]):
            raise RuntimeError("Не удалось соединить элементы видеопотока GStreamer")

    # --- Аудио-ветка ---
    if config.get("audio", False):
        audio_source = config.get("audio_source", "pulse")
        audio_input = config.get("audio_input", "default")
        audio_codec = config.get("audio_codec", "aac")
        audio_bitrate = int(config.get("audio_bitrate", 128000))

        if audio_source == "pulse":
            asrc = el("pulsesrc")
            if audio_input and audio_input != "default":
                asrc.set_property("device", audio_input)
        else:
            asrc = el("alsasrc")
            if audio_input and audio_input != "default":
                asrc.set_property("device", audio_input)

        audioconvert = el("audioconvert")
        audioresample = el("audioresample")

        if audio_source == "pulse":
            audio_caps = el("capsfilter")
            audio_caps.set_property(
                "caps", make_caps("audio/x-raw,rate=48000,channels=2")
            )
            audio_elements = [asrc, audio_caps, audioresample, audioconvert]
        else:
            audio_caps = el("capsfilter")
            audio_caps.set_property("caps", make_caps("audio/x-raw,rate=48000"))
            audio_elements = [asrc, audio_caps, audioresample, audioconvert]

        if audio_codec in ("aac",):
            aenc = el("avenc_aac")
            aenc.set_property("bitrate", audio_bitrate)
            apay = Gst.ElementFactory.make("rtpmp4apay", "pay1")
            audio_elements += [aenc, apay]
        elif audio_codec in ("libmp3lame", "mp3"):
            aenc = el("lamemp3enc")
            aenc.set_property("target", 1)
            aenc.set_property("bitrate", audio_bitrate // 1000)
            apay = Gst.ElementFactory.make("rtpmpapay", "pay1")
            audio_elements += [aenc, apay]
        elif audio_codec in ("opus", "libopus"):
            aenc = el("opusenc")
            aenc.set_property("bitrate", audio_bitrate)
            apay = Gst.ElementFactory.make("rtpopuspay", "pay1")
            audio_elements += [aenc, apay]
        else:
            raise ValueError(f"Неподдерживаемый аудиокодек GStreamer: {audio_codec}")

        for e in audio_elements:
            if e is None:
                raise RuntimeError("Недоступен элемент аудиопотока GStreamer")
            pipeline.add(e)
        for i in range(len(audio_elements) - 1):
            if not audio_elements[i].link(audio_elements[i + 1]):
                raise RuntimeError(
                    "Не удалось соединить элементы аудиопотока GStreamer"
                )

    return pipeline


class RTSPFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(self, config):
        super().__init__()
        self.config = config

    def do_create_element(self, url):
        print(f"RTSP client connected: {url.get_request_uri()}", flush=True)
        return build_pipeline(self.config)


class RTSPServer:
    def __init__(self, port, mount_point, config_json):
        self.port = port
        self.mount_point = mount_point
        self.config = json.loads(config_json)
        self.loop = GLib.MainLoop()

    def run(self):
        Gst.init(None)

        # Проверяем наличие элементов и совместимость связей до объявления готовности.
        probe = build_pipeline(self.config)
        if probe is None:
            raise RuntimeError("Не удалось создать RTSP pipeline")
        probe.set_state(Gst.State.NULL)

        server = GstRtspServer.RTSPServer()
        server.set_service(str(self.port))
        factory = RTSPFactory(self.config)
        mount_points = server.get_mount_points()
        mount_points.add_factory(self.mount_point, factory)
        if not server.attach(None):
            raise RuntimeError(f"Не удалось привязать RTSP-порт {self.port}")

        print(f"RTSP server: rtsp://0.0.0.0:{self.port}{self.mount_point}", flush=True)
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(
            f"Usage: {sys.argv[0]} <port> <mount_point> <config_json>", file=sys.stderr
        )
        sys.exit(1)
    srv = RTSPServer(int(sys.argv[1]), sys.argv[2], sys.argv[3])
    srv.run()
