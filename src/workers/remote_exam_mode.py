#!/usr/bin/env python3
"""Управление режимом экзамена/контрольной работы на удалённом хосте (сеть + USB-накопители).
Все изменения живут только в /run и оперативной таблице ядра (сбрасываются при ребуте).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

STATE_DIR = Path("/run/dno-tool")
STATE_FILE = STATE_DIR / "exam_mode.json"
UDEV_RULE = Path("/run/udev/rules.d/99-dno-exam-block-usb.rules")
CHAIN_NAME = "DNO_EXAM_OUT"


def _run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=check)


def _operator_ip() -> str:
    ssh_client = os.environ.get("SSH_CLIENT", "").strip()
    if ssh_client:
        return ssh_client.split()[0]
    return ""


def enable_exam_mode(allowed_ips: list[str], block_usb: bool = True) -> str:
    if os.geteuid() != 0:
        raise RuntimeError("Для включения режима экзамена требуются права root")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    op_ip = _operator_ip()
    whitelist = sorted(set(filter(None, [op_ip, *allowed_ips])))

    # 1. Изоляция исходящего трафика учеников (UID 1000..65533) через отдельную цепочку iptables
    _run(["iptables", "-N", CHAIN_NAME])
    _run(["iptables", "-F", CHAIN_NAME])
    _run(["iptables", "-A", CHAIN_NAME, "-o", "lo", "-j", "ACCEPT"])
    _run(["iptables", "-A", CHAIN_NAME, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"])
    for ip in whitelist:
        _run(["iptables", "-A", CHAIN_NAME, "-d", ip, "-j", "ACCEPT"])
    # Блокируем только трафик пользовательских сессий (UID 1000..65533), не затрагивая системные демоны и root
    _run([
        "iptables", "-A", CHAIN_NAME,
        "-m", "owner", "--uid-owner", "1000-65533",
        "-j", "REJECT", "--reject-with", "icmp-port-unreachable",
    ])
    if _run(["iptables", "-C", "OUTPUT", "-j", CHAIN_NAME]).returncode != 0:
        _run(["iptables", "-I", "OUTPUT", "1", "-j", CHAIN_NAME], check=True)

    # 2. Временная блокировка новых USB-накопителей в /run/udev/rules.d
    if block_usb:
        UDEV_RULE.parent.mkdir(parents=True, exist_ok=True)
        UDEV_RULE.write_text(
            'ACTION=="add", SUBSYSTEMS=="usb", DRIVERS=="usb-storage", ATTR{authorized}="0"\n'
            'ACTION=="add", SUBSYSTEMS=="usb", DRIVERS=="uas", ATTR{authorized}="0"\n',
            encoding="utf-8",
        )
        _run(["udevadm", "control", "--reload"])

    STATE_FILE.write_text(
        json.dumps({"active": True, "whitelist": whitelist, "block_usb": block_usb}),
        encoding="utf-8",
    )
    return f"dno-exam: Режим экзамена включён (белый список: {', '.join(whitelist) or 'только lo'})"


def disable_exam_mode() -> str:
    if os.geteuid() != 0:
        raise RuntimeError("Для отключения режима экзамена требуются права root")

    while _run(["iptables", "-D", "OUTPUT", "-j", CHAIN_NAME]).returncode == 0:
        pass
    _run(["iptables", "-F", CHAIN_NAME])
    _run(["iptables", "-X", CHAIN_NAME])

    if UDEV_RULE.exists():
        UDEV_RULE.unlink(missing_ok=True)
        _run(["udevadm", "control", "--reload"])

    STATE_FILE.unlink(missing_ok=True)
    return "dno-exam: Режим экзамена отключён (доступ к сети и USB восстановлен)"


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    extra_ips = [ip.strip() for ip in sys.argv[2].split(",") if ip.strip()] if len(sys.argv) > 2 else []
    try:
        if action == "enable":
            print(enable_exam_mode(extra_ips, block_usb=True))
        elif action == "disable":
            print(disable_exam_mode())
        else:
            active = STATE_FILE.exists()
            print(f"dno-exam: {'active' if active else 'inactive'}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"dno-exam: ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
