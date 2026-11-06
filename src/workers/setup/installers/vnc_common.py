"""Independent installer using the operation context supplied by the worker."""

from src.workers.setup.templates import (
    _as_user_func,
)
from src.workers.setup.context import InstallerContext


class VncOperations(InstallerContext):
    def _open_firewall_v4(self, run, port, log):
        from src.workers.setup.installers.veyon import VeyonInstaller

        return VeyonInstaller(self.context)._open_firewall_v4(run, port, log)

    def _shutdown_legacy_krfb(self, run_cmd, log) -> None:
        """Остановить krfb (старый бекенд) перед установкой x11vnc.

        Только стоп + disable (юниты/конфиги не удаляются - можно вернуть
        вручную). Порядок:
          1. Root: снять автозапуск юнита krfb-dno ГЛОБАЛЬНО. Enable делался
             systemctl --global (symlink в /etc/systemd/user/*target.wants/),
             поэтому systemctl --user disable от пользователя его НЕ удаляет:
             гасим симметрично от root (--global disable + удаление
             wants-symlinks в /etc/systemd/user и /home/*).
          2. Best-effort: stop + disable + daemon-reload юнита в
             user-менеджерах ВСЕХ учётных записей /home/* (systemctl --user
             от root напрямую невозможен; цикл, а не только X-пользователь:
             под юнитом может быть больше одного экземпляра krfb).
        Затем pkill -x krfb (может быть запущен вне юнита). Если krfb на
        хосте нет вовсе (нет бинарника, юнита и процесса) - не делаем ничего.
        """
        # Есть ли krfb вообще: бинарник / user unit / процесс.
        code, out, _ = run_cmd(
            "command -v krfb >/dev/null && echo KRFBI || true; "
            "test -f /etc/systemd/user/krfb-dno.service -o "
            "-f /etc/systemd/user/app-org.kde.krfb.service && echo UNITI || true; "
            "pgrep -x krfb >/dev/null && echo PROCI || true; "
            "ls /etc/systemd/user/*target.wants/krfb-dno.service "
            "/etc/systemd/user/*target.wants/app-org.kde.krfb.service "
            "/home/*/.config/systemd/user/*target.wants/krfb-dno.service "
            "/home/*/.config/systemd/user/*target.wants/app-org.kde.krfb.service "
            ">/dev/null 2>&1 && echo WANTSI || true"
        )
        present = out or ""
        # Сообщать только о реально включённом или запущенном KRFB: сам
        # пакет стоит в МОС по умолчанию.
        was_enabled = "WANTSI" in present
        if "KRFBI" not in present and "UNITI" not in present and "PROCI" not in present:
            return

        # 1. Root: снять автозапуск юнита krfb-dno глобально. Первым шагом
        # и независимо от dnoasuser: даже если user-стоп ниже провалится,
        # на следующем входе пользователя юнит не поднимется.
        run_cmd(
            "systemctl --global disable krfb-dno.service app-org.kde.krfb.service 2>/dev/null; "
            "rm -f /etc/systemd/user/*target.wants/krfb-dno.service "
            "/etc/systemd/user/*target.wants/app-org.kde.krfb.service; "
            "rm -f /home/*/.config/systemd/user/*target.wants/krfb-dno.service "
            "/home/*/.config/systemd/user/*target.wants/app-org.kde.krfb.service; "
            "true",
            timeout=30,
        )

        # 2. Best-effort: stop + disable + daemon-reload в user-менеджерах
        # всех учётных записей. Ошибка цикла (отказ setpriv/runuser на
        # учётках с pam_limits) не валит функцию: pkill ниже убьёт процесс,
        # а авто-restart Restart=always невозможен - юнит больше не в wants.
        stop_cmd = (
            _as_user_func() + "; for h in /home/*/; do u=$(basename $h); "
            'dnoasuser "$u" bash -c '
            "'systemctl --user stop krfb-dno.service app-org.kde.krfb.service 2>/dev/null; "
            "systemctl --user disable krfb-dno.service app-org.kde.krfb.service 2>/dev/null; "
            "systemctl --user daemon-reload 2>/dev/null; true' "
            "2>/dev/null; done; true"
        )
        code, out, err = run_cmd(stop_cmd, timeout=60)
        if code != 0:
            # SSH-таймаут или сбой канала - фиксируем в лог и продолжаем.
            log(
                f"krfb: не удалось остановить user-юниты ({err or out or '?'}), "
                "продолжаю через pkill"
            )
        code, out, _ = run_cmd(
            "pgrep -x krfb >/dev/null && echo ACTIVE || echo INACTIVE"
        )
        if (out or "").strip() == "ACTIVE":
            run_cmd("pkill -x krfb 2>/dev/null; sleep 1; true", timeout=15)
            code2, out2, _ = run_cmd(
                "pgrep -x krfb >/dev/null && echo ACTIVE || echo INACTIVE"
            )
            if (out2 or "").strip() == "ACTIVE":
                log("WARNING: не удалось остановить krfb")
            else:
                log(
                    "Обнаружен другой вариант общего экрана (krfb) - остановлен и снят с автозапуска"
                )
        elif was_enabled:
            log("Обнаружен другой вариант общего экрана (krfb) - снят с автозапуска")

    def _shutdown_legacy_x11vnc(self, run_cmd, log) -> None:
        """Остановить x11vnc (старый бекенд) перед установкой krfb.

        Только стоп + disable (юнит/конфиги/пароль /etc/x11vnc не удаляются).
        """
        code, out, err = run_cmd("systemctl is-active x11vnc")
        active = code == 0 and (out or "").strip() == "active"
        # is-enabled: код 0 для enabled/alias/... состояний.
        code2, out2, _ = run_cmd("systemctl is-enabled x11vnc 2>/dev/null")
        enabled = code2 == 0 and bool((out2 or "").strip())
        if not active and not enabled:
            return
        run_cmd(
            "systemctl stop x11vnc.service 2>/dev/null; "
            "systemctl disable x11vnc.service 2>/dev/null; true",
            timeout=30,
        )
        verb = "остановлен и снят с автозапуска" if active else "снят с автозапуска"
        log(f"Обнаружен другой вариант общего экрана (x11vnc) - {verb}")

    def _shutdown_kmsvnc(self, run_cmd, log) -> None:
        """Остановить kmsvnc и снять с автозапуска перед другим бекендом.

        ``Conflicts=x11vnc`` останавливает kmsvnc при запуске x11vnc, но не
        выключает: после перезагрузки поднялись бы обе службы. Пакет и
        настройки остаются, вернуть можно повторной установкой kmsvnc.
        """
        code, out, _ = run_cmd(
            "systemctl is-enabled kmsvnc.service 2>/dev/null; "
            "systemctl is-active kmsvnc.service 2>/dev/null; true",
            timeout=15,
        )
        states = (out or "").split()
        if not ({"enabled", "active", "activating"} & set(states)):
            return
        run_cmd(
            "systemctl disable --now kmsvnc.service 2>/dev/null; true", timeout=30
        )
        log("Обнаружен другой вариант общего экрана (kmsvnc) - остановлен и снят с автозапуска")

    def _disable_stock_krfb_unit(self, run_cmd, log) -> None:
        """Погасить дистрибутивный user unit krfb.service (конкурент krfb-dno).

        Штатный юнит запускает ``krfb`` БЕЗ ``--nodialog``. При живом нашем
        krfb-dno второй процесс через KDBusService::Unique пересылает
        activateRequested первому - и тот показывает окно "Совместный
        доступ". Поэтому перед включением krfb-dno: --global disable +
        stop/disable в user-менеджерах всех /home/* (best-effort, ошибку
        цикла в лог, не фатально - pkill ниже добьёт процесс).
        """
        run_cmd(
            "systemctl --global disable krfb.service 2>/dev/null; "
            "rm -f /etc/systemd/user/graphical-session.target.wants/krfb.service; "
            "true",
            timeout=30,
        )
        stop_cmd = (
            _as_user_func() + "; for h in /home/*/; do u=$(basename $h); "
            'dnoasuser "$u" bash -c '
            "'systemctl --user stop krfb.service 2>/dev/null; "
            "systemctl --user disable krfb.service 2>/dev/null; true' "
            "2>/dev/null; done; true"
        )
        code, out, err = run_cmd(stop_cmd, timeout=60)
        if code != 0:
            log(
                "krfb: не удалось остановить штатные user-юниты krfb.service "
                f"({err or out or '?'}), продолжаю"
            )

    def _remove_other_backends(self, run_cmd, keep: str, log) -> None:
        """Удалить VNC-серверы, кроме ``keep``, после успешной проверки нового.

        До проверки прежние серверы только остановлены: при сбое нового
        откат вернёт их. После успеха удаляются службы, пароли и настройки
        мастера и пакеты x11vnc/kmsvnc (``rpm -e``: пакет, нужный другим,
        остаётся). Пакет KRFB входит в состав МОС и не удаляется, снимаются
        служба мастера и пароль автоматического доступа. Ошибки удаления
        установку не проваливают.
        """
        packaged = {
            "x11vnc": (
                "/etc/systemd/system/x11vnc.service /usr/local/bin/x11vnc-dno-tool.sh "
                "/var/log/x11vnc.log",
                "/etc/x11vnc",
            ),
            "kmsvnc": ("/etc/systemd/system/kmsvnc.service", "/etc/kmsvnc"),
        }
        for name, (files, directory) in packaged.items():
            if name == keep:
                continue
            code, out, _ = run_cmd(
                f"present=; for p in {files} {directory}; do [ -e \"$p\" ] && present=1; done; "
                f"rpm -q {name} >/dev/null 2>&1 && present=1; "
                f"dpkg -s {name} >/dev/null 2>&1 && present=1; "
                f'[ -n "$present" ] || {{ echo ABSENT; exit 0; }}; '
                f"systemctl disable --now {name}.service >/dev/null 2>&1; "
                f"rm -f -- {files}; rm -rf -- {directory}; systemctl daemon-reload; "
                f"if rpm -q {name} >/dev/null 2>&1; then "
                f"rpm -e {name} >/dev/null 2>&1 && echo PKG_REMOVED || echo PKG_KEPT; "
                f"elif dpkg -s {name} >/dev/null 2>&1; then "
                f"apt-get -y -q remove {name} >/dev/null 2>&1 && echo PKG_REMOVED || echo PKG_KEPT; "
                f"fi; echo CLEANED",
                timeout=120,
            )
            out = out or ""
            if "ABSENT" in out:
                continue
            if code != 0 or "CLEANED" not in out:
                log(f"WARNING: {name} удалён не полностью")
            elif "PKG_KEPT" in out:
                log(f"{name}: служба и настройки удалены, пакет оставлен (нужен другим пакетам)")
            else:
                log(f"Прежний VNC-сервер {name} удалён")
        if keep != "krfb":
            self._remove_krfb_setup(run_cmd, log)

    def _remove_krfb_setup(self, run_cmd, log) -> None:
        """Убрать службу KRFB мастера и пароль автоматического доступа."""
        code, out, _ = run_cmd(
            "ls /etc/systemd/user/krfb-dno.service /etc/systemd/user/app-org.kde.krfb.service "
            "/home/*/.config/systemd/user/krfb-dno.service >/dev/null 2>&1 && echo PRESENT; "
            "grep -ls unattendedPassword /home/*/.config/krfbrc >/dev/null 2>&1 && echo PRESENT; "
            "true",
            timeout=15,
        )
        if "PRESENT" not in (out or ""):
            return
        self._shutdown_legacy_krfb(run_cmd, log)
        script = (
            _as_user_func() + "; "
            "rm -f /etc/systemd/user/krfb-dno.service /etc/systemd/user/app-org.kde.krfb.service "
            "/home/*/.config/systemd/user/krfb-dno.service; "
            "for h in /home/*/; do u=$(basename $h); "
            '[ -f "$h.config/krfbrc" ] || continue; '
            'dnoasuser "$u" bash -c '
            "'K=$(command -v kwriteconfig6 || command -v kwriteconfig5) || exit 0; "
            '"$K" --file "$HOME/.config/krfbrc" --group Security --key unattendedPassword --delete; '
            '"$K" --file "$HOME/.config/krfbrc" --group Security --key allowUnattendedAccess --type bool false; '
            "systemctl --user daemon-reload' 2>/dev/null; done; "
            "systemctl daemon-reload; echo CLEANED"
        )
        code, out, _ = run_cmd(script, timeout=60)
        if code != 0 or "CLEANED" not in (out or ""):
            log("WARNING: служба KRFB удалена не полностью")
        else:
            log(
                "Служба KRFB мастера и пароль автоматического доступа удалены "
                "(сам KRFB входит в МОС и остаётся)"
            )
