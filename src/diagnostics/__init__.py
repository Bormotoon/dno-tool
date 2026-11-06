"""Диагностика процесса: автосбор стеков и дампов при падении."""

from .crash_report import install_crash_report, invoke_veh_for_test, status

__all__ = ["install_crash_report", "invoke_veh_for_test", "status"]
