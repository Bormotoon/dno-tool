"""Проверить комплект FFmpeg и расширения PyAV в сборке приложения."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.abc
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from fractions import Fraction
from pathlib import Path


def audit(binary: Path, media_root: Path | None = None) -> dict[str, object]:
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(binary))
    normalized = {name.replace("\\", "/"): name for name in reader.toc}
    onedir = (binary.parent / "lib/media-runtime.json").is_file()
    default_root = binary.parent / ("lib" if onedir else "assets/media")
    if media_root is None:
        if onedir or (
            "media-runtime.json" not in normalized
            and (default_root / "media-runtime.json").is_file()
        ):
            media_root = default_root
    if media_root is None:
        manifest_name = normalized.get("media-runtime.json")
        if manifest_name is None:
            raise FileNotFoundError("В пакете не найден media-runtime.json.")
        manifest_bytes = reader.extract(manifest_name)
    else:
        manifest_bytes = (media_root / "media-runtime.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    checks: dict[str, bool] = {}
    pyz_name = next(name for name, entry in reader.toc.items() if entry[-1] == "z")
    modules = reader.open_embedded_archive(pyz_name).toc
    checks["no_development_diagnostics"] = not any(
        name in modules
        for name in (
            "src.diagnostics.media_check",
            "src.diagnostics.video_editor_check",
            "src.diagnostics.video_editor_profile",
        )
    )
    # Эти расширения обязательны уже при импорте главного окна приложения.
    for module in ("QtQml", "QtQuick", "QtQuickWidgets", "QtMultimedia"):
        prefix = f"PySide6/{module}."
        checks["required_" + module] = (
            any(
                path.suffix in (".so", ".pyd")
                for path in (media_root / "PySide6").glob(module + ".*")
            )
            if onedir
            else any(
                name.startswith(prefix) and name.endswith((".so", ".pyd"))
                for name in normalized
            )
        )
    for name, expected in manifest["libraries"].items():
        if media_root is None:
            archive_name = normalized.get(name)
            payload = reader.extract(archive_name) if archive_name else b""
            checks[name] = bool(payload) and hashlib.sha256(payload).hexdigest() == expected
            checks["embedded_" + name] = archive_name is not None
        else:
            file = media_root / name
            checks[name] = (
                file.is_file()
                and hashlib.sha256(file.read_bytes()).hexdigest() == expected
            )
            checks["external_" + name] = not any(
                Path(item).name == name for item in normalized
            )
    for name, expected in manifest.get("pyav_extensions", {}).items():
        full_name = "av/" + name
        if onedir:
            file = media_root / full_name
            checks[full_name] = (
                file.is_file()
                and hashlib.sha256(file.read_bytes()).hexdigest() == expected
            )
            continue
        actual_name = normalized.get(full_name, full_name)
        entry = reader.toc.get(actual_name)
        checks[full_name] = (
            entry is not None
            and entry[-1] == "b"
            and hashlib.sha256(reader.extract(actual_name)).hexdigest() == expected
        )
    checks["no_cli"] = not any(
        Path(name).name in ("ffmpeg", "ffprobe", "ffmpeg.exe", "ffprobe.exe")
        for name in reader.toc
    )
    for name, expected in manifest.get("assets", {}).items():
        if media_root is None:
            archive_name = normalized.get(name.replace("\\", "/"))
            payload = reader.extract(archive_name) if archive_name else b""
            checks[name] = bool(payload) and hashlib.sha256(payload).hexdigest() == expected
        else:
            file = media_root / name
            checks[name] = (
                file.is_file()
                and hashlib.sha256(file.read_bytes()).hexdigest() == expected
            )
    checks["no_wheel_libraries"] = not any("av.libs" in name for name in reader.toc)
    if onedir:
        xkb = manifest.get("xkb", {}).get("libraries", {})
        checks["xkb_pair_recorded"] = set(xkb) == {
            "libxkbcommon.so.0",
            "libxkbcommon-x11.so.0",
        }
        for name, expected in xkb.items():
            file = media_root / name
            checks["xkb_" + name] = (
                file.is_file()
                and hashlib.sha256(file.read_bytes()).hexdigest() == expected
            )
        checks["no_cli"] = checks["no_cli"] and not any(
            file.name in ("ffmpeg", "ffprobe") for file in media_root.rglob("*")
        )
        checks["no_wheel_libraries"] = checks["no_wheel_libraries"] and not any(
            "av.libs" in file.parts for file in media_root.rglob("*")
        )
        checks["external_assets"] = (binary.parent / "assets/themes").is_dir() and not (
            media_root / "assets"
        ).exists()
    return {
        "binary": binary.name,
        "layout": (
            "onedir/lib"
            if onedir
            else "onefile/bundled-media"
            if media_root is None
            else "onefile/external-media"
        ),
        "pyav": manifest["pyav"],
        "ffmpeg": manifest["ffmpeg"],
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "checks": checks,
        "ok": all(checks.values()),
    }


class BundleImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Читать Python-обёртки Qt/PyAV из проверяемого executable."""

    def __init__(self, archive, root: Path) -> None:
        self.archive, self.root = archive, root

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.archive.toc and any(
            fullname == name or fullname.startswith(name + ".")
            for name in ("av", "PySide6", "shiboken6")
        ):
            package = self.archive.toc[fullname][0] in (1, 3)
            return importlib.util.spec_from_loader(fullname, self, is_package=package)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        path = self.root.joinpath(*module.__name__.split("."))
        if module.__spec__.submodule_search_locations is not None:
            module.__path__ = [str(path)]
            path = path / "__init__.py"
        else:
            path = path.with_suffix(".py")
        module.__file__ = str(path)
        exec(  # noqa: S102 — код проверяемого сборочного артефакта
            self.archive.extract(module.__name__), module.__dict__
        )


def smoke(
    binary: Path, report: Path, workspace: Path, platform: str = "offscreen"
) -> None:
    """Проверить API извлечённых библиотек отдельным сборочным процессом."""
    from PyInstaller.archive.readers import CArchiveReader

    binary = binary.resolve()
    reader = CArchiveReader(str(binary))
    folder = workspace
    temporary = Path(folder)
    root = binary.parent / "lib"
    if os.name == "nt":
        root = temporary / "runtime"
        root.mkdir()
        for name, entry in reader.toc.items():
            if entry[-1] not in ("b", "x"):
                continue
            target = root / name.replace("\\", "/")
            if not target.resolve().is_relative_to(root):
                raise ValueError("Недопустимый путь в архиве приложения.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(reader.extract(name))
    media = (
        root
        if os.name == "nt" and (root / "media-runtime.json").is_file()
        else binary.parent / "assets/media"
        if os.name == "nt"
        else root
    )
    manifest = json.loads((media / "media-runtime.json").read_text(encoding="utf-8"))
    handles = []
    if os.name == "nt":
        for directory in (media, root, root / "PySide6"):
            handles.append(os.add_dll_directory(str(directory)))
    else:
        for name in (
            "avutil",
            "swresample",
            "swscale",
            "avcodec",
            "avformat",
            "avfilter",
            "avdevice",
        ):
            filename = next(
                item
                for item in manifest["libraries"]
                if item.startswith("lib" + name + ".so.")
            )
            handles.append(ctypes.CDLL(str(media / filename), mode=ctypes.RTLD_GLOBAL))
    pyz_name = next(name for name, entry in reader.toc.items() if entry[-1] == "z")
    sys.meta_path.insert(
        0, BundleImporter(reader.open_embedded_archive(pyz_name), root)
    )
    sys.path.insert(0, str(root))
    os.environ["QT_QPA_PLATFORM"] = platform
    os.environ["QT_PLUGIN_PATH"] = str(
        next(
            path
            for path in (root / "PySide6/Qt/plugins", root / "PySide6/plugins")
            if path.is_dir()
        )
    )
    import av
    from av.filter import Graph
    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
    from PySide6.QtWidgets import QApplication

    assert Path(av._core.__file__).resolve().is_relative_to(root)
    assert av.__version__ == manifest["pyav"]
    assert av.ffmpeg_version_info == manifest["ffmpeg"]
    font = media / "media-fonts/DejaVuSans.ttf"
    if not font.is_file():
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/arial.ttf"
    graph = Graph()
    source = graph.add("testsrc2", "s=160x96:r=25:d=1")
    text = graph.add(
        "drawtext", text="Проверка PyAV", fontfile=str(font), fontsize="16"
    )
    sink = graph.add("buffersink")
    source.link_to(text)
    text.link_to(sink)
    graph.configure()
    video_path = temporary / "probe.mkv"
    with av.open(str(video_path), "w") as output:
        video = output.add_stream("libx264", rate=25)
        video.width, video.height, video.pix_fmt = 160, 96, "yuv420p"
        audio = output.add_stream("aac", rate=48000)
        audio.layout = "stereo"
        for index in range(25):
            frame = sink.pull()
            for packet in video.encode(frame):
                output.mux(packet)
        for index in range(47):
            frame = av.AudioFrame(format="fltp", layout="stereo", samples=1024)
            frame.sample_rate, frame.time_base, frame.pts = (
                48000,
                Fraction(1, 48000),
                index * 1024,
            )
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in audio.encode(frame):
                output.mux(packet)
        for stream in (video, audio):
            for packet in stream.encode(None):
                output.mux(packet)
    counts = {"video": 0, "audio": 0}
    with av.open(str(video_path)) as container:
        for packet in container.demux():
            counts[packet.stream.type] += sum(1 for _ in packet.decode())
    assert counts["video"] == 25 and counts["audio"] > 0
    app = QApplication.instance() or QApplication([])
    qml_checked = bool(list((root / "PySide6").glob("QtQuickWidgets.*")))
    if not qml_checked:
        raise RuntimeError("В сборке отсутствует обязательный PySide6.QtQuickWidgets.")
    from PySide6.QtQml import QQmlComponent
    from PySide6.QtQuickWidgets import QQuickWidget

    qml_path = next(
        path for path in (root / "PySide6/Qt/qml", root / "PySide6/qml")
        if path.is_dir()
    )
    quick = QQuickWidget()
    quick.engine().setImportPathList([str(qml_path), "qrc:/qt-project.org/imports"])
    component = QQmlComponent(quick.engine(), quick)
    component.setData(
        b"import QtQuick\nimport QtMultimedia\nRectangle { VideoOutput {} }",
        QUrl(),
    )
    qml_object = component.create()
    if qml_object is None:
        raise RuntimeError("Ошибка QML камер: " + "; ".join(
            error.toString() for error in component.errors()
        ))
    quick.setContent(QUrl(), component, qml_object)
    player, display = QMediaPlayer(), QVideoSink()
    frames = []
    display.videoFrameChanged.connect(lambda frame: frames.append(frame.isValid()))
    player.setVideoSink(display)
    player.setSource(QUrl.fromLocalFile(str(video_path)))
    player.play()
    deadline = time.monotonic() + 10
    while not any(frames) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    player.stop()
    player.setSource(QUrl())
    assert any(frames), player.errorString()
    import psutil

    loaded = [
        item.path
        for item in psutil.Process().memory_maps()
        if Path(item.path).name in manifest["libraries"]
    ]
    assert len(loaded) == 7 and all(
        Path(path).resolve().parent == media.resolve() for path in loaded
    )
    xkb_loaded = []
    if platform == "xcb":
        xkb_expected = manifest["xkb"]["libraries"]
        xkb_loaded = [
            item.path
            for item in psutil.Process().memory_maps()
            if Path(item.path).name in xkb_expected
        ]
        assert len(xkb_loaded) == 2 and all(
            Path(path).resolve().parent == media.resolve() for path in xkb_loaded
        )
    report.write_text(
        json.dumps(
            {
                "ok": True,
                "pyav": av.__version__,
                "ffmpeg": av.ffmpeg_version_info,
                "decoded": counts,
                "qt_frames": sum(frames),
                "camera_qml_checked": qml_checked,
                "loaded_media_libraries": loaded,
                "qt_platform": app.platformName(),
                "loaded_xkb_libraries": xkb_loaded,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def run_smoke(binary: Path, report: Path, platform: str = "offscreen") -> dict:
    environment = dict(os.environ)
    if os.name != "nt":
        environment["LD_LIBRARY_PATH"] = (
            str(binary.resolve().parent / "lib")
            + os.pathsep
            + environment.get("LD_LIBRARY_PATH", "")
        )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryDirectory(prefix="dno-media-api-") as folder:
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "smoke",
                str(binary.resolve()),
                "--report",
                str(report.resolve()),
                "--platform",
                platform,
                "--workspace",
                folder,
            ],
            env=environment,
            creationflags=flags,
            check=True,
            timeout=180,
        )
    return json.loads(report.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "smoke", "zip"))
    parser.add_argument("binary", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--platform", choices=("offscreen", "xcb"), default="offscreen")
    args = parser.parse_args()
    if args.command == "smoke":
        if args.workspace is None:
            run_smoke(args.binary, args.report, args.platform)
        else:
            smoke(args.binary, args.report, args.workspace, args.platform)
        return 0
    if args.command == "zip":
        with tempfile.TemporaryDirectory(prefix="dno-zip-") as folder:
            root = Path(folder)
            with zipfile.ZipFile(args.binary) as archive:
                if any(
                    not (root / name).resolve().is_relative_to(root)
                    for name in archive.namelist()
                ):
                    raise ValueError("Недопустимый путь в ZIP.")
                archive.extractall(root)
            report = audit(root / "dno-tool.exe")
            report["runtime"] = run_smoke(root / "dno-tool.exe", root / "runtime.json")
        with args.binary.open("rb") as file:
            report["zip_sha256"] = hashlib.file_digest(file, "sha256").hexdigest()
    else:
        report = audit(args.binary)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Библиотеки из пакета: {report['ok']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
