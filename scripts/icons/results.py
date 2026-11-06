"""Бейджи результатов выполнения.

Дословно из gen_icon_themes.py (build_painters). Имя иконки
добавляется/правится только здесь.
"""

from ._base import (
    check,
    cross,
    exclam,
    fi,
    st,
    status_dot,
)

PAINTERS: dict = {}

PAINTERS["result_success"] = lambda m, c: status_dot(m, "#22C55E", check("@@sym@@", "@@"))
PAINTERS["result_failure"] = lambda m, c: status_dot(m, "#EF4444", cross("@@sym@@", "@@"))
PAINTERS["result_warning"] = lambda m, c: status_dot(m, "#F59E0B", exclam("@@sym@@", "@@"))
PAINTERS["result_pending"] = lambda m, c: status_dot(
    m, "#9CA3AF", f'<circle cx="64" cy="64" r="8" {fi("@@")}/>'
)
PAINTERS["result_executing"] = lambda m, c: status_dot(
    m,
    "#3B82F6",
    f'<path d="M42 64 A22 22 0 0 1 86 64" {st("@@", 9)}/>'
    f'<circle cx="86" cy="64" r="6" {fi("@@")}/>',
)
PAINTERS["result_cancelled"] = lambda m, c: status_dot(
    m,
    "#6B7280",
    f'<rect x="54" y="50" width="9" height="28" rx="4" {fi("@@")}/>'
    f'<rect x="67" y="50" width="9" height="28" rx="4" {fi("@@")}/>',
)
PAINTERS["result_skip"] = lambda m, c: status_dot(
    m,
    "#A855F7",
    f'<path d="M50 46 L72 64 L50 82 M70 46 L92 64 L70 82" {st("@@", 8)}/>',
)
PAINTERS["result_connection_lost"] = lambda m, c: status_dot(
    m,
    "#DC2626",
    f'<path d="M40 78 L58 46 L70 62 L80 50 L90 78 Z" {fi("@@")}/>'
    f'<path d="M38 88 L90 40" {st("@@", 7)}/>',
)

COLORS = {
    "result_success": "#22C55E",
    "result_failure": "#EF4444",
    "result_warning": "#F59E0B",
    "result_pending": "#9CA3AF",
    "result_executing": "#3B82F6",
    "result_cancelled": "#6B7280",
    "result_skip": "#A855F7",
    "result_connection_lost": "#DC2626",
}
