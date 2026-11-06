"""Диалог записи и трансляции с асинхронным управлением удалённой сессией."""

import html as html_module
import os
import re
import shutil
import time
from datetime import datetime

from PySide6.QtCore import (
    QEvent,
    QProcess,
    QRegularExpression,
    Qt,
    QThread,
    QTimer,
)
from PySide6.QtGui import QCloseEvent, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.domain.models import DeviceModel
from src.workers.remote_recording import InstallGStreamerThread, RecordingSSHThread

from .recording_coordinator import RecordingCoordinator, RecordingState

# ---------------------------------------------------------------------------
# Профили качества
# ---------------------------------------------------------------------------
_PROFILES = {
    0: {
        "name": "Минимальная задержка",
        "codec": "mpeg2video",
        "resolution": "1280x720",
        "fps": 15,
        "bitrate": "1M",
    },
    1: {
        "name": "Баланс",
        "codec": "libx264",
        "resolution": "1280x720",
        "fps": 25,
        "bitrate": "2M",
    },
    2: {
        "name": "Максимальное качество",
        "codec": "libx264",
        "resolution": "1920x1080",
        "fps": 30,
        "bitrate": "4M",
    },
}


# ---------------------------------------------------------------------------
# Кэш сканирования (TTL 5 мин)
# ---------------------------------------------------------------------------
_scan_cache_video: dict[str, tuple[float, list]] = {}
_scan_cache_audio: dict[str, tuple[float, list]] = {}
_scan_cache_monitors: dict[str, tuple[float, list]] = {}


def _get_cached_scan(host: str, cache: dict) -> list | None:
    if host not in cache:
        return None
    ts, results = cache[host]
    if time.time() - ts > 300:
        del cache[host]
        return None
    return results


def _set_cached_scan(host: str, results: list, cache: dict) -> None:
    cache[host] = (time.time(), results)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def _find_in_path(name: str) -> str | None:
    path = shutil.which(name)
    if path:
        return path
    if os.name == "nt" and not name.endswith(".exe"):
        path = shutil.which(name + ".exe")
        if path:
            return path
    return None


def _validate_binary(text: str) -> bool:
    return bool(_find_in_path(text.strip()))


# ---------------------------------------------------------------------------
# Поток проверки SSH
# ---------------------------------------------------------------------------
class SSHCheckThread(RecordingSSHThread):
    def __init__(self, device: DeviceModel) -> None:
        super().__init__(device, timeout=12)

    def perform(self) -> None:
        t0 = time.monotonic()
        out, err, code = self.execute("echo OK")
        self.result = (
            code == 0 and out.strip() == "OK",
            err or f"SSH OK ({(time.monotonic() - t0) * 1000:.0f} мс)",
        )


class ScanThread(RecordingSSHThread):
    def __init__(self, device: DeviceModel, scan_monitors: bool = False):
        super().__init__(device, timeout=35)
        self.scan_monitors = scan_monitors

    def perform(self) -> None:
        video_cmd = (
            'for dev in /dev/video*; do [ -c "$dev" ] || continue; '
            'echo "DEVICE:$dev"; '
            "if command -v v4l2-ctl >/dev/null 2>&1; then "
            'v4l2-ctl --device="$dev" --list-formats-ext 2>&1; '
            'elif command -v ffmpeg >/dev/null 2>&1; then ffmpeg -f v4l2 -list_formats all -i "$dev" 2>&1 || true; '
            'else echo "Для сканирования камеры нужен v4l2-ctl или ffmpeg" >&2; exit 1; fi; '
            "echo END_DEVICE; done"
        )
        video, err, code = self.execute(video_cmd)
        if code:
            raise RuntimeError(err or video)
        audio, _, _ = self.execute(
            "if command -v pactl >/dev/null 2>&1; then pactl list short sources 2>/dev/null; fi; "
            "if command -v arecord >/dev/null 2>&1; then arecord -l 2>/dev/null; fi; true"
        )
        monitors = []
        if self.scan_monitors:
            from src.workers.remote_recording import desktop_command

            monitor, err, code = self.execute(desktop_command("xrandr --listmonitors"))
            if code:
                raise RuntimeError(
                    err or monitor or "Не удалось получить список мониторов X11"
                )
            monitors = self._parse_monitor_output(monitor)
        self.result = (
            self._parse_output(video),
            self._parse_audio_output(audio),
            monitors,
            "",
        )

    @staticmethod
    def _parse_output(output: str) -> list:
        devices = []
        current = None
        fmt = None
        sizes = []
        formats = {
            "MJPG": "mjpeg",
            "JPEG": "mjpeg",
            "MJPEG": "mjpeg",
            "YUYV": "yuyv422",
            "YUY2": "yuyv422",
            "NV12": "nv12",
            "UYVY": "uyvy422",
            "YU12": "yuv420p",
            "RGB3": "rgb24",
            "BGR3": "bgr24",
            "H264": "h264",
        }
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("DEVICE:"):
                current = {
                    "device": line[7:],
                    "capabilities": {},
                    "mjpeg_caps": {},
                    "formats": {},
                }
                devices.append(current)
                fmt, sizes = None, []
            elif line == "END_DEVICE":
                current = None
            elif current is not None:
                match = re.match(r"\[\d+\]:\s*'([^']+)'", line)
                if match:
                    fmt = formats.get(match[1].upper())
                    sizes = []
                elif line.startswith("Size: Discrete"):
                    sizes = re.findall(r"\d+x\d+", line)
                elif line.startswith("Size: Stepwise"):
                    match = re.search(
                        r"(\d+)x(\d+) - (\d+)x(\d+) with step (\d+)/(\d+)", line
                    )
                    sizes = []
                    if match:
                        lo_w, lo_h, hi_w, hi_h, step_w, step_h = map(
                            int, match.groups()
                        )
                        for w, h in (
                            (1920, 1080),
                            (1280, 720),
                            (1024, 768),
                            (800, 600),
                            (640, 480),
                        ):
                            if (
                                lo_w <= w <= hi_w
                                and lo_h <= h <= hi_h
                                and (w - lo_w) % max(1, step_w) == 0
                                and (h - lo_h) % max(1, step_h) == 0
                            ):
                                sizes.append(f"{w}x{h}")
                elif line.startswith("Interval:") and fmt:
                    match = re.search(r"\((\d+(?:\.\d+)?) fps\)", line)
                    if match:
                        fps = float(match[1])
                        fps = int(fps) if fps.is_integer() else fps
                        for size in sizes:
                            for caps in (
                                current["capabilities"],
                                current["formats"].setdefault(fmt, {}),
                            ):
                                if fps not in caps.setdefault(size, []):
                                    caps[size].append(fps)
                            if fmt == "mjpeg":
                                current["mjpeg_caps"] = current["formats"][fmt]
                elif "[video4linux2" in line:
                    match = re.search(
                        r"(?:Raw|Compressed)\s*:\s*(\w+)\s*:.*?:\s*(.*)", line
                    )
                    if match:
                        fallback_fmt = match[1].lower()
                        for size in re.findall(r"\d+x\d+", match[2]):
                            current["capabilities"].setdefault(size, [])
                            current["formats"].setdefault(fallback_fmt, {}).setdefault(
                                size, []
                            )
        return devices

    @staticmethod
    def _parse_audio_output(output: str) -> list:
        """Парсит вывод pactl list short sources или arecord -l."""
        devices = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            # pactl list short sources
            # Формат: ID	NAME	Module	SampleSpec	ChannelMap	State
            if re.match(r"^\d+\s+\S+", line) and "card" in line:
                parts = line.split("\t")
                if len(parts) >= 2:
                    name = parts[1].strip()
                    desc = parts[1].strip()
                    devices.append(
                        {"name": name, "description": desc, "driver": "pulse"}
                    )
            elif re.match(r"^\d+\s+\S+", line):
                parts = line.split("\t")
                if len(parts) >= 2:
                    name = parts[1].strip()
                    devices.append(
                        {"name": name, "description": name, "driver": "pulse"}
                    )
            # arecord -l
            # Формат: card 0: PCH [Intel PCH], device 0: ALC256 Analog [ALC256 Analog]
            elif line.lower().startswith("card "):
                m = re.search(
                    r"card\s+(\d+)[^,]+,\s*device\s+(\d+):\s+(.+)", line, re.IGNORECASE
                )
                if m:
                    card_num = m.group(1)
                    dev_num = m.group(2)
                    desc = m.group(3).strip()
                    hw_name = f"hw:{card_num},{dev_num}"
                    devices.append(
                        {"name": hw_name, "description": desc, "driver": "alsa"}
                    )
        return devices

    @staticmethod
    def _parse_monitor_output(output: str) -> list:
        """Парсит вывод xrandr --listmonitors или xrandr --query | grep connected."""
        monitors = []
        if not output:
            return monitors

        # формат xrandr --listmonitors:
        # Monitors: 2
        #  0: +*HDMI-1 1920/527x1080/296+0+0  HDMI-1
        #  1: +DP-1 1920/527x1080/296+1920+0  DP-1
        for line in output.splitlines():
            line = line.strip()
            m = re.match(
                r"^(\d+):\s+\+?\*?(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)\s+(\S+)",
                line,
            )
            if m:
                idx = int(m.group(1))
                name = m.group(2)
                width = m.group(3)
                height = m.group(4)
                x_offset = int(m.group(5))
                y_offset = int(m.group(6))
                display_name = m.group(7) if m.group(7) != name else name
                monitors.append(
                    {
                        "index": idx,
                        "name": name,
                        "display_name": display_name,
                        "width": int(width),
                        "height": int(height),
                        "x_offset": x_offset,
                        "y_offset": y_offset,
                        "x11_input": ":0.0",
                    }
                )

        # Фоллбэк: формат xrandr --query | grep connected:
        # HDMI-1 connected primary 1920x1080+0+0 (normal left...)
        # DP-1 connected 1920x1080+1920+0 (normal left...)
        if not monitors:
            idx = 0
            for line in output.splitlines():
                line = line.strip()
                m = re.match(
                    r"^(\S+)\s+connected\s+(?:primary\s+)?(\d+)x(\d+)\+(\d+)\+(\d+)",
                    line,
                )
                if m:
                    name = m.group(1)
                    width = int(m.group(2))
                    height = int(m.group(3))
                    x_offset = int(m.group(4))
                    y_offset = int(m.group(5))
                    monitors.append(
                        {
                            "index": idx,
                            "name": name,
                            "display_name": name,
                            "width": width,
                            "height": height,
                            "x_offset": x_offset,
                            "y_offset": y_offset,
                            "x11_input": ":0.0",
                        }
                    )
                    idx += 1

        # Вычислить общий размер виртуального экрана
        if monitors:
            screen_w = max(m["x_offset"] + m["width"] for m in monitors)
            screen_h = max(m["y_offset"] + m["height"] for m in monitors)
            for m in monitors:
                m["screen_width"] = screen_w
                m["screen_height"] = screen_h

        return monitors

        # формат xrandr --listmonitors:
        # Monitors: 2
        #  0: +*HDMI-1 1920/527x1080/296+0+0  HDMI-1
        #  1: +DP-1 1920/527x1080/296+1920+0  DP-1
        for line in output.splitlines():
            line = line.strip()
            m = re.match(
                r"^(\d+):\s+\+?\*?(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)\s+(\S+)",
                line,
            )
            if m:
                idx = int(m.group(1))
                name = m.group(2)
                width = m.group(3)
                height = m.group(4)
                x_offset = int(m.group(5))
                y_offset = int(m.group(6))
                display_name = m.group(7) if m.group(7) != name else name
                monitors.append(
                    {
                        "index": idx,
                        "name": name,
                        "display_name": display_name,
                        "width": int(width),
                        "height": int(height),
                        "x_offset": x_offset,
                        "y_offset": y_offset,
                        "x11_input": ":0.0",
                    }
                )

        # Фоллбэк: формат xrandr --query | grep connected:
        # HDMI-1 connected primary 1920x1080+0+0 (normal left...)
        # DP-1 connected 1920x1080+1920+0 (normal left...)
        if not monitors:
            idx = 0
            for line in output.splitlines():
                line = line.strip()
                m = re.match(
                    r"^(\S+)\s+connected\s+(?:primary\s+)?(\d+)x(\d+)\+(\d+)\+(\d+)",
                    line,
                )
                if m:
                    name = m.group(1)
                    width = int(m.group(2))
                    height = int(m.group(3))
                    x_offset = int(m.group(4))
                    y_offset = int(m.group(5))
                    monitors.append(
                        {
                            "index": idx,
                            "name": name,
                            "display_name": name,
                            "width": width,
                            "height": height,
                            "x_offset": x_offset,
                            "y_offset": y_offset,
                            "x11_input": ":0.0",
                        }
                    )
                    idx += 1

        return monitors


# ---------------------------------------------------------------------------
# Основной диалог
# ---------------------------------------------------------------------------
class RemoteRecordingDialog(QDialog):
    """
    Диалог удалённой записи видео с устройства.
    FFmpeg запускается на удалённом хосте через SSH как сервер.
    Открывается в non-modal режиме.
    """

    def __init__(self, device: DeviceModel, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.device = device
        self.setWindowTitle(f"Удалённая запись — {device.name}")
        self.resize(820, 650)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._coordinator = RecordingCoordinator(self)
        self._coordinator.changed.connect(self._session_changed)
        self._coordinator.snapshot.connect(self._session_snapshot)
        self._coordinator.log.connect(self._session_log)
        self._coordinator.drained.connect(self._finish_close)
        self._closing = False
        self._close_ready = False
        self._completion_code = QDialog.DialogCode.Rejected
        self._generated_filename = ""
        self._open_download_in_editor = False
        self._view_state = None

        self._player_processes: list[QProcess] = []
        self._scan_thread: QThread | None = None
        self._ssh_check_thread: QThread | None = None
        self._scan_results: list = []
        self._scan_audio_results: list = []
        self._scan_monitor_results: list = []
        self._rec_filepath: str = ""
        self._editor_download = None
        self._rec_start_time: float = 0.0
        self._rec_log_lines: list[tuple[str, str | None]] = []

        self._init_ui()
        self._load_settings()
        self._update_connection_url()
        self._update_buttons()

    # -------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------
    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # --- Верхний статус + SSH-проверка ---
        top_row = QHBoxLayout()
        self.status_label = QLabel("🔴 Сервер остановлен")
        self.status_label.setWordWrap(True)
        top_row.addWidget(self.status_label)
        top_row.addStretch()
        self.ssh_check_btn = QPushButton("🔌 Проверить SSH")
        self.ssh_check_btn.setToolTip("Проверить доступность SSH-соединения")
        self.ssh_check_btn.clicked.connect(self._check_ssh)
        top_row.addWidget(self.ssh_check_btn)
        self.ssh_status_label = QLabel("")
        self.ssh_status_label.setWordWrap(True)
        top_row.addWidget(self.ssh_status_label)
        layout.addLayout(top_row)

        # --- Вкладка "Источник" (верхний уровень) ---
        source_tab = QWidget()
        source_layout = QVBoxLayout(source_tab)
        source_layout.setSpacing(6)

        # --- Тип захвата ---
        capture_row = QFormLayout()
        capture_row.setSpacing(4)

        self.capture_type_combo = QComboBox()
        self.capture_type_combo.addItem("Экран (x11grab)", "x11grab")
        self.capture_type_combo.addItem("Камера (v4l2)", "v4l2")
        self.capture_type_combo.currentIndexChanged.connect(
            self._on_capture_type_changed
        )

        self.capture_input_combo = QComboBox()
        self.capture_input_combo.setEditable(False)
        self.capture_input_combo.setPlaceholderText(":0.0")
        self.capture_input_combo.currentTextChanged.connect(self._update_connection_url)
        self.capture_input_combo.currentIndexChanged.connect(self._on_device_selected)

        self.scan_btn = QPushButton("🔍 Сканировать")
        self.force_scan_btn = QPushButton("Обновить устройства")
        self.force_scan_btn.setToolTip("Повторно опросить удалённый хост без кэша")
        self.force_scan_btn.clicked.connect(lambda: self._scan_devices(True))
        self.scan_btn.setToolTip("Сканировать доступные устройства на удалённом хосте")
        self.scan_btn.clicked.connect(lambda: self._scan_devices())

        capture_input_row = QHBoxLayout()
        capture_input_row.setSpacing(4)
        capture_input_row.addWidget(self.capture_input_combo, stretch=3)
        capture_input_row.addWidget(self.scan_btn, stretch=1)

        capture_row.addRow("Тип захвата:", self.capture_type_combo)
        capture_row.addRow("Источник:", capture_input_row)
        source_layout.addLayout(capture_row)

        # --- Видео ---
        video_group = QGroupBox("Видео")
        video_group.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        video_layout = QFormLayout(video_group)
        video_layout.setSpacing(4)

        self.profile_combo = QComboBox()
        for k, v in _PROFILES.items():
            self.profile_combo.addItem(v["name"], k)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)

        self.input_format_combo = QComboBox()
        self.input_format_combo.addItem("Авто", "")
        self.input_format_combo.addItem("MJPEG (высокий FPS)", "mjpeg")
        self.input_format_combo.addItem("YUYV (raw)", "yuyv422")
        self.input_format_combo.addItem("NV12", "nv12")
        self.input_format_combo.addItem("UYVY", "uyvy422")

        self.codec_combo = QComboBox()
        self.codec_combo.addItem("MPEG-2 (лёгкий)", "mpeg2video")
        self.codec_combo.addItem("MPEG-4", "mpeg4")
        self.codec_combo.addItem("H.264", "libx264")
        self.codec_combo.addItem("H.265", "libx265")

        self.resolution_combo = QComboBox()
        self.resolution_combo.addItem("1920x1080", "1920x1080")
        self.resolution_combo.addItem("1280x720", "1280x720")
        self.resolution_combo.addItem("1024x768", "1024x768")
        self.resolution_combo.addItem("800x600", "800x600")
        self.resolution_combo.currentIndexChanged.connect(self._on_resolution_changed)

        self.fps_combo = QComboBox()
        self.fps_combo.addItem("25", 25)

        self.bitrate_edit = QLineEdit()
        self.bitrate_edit.setPlaceholderText("2M")
        self.bitrate_edit.setText("2M")
        rx = QRegularExpression(r"^\d+[kKmMgG]?$")
        self.bitrate_edit.setValidator(QRegularExpressionValidator(rx, self))

        video_layout.addRow("Профиль:", self.profile_combo)
        video_layout.addRow("Формат ввода:", self.input_format_combo)
        video_layout.addRow("Кодек:", self.codec_combo)
        video_layout.addRow("Разрешение:", self.resolution_combo)
        video_layout.addRow("FPS:", self.fps_combo)
        video_layout.addRow("Битрейт видео:", self.bitrate_edit)

        # --- Аудио ---
        self._audio_group = QGroupBox("Аудио")
        self._audio_group.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        self._audio_group.setCheckable(True)
        self._audio_group.setChecked(False)
        audio_layout = QFormLayout(self._audio_group)
        audio_layout.setSpacing(4)

        self.audio_source_combo = QComboBox()
        self.audio_source_combo.addItem("PulseAudio", "pulse")
        self.audio_source_combo.addItem("ALSA", "alsa")
        self.audio_source_combo.currentIndexChanged.connect(
            self._on_audio_driver_changed
        )

        self.audio_device_combo = QComboBox()
        self.audio_device_combo.setEditable(True)
        self.audio_device_combo.setPlaceholderText("Нажмите «Сканировать»")
        self.audio_device_combo.currentIndexChanged.connect(
            self._on_audio_device_changed
        )

        self.audio_codec_combo = QComboBox()
        self.audio_codec_combo.addItem("AAC", "aac")
        self.audio_codec_combo.addItem("MP3", "libmp3lame")
        self.audio_codec_combo.addItem("PCM 16-bit", "pcm_s16le")
        self.audio_codec_combo.addItem("Opus", "libopus")

        self.audio_bitrate_edit = QLineEdit()
        self.audio_bitrate_edit.setPlaceholderText("128k")
        self.audio_bitrate_edit.setText("128k")
        rx_audio = QRegularExpression(r"^\d+[kKmMgG]?$")
        self.audio_bitrate_edit.setValidator(
            QRegularExpressionValidator(rx_audio, self)
        )

        audio_layout.addRow("Драйвер:", self.audio_source_combo)
        audio_layout.addRow("Устройство:", self.audio_device_combo)
        audio_layout.addRow("Кодек:", self.audio_codec_combo)
        audio_layout.addRow("Битрейт:", self.audio_bitrate_edit)

        params_row = QHBoxLayout()
        params_row.setSpacing(8)
        params_row.addWidget(video_group, stretch=1)
        params_row.addWidget(self._audio_group, stretch=1)
        source_layout.addLayout(params_row)

        # ===================== Вложенные вкладки Стрим/Запись =====================
        self.mode_tabs = QTabWidget()

        # --- Вкладка "Стрим" (вложенная) ---
        stream_tab = QWidget()
        stream_layout = QVBoxLayout(stream_tab)
        stream_layout.setSpacing(6)

        # --- Движок сервера ---
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("GStreamer (RTSP)", "gstreamer")
        self.engine_combo.addItem("FFmpeg", "ffmpeg")
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)

        self.gst_install_btn = QPushButton("📦 Установить")
        self.gst_install_btn.setToolTip(
            "Установить GStreamer на удалённый хост через dnf"
        )
        self.gst_install_btn.clicked.connect(self._install_gstreamer)

        self.gst_status_label = QLabel("")

        self.transport_combo = QComboBox()
        self.transport_combo.addItem("TCP (listen)", "tcp")
        self.transport_combo.addItem("HTTP (listen)", "http")
        self.transport_combo.currentIndexChanged.connect(self._on_transport_changed)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(8080)
        self.port_spin.valueChanged.connect(self._update_connection_url)

        engine_row = QHBoxLayout()
        engine_row.setSpacing(4)
        engine_row.addWidget(self.engine_combo, 1)
        engine_row.addWidget(self.gst_install_btn)
        engine_row.addWidget(self.gst_status_label)

        proto_row = QHBoxLayout()
        proto_row.setSpacing(6)
        lbl_proto = QLabel("Протокол:")
        lbl_proto.setBuddy(self.transport_combo)
        proto_row.addWidget(lbl_proto)
        proto_row.addWidget(self.transport_combo, 1)
        lbl_port = QLabel("Порт:")
        lbl_port.setBuddy(self.port_spin)
        proto_row.addWidget(lbl_port)
        proto_row.addWidget(self.port_spin, 1)

        proto_form = QFormLayout()
        proto_form.setSpacing(6)
        proto_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        proto_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        proto_form.addRow("Движок:", engine_row)
        proto_form.addRow(proto_row)
        stream_layout.addLayout(proto_form)

        # --- URL потока ---
        url_group = QGroupBox("URL потока")
        url_group.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        url_layout = QVBoxLayout(url_group)
        url_layout.setSpacing(4)
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setReadOnly(True)
        self.url_edit.setPlaceholderText("URL появится после запуска сервера")
        url_row.addWidget(self.url_edit)
        self.copy_url_btn = QPushButton("📋")
        self.copy_url_btn.setToolTip("Копировать URL в буфер обмена")
        self.copy_url_btn.setFixedWidth(36)
        self.copy_url_btn.clicked.connect(self._copy_url_to_clipboard)
        url_row.addWidget(self.copy_url_btn)
        url_layout.addLayout(url_row)
        stream_layout.addWidget(url_group)

        # --- Пути к плеерам ---
        paths_group = QGroupBox("Пути к плеерам")
        paths_group.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        paths_form = QFormLayout(paths_group)
        paths_form.setSpacing(4)

        self.ffplay_path_edit = QLineEdit()
        self.vlc_path_edit = QLineEdit()

        for edit, name in [
            (self.ffplay_path_edit, "ffplay"),
            (self.vlc_path_edit, "vlc"),
        ]:
            edit.editingFinished.connect(
                lambda e=edit, n=name: self._validate_binary_field(e, n)
            )
            row = QHBoxLayout()
            row.setSpacing(4)
            row.addWidget(edit)
            btn = QPushButton("Обзор...")
            btn.clicked.connect(
                lambda checked=False, n=name, e=edit: self._browse_path(n, e)
            )
            row.addWidget(btn)
            paths_form.addRow(f"{name.capitalize()}:", row)

        stream_layout.addWidget(paths_group)

        # --- Кнопки управления потоком ---
        stream_btn_layout = QHBoxLayout()
        stream_btn_layout.setSpacing(4)
        self.start_btn = QPushButton("▶ Запустить сервер")
        self.stop_btn = QPushButton("⏹ Остановить")
        self.ffplay_btn = QPushButton("ffplay")
        self.vlc_btn = QPushButton("VLC")

        self.start_btn.clicked.connect(self._start_server)
        self.stop_btn.clicked.connect(self._stop_server)
        self.ffplay_btn.clicked.connect(lambda: self._open_in_player("ffplay"))
        self.vlc_btn.clicked.connect(lambda: self._open_in_player("vlc"))

        stream_btn_layout.addWidget(self.start_btn)
        stream_btn_layout.addWidget(self.stop_btn)
        stream_btn_layout.addStretch()
        self.stream_log_btn = QPushButton("📋 Лог")
        self.stream_log_btn.setToolTip("Просмотреть лог FFmpeg")
        self.stream_log_btn.clicked.connect(self._show_stream_log_dialog)
        stream_btn_layout.addWidget(self.stream_log_btn)
        stream_btn_layout.addWidget(self.ffplay_btn)
        stream_btn_layout.addWidget(self.vlc_btn)
        stream_layout.addLayout(stream_btn_layout)

        stream_layout.addStretch()
        self.mode_tabs.addTab(stream_tab, "Стрим")

        # --- Вкладка "Запись" (вложенная) ---
        rec_tab = QWidget()
        rec_layout = QVBoxLayout(rec_tab)
        rec_layout.setSpacing(6)

        # --- Настройки записи + Наложение текста ---
        rec_settings_group = QGroupBox("Настройки записи")
        rec_settings_group.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        rec_settings_layout = QVBoxLayout(rec_settings_group)
        rec_settings_layout.setSpacing(4)

        self.rec_path_edit = QLineEdit()
        self.rec_path_edit.setPlaceholderText("/home/user/recordings")
        rec_paths_form = QFormLayout()
        rec_paths_form.setSpacing(4)
        rec_paths_form.addRow("Путь на сервере:", self.rec_path_edit)

        self.rec_format_combo = QComboBox()
        self.rec_format_combo.addItem("MKV (рекомендуется)", "mkv")
        self.rec_format_combo.addItem("MP4", "mp4")
        self.rec_format_combo.addItem("TS", "ts")
        self.rec_format_combo.addItem("AVI", "avi")
        rec_paths_form.addRow("Формат:", self.rec_format_combo)

        self.rec_filename_edit = QLineEdit()
        self.rec_filename_edit.editingFinished.connect(self._on_filename_edited)
        rec_paths_form.addRow("Файл записи:", self.rec_filename_edit)

        rec_settings_layout.addLayout(rec_paths_form)
        self.tmp_warning = QLabel(
            "/tmp — временное хранилище: записи могут удаляться при перезагрузке."
        )
        self.tmp_warning.setWordWrap(True)
        rec_settings_layout.addWidget(self.tmp_warning)
        self.rec_path_edit.textChanged.connect(self._update_tmp_warning)
        rec_layout.addWidget(rec_settings_group)

        # --- Статус + Информация о записи ---
        rec_info_group = QGroupBox("Информация")
        rec_info_group.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        rec_info_layout = QFormLayout(rec_info_group)
        rec_info_layout.setSpacing(2)
        self.rec_status_label = QLabel("Запись не активна")
        self.rec_file_label = QLabel("—")
        self.rec_size_label = QLabel("—")
        self.rec_duration_label = QLabel("—")
        rec_info_layout.addRow("Статус:", self.rec_status_label)
        rec_info_layout.addRow("Файл:", self.rec_file_label)
        rec_info_layout.addRow("Размер:", self.rec_size_label)
        rec_info_layout.addRow("Длительность:", self.rec_duration_label)
        rec_layout.addWidget(rec_info_group)

        # --- Кнопки записи ---
        rec_btn_layout = QHBoxLayout()
        rec_btn_layout.setSpacing(4)
        self.rec_start_btn = QPushButton("⏺ Начать запись")
        self.rec_start_btn.clicked.connect(self._start_recording)
        self.rec_stop_btn = QPushButton("⏹ Остановить")
        self.rec_stop_btn.clicked.connect(self._stop_recording)
        self.rec_stop_btn.setEnabled(False)
        rec_btn_layout.addWidget(self.rec_start_btn)
        rec_btn_layout.addWidget(self.rec_stop_btn)
        self.rec_editor_btn = QPushButton("Открыть в редакторе…", self)
        self.rec_editor_btn.setToolTip(
            "Скачать завершённую запись и открыть её в видеоредакторе"
        )
        self.rec_editor_btn.clicked.connect(self._download_to_video_editor)
        rec_btn_layout.addWidget(self.rec_editor_btn)
        self.rec_download_btn = QPushButton("Скачать…")
        self.rec_download_btn.clicked.connect(lambda: self._download_recording())
        self.download_cancel_btn = QPushButton("Отменить загрузку")
        self.download_cancel_btn.clicked.connect(self._cancel_download)
        rec_download_layout = QHBoxLayout()
        rec_download_layout.addWidget(self.rec_download_btn)
        rec_download_layout.addWidget(self.download_cancel_btn)
        self.download_status = QLabel("")
        self.download_status.setWordWrap(True)
        rec_btn_layout.addStretch()
        self.rec_log_btn = QPushButton("📋 Лог")
        self.rec_log_btn.setToolTip("Просмотреть лог записи")
        self.rec_log_btn.clicked.connect(self._show_rec_log_dialog)
        rec_btn_layout.addWidget(self.rec_log_btn)
        rec_layout.addLayout(rec_btn_layout)
        rec_layout.addLayout(rec_download_layout)
        rec_layout.addWidget(self.download_status)

        rec_layout.addStretch()
        self.mode_tabs.addTab(rec_tab, "Запись")

        source_layout.addWidget(self.mode_tabs, 1)

        # --- Собираем верхний TabWidget ---
        self.tabs = QTabWidget()
        self.tabs.addTab(source_tab, "Источник")
        layout.addWidget(self.tabs, 1)

        # --- Лог (внутренний) ---
        self._log_lines: list[tuple[str, str | None]] = []

        # --- Кнопка закрытия ---
        close_layout = QHBoxLayout()
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.close)
        close_layout.addStretch()
        self.retry_btn = QPushButton("Повторить проверку")
        self.retry_btn.clicked.connect(self._coordinator.poll)
        close_layout.addWidget(self.force_scan_btn)
        close_layout.addWidget(self.retry_btn)
        close_layout.addWidget(self.close_btn)
        layout.addLayout(close_layout)

        # Обновить имя файла при изменении формата
        self.rec_format_combo.currentIndexChanged.connect(self._update_rec_filename)
        self._update_rec_filename()

        # Начальное состояние движка
        self._on_engine_changed(0)

    # -------------------------------------------------------------------
    # Настройки
    # -------------------------------------------------------------------
    def _load_settings(self) -> None:
        from src.architecture import ViewState
        from src.di import get_container

        self._view_state = get_container().resolve_optional(ViewState)
        if self._view_state is None:
            self._view_state = ViewState()
        self._device_state_id = "remote_recording." + self.device.iid
        self._view_state.register("remote_recording.players", persist=True)
        self._view_state.register(self._device_state_id, persist=True)
        players = self._view_state.get("remote_recording.players") or {}
        for key, fallback in (("ffplay_path", "ffplay"), ("vlc_path", "vlc")):
            getattr(self, key + "_edit").setText(
                players.get(key) or _find_in_path(fallback) or fallback
            )
        saved = self._view_state.get(self._device_state_id) or {}
        for key, attribute in (
            ("video", "_scan_results"),
            ("audio", "_scan_audio_results"),
            ("monitors", "_scan_monitor_results"),
        ):
            setattr(self, attribute, saved.get(key, []))
        for name in self._capture_widget_names():
            if name not in saved:
                continue
            widget, value = getattr(self, name), saved[name]
            if isinstance(widget, QComboBox):
                index = widget.findData(value["data"])
                if index < 0:
                    widget.addItem(value["text"], value["data"])
                    index = widget.count() - 1
                widget.setCurrentIndex(index)
            elif isinstance(widget, QLineEdit):
                widget.setText(value)
            elif isinstance(widget, QSpinBox):
                widget.setValue(value)
            elif isinstance(widget, QGroupBox):
                widget.setChecked(value)
        if not self.rec_path_edit.text():
            self.rec_path_edit.setText("/tmp/recordings")
        self._generated_filename = saved.get(
            "generated_filename", self._generated_filename
        )
        self._update_rec_filename()
        self._validate_all_binaries()
        self._update_tmp_warning()

    @staticmethod
    def _capture_widget_names() -> tuple[str, ...]:
        return (
            "capture_type_combo",
            "engine_combo",
            "transport_combo",
            "capture_input_combo",
            "profile_combo",
            "input_format_combo",
            "codec_combo",
            "resolution_combo",
            "fps_combo",
            "bitrate_edit",
            "port_spin",
            "_audio_group",
            "audio_source_combo",
            "audio_device_combo",
            "audio_codec_combo",
            "audio_bitrate_edit",
            "rec_path_edit",
            "rec_format_combo",
            "rec_filename_edit",
        )

    def _save_settings(self) -> None:
        if self._view_state is None:
            return
        players = {
            "ffplay_path": self.ffplay_path_edit.text(),
            "vlc_path": self.vlc_path_edit.text(),
        }
        settings = {
            "generated_filename": self._generated_filename,
            "video": self._scan_results,
            "audio": self._scan_audio_results,
            "monitors": self._scan_monitor_results,
        }
        for name in self._capture_widget_names():
            widget = getattr(self, name)
            if isinstance(widget, QComboBox):
                value = {"data": widget.currentData(), "text": widget.currentText()}
            elif isinstance(widget, QLineEdit):
                value = widget.text()
            elif isinstance(widget, QSpinBox):
                value = widget.value()
            else:
                value = widget.isChecked()
            settings[name] = value
        for key, value in (
            ("remote_recording.players", players),
            (self._device_state_id, settings),
        ):
            self._view_state.update(key, value, track_change=False)
            self._view_state._save_persistent(key, value)

    def _update_tmp_warning(self) -> None:
        path = self.rec_path_edit.text().rstrip("/")
        self.tmp_warning.setVisible(path == "/tmp" or path.startswith("/tmp/"))

    def _validate_binary_field(self, edit: QLineEdit, name: str) -> bool:
        ok = _validate_binary(edit.text() or name)
        if ok:
            edit.setToolTip("")
        else:
            edit.setToolTip(f"Не найден в PATH: {edit.text() or name}")
        return ok

    def _validate_all_binaries(self) -> None:
        self._validate_binary_field(self.ffplay_path_edit, "ffplay")
        self._validate_binary_field(self.vlc_path_edit, "vlc")

    # -------------------------------------------------------------------
    # Профили
    # -------------------------------------------------------------------
    def _on_profile_changed(self, index: int) -> None:
        """При смене профиля обновить кодек и разрешение. FPS выставится автоматически через _on_resolution_changed на максимум поддерживаемого."""
        if index < 0:
            return
        key = self.profile_combo.currentData()
        prof = _PROFILES.get(key)
        if not prof:
            return
        self.codec_combo.setCurrentIndex(self.codec_combo.findData(prof["codec"]))
        res_idx = self.resolution_combo.findData(prof["resolution"])
        if res_idx >= 0:
            self.resolution_combo.setCurrentIndex(res_idx)
            self._on_resolution_changed(res_idx)
        self.bitrate_edit.setText(prof["bitrate"])

    # -------------------------------------------------------------------
    # Тип захвата
    # -------------------------------------------------------------------
    def _on_capture_type_changed(self) -> None:
        capture_type = self.capture_type_combo.currentData()
        placeholders = {
            "x11grab": ":0.0",
            "v4l2": "/dev/video0",
        }
        self.capture_input_combo.setPlaceholderText(placeholders.get(capture_type, ""))
        self.capture_input_combo.blockSignals(True)
        self.capture_input_combo.clear()
        self.capture_input_combo.blockSignals(False)

        if capture_type == "v4l2":
            cached = _get_cached_scan(self.device.host, _scan_cache_video)
            if cached is not None:
                self._scan_results = cached
                for item in cached:
                    caps = item.get("capabilities", {})
                    label = item["device"]
                    if caps:
                        resolutions = sorted(
                            caps.keys(),
                            key=lambda r: int(r.split("x")[0]) * int(r.split("x")[1]),
                            reverse=True,
                        )
                        label += f" ({', '.join(resolutions[:3])})"
                    self.capture_input_combo.addItem(label, item["device"])
                if self.capture_input_combo.count() > 0:
                    self.capture_input_combo.setCurrentIndex(0)
                    self._on_device_selected(0)
        elif capture_type == "x11grab":
            mon_cached = _get_cached_scan(self.device.host, _scan_cache_monitors)
            if mon_cached is not None:
                self._scan_monitor_results = mon_cached
                self._populate_monitor_combo(mon_cached)

        # Аудио кэш
        audio_cached = _get_cached_scan(self.device.host, _scan_cache_audio)
        if audio_cached is not None:
            self._scan_audio_results = audio_cached
            current_driver = self.audio_source_combo.currentData()
            self._populate_audio_combo(current_driver)

        if capture_type != "v4l2" or not self._scan_results:
            self._reset_resolution_defaults()

    # -------------------------------------------------------------------
    # Сканирование
    # -------------------------------------------------------------------
    def _populate_audio_combo(self, driver: str | None = None) -> None:
        """Заполнить combo аудио-устройств из кэша, опционально фильтруя по драйверу."""
        if not self._scan_audio_results:
            return
        self.audio_device_combo.blockSignals(True)
        self.audio_device_combo.clear()
        for item in self._scan_audio_results:
            item_driver = item.get("driver", "pulse")
            if driver and item_driver != driver:
                continue
            label = item["description"] or item["name"]
            self.audio_device_combo.addItem(f"[{item_driver}] {label}", item["name"])
        if self.audio_device_combo.count() > 0:
            self.audio_device_combo.setCurrentIndex(0)
        self.audio_device_combo.blockSignals(False)

    # -------------------------------------------------------------------
    # Аудио-драйвер
    # -------------------------------------------------------------------
    def _on_audio_driver_changed(self, index: int) -> None:
        driver = self.audio_source_combo.itemData(index)
        self._populate_audio_combo(driver)

    # -------------------------------------------------------------------
    # Заполнение combo мониторов
    # -------------------------------------------------------------------
    def _populate_monitor_combo(self, monitors: list) -> None:
        """Заполнить combo источника для x11grab из списка мониторов."""
        self.capture_input_combo.blockSignals(True)
        self.capture_input_combo.clear()
        if monitors:
            total_w = sum(m["width"] for m in monitors)
            max_h = max(m["height"] + m["y_offset"] for m in monitors)
            self.capture_input_combo.addItem(f"Все мониторы ({total_w}x{max_h})", "all")
            for mon in monitors:
                label = f"{mon['name']} ({mon['width']}x{mon['height']}+{mon['x_offset']}+{mon['y_offset']})"
                self.capture_input_combo.addItem(label, f"mon_{mon['index']}")
            if self.capture_input_combo.count() > 0:
                self.capture_input_combo.setCurrentIndex(0)
        else:
            self.capture_input_combo.setPlaceholderText(":0.0")
        self.capture_input_combo.blockSignals(False)

    # -------------------------------------------------------------------
    # Сканирование
    # -------------------------------------------------------------------
    def _scan_devices(self, force: bool = False) -> None:
        if self._scan_thread is not None or self._closing:
            return
        if force:
            for cache in (_scan_cache_video, _scan_cache_audio, _scan_cache_monitors):
                cache.pop(self.device.host, None)
        scan_monitors = self.capture_type_combo.currentData() == "x11grab"
        vid = _get_cached_scan(self.device.host, _scan_cache_video)
        aud = _get_cached_scan(self.device.host, _scan_cache_audio)
        mon = _get_cached_scan(self.device.host, _scan_cache_monitors)
        if (
            vid is not None
            and aud is not None
            and (not scan_monitors or mon is not None)
        ):
            self._on_scan_result(vid, aud, mon or [], "")
            return
        self.scan_btn.setEnabled(False)
        self.force_scan_btn.setEnabled(False)
        self.ssh_status_label.setText("Сканирование…")
        thread = ScanThread(self.device, scan_monitors)
        self._scan_thread = thread

        def apply() -> None:
            self._scan_thread = None
            if not self._closing:
                self._on_scan_result(*(thread.result or ([], [], [], thread.error)))

        self._coordinator.own(thread, apply)
        self._update_buttons()

    def _on_scan_result(
        self,
        video_devices: list,
        audio_devices: list,
        monitor_devices: list,
        error: str,
    ) -> None:
        self.scan_btn.setEnabled(True)
        self.force_scan_btn.setEnabled(True)
        self.ssh_status_label.setText(
            "Ошибка сканирования: " + error if error else "Сканирование завершено"
        )
        if error:
            self._append_log("[ERROR] " + error)
            return

        _set_cached_scan(self.device.host, video_devices, _scan_cache_video)
        _set_cached_scan(self.device.host, audio_devices, _scan_cache_audio)

        if monitor_devices is not None:
            _set_cached_scan(self.device.host, monitor_devices, _scan_cache_monitors)

        self._scan_results = video_devices
        self._scan_audio_results = audio_devices
        self._scan_monitor_results = monitor_devices

        # --- Видео: заполняем combo только для v4l2 ---
        capture_type = self.capture_type_combo.currentData()
        if capture_type == "v4l2":
            self.capture_input_combo.blockSignals(True)
            self.capture_input_combo.clear()
            if video_devices:
                for item in video_devices:
                    caps = item.get("capabilities", {})
                    label = item["device"]
                    if caps:
                        resolutions = sorted(
                            caps.keys(),
                            key=lambda r: int(r.split("x")[0]) * int(r.split("x")[1]),
                            reverse=True,
                        )
                        label += f" ({', '.join(resolutions[:3])})"
                    self.capture_input_combo.addItem(label, item["device"])
                if self.capture_input_combo.count() > 0:
                    self.capture_input_combo.setCurrentIndex(0)
            self.capture_input_combo.blockSignals(False)
            if video_devices:
                self._on_device_selected(0)
        elif capture_type == "x11grab":
            self._populate_monitor_combo(monitor_devices)
        self._update_connection_url()

        # --- Аудио ---
        current_driver = self.audio_source_combo.currentData()
        self._populate_audio_combo(current_driver)

    # -------------------------------------------------------------------
    # Выбор устройства / разрешения
    # -------------------------------------------------------------------
    def _on_device_selected(self, index: int) -> None:
        if index < 0:
            return
        device_path = self.capture_input_combo.itemData(index)
        if not device_path:
            return

        capture_type = self.capture_type_combo.currentData()

        # Для x11grab проверяем мониторы
        if capture_type == "x11grab":
            if device_path == "all":
                # Все мониторы — без обрезки
                self._reset_resolution_defaults()
                self._update_connection_url()
                return
            for mon in self._scan_monitor_results:
                if f"mon_{mon['index']}" == device_path:
                    native_res = f"{mon['width']}x{mon['height']}"
                    self.resolution_combo.blockSignals(True)
                    self.resolution_combo.clear()
                    self.resolution_combo.addItem(native_res, native_res)
                    for res in ("1920x1080", "1280x720", "1024x768", "800x600"):
                        if res != native_res:
                            self.resolution_combo.addItem(res, res)
                    self.resolution_combo.setCurrentIndex(0)
                    self.resolution_combo.blockSignals(False)
                    self._on_resolution_changed(0)
                    self._update_connection_url()
                    return
            self._update_connection_url()
            return

        device_data = None
        for item in self._scan_results:
            if item["device"] == device_path:
                device_data = item
                break

        caps = device_data.get("capabilities", {}) if device_data else {}
        self._update_connection_url()

        if caps:
            self.resolution_combo.blockSignals(True)
            self.resolution_combo.clear()
            for res in sorted(
                caps.keys(),
                key=lambda r: int(r.split("x")[0]) * int(r.split("x")[1]),
                reverse=True,
            ):
                self.resolution_combo.addItem(res, res)
            if self.resolution_combo.count() > 0:
                self.resolution_combo.setCurrentIndex(0)
            self.resolution_combo.blockSignals(False)
            self._on_resolution_changed(0)

    def _on_audio_device_changed(self, index: int) -> None:
        if index < 0:
            return

    def _on_resolution_changed(self, index: int) -> None:
        if index < 0:
            return
        resolution = self.resolution_combo.itemData(index)
        if not resolution:
            return

        device_path = self.capture_input_combo.currentData()
        supported_fps: list[int] = []
        for item in self._scan_results:
            if item["device"] == device_path:
                supported_fps = item.get("capabilities", {}).get(resolution, [])
                break

        self.fps_combo.blockSignals(True)
        self.fps_combo.clear()
        if supported_fps:
            supported_fps = sorted(set(supported_fps))
            for fps in supported_fps:
                self.fps_combo.addItem(str(fps), fps)
            self.fps_combo.setCurrentIndex(self.fps_combo.count() - 1)
        else:
            for fps in (15, 20, 25, 30, 60):
                self.fps_combo.addItem(str(fps), fps)
            self.fps_combo.setCurrentIndex(2)
        self.fps_combo.blockSignals(False)

    def _reset_resolution_defaults(self) -> None:
        self.resolution_combo.blockSignals(True)
        self.resolution_combo.clear()
        for res in ("1920x1080", "1280x720", "1024x768", "800x600"):
            self.resolution_combo.addItem(res, res)
        self.resolution_combo.setCurrentIndex(0)
        self.resolution_combo.blockSignals(False)
        self.fps_combo.blockSignals(True)
        self.fps_combo.clear()
        for fps in (15, 20, 25, 30, 60):
            self.fps_combo.addItem(str(fps), fps)
        self.fps_combo.setCurrentIndex(2)
        self.fps_combo.blockSignals(False)
        self.input_format_combo.setCurrentIndex(0)

    def _get_capture_input(self) -> str:
        data = self.capture_input_combo.currentData()
        if (
            self.capture_input_combo.isEditable()
            and self.capture_input_combo.currentText()
            != self.capture_input_combo.itemText(
                self.capture_input_combo.currentIndex()
            )
        ):
            return self.capture_input_combo.currentText().strip()
        if data is None and self.capture_input_combo.currentText().strip():
            return self.capture_input_combo.currentText().strip()
        if data is None and self.capture_input_combo.currentText().strip():
            return self.capture_input_combo.currentText().strip()
        if data == "all":
            return ":0.0"
        if data and data.startswith("mon_"):
            return ":0.0"
        if data and not data.startswith("mon_"):
            return data
        capture_type = self.capture_type_combo.currentData()
        defaults = {
            "x11grab": ":0.0",
            "v4l2": "/dev/video0",
        }
        return defaults.get(capture_type, "")

    def _get_stream_url(self) -> str:
        transport = self.transport_combo.currentData()
        port = self.port_spin.value()
        host = self.device.host
        if transport == "tcp":
            return f"tcp://{host}:{port}"
        elif transport == "http":
            return f"http://{host}:{port}/stream.ts"
        elif transport == "rtsp":
            return f"rtsp://{host}:{port}/stream"
        return f"tcp://{host}:{port}"

    def _update_connection_url(self) -> None:
        url = (
            self._coordinator.session.url
            if self._coordinator.active
            else self._get_stream_url()
        )
        self.url_edit.setPlaceholderText(f"URL: {url}")

    # -------------------------------------------------------------------
    # Выбор движка (FFmpeg / GStreamer)
    # -------------------------------------------------------------------
    def _on_engine_changed(self, index: int) -> None:
        engine = self.engine_combo.currentData() if index >= 0 else "gstreamer"
        is_gst = engine == "gstreamer"

        self.gst_install_btn.setVisible(is_gst)
        self.gst_status_label.setVisible(is_gst)

        # Управляем доступностью протоколов
        self.transport_combo.blockSignals(True)

        if not is_gst:
            # FFmpeg: TCP, HTTP
            self.transport_combo.clear()
            self.transport_combo.addItem("TCP (listen)", "tcp")
            self.transport_combo.addItem("HTTP (listen)", "http")
            self.port_spin.setValue(8080)
        else:
            # GStreamer: только RTSP
            self.transport_combo.clear()
            self.transport_combo.addItem("RTSP", "rtsp")
            self.port_spin.setValue(8554)

        self.transport_combo.blockSignals(False)
        self._update_connection_url()
        self._update_buttons()

    def _on_transport_changed(self, index: int) -> None:
        transport = self.transport_combo.currentData()
        if transport == "rtsp":
            self.port_spin.setValue(8554)
        elif self.port_spin.value() == 8554:
            self.port_spin.setValue(8080)
        self._update_connection_url()

    # -------------------------------------------------------------------
    # Проверка и установка GStreamer
    # -------------------------------------------------------------------
    def _install_gstreamer(self) -> None:
        if (
            self._coordinator.active
            or self._closing
            or getattr(self, "_installing", False)
        ):
            return
        self._installing = True
        self.gst_install_btn.setEnabled(False)
        self.gst_status_label.setText("Установка GStreamer...")
        worker = InstallGStreamerThread(self.device)
        self._coordinator.own(
            worker,
            lambda: self._on_gst_install_finished(
                self.device.iid, not worker.error, worker.error or worker.result
            ),
        )
        self._update_buttons()

    def _on_gst_install_finished(
        self, device_iid: str, success: bool, message: str
    ) -> None:
        self._installing = False
        self.gst_install_btn.setEnabled(True)
        if success:
            self.gst_status_label.setStyleSheet("color: green;")
            self.gst_status_label.setText(f"✅ {message}")
        else:
            self.gst_status_label.setStyleSheet("color: red;")
            self.gst_status_label.setText(f"❌ {message}")

    def _copy_url_to_clipboard(self) -> None:
        url = self.url_edit.text()
        if not url:
            # Попробуем достать из placeholder
            ph = self.url_edit.placeholderText()
            if ph.startswith("URL: "):
                url = ph[5:].split(" ")[0]
        if url:
            QApplication.clipboard().setText(url)
            self.copy_url_btn.setText("✓")
            QTimer.singleShot(
                1200, self.copy_url_btn, lambda: self.copy_url_btn.setText("📋")
            )

    # -------------------------------------------------------------------
    # Пути / Browse
    # -------------------------------------------------------------------
    def _browse_path(self, name: str, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Выберите {name}",
            "",
            "Исполняемые файлы (*.exe);;Все файлы (*)"
            if os.name == "nt"
            else "Все файлы (*)",
        )
        if path:
            edit.setText(path)
            self._validate_binary_field(edit, name)
            self._save_settings()

    # -------------------------------------------------------------------
    # SSH-проверка
    # -------------------------------------------------------------------
    def _check_ssh(self) -> None:
        if self._ssh_check_thread is not None or self._closing:
            return
        self.ssh_check_btn.setEnabled(False)
        self.ssh_status_label.setText("Проверка…")
        thread = SSHCheckThread(self.device)
        self._ssh_check_thread = thread

        def apply() -> None:
            self._ssh_check_thread = None
            if not self._closing:
                self._on_ssh_checked(*(thread.result or (False, thread.error)))

        self._coordinator.own(thread, apply)

    def _on_ssh_checked(self, ok: bool, message: str) -> None:
        self.ssh_check_btn.setEnabled(True)
        self.ssh_status_label.setText(message)

    # -------------------------------------------------------------------
    # Запуск / Остановка
    # -------------------------------------------------------------------
    def _start_server(self) -> None:
        self._begin_session("stream")

    def _capture_settings(self) -> dict:
        settings = {
            "engine": self.engine_combo.currentData(),
            "capture_type": self.capture_type_combo.currentData(),
            "capture_input": self._get_capture_input(),
            "input_format": self.input_format_combo.currentData() or "",
            "transport": self.transport_combo.currentData(),
            "codec": self.codec_combo.currentData(),
            "resolution": self.resolution_combo.currentData()
            or self.resolution_combo.currentText(),
            "bitrate": self.bitrate_edit.text() or "2M",
            "fps": self.fps_combo.currentData() or 25,
            "port": self.port_spin.value(),
            "enable_audio": self._audio_group.isChecked(),
            "audio_source": self.audio_source_combo.currentData() or "pulse",
            "audio_input": self.audio_device_combo.currentData()
            or self.audio_device_combo.currentText()
            or "default",
            "audio_codec": self.audio_codec_combo.currentData() or "aac",
            "audio_bitrate": self.audio_bitrate_edit.text() or "128k",
        }
        if settings["capture_type"] == "v4l2":
            for item in self._scan_results:
                if item["device"] != settings["capture_input"]:
                    continue
                modes = item.get("formats", {})
                choices = []
                for fmt, caps in modes.items():
                    rates = caps.get(settings["resolution"])
                    if rates is not None and (not rates or settings["fps"] in rates):
                        choices.append(fmt)
                if not settings["input_format"]:
                    if not choices:
                        raise ValueError(
                            "Камера не поддерживает выбранные разрешение и FPS"
                        )
                    settings["input_format"] = (
                        "mjpeg" if "mjpeg" in choices else choices[0]
                    )
                elif settings["input_format"] not in choices:
                    raise ValueError(
                        "Формат камеры не поддерживает выбранные разрешение и FPS"
                    )
        else:
            selected = self.capture_input_combo.currentData()
            for mon in self._scan_monitor_results:
                if selected == f"mon_{mon['index']}":
                    settings.update(
                        monitor_x_offset=mon["x_offset"],
                        monitor_y_offset=mon["y_offset"],
                        monitor_width=mon["width"],
                        monitor_height=mon["height"],
                        crop_left=mon["x_offset"],
                        crop_top=mon["y_offset"],
                        crop_right=mon.get(
                            "screen_width", mon["width"] + mon["x_offset"]
                        )
                        - mon["x_offset"]
                        - mon["width"],
                        crop_bottom=mon.get(
                            "screen_height", mon["height"] + mon["y_offset"]
                        )
                        - mon["y_offset"]
                        - mon["height"],
                    )
        return settings

    def _begin_session(self, mode: str) -> None:
        if (
            self._coordinator.active
            or self._editor_download is not None
            or self._closing
            or self._scan_thread is not None
            or getattr(self, "_installing", False)
        ):
            return
        try:
            self._save_settings()
            settings = self._capture_settings()
            if not settings["capture_input"]:
                raise ValueError("Укажите источник захвата")
            output = ""
            if mode == "record":
                self._update_rec_filename()
                self._on_filename_edited()
                name = self.rec_filename_edit.text().strip()
                if (
                    not name
                    or name in (".", "..")
                    or "/" in name
                    or "\\" in name
                    or "\0" in name
                ):
                    raise ValueError("Укажите имя файла без пути")
                if not self.rec_path_edit.text().strip().startswith("/"):
                    raise ValueError("Укажите абсолютный путь на сервере")
                output = self._get_rec_filepath()
                self._rec_filepath = output
                self.rec_file_label.setText(output)
            self._rec_start_time = time.time()
            self._coordinator.start(self.device, settings, mode, output)
        except (ValueError, TypeError) as error:
            QMessageBox.warning(self, "Удалённая запись", str(error))

    def _stop_server(self) -> None:
        self._coordinator.stop()

    def _open_in_player(self, player: str) -> None:
        session = self._coordinator.session
        if session is None or self._coordinator.state != RecordingState.RUNNING:
            return
        import json

        url = session.url
        transport = json.loads(session.settings_json)["transport"]
        if player == "ffplay":
            path = self.ffplay_path_edit.text() or "ffplay"
            args = [url]
            if transport == "rtsp":
                args = ["-rtsp_transport", "tcp", url]
        else:
            path = self.vlc_path_edit.text() or "vlc"
            args = [url]
            if transport == "rtsp":
                args = ["--rtsp-tcp", url]

        process = QProcess(self)
        process.finished.connect(lambda: self._player_done(process))

        def on_error(error: QProcess.ProcessError) -> None:
            if not self._closing:
                self._on_player_error(player, error)
            if error == QProcess.ProcessError.FailedToStart:
                self._player_done(process)

        process.errorOccurred.connect(on_error)
        self._player_processes.append(process)
        process.start(path, args)

    def _player_done(self, process: QProcess) -> None:
        if process in self._player_processes:
            self._player_processes.remove(process)
            process.deleteLater()
        if (
            self._closing
            and not self._coordinator.workers
            and not self._coordinator.active
        ):
            self._finish_close()

    def _on_player_error(self, player: str, error: QProcess.ProcessError) -> None:
        try:
            if not self.isVisible():
                return
        except RuntimeError:
            return
        errors = {
            QProcess.ProcessError.FailedToStart: (
                f"{player} не может быть запущен. Проверьте путь."
            ),
            QProcess.ProcessError.Crashed: f"{player} завершился аварийно.",
            QProcess.ProcessError.Timedout: f"{player} не ответил вовремя.",
            QProcess.ProcessError.ReadError: f"Ошибка чтения из {player}.",
            QProcess.ProcessError.WriteError: f"Ошибка записи в {player}.",
        }
        QMessageBox.warning(
            self, "Ошибка", errors.get(error, f"Неизвестная ошибка {player}")
        )

    # -------------------------------------------------------------------
    # История трансляции
    # -------------------------------------------------------------------

    def _append_log(self, line: str, kind: str | None = None) -> None:
        self._log_lines.append((line, kind))
        del self._log_lines[:-2000]

    # -------------------------------------------------------------------
    # Статус / Кнопки
    # -------------------------------------------------------------------
    def _update_buttons(self) -> None:
        coordinator = self._coordinator
        busy = (
            coordinator.active
            or self._editor_download is not None
            or self._closing
            or self._scan_thread is not None
            or getattr(self, "_installing", False)
        )
        running = coordinator.active and coordinator.session.mode == "stream"
        recording = coordinator.active and coordinator.session.mode == "record"
        stopping = coordinator.state == RecordingState.STOPPING
        self.start_btn.setEnabled(not busy)
        self.rec_start_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(running and not stopping)
        self.rec_stop_btn.setEnabled(recording and not stopping)
        playing = running and coordinator.state == RecordingState.RUNNING
        self.ffplay_btn.setEnabled(playing)
        self.vlc_btn.setEnabled(playing)
        self.rec_editor_btn.setEnabled(coordinator.downloadable and not busy)
        self.rec_download_btn.setEnabled(coordinator.downloadable and not busy)
        self.download_cancel_btn.setEnabled(self._editor_download is not None)
        self.retry_btn.setEnabled(
            coordinator.state == RecordingState.LOST and coordinator.operation is None
        )
        for name in self._capture_widget_names():
            getattr(self, name).setEnabled(not busy)
        self.gst_install_btn.setEnabled(
            not busy and not getattr(self, "_installing", False)
        )
        self.scan_btn.setEnabled(not busy and self._scan_thread is None)
        self.force_scan_btn.setEnabled(not busy and self._scan_thread is None)

    def _session_changed(self) -> None:
        coordinator = self._coordinator
        self._closing = coordinator.closing
        label = coordinator.state.value
        self.status_label.setText(label)
        if coordinator.session and coordinator.session.mode == "record":
            self.rec_status_label.setText(label)
        if coordinator.session and coordinator.session.mode == "stream":
            self.url_edit.setText(
                coordinator.session.url
                if coordinator.state == RecordingState.RUNNING
                else ""
            )
        self.close_btn.setText(
            "Завершение…"
            if self._closing
            else "Закрыть без остановки…"
            if coordinator.stop_unconfirmed
            else "Закрыть"
        )
        self._update_buttons()

    def _session_snapshot(self, snapshot: dict) -> None:
        size = snapshot.get("size", 0)
        self.rec_size_label.setText(f"{size / 1048576:.1f} МиБ")
        mins, secs = divmod(int(time.time() - self._rec_start_time), 60)
        self.rec_duration_label.setText(f"{mins}:{secs:02d}")

    def _session_log(self, line: str) -> None:
        if self._coordinator.session and self._coordinator.session.mode == "record":
            self._append_rec_log(line)
        else:
            self._append_log(line)

    def showEvent(self, event: QEvent) -> None:
        super().showEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._close_ready:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        if self._coordinator.stop_unconfirmed:
            # Повторная остановка уже не подтвердилась: окно не должно стать ловушкой.
            app_exit = getattr(self.parent(), "_shutdown_pending", False)
            if not app_exit:
                answer = QMessageBox.question(
                    self,
                    "Остановка не подтверждена",
                    "Не удалось подтвердить остановку процесса на удалённом хосте.\n\n"
                    "«Повторить» — ещё раз попытаться остановить.\n"
                    "«Закрыть» — закрыть окно; процесс на хосте может продолжить работу.",
                    QMessageBox.StandardButton.Retry
                    | QMessageBox.StandardButton.Close
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Retry,
                )
                if answer == QMessageBox.StandardButton.Cancel:
                    return
                if answer == QMessageBox.StandardButton.Retry:
                    self._closing = True
                    self._coordinator.shutdown()
                    self._session_changed()
                    return
            self._save_settings()
            self._closing = True
            self._kill_player_processes()
            self._coordinator.abandon()
            self._session_changed()
            return
        self._save_settings()
        self._closing = True
        self._kill_player_processes()
        self._coordinator.shutdown()
        self._session_changed()

    def _finish_close(self) -> None:
        if self._player_processes or self._close_ready:
            return
        self._close_ready = True
        QTimer.singleShot(0, self, lambda: self.done(self._completion_code))

    def has_pending_workers(self) -> bool:
        # Неподтверждённая остановка без выполняющейся операции ждать нечего:
        # иначе выход из приложения висел бы до принудительного срока.
        coordinator = self._coordinator
        return (
            bool(coordinator.workers)
            or (coordinator.active and coordinator.state != RecordingState.LOST)
            or coordinator.operation is not None
            or bool(self._player_processes)
            or getattr(self, "_installing", False)
        )

    def _kill_player_processes(self) -> None:
        for proc in self._player_processes:
            if proc.state() != QProcess.ProcessState.NotRunning:
                proc.kill()

    def _browse_rec_path(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        path, ok = QInputDialog.getText(
            self,
            "Путь на сервере",
            "Укажите путь для сохранения записей на удалённой машине:",
            text=self.rec_path_edit.text(),
        )
        if ok and path.strip():
            self.rec_path_edit.setText(path.strip())
            self._update_rec_filename()
            self._save_settings()

    def _update_rec_filename(self) -> None:
        previous = self._generated_filename
        if self.rec_filename_edit.text() and self.rec_filename_edit.text() != previous:
            return
        host = self.device.host.replace(":", "_").replace("/", "_")
        fmt = self.rec_format_combo.currentData() or "mkv"
        self._generated_filename = (
            f"{host}_{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')}.{fmt}"
        )
        self.rec_filename_edit.setText(self._generated_filename)

    def _on_filename_edited(self) -> None:
        if not self.rec_filename_edit.text().strip():
            self._update_rec_filename()
        elif not os.path.splitext(self.rec_filename_edit.text())[1]:
            self.rec_filename_edit.setText(
                self.rec_filename_edit.text()
                + "."
                + (self.rec_format_combo.currentData() or "mkv")
            )

    def _get_rec_filepath(self) -> str:
        rec_dir = self.rec_path_edit.text().strip().rstrip("/")
        if not rec_dir:
            rec_dir = "/tmp"
        filename = self.rec_filename_edit.text().strip()
        return f"{rec_dir}/{filename}"

    def _start_recording(self) -> None:
        self._begin_session("record")

    def _stop_recording(self) -> None:
        self._coordinator.stop()

    def _download_to_video_editor(self) -> None:
        self._download_recording(True)

    def _download_recording(self, open_editor: bool = False) -> None:
        from pathlib import Path

        from src.workers.video_editor.recording_download import RecordingDownloadWorker

        if (
            not self._coordinator.downloadable
            or self._editor_download is not None
            or self._closing
        ):
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Скачать завершённую запись",
            Path(self._rec_filepath).name,
            "Медиа (*.mkv *.mp4 *.ts *.avi)",
        )
        if not path:
            return
        if Path(path).exists():
            QMessageBox.warning(
                self, "Загрузка записи", "Файл уже существует. Выберите другое имя."
            )
            return
        worker = RecordingDownloadWorker(
            self._coordinator.session.device, self._rec_filepath, path, False
        )
        self._editor_download = worker
        self._open_download_in_editor = open_editor
        worker.progress.connect(
            lambda percent: self.download_status.setText(f"Загрузка: {percent}%")
        )
        self._coordinator.own(worker, self._editor_recording_downloaded)
        self._update_buttons()

    def _cancel_download(self) -> None:
        if self._editor_download is not None:
            self._editor_download.cancelled.set()

    def _editor_recording_downloaded(self) -> None:
        worker, self._editor_download = self._editor_download, None
        self._update_buttons()
        self.download_status.setText(worker.error or f"Сохранено: {worker.output}")
        if worker.error or self._closing or not self._open_download_in_editor:
            return
        from src.ui.dialogs.tools.video_editor import VideoEditorDialog

        editor = VideoEditorDialog(self)
        editor.import_paths([worker.output])
        editor.setModal(False)
        editor.finished.connect(editor.deleteLater)
        editor.show()

    def reject(self) -> None:
        self.done(QDialog.DialogCode.Rejected)

    def done(self, result: int) -> None:
        if self._close_ready:
            super().done(result)
            return
        self._completion_code = result
        self.close()

    def _append_rec_log(self, line: str) -> None:
        kind = None
        if line.startswith("[ERROR]"):
            kind = "error"
        elif line.startswith("[WARN]"):
            kind = "warning"
        elif line.startswith("[INFO]"):
            kind = "info"
        self._rec_log_lines.append((line, kind))
        del self._rec_log_lines[:-2000]

    def _show_stream_log_dialog(self) -> None:
        self._show_log_dialog(False)

    def _show_rec_log_dialog(self) -> None:
        self._show_log_dialog(True)

    def _show_log_dialog(self, recording: bool) -> None:
        from contextlib import suppress

        dialog = QDialog(self)
        dialog.setWindowTitle("Лог записи" if recording else "Лог трансляции")
        dialog.resize(700, 400)
        layout = QVBoxLayout(dialog)
        text = QTextEdit(dialog)
        text.setReadOnly(True)
        text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        text.document().setMaximumBlockCount(2000)
        layout.addWidget(text)

        def render() -> None:
            history = self._rec_log_lines if recording else self._log_lines
            rendered = []
            for line, kind in history:
                color = {
                    "error": "#d32f2f",
                    "warning": "#f57c00",
                    "info": "#388e3c",
                }.get(kind)
                safe = html_module.escape(line)
                rendered.append(
                    f'<span style="color:{color}">{safe}</span>' if color else safe
                )
            text.setHtml("<pre>" + "<br>".join(rendered) + "</pre>")
            text.verticalScrollBar().setValue(text.verticalScrollBar().maximum())

        buttons = QHBoxLayout()
        refresh = QPushButton("Обновить с сервера", dialog)
        refresh.clicked.connect(self._coordinator.poll)
        close = QPushButton("Закрыть", dialog)
        close.clicked.connect(dialog.accept)
        buttons.addWidget(refresh)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        render()
        self._coordinator.changed.connect(render)
        self._coordinator.poll()
        try:
            dialog.exec()
        finally:
            with suppress(RuntimeError):
                self._coordinator.changed.disconnect(render)
                dialog.deleteLater()
