"""Проверить состав RPM, требования glibc и совпадение упакованного бинарника."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def query(rpm: Path, *options: str) -> str:
    """Получить метаданные без установки пакета и запуска его скриптов."""
    return subprocess.check_output(
        ["rpm", "-qp", *options, str(rpm.resolve())], text=True
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rpm", type=Path)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--glibc", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    files = query(args.rpm, "--list").splitlines()
    dependencies = query(args.rpm, "--requires").splitlines()
    provides = query(args.rpm, "--provides").splitlines()
    digests = dict(
        line.split("\t", 1)
        for line in query(
            args.rpm, "--queryformat", "[%{FILENAMES}\t%{FILEDIGESTS}\n]"
        ).splitlines()
    )
    required = {
        "/opt/dno-tool/dno-tool",
        "/opt/dno-tool/assets/icons/themes.json",
        "/opt/dno-tool/assets/custom/mono.zip",
        "/opt/dno-tool/assets/custom/color.zip",
        "/usr/bin/dno-tool",
        "/usr/bin/dno-tool-admin",
        "/usr/share/applications/com.dno-tool.desktop",
        "/usr/share/applications/com.dno-tool-admin.desktop",
        "/usr/share/polkit-1/actions/com.dno-tool.policy",
    }
    required.update(
        f"/opt/dno-tool/assets/icons/{name}.zip"
        for name in (
            "badge",
            "classic",
            "flat",
            "line",
            "midnight",
            "neon",
            "paper",
            "solid",
        )
    )
    with args.binary.open("rb") as stream:
        binary_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    checks = {
        "required_files": required <= set(files),
        "no_commands_json": not any(
            Path(name).name == "commands.json" for name in files
        ),
        "glibc_dependency": f"glibc >= {args.glibc}" in dependencies,
        "polkit": "polkit" in dependencies,
        "ssh_client": "openssh-clients" in dependencies,
        "binary_matches": digests.get("/opt/dno-tool/dno-tool") == binary_digest,
        "no_build_id_links": not any("/.build-id" in name for name in files),
        "no_system_lib_directory": not any(
            name in ("/usr/lib", "/usr/lib64")
            or name.startswith(("/usr/lib/", "/usr/lib64/"))
            for name in files
        ),
        "no_private_library_provides": not any(".so" in item for item in provides),
        "no_private_library_requires": not any(
            item.startswith(
                (
                    "libQt6",
                    "libavcodec",
                    "libavfilter",
                    "libavformat",
                    "libpython",
                    "libstdc++",
                )
            )
            for item in dependencies
        ),
    }
    media_root = args.binary.parent / "lib"
    manifest = json.loads(
        (media_root / "media-runtime.json").read_text(encoding="utf-8")
    )
    for name, expected in {
        **manifest["libraries"],
        **manifest.get("assets", {}),
    }.items():
        checks["media/" + name] = digests.get("/opt/dno-tool/lib/" + name) == expected
    checks["themes_external"] = any(
        name.startswith("/opt/dno-tool/assets/themes/") for name in files
    )
    # Пакет kmsvnc, который мастер настройки ставит на хосты.
    checks["kmsvnc_package"] = any(
        name.startswith("/opt/dno-tool/assets/remote/kmsvnc-") and name.endswith(".rpm")
        for name in files
    )
    checks["no_assets_in_lib"] = not any(
        name.startswith("/opt/dno-tool/lib/assets/") for name in files
    )
    # Проверка и запуск идут на файлах, извлечённых непосредственно из RPM.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "media"))
    from verify import audit, run_smoke

    with tempfile.TemporaryDirectory(prefix="dno-rpm-media-") as folder:
        root = Path(folder)
        with tempfile.TemporaryFile() as archive:
            subprocess.run(
                ["rpm2cpio", str(args.rpm.resolve())], stdout=archive, check=True
            )
            archive.seek(0)
            subprocess.run(
                ["cpio", "-idm", "--quiet", "--no-absolute-filenames"],
                stdin=archive,
                cwd=root,
                check=True,
            )
        application = root / "opt/dno-tool/dno-tool"
        checks["extracted_media_audit"] = bool(audit(application)["ok"])
        runtime_report = root / "runtime-report.json"
        checks["extracted_api"] = run_smoke(application, runtime_report)["ok"]

    with args.rpm.open("rb") as stream:
        rpm_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {
        "rpm": args.rpm.name,
        "rpm_sha256": rpm_digest,
        "binary_sha256": binary_digest,
        "files": files,
        "dependencies": dependencies,
        "provides": provides,
        "checks": checks,
        "ok": all(checks.values()),
    }
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Проверки RPM:", checks)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
