"""VNC lifecycle: snapshot, backend configuration, read-back, checked restore."""

from dataclasses import replace
from src.logger import logger
from src.workers.setup.phases import (
    PhasedInstaller,
    PhaseResult,
    RecoveryResult,
    RecoveryState,
)
from src.workers.setup.types import (
    RESULT_SUCCESS,
    RESULT_ERROR,
    RESULT_ABORT,
    RESULT_WAITING,
)
from src.workers.setup.templates import _as_user_func


class VncInstaller(PhasedInstaller):
    service = "vnc"

    @staticmethod
    def _backend_type(name):
        from src.workers.setup.installers.x11vnc import X11VncBackend
        from src.workers.setup.installers.krfb import KrfbBackend
        from src.workers.setup.installers.kmsvnc import KmsvncBackend

        return {
            "x11vnc": X11VncBackend,
            "krfb": KrfbBackend,
            "kmsvnc": KmsvncBackend,
        }[name]

    def _backend(self):
        backend_type = self._backend_type(self.parameters.vnc_server)
        return backend_type(replace(self.context, report=self._report))

    def prepare(self, request):
        result = super().prepare(request)
        if result.code != RESULT_SUCCESS:
            return result
        code, session_types, _ = request.run(
            "for sid in $(loginctl list-sessions --no-legend | awk '{print $1}'); do "
            'state=$(loginctl show-session "$sid" -p State --value); '
            '[ "$state" = active ] || continue; '
            'class=$(loginctl show-session "$sid" -p Class --value); '
            'case "$class" in user|user-early) ;; *) continue;; esac; '
            'loginctl show-session "$sid" -p Type --value; done',
            timeout=10,
        )
        detected = set(session_types.splitlines()) if code == 0 else set()
        session_label = "+".join(sorted(detected)) if detected else "не обнаружена"
        selected = self.parameters.vnc_server
        # Ставится ровно выбранный сервер, подмены нет: если он на хосте
        # работать не будет, мастер ничего не меняет и объясняет почему.
        self.context.log(
            request.device.host,
            "vnc",
            f"Графическая сессия: {session_label}; используется выбранный VNC backend {selected}.",
        )
        if "wayland" in detected and selected == "x11vnc":
            message = (
                "На хосте сеанс Wayland: x11vnc его не видит. Выберите kmsvnc или KRFB."
            )
            self.context.log(request.device.host, "vnc", f"x11vnc: хост не подходит. {message}")
            return PhaseResult(RESULT_ERROR, message)
        if "wayland" in detected and selected == "krfb":
            # Настройка KRFB под Wayland: доступ к экрану через портал KDE.
            self.context.log(
                request.device.host,
                "vnc",
                "KRFB запускается через временный app-org.kde.krfb unit для portal app_id org.kde.krfb.",
            )
            options = tuple(self.context.options) + (("wayland_session", True),)
            self.context = replace(self.context, options=options)
        from src.workers.setup.recovery.vnc import UnmanagedVncError, VncSwitch

        port = request.device.vnc_port or self.parameters.vnc_port or 5900
        self.switch = VncSwitch(request.run, _as_user_func(), port)
        self._port_notes = []
        try:
            self.switch.prepare()
        except UnmanagedVncError as error:
            self.context.log(request.device.host, "vnc", f"VNC: {error}")
            return PhaseResult(RESULT_ERROR, str(error))
        for entry in self.switch.foreign:
            self.context.log(
                request.device.host,
                "vnc",
                f"VNC: порт {port} занимает {self.switch.describe(entry)}, "
                "она будет остановлена",
            )
        self.backend = self._backend()
        return result

    def _apply_install(self, device, ssh, run, write, append):
        from src.workers.setup.recovery.vnc import PortReleaseError

        # Чужая служба на порту останавливается до установки: иначе новый
        # сервер не откроет порт. Откат вернёт её вместе с прежним сервером.
        try:
            self._port_notes = self.switch.release_foreign()
        except PortReleaseError as error:
            self.context.log(device.host, "vnc", f"VNC: {str(error).splitlines()[0]}")
            self._report(device, "vnc", RESULT_ERROR, str(error))
            return
        for note in self._port_notes:
            self.context.log(device.host, "vnc", f"VNC: {note}")
        self.backend.configure(device, ssh, run, write)

    def verify(self, request, applied):
        self._results = []
        try:
            self.backend.check(request.device, request.run)
            result = (
                PhaseResult(*self._results[-1])
                if self._results
                else PhaseResult(RESULT_ERROR, "Нет результата проверки VNC")
            )
        finally:
            self._results = None
        if result.code == RESULT_SUCCESS:
            from src.workers.setup.transport import probe_rfb_banner

            if not probe_rfb_banner(
                request.device.host,
                request.device.vnc_port or self.parameters.vnc_port,
                self._local_abort,
            ):
                return PhaseResult(
                    RESULT_ERROR, "VNC/RFB недоступен с машины оператора"
                )
            # Новый сервер работает: прежние больше не нужны для отката.
            removed = []
            self.backend._remove_other_backends(
                request.run, self.parameters.vnc_server, removed.append
            )
            if removed:
                result = PhaseResult(
                    result.code, result.message + "\n" + "\n".join(removed)
                )
        if result.code == RESULT_WAITING and self.switch.had_service:
            recovery = self.rollback(request)
            return PhaseResult(
                RESULT_ERROR
                if recovery.state == RecoveryState.FAILED
                else RESULT_WAITING,
                result.message + "\n" + recovery.message,
            )
        message = applied.message + "\n" + result.message
        if getattr(self, "_port_notes", None):
            message = "\n".join(self._port_notes) + "\n" + message
        return PhaseResult(result.code, message)

    def rollback(self, request):
        if hasattr(self, "_recovery_result"):
            return self._recovery_result
        try:
            restored = self.switch.rollback()
        except Exception:
            restored = False
        self._restore_failed = not restored
        if restored:
            self._recovery_result = RecoveryResult(
                RecoveryState.RESTORED,
                "Исходное состояние VNC восстановлено. Установленные пакеты не удалены.",
            )
        else:
            self._recovery_result = RecoveryResult(
                RecoveryState.FAILED,
                "Автовосстановление не удалось. Резервная копия: "
                + str(self.switch.path),
            )
        return self._recovery_result

    def install(self, device, ssh, run, write, append):
        self._restore_failed = False
        self.__dict__.pop("_recovery_result", None)
        self.__dict__.pop("switch", None)
        try:
            return super().install(device, ssh, run, write, append)
        finally:
            if hasattr(self, "switch") and not self._restore_failed:
                try:
                    self.switch.cleanup()
                except Exception:
                    logger.warning("VNC snapshot cleanup failed")

    def _install_vnc(self, device, ssh, run, write):
        self._legacy_backend(device, ssh, run, write, "x11vnc")

    def _install_krfb(self, device, ssh, run, write):
        self._legacy_backend(device, ssh, run, write, "krfb")

    def _legacy_backend(self, device, ssh, run, write, name):
        backend_type = self._backend_type(name)
        outcome = []
        backend = backend_type(
            replace(self.context, report=lambda d, s, c, m: outcome.append((c, m)))
        )
        backend.configure(device, ssh, run, write)
        if outcome and outcome[-1][0] == RESULT_SUCCESS:
            backend.check(device, run)
        self._report(
            device,
            "vnc",
            *(outcome[-1] if outcome else (RESULT_ERROR, "Нет результата VNC")),
        )

    def _install_vnc_safely(self, device, ssh, run_cmd, write_file, installer):
        from src.workers.setup.recovery.vnc import VncSwitch

        switch = VncSwitch(
            run_cmd, _as_user_func(), device.vnc_port or self.parameters.vnc_port
        )
        prepared = False
        result = (RESULT_ERROR, "Не удалось выполнить настройку VNC")
        self._results = []
        try:
            switch.prepare()
            prepared = True
            switch.release_foreign()
            installer(device, ssh, run_cmd, write_file)
            if self._results:
                result = self._results[-1]
            if result[0] == RESULT_SUCCESS:
                from src.workers.setup.transport import probe_rfb_banner

                port = device.vnc_port or self.parameters.vnc_port
                if not probe_rfb_banner(device.host, port, self._local_abort):
                    result = (RESULT_ERROR, "VNC/RFB недоступен с машины оператора")
        except Exception:
            result = (RESULT_ERROR, "Ошибка настройки VNC")
        finally:
            self._results = None
            if self._local_abort.is_set():
                result = (RESULT_ABORT, "Установка VNC прервана")
            if prepared and (
                result[0] not in (RESULT_SUCCESS, RESULT_WAITING)
                or (result[0] == RESULT_WAITING and switch.had_service)
            ):
                try:
                    restored = switch.rollback()
                except Exception:
                    restored = False
                if restored:
                    result = (
                        result[0],
                        result[1] + "\nИсходное состояние VNC восстановлено.",
                    )
                else:
                    result = (
                        RESULT_ERROR,
                        result[1]
                        + "\nАвтовосстановление не удалось. Резервная копия: "
                        + switch.path,
                    )
            else:
                restored = True
            if restored:
                try:
                    switch.cleanup()
                except Exception:
                    logger.warning("VNC snapshot cleanup failed")
            self._report(device, "vnc", *result)
