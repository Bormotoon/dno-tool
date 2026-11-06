"""Отменяемые SSH-операции и неизменяемые параметры удалённой сессии."""

from __future__ import annotations

import base64
import copy
import json
import shlex
import sys
import threading
import time
import zlib
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QThread

from src.domain.models import DeviceModel


def resource_text(name: str) -> str:
    """Найти передаваемый скрипт также внутри PyInstaller."""
    for path in (
        Path(__file__).with_name(name),
        Path(getattr(sys, "_MEIPASS", ".")) / "src/workers" / name,
    ):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(name)


def desktop_command(command: str) -> str:
    """Сканирование использует тот же поиск XAUTHORITY, что и захват."""
    runtime = resource_text("remote_recording_runtime.py")
    definition = runtime[
        runtime.index("def desktop_environment(") : runtime.index("def supervise(")
    ]
    code = "import os; from pathlib import Path; import subprocess\n" + definition
    code += (
        "\nraise SystemExit(subprocess.call("
        + repr(shlex.split(command))
        + ", env=desktop_environment(':0.0')))"
    )
    return "python3 -c " + shlex.quote(code)


def bitrate(value: str) -> int:
    """Перевести стандартный битрейт FFmpeg в биты в секунду."""
    suffix = value[-1:].lower()
    factor = {"k": 1000, "m": 1000000, "g": 1000000000}.get(suffix, 1)
    return int(float(value[:-1] if factor != 1 else value) * factor)


@dataclass(frozen=True)
class RecordingSession:
    iid: str
    device_json: str
    settings_json: str
    mode: str
    output: str
    url: str

    @classmethod
    def create(
        cls, device: DeviceModel, settings: dict, mode: str, output: str = ""
    ) -> RecordingSession:
        # Модель подключения копируется отдельно; секреты не включаются в логи.
        return cls(
            uuid4().hex,
            json.dumps(device.to_dict()),
            json.dumps(settings),
            mode,
            output,
            f"{settings['transport']}://{device.host}:{settings['port']}"
            + (
                "/stream"
                if settings["transport"] == "rtsp"
                else "/stream.ts"
                if settings["transport"] == "http"
                else ""
            ),
        )

    @property
    def device(self) -> DeviceModel:
        return DeviceModel(json.loads(self.device_json))

    @property
    def directory(self) -> str:
        return "/tmp/dno-tool-" + self.iid


def build_config(session: RecordingSession) -> dict:
    """Собрать аргументы процесса без оболочки и без чтения виджетов."""
    s = json.loads(session.settings_json)
    source, capture = s["capture_type"], s["capture_input"]
    width, height = map(int, s["resolution"].split("x"))
    fps = s["fps"]
    cfg = {
        "capture_type": source,
        "display": capture,
        "port": s["port"],
        "output": session.output,
    }
    if s.get("engine") == "gstreamer" and session.mode == "stream":
        if s["transport"] not in ("rtsp", "tcp"):
            raise ValueError("GStreamer поддерживает RTSP и TCP")
        if s["transport"] == "rtsp":
            gst = dict(
                s,
                width=width,
                height=height,
                audio=s["enable_audio"],
                bitrate_kbps=bitrate(s["bitrate"]) // 1000,
                audio_bitrate=bitrate(s["audio_bitrate"]),
            )
            cfg["argv"] = [
                "python3",
                session.directory + "/gst_rtsp_server.py",
                str(s["port"]),
                "/stream",
                json.dumps(gst),
            ]
            cfg["rtsp_script"] = resource_text("gst_rtsp_server.py")
            return cfg
        parts = ["gst-launch-1.0", "-e"]
        rate = Fraction(str(fps)).limit_denominator(1001)
        gst_fps = f"{rate.numerator}/{rate.denominator}"
        if source == "x11grab":
            parts += [
                "ximagesrc",
                "display-name=" + capture,
                "use-damage=false",
                "!",
                f"video/x-raw,framerate={gst_fps}",
            ]
            if "crop_left" in s:
                parts += ["!", "videocrop"] + [
                    f"{key}={s['crop_' + key]}"
                    for key in ("left", "top", "right", "bottom")
                ]
            parts += [
                "!",
                "videoscale",
                "!",
                f"video/x-raw,width={width},height={height}",
            ]
        else:
            parts += ["v4l2src", "device=" + capture, "!"]
            fmt = s.get("input_format")
            raw_formats = {
                "yuyv422": "YUY2",
                "nv12": "NV12",
                "uyvy422": "UYVY",
                "yuv420p": "I420",
                "rgb24": "RGB",
                "bgr24": "BGR",
            }
            if fmt and fmt not in ("mjpeg", "h264") and fmt not in raw_formats:
                raise ValueError("Неподдерживаемый формат камеры GStreamer: " + fmt)
            caps = (
                "image/jpeg"
                if fmt == "mjpeg"
                else "video/x-h264"
                if fmt == "h264"
                else "video/x-raw" + (",format=" + raw_formats[fmt] if fmt else "")
            )
            parts += [f"{caps},width={width},height={height},framerate={gst_fps}"]
            if fmt == "mjpeg":
                parts += ["!", "jpegdec"]
            elif fmt == "h264":
                parts += ["!", "h264parse", "!", "avdec_h264"]
        parts += [
            "!",
            "videoconvert",
            "!",
            "video/x-raw,format=I420",
            "!",
            "queue",
            "max-size-buffers=3",
            "max-size-bytes=0",
            "max-size-time=0",
            "leaky=downstream",
            "!",
        ]
        codec = s["codec"]
        if codec in ("libx264", "libx265"):
            parts += [
                "x264enc" if codec == "libx264" else "x265enc",
                "tune=zerolatency",
                "speed-preset=ultrafast",
                f"bitrate={bitrate(s['bitrate']) // 1000}",
            ]
        elif codec in ("mpeg2video", "mpeg4"):
            parts += ["avenc_" + codec, f"bitrate={bitrate(s['bitrate'])}"]
        else:
            raise ValueError("Неподдерживаемый видеокодек GStreamer")
        parts += ["!", "mux."]
        if s["enable_audio"]:
            parts += [
                "pulsesrc" if s["audio_source"] == "pulse" else "alsasrc",
                "device=" + s["audio_input"],
                "!",
                "audioconvert",
                "!",
                "audioresample",
                "!",
            ]
            enc = {
                "aac": "avenc_aac",
                "libmp3lame": "lamemp3enc",
                "libopus": "opusenc",
            }.get(s["audio_codec"])
            if not enc:
                raise ValueError("Неподдерживаемый аудиокодек GStreamer")
            parts += [
                enc,
                f"bitrate={bitrate(s['audio_bitrate']) // (1000 if enc == 'lamemp3enc' else 1)}",
            ]
            if enc == "lamemp3enc":
                parts += ["target=bitrate"]
            parts += ["!", "mux."]
        parts += [
            "mpegtsmux",
            "name=mux",
            "!",
            "tcpserversink",
            "host=0.0.0.0",
            f"port={s['port']}",
        ]
        cfg["argv"] = parts
        return cfg
    args = [
        s.get("ffmpeg_path") or "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-progress",
        "pipe:1",
        "-nostats",
        "-f",
        source,
        "-framerate",
        str(fps),
    ]
    if source == "v4l2":
        if s.get("input_format"):
            args += ["-input_format", s["input_format"]]
        args += ["-video_size", s["resolution"]]
    elif s.get("monitor_width"):
        args += ["-video_size", f"{s['monitor_width']}x{s['monitor_height']}"]
        capture += f"+{s['monitor_x_offset']},{s['monitor_y_offset']}"
    args += ["-i", capture]
    if s["enable_audio"]:
        audio_input = s["audio_input"]
        if s["audio_source"] == "alsa" and audio_input.startswith("hw:"):
            audio_input = "plug" + audio_input
        args += ["-f", s["audio_source"], "-ar", "48000", "-i", audio_input]
    args += ["-c:v", s["codec"]]
    if s["codec"] in ("libx264", "libx265"):
        args += ["-preset", "veryfast" if session.mode == "record" else "ultrafast"]
        if session.mode == "stream":
            args += ["-tune", "zerolatency"]
    args += [
        "-pix_fmt",
        "yuv420p",
        "-vf",
        f"scale={width}:{height}",
        "-b:v",
        s["bitrate"],
        "-r",
        str(fps),
    ]
    args += (
        ["-c:a", s["audio_codec"], "-b:a", s["audio_bitrate"]]
        if s["enable_audio"]
        else ["-an"]
    )
    if session.mode == "record":
        args += ["-y", session.output]
    elif s["transport"] == "tcp":
        args += ["-f", "mpegts", f"tcp://0.0.0.0:{s['port']}?listen=1"]
    elif s["transport"] == "http":
        args += [
            "-listen",
            "1",
            "-f",
            "mpegts",
            f"http://0.0.0.0:{s['port']}/stream.ts",
        ]
    else:
        raise ValueError("FFmpeg поддерживает TCP и HTTP")
    cfg["argv"] = args
    return cfg


class RecordingSSHThread(QThread):
    """Собственные SSH-клиент и канал с отменой и общим deadline."""

    def __init__(self, device: DeviceModel, timeout: float = 40, parent=None) -> None:
        super().__init__(parent)
        self.device = copy.deepcopy(device)
        self.cancelled = threading.Event()
        self.timeout = timeout
        self.client = None
        self.result = None
        self.error = ""

    def cancel(self) -> None:
        self.cancelled.set()

    def connect_ssh(self) -> None:
        import paramiko

        from src.workers.command.executor_base import create_ssh_client

        self.deadline = time.monotonic() + self.timeout
        self.check()
        self.client = paramiko.SSHClient()
        self._connection_done = threading.Event()

        def interrupt() -> None:
            while not self._connection_done.wait(0.05):
                if self.cancelled.is_set() or time.monotonic() >= self.deadline:
                    self.client.close()
                    return

        watcher = threading.Thread(target=interrupt, daemon=True)
        watcher.start()
        try:
            create_ssh_client(self.device, client=self.client, connect_timeout=5)
        finally:
            self._connection_done.set()
            watcher.join(1)
        self.check()

    def check(self) -> None:
        if self.cancelled.is_set():
            raise InterruptedError("Операция отменена")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Истекло время SSH-операции")

    def execute(self, command: str, input_text: str = "") -> tuple[str, str, int]:
        self.check()
        channel = self.client.get_transport().open_session(timeout=5)
        # Watchdog закрывает только собственный канал, включая зависший exec request.
        done = threading.Event()

        def watchdog() -> None:
            while not done.wait(0.05):
                if self.cancelled.is_set() or time.monotonic() >= self.deadline:
                    channel.close()
                    return

        watcher = threading.Thread(target=watchdog, daemon=True)
        watcher.start()
        output, errors = bytearray(), bytearray()
        try:
            channel.settimeout(1)
            channel.exec_command(command)
            if input_text:
                channel.sendall(input_text.encode())
            channel.shutdown_write()
            while True:
                self.check()
                for ready, receive, buffer in (
                    (channel.recv_ready, channel.recv, output),
                    (channel.recv_stderr_ready, channel.recv_stderr, errors),
                ):
                    if ready():
                        buffer.extend(receive(65536))
                        if len(buffer) > 2 * 1024 * 1024:
                            raise ValueError("Слишком большой ответ SSH")
                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    return (
                        output.decode("utf-8", "replace"),
                        errors.decode("utf-8", "replace"),
                        channel.recv_exit_status(),
                    )
                if channel.closed:
                    raise ConnectionError("SSH-канал закрыт без результата")
                self.cancelled.wait(0.02)
        finally:
            done.set()
            channel.close()
            watcher.join(1)

    def perform(self) -> None:
        raise NotImplementedError

    def run(self) -> None:
        try:
            self.connect_ssh()
            self.perform()
        except Exception as error:  # noqa: BLE001 — ошибка передаётся координатору после QThread.finished
            self.error = str(error)
        finally:
            if self.client is not None:
                self.client.close()


class SessionOperation(RecordingSSHThread):
    def __init__(self, session: RecordingSession, action: str, parent=None) -> None:
        super().__init__(session.device, parent=parent)
        self.session, self.action = session, action
        self.dispatched = False

    def perform(self) -> None:
        root = self.session.directory
        if self.action == "start":
            config = build_config(self.session)
            files = {
                "runtime.py": resource_text("remote_recording_runtime.py"),
                "config.json": json.dumps(config),
            }
            if "rtsp_script" in config:
                files["gst_rtsp_server.py"] = config.pop("rtsp_script")
                files["config.json"] = json.dumps(config)
            payload = base64.b64encode(
                zlib.compress(json.dumps(files, ensure_ascii=False).encode())
            ).decode()
            bootstrap = (
                "import os,json,base64,zlib; from pathlib import Path; "
                f"os.umask(0o077); p=Path({root!r}); p.mkdir(mode=0o700); "
                f"files=json.loads(zlib.decompress(base64.b64decode({payload!r}))); "
                "[(p/name).write_text(data,encoding='utf-8') for name,data in files.items()]"
            )
            out, err, code = self.execute("python3 -c " + shlex.quote(bootstrap))
            if code:
                raise RuntimeError(err or out)
        self.dispatched = True
        out, err, code = self.execute(
            "python3 "
            + shlex.quote(root + "/runtime.py")
            + " "
            + shlex.quote(root)
            + " "
            + self.action
        )
        if code:
            raise RuntimeError(err or out)
        self.result = json.loads(out)


_GST_PACKAGES = [
    "gstreamer1.0-tools",
    "gstreamer1.0-plugins-base",
    "gstreamer1.0-plugins-good",
    "gstreamer1.0-plugins-bad",
    "gstreamer1.0-plugins-ugly",
    "gstreamer1.0-rtsp-server",
    "gstreamer1.0-libav",
    "gstreamer1.0-pulse",
    "gstreamer1.0-vaapi",
    "lib64gstrtspserver-gir1.0",
    "python3-gobject",
]


class InstallGStreamerThread(RecordingSSHThread):
    """Установка зависимостей с общим сроком выполнения и отменой ожидания."""

    def __init__(self, device: DeviceModel) -> None:
        super().__init__(device, timeout=300)

    def perform(self) -> None:
        from src.workers.command.executor_base import get_credentials
        from src.workers.command.sudo_utils import should_use_sudo, wrap_sudo_cmd

        packages = list(_GST_PACKAGES)
        for typelib in ("Gst-1.0", "GstRtsp-1.0", "GstRtspServer-1.0"):
            found, _, _ = self.execute(
                f"find /usr/lib64/girepository-1.0 /usr/lib/girepository-1.0 -name '{typelib}.typelib' 2>/dev/null | head -1"
            )
            if not found.strip():
                found, _, _ = self.execute(
                    f"dnf repoquery --whatprovides '*/{typelib}.typelib' --qf '%{{name}}' 2>/dev/null | head -1"
                )
                if found.strip():
                    packages.append(found.strip().splitlines()[0])
        command = "dnf install -y " + " ".join(shlex.quote(p) for p in packages)
        sudo = should_use_sudo(self.device)
        password = get_credentials(self.device).password if sudo else None
        out, err, code = self.execute(
            wrap_sudo_cmd(command, sudo),
            (password + "\n") if password is not None else "",
        )
        if code:
            raise RuntimeError(err or out or f"dnf завершился с кодом {code}")
        self.result = "GStreamer установлен"
