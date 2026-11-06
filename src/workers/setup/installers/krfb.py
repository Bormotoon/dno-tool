"""Independent installer using the operation context supplied by the worker."""

from typing import List
from src.workers.setup.types import (
    RESULT_SUCCESS,
    RESULT_ERROR,
    RESULT_ABORT,
    RESULT_WAITING,
)
from src.workers.setup.templates import (
    _as_user_func,
    _as_user_call,
    _KRFB_CONFIG_SCRIPT,
)
from src.workers.setup.templates import (
    _KRFB_UNIT_TEMPLATE,
    _KRFB_WAYLAND_UNIT_TEMPLATE,
    _KRFB_HIDE_TRAY_SCRIPT,
)


from src.workers.setup.installers.vnc_common import VncOperations


class KrfbBackend(VncOperations):
    def configure(self, device, ssh, run_cmd, write_file) -> None:
        """Установить и настроить krfb (KDE Remote Frame Buffer) на хосте.

        Шаги (протокол подтверждён на MOS Desktop 12, krfb 23.08):
          0. Чужой бекенд (x11vnc) - стоп + disable. Штатный user unit
             krfb.service (запуск БЕЗ --nodialog) - stop + disable глобально
             и per-user: иначе второй процесс через D-Bus Unique будит
             первый и тот показывает окно "Совместный доступ".
          1. Проверка/установка пакета krfb (dnf/apt-get).
          2. Конфигурация kwriteconfig5 для ВСЕХ пользователей системно:
             [TCP] port + useDefaultPort=false,
             [Security] noWallet=true, allowUnattendedAccess=true,
             allowDesktopControl=true, desktopPassword + unattendedPassword
             (obscure: chr(0x1001F - ord(c)); оба ключа синхронно),
             [MainWindow] startMinimized=true (--nodialog НЕ подавляет
             восстановление окна из сохранённой KDE-сессии; без этого флага
             окно появляется у части пользователей). Активный X-пользователь
             настраивается строго (ошибка - RESULT_ERROR); остальные
             /home/* + /etc/skel (будущие пользователи) - best-effort.
          3. systemd user unit krfb-dno.service: ВСЕГДА перезаписывается
              актуальным шаблоном (идемпотентно, чистит юниты с мусорной
              первой строкой от старых версий), затем daemon-reload +
              enable --now. Активация - во ВСЕХ активных графических сессиях,
              не только у текущего X-пользователя.
          4. Firewall: открытие порта.
          5. Проверка: TCP-порт слушается.

        Результаты эмитятся под именем сервиса "vnc" - колонки таблицы
        мастера и флаг vnc_server согласованы (одна колонка VNC).

        Требует графическую сессию пользователя (X11 или Wayland KDE); при её отсутствии krfb не запустится
        (systemd user unit Restart=always перезапустит после входа).
        """
        svc = "vnc"
        output_lines: List[str] = []
        wayland_session = dict(self.context.options).get("wayland_session", False)
        unit_name = "krfb-dno"
        unit_path = f"/etc/systemd/user/{unit_name}.service"
        unit_template = (
            _KRFB_WAYLAND_UNIT_TEMPLATE if wayland_session else _KRFB_UNIT_TEMPLATE
        )

        def log(msg: str):
            output_lines.append(msg)

        self.context.log(device.host, svc, "Начало настройки общего экрана (krfb)")
        if wayland_session:
            log("Обнаружен Wayland: KRFB запустится через временный app-org.kde.krfb unit, чтобы KDE применил сохранённое разрешение портала")

        # 0. Другой бекенд (x11vnc) - стоп + disable. Штатный user unit
        # krfb.service (дистрибутивный, Exec БЕЗ --nodialog) - конкурент
        # нашему krfb-dno: второй процесс через D-Bus Unique будит первый
        # и тот показывает окно. Глушим его глобально и per-user.

        # 1. Пакет krfb
        if self._local_abort.is_set():
            self._report(device, svc, RESULT_ABORT, "Прервано")
            return
        code, out, err = run_cmd("command -v krfb")
        if code == 0 and out:
            log("krfb уже установлен")
        else:
            log("Устанавливаю krfb...")
            from src.workers.setup.packages import PackageManager

            code, out, err = PackageManager(run_cmd, self._local_abort).install(
                ("krfb",)
            )
            if code != 0:
                msg = f"Не удалось установить krfb: {err or out}"
                log(msg)
                self.context.log(device.host, svc, "krfb: пакет не установлен")
                self._report(device, svc, RESULT_ERROR, "\n".join(output_lines))
                return
            log("Пакет krfb установлен")

        # Verify: krfb бинарник и kwriteconfig5 (зависимость kf5-kconfig)
        code, out, err = run_cmd("command -v kwriteconfig5")
        if code != 0:
            log(
                "WARNING: нет утилиты kwriteconfig5 (пакет kf5-kconfig) - "
                "автоматическая настройка невозможна"
            )
            self.context.log(device.host, svc, "krfb: нет утилиты настройки")
            self._report(device, svc, RESULT_ERROR, "\n".join(output_lines))
            return

        # 2. Конфигурация krfbrc
        if self._local_abort.is_set():
            self._report(device, svc, RESULT_ABORT, "Прервано")
            return
        port = getattr(device, "vnc_port", None) or self.parameters.vnc_port or 5900

        # Определяем X-пользователя (владельца X-сессии) на хосте.
        code, xu, err = run_cmd(
            "for sid in $(loginctl list-sessions --no-legend | awk '{print $1}'); do "
            'kind=$(loginctl show-session "$sid" -p Type --value); '
            'case "$kind" in x11|wayland) ;; *) continue;; esac; '
            '[ "$(loginctl show-session "$sid" -p State --value)" = active ] || continue; '
            'loginctl show-session "$sid" -p Name --value; break; done'
        )
        xu = (xu or "").strip()

        # Скрипт конфигурации (выполняется от пользователя через dnoasuser
        # (setpriv без PAM, fallback runuser) либо с HOME=/etc/skel от root
        # для будущих пользователей). Пароль obscure: chr(0x1001F - ord(c)) -
        # вычисляем на стороне хоста python3 (есть на всех целевых системах).
        # Сервис системный: настраиваем ВСЕХ пользователей, а не только
        # активного (иначе у части пользователей остаётся дефолтный krfbrc
        # без startMinimized и окно появляется).
        cfg_script = _KRFB_CONFIG_SCRIPT.format(
            port=int(port),
            password_literal=repr(self.parameters.vnc_password),
        )

        # Скрипты с паролем идут по SSH stdin. В argv дочернего bash
        # остаются только -s; quoted heredoc исключает shell-подстановки.
        def config_call(prefix):
            return (
                prefix
                + " bash -s <<'DNO_KRFB_CONFIG'\n"
                + cfg_script
                + "\nDNO_KRFB_CONFIG\n"
            )

        xu_q = (xu or "").replace("'", "'\\''")

        if self._local_abort.is_set():
            self._report(device, svc, RESULT_ABORT, "Прервано")
            return
        log("Настраиваю параметры krfb (порт/пароль/автодоступ)...")
        if xu:
            # 1. Активный X-пользователь - СТРОГО (ошибка валит шаг).
            xu_cmd = config_call(_as_user_call(xu))
            code, out, err = run_cmd(
                "bash -s", timeout=60, stdin_data=xu_cmd, sensitive=True
            )
            clean_out = "\n".join(
                ln
                for ln in (out or "").splitlines()
                if ln.strip() and ln.strip() not in ("CONFIG_OK", "password-set")
            )
            if clean_out:
                log(clean_out)
            if code != 0 or "CONFIG_OK" not in (out or ""):
                msg = f"Ошибка настройки krfb: {err or out}"
                log(msg)
                self.context.log(device.host, svc, "krfb: ошибка настройки параметров")
                self._report(device, svc, RESULT_ERROR, "\n".join(output_lines))
                return
            # 2. Остальные /home/* (кроме xu) + /etc/skel - best-effort.
            others_cmd = (
                _as_user_func() + "; "
                "for h in /home/*/; do u=$(basename $h); "
                f"[ \"$u\" = '{xu_q}' ] && continue; "
                + config_call('dnoasuser "$u"')
                + '[ $? -eq 0 ] || echo "SKIP:$u"; done; '
                + config_call("HOME=/etc/skel")
                + "[ $? -eq 0 ] || echo SKIP:skel; true"
            )
            _, out2, _ = run_cmd(
                "bash -s", timeout=120, stdin_data=others_cmd, sensitive=True
            )
            skipped = sorted(
                {
                    ln.split(":", 1)[1].strip()
                    for ln in (out2 or "").splitlines()
                    if ln.strip().startswith("SKIP:") and ln.split(":", 1)[1].strip()
                }
            )
            if skipped:
                log(f"Пропущены пользователи (без настройки): {', '.join(skipped)}")
        else:
            # Без X-сессии: все /home/* + /etc/skel. Успех - хотя бы один
            # реальный пользователь (skel не в счёт - будущих ещё нет).
            log(
                "Активный сеанс пользователя не найден - параметры записываю "
                "для всех пользователей компьютера (применятся при первом входе)"
            )
            full_cmd = (
                _as_user_func() + "; OK=0; "
                "for h in /home/*/; do u=$(basename $h); "
                "if " + config_call('dnoasuser "$u"') + "then OK=$((OK+1)); "
                'else echo "SKIP:$u"; fi; done; '
                + config_call("HOME=/etc/skel")
                + "[ $? -eq 0 ] || echo SKIP:skel; "
                "[ $OK -gt 0 ]"
            )
            code, out, err = run_cmd(
                "bash -s", timeout=120, stdin_data=full_cmd, sensitive=True
            )
            clean_out = "\n".join(
                ln
                for ln in (out or "").splitlines()
                if ln.strip()
                and ln.strip() not in ("CONFIG_OK", "password-set")
                and not ln.strip().startswith("SKIP:")
            )
            if clean_out:
                log(clean_out)
            skipped = sorted(
                {
                    ln.split(":", 1)[1].strip()
                    for ln in (out or "").splitlines()
                    if ln.strip().startswith("SKIP:") and ln.split(":", 1)[1].strip()
                }
            )
            if skipped:
                log(f"Пропущены пользователи (без настройки): {', '.join(skipped)}")
            if code != 0 or "CONFIG_OK" not in (out or ""):
                msg = f"Ошибка настройки krfb: {err or out}"
                log(msg)
                self.context.log(device.host, svc, "krfb: ошибка настройки параметров")
                self._report(device, svc, RESULT_ERROR, "\n".join(output_lines))
                return
        log(
            "Параметры сохранены (порт %d, пароль %s)"
            % (port, "установлен" if self.parameters.vnc_password else "не задан")
        )

        # 3. systemd user unit (для всех X-пользователей). Юнит ВСЕГДА
        # перезаписываем текущим шаблоном (идемпотентно: файл наш
        # собственный): чистит мусорные первые строки от старых версий
        # (пароль sudo в stdin tee) и применяет актуальный шаблон. Затем
        # daemon-reload + активация (krfb перечитывает пароль только
        # при старте рестартом юнита).
        if self._local_abort.is_set():
            self._report(device, svc, RESULT_ABORT, "Прервано")
            return
        log("Записываю службу автозапуска krfb...")
        # 0644: юнит читает пользовательский менеджер systemd, не root.
        code, err2 = write_file(unit_path, unit_template, mode=0o644)
        if code != 0:
            self.context.log(device.host, svc, "krfb: ошибка записи службы")
            self._report(
                device,
                svc,
                RESULT_ERROR,
                f"Ошибка записи службы krfb: {err2}\n" + "\n".join(output_lines),
            )
            return
        # Verify: первая строка [Unit]; при расхождении - дамп содержимого.
        code, out, _ = run_cmd(f"head -n1 {unit_path}")
        if (out or "").strip() != "[Unit]":
            log("Файл службы krfb повреждён (записался некорректно)")
            _, dump, _ = run_cmd(
                f"head -c 200 {unit_path} 2>/dev/null"
            )
            if dump:
                log("--- содержимое файла службы ---")
                log(dump[:500])
            self.context.log(device.host, svc, "krfb: файл службы поврежден")
            self._report(device, svc, RESULT_ERROR, "\n".join(output_lines))
            return

        # daemon-reload + активация. Перезапись юнита требует reload.
        # Активация системная (все пользователи, не только активный xu):
        #   - системный уровень: systemctl --global enable krfb-dno (без PAM,
        #     покрывает всех текущих и будущих пользователей) + --global
        #     disable штатного krfb.service (конкурент без --nodialog);
        #   - pkill -x krfb ДО активации: внешний/старый krfb (со старым
        #     паролем) иначе выживет - после pkill его поднимет юнит с
        #     Restart=always, уже с новым конфигом;
        #   - per-user цикл по ВСЕМ активным графическим сессиям
        #     (dnoasuser = setpriv/runuser): daemon-reload + enable krfb-dno
        #     + stop/disable штатного krfb.service + restart krfb-dno
        #     (пароль/порт/скрытие окна применяются сразу).
        self._shutdown_legacy_x11vnc(run_cmd, log)
        self._shutdown_kmsvnc(run_cmd, log)
        self._disable_stock_krfb_unit(run_cmd, log)
        run_cmd(
            "systemctl --global disable krfb-dno.service app-org.kde.krfb.service 2>/dev/null; "
            "rm -f /etc/systemd/user/*target.wants/krfb-dno.service "
            "/etc/systemd/user/*target.wants/app-org.kde.krfb.service "
            "/home/*/.config/systemd/user/*target.wants/krfb-dno.service "
            "/home/*/.config/systemd/user/*target.wants/app-org.kde.krfb.service; true",
            timeout=30,
        )
        stop_cmd = (
            _as_user_func()
            + "; for h in /home/*/; do u=$(basename \"$h\"); "
            'dnoasuser "$u" bash -c '
            "'systemctl --user stop krfb-dno.service app-org.kde.krfb.service 2>/dev/null; "
            "systemctl --user disable krfb-dno.service app-org.kde.krfb.service 2>/dev/null' "
            "2>/dev/null; done; true"
        )
        run_cmd(stop_cmd, timeout=60)
        run_cmd("pkill -x krfb 2>/dev/null; true", timeout=15)
        run_cmd(
            "systemctl --global disable krfb-dno.service app-org.kde.krfb.service 2>/dev/null; "
            "rm -f /etc/systemd/user/*target.wants/krfb-dno.service "
            "/etc/systemd/user/*target.wants/app-org.kde.krfb.service "
            "/home/*/.config/systemd/user/*target.wants/krfb-dno.service "
            "/home/*/.config/systemd/user/*target.wants/app-org.kde.krfb.service; "
            f"systemctl --global enable {unit_name}.service 2>/dev/null; "
            "systemctl --global disable krfb.service 2>/dev/null; true",
            timeout=30,
        )
        code, _, _ = run_cmd(f"systemctl --global is-enabled {unit_name}.service")
        if code:
            self._report(device, svc, RESULT_ERROR, "Автозапуск KRFB не подтверждён")
            return
        apply_cmd = (
            _as_user_func() + "; "
            "SESSIONS=$(for sid in $(loginctl list-sessions --no-legend | awk '{print $1}'); do "
            'kind=$(loginctl show-session "$sid" -p Type --value); '
            'case "$kind" in x11|wayland) ;; *) continue;; esac; '
            '[ "$(loginctl show-session "$sid" -p State --value)" = active ] || continue; '
            'name=$(loginctl show-session "$sid" -p Name --value); '
            'printf "%s:%s\\n" "$kind" "$name"; done | sort -u); OK=1; '
            'for entry in $SESSIONS; do kind=${entry%%:*}; u=${entry#*:}; '
            'if [ "$kind" = wayland ]; then '
            'dnoasuser "$u" bash -c '
            "'if command -v flatpak >/dev/null 2>&1; then "
            "flatpak permission-set kde-authorized remote-desktop org.kde.krfb yes && "
            "true; "
            "else dbus-send --session --print-reply --dest=org.freedesktop.impl.portal.PermissionStore "
            "/org/freedesktop/impl/portal/PermissionStore "
            "org.freedesktop.impl.portal.PermissionStore.SetPermission string:kde-authorized "
            "boolean:false string:remote-desktop string:org.kde.krfb array:string:yes && "
            "true; fi' "
            '|| { OK=0; continue; }; fi; '
            'dnoasuser "$u" bash -c '
            "'systemctl --user stop krfb-dno.service app-org.kde.krfb.service 2>/dev/null; "
            "systemctl --user disable krfb-dno.service app-org.kde.krfb.service 2>/dev/null; "
            "systemctl --user daemon-reload && "
            f"systemctl --user enable {unit_name}.service && "
            f"systemctl --user restart {unit_name}.service && "
            f"systemctl --user is-active --quiet {unit_name}.service' "
            "|| OK=0; done; [ $OK -eq 1 ]"
        )
        code, out, err = run_cmd(apply_cmd, timeout=120)
        if code:
            detail = (err or out or "").strip()
            if detail:
                self.context.log(device.host, svc, f"KRFB: не удалось выдать разрешение портала или запустить пользовательский юнит: {detail}")
            self._report(
                device,
                svc,
                RESULT_ERROR,
                "Запуск KRFB в графической сессии не подтверждён"
                + (f": {detail}" if detail else ""),
            )
            return
        log("Служба krfb записана и добавлена в автозапуск")

        _, tray_sessions, _ = run_cmd(
            "for sid in $(loginctl list-sessions --no-legend | awk '{print $1}'); do "
            'kind=$(loginctl show-session "$sid" -p Type --value); '
            'case "$kind" in x11|wayland) ;; *) continue;; esac; '
            '[ "$(loginctl show-session "$sid" -p State --value)" = active ] || continue; '
            'loginctl show-session "$sid" -p Name --value; done | sort -u',
            timeout=15,
        )
        for tray_user in tray_sessions.splitlines():
            tray_user = tray_user.strip()
            if not tray_user:
                continue
            tray_call = (
                _as_user_call(tray_user)
                + " bash -s <<'DNO_HIDE_TRAY'\n"
                + _KRFB_HIDE_TRAY_SCRIPT
                + "\nDNO_HIDE_TRAY\n"
            )
            tray_code, tray_out, tray_err = run_cmd(
                "bash -s", timeout=30, stdin_data=tray_call
            )
            for tray_line in (tray_out or "").splitlines():
                if tray_line.startswith("TRAY_APPLIED:"):
                    log(
                        "\u0421\u043a\u0440\u044b\u0442 \u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0439 \u0438\u043d\u0434\u0438\u043a\u0430\u0442\u043e\u0440 \u043f\u043e\u0440\u0442\u0430\u043b\u0430: "
                        f"{tray_line.split(':', 1)[1]}"
                    )
                elif tray_line.startswith("TRAY_CONFIGURED:"):
                    log(
                        "\u0418\u043d\u0434\u0438\u043a\u0430\u0442\u043e\u0440\u044b \u043f\u043e\u0440\u0442\u0430\u043b\u0430 \u0443\u0434\u0430\u043b\u0451\u043d\u043d\u043e\u0433\u043e \u0443\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d\u044b \u043a\u0430\u043a \u0441\u043a\u0440\u044b\u0442\u044b\u0435"
                    )
                elif tray_line == "TRAY_LIVE_APPLY_UNAVAILABLE":
                    log("WARNING: Plasma \u043d\u0435 \u043f\u0440\u0435\u0434\u043e\u0441\u0442\u0430\u0432\u0438\u043b\u0430 \u0438\u043d\u0442\u0435\u0440\u0444\u0435\u0439\u0441 \u043f\u0440\u0438\u043c\u0435\u043d\u0435\u043d\u0438\u044f \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 \u0437\u043d\u0430\u0447\u043a\u043e\u0432")
                elif tray_line == "TRAY_LIVE_APPLY_FAILED":
                    log("WARNING: Plasma \u043d\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u043b\u0430 \u043f\u0440\u0438\u043c\u0435\u043d\u0435\u043d\u0438\u0435 \u0441\u043a\u0440\u044b\u0442\u0438\u044f \u0437\u043d\u0430\u0447\u043a\u043e\u0432")
                elif tray_line.startswith("TRAY_"):
                    log(f"\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430 \u0437\u043d\u0430\u0447\u043a\u043e\u0432 \u0432 \u043b\u043e\u0442\u043a\u0435: {tray_line}")
            if tray_code:
                detail = (tray_err or tray_out or "").strip()
                log(
                    f"Не удалось настроить скрытие значков в лотке пользователя "
                    f"{tray_user}" + (f": {detail}" if detail else ".")
                )

        # 4. Firewall
        self._open_firewall_v4(run_cmd, port, log)

        self._report(device, svc, RESULT_SUCCESS, "\n".join(output_lines))

    def check(self, device, run_cmd):
        svc = "vnc"
        output_lines = []
        log = output_lines.append
        port = device.vnc_port or self.parameters.vnc_port
        # 5. Проверка
        if self._local_abort.is_set():
            self._report(device, svc, RESULT_ABORT, "Прервано")
            return

        self._local_abort.wait(2)
        # krfb слушает только при активной X-сессии; без неё порт закрыт,
        # но unit включён - применится после входа пользователя.
        code, out, err = run_cmd(
            f"ss -tlnp 2>/dev/null | grep ':{port} ' | grep -w krfb"
        )
        if code == 0 and (out or "").strip():
            log(f"Порт {port} слушается - общий экран krfb готов")
            self.context.log(device.host, svc, "krfb установлен и готов к работе")
            self._report(device, svc, RESULT_SUCCESS, "\n".join(output_lines))
            return
        code, state, _ = run_cmd(
            "found=0; for sid in $(loginctl list-sessions --no-legend | awk '{print $1}'); do "
            'kind=$(loginctl show-session "$sid" -p Type --value); '
            'case "$kind" in x11|wayland) found=1;; esac; '
            "done; [ $found -eq 0 ] && echo NO_SESSION || echo HAS_SESSION"
        )
        if code == 0 and state.strip() == "NO_SESSION":
            self._report(
                device,
                svc,
                RESULT_WAITING,
                "KRFB настроен, ожидает графическую сессию пользователя",
            )
        else:
            self._report(
                device, svc, RESULT_ERROR, "KRFB не открыл порт при доступной сессии"
            )
