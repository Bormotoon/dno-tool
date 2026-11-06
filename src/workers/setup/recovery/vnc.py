"""Save remote VNC configuration and restore the previous server on failure."""

import re
import shlex


_PATHS = r"""/etc/systemd/system/x11vnc.service /etc/systemd/system/*target.wants/x11vnc.service
/usr/local/bin/x11vnc-dno-tool.sh /etc/x11vnc/passwd
/etc/systemd/system/kmsvnc.service /etc/systemd/system/*target.wants/kmsvnc.service /etc/kmsvnc/passwd
/etc/systemd/user/krfb.service /etc/systemd/user/krfb-dno.service /etc/systemd/user/app-org.kde.krfb.service
/etc/systemd/user/*target.wants/krfb.service /etc/systemd/user/*target.wants/krfb-dno.service /etc/systemd/user/*target.wants/app-org.kde.krfb.service
/home/*/.config/krfbrc /etc/skel/.config/krfbrc
/home/*/.config/systemd/user/krfb.service /home/*/.config/systemd/user/krfb-dno.service /home/*/.config/systemd/user/app-org.kde.krfb.service
/home/*/.config/systemd/user/*target.wants/krfb.service
/home/*/.config/systemd/user/*target.wants/krfb-dno.service
/home/*/.config/systemd/user/*target.wants/app-org.kde.krfb.service""".replace("\n", " ")

# Файлы снимка в каталоге на хосте; cleanup удаляет только их.
_SNAPSHOT_FILES = (
    "files config.tar x11-active kms-active listeners users accounts "
    "foreign created"
)

# Кто держит VNC-порт или работает VNC-сервером: служба по cgroup процесса.
# Определения общие для снимка и освобождения порта. dnoowner печатает
# одну строку: «known» — служба мастера (её останавливает backend и
# возвращает откат), «system UNIT» / «user ПОЛЬЗОВАТЕЛЬ UNIT» — чужая
# служба, «none ПРИЧИНА» — процесс вне служб или служба, которую
# останавливать нельзя.
_OWNER_FUNCS = r"""
dnocgroup() {
  [ -r "/proc/$1/cgroup" ] || return 0
  while IFS= read -r line; do
    case "$line" in
      0::*) printf '%s\n' "${line#0::}"; return 0;;
      *:name=systemd:*) printf '%s\n' "${line#*:name=systemd:}"; return 0;;
    esac
  done < "/proc/$1/cgroup"
}
dnosockets() {
  # Сокет-юниты, слушающие VNC-порт: процесс-держатель — сам systemd.
  [ -n "$PORT" ] || return 0
  awk -v p=":$PORT" '{ n = length($1) - length(p); if (n > 0 && substr($1, n + 1) == p) print $2 }' | sort -u
}
dnoprotected() {
  # Службы, остановка которых оборвёт доступ к хосту или сеанс.
  case "$1" in
    ssh.service|sshd.service|ssh@*|sshd@*|dbus*|systemd-*|user@*|init.scope) return 0;;
    display-manager.service|sddm*|gdm*|lightdm*|NetworkManager*|polkit*|veyon*) return 0;;
  esac
  return 1
}
dnoowner() {
  pid=$1
  cg=$(dnocgroup "$pid" || true)
  first=; second=
  rest=$cg
  while [ -n "$rest" ]; do
    c=${rest%%/*}
    if [ "$c" = "$rest" ]; then rest=; else rest=${rest#*/}; fi
    case "$c" in
      *.service|*.scope|*.socket)
        if [ -z "$first" ]; then first=$c; elif [ -z "$second" ]; then second=$c; fi;;
    esac
  done
  case "$first" in
    init.scope)
      found=
      for s in $(systemctl list-sockets --all --no-legend 2>/dev/null | dnosockets); do
        echo "system $s"; found=1
      done
      [ -n "$found" ] || echo "none вне служб"
      ;;
    user@*.service)
      uid=${first#user@}; uid=${uid%.service}
      u=$(getent passwd "$uid" | cut -d: -f1)
      if [ -z "$u" ]; then echo "none неизвестный пользователь $uid"; return 0; fi
      case "$second" in
        ""|init.scope)
          found=
          for s in $(dnoasuser "$u" systemctl --user list-sockets --all --no-legend 2>/dev/null | dnosockets); do
            echo "user $u $s"; found=1
          done
          [ -n "$found" ] || echo "none вне служб"
          ;;
        krfb.service|krfb-dno.service|app-org.kde.krfb.service) echo known;;
        *.service|*.socket) echo "user $u $second";;
        *) echo "none запущен из сеанса пользователя $u";;
      esac
      ;;
    x11vnc.service|kmsvnc.service) echo known;;
    session-*.scope) echo "none запущен вручную в сеансе ${first%.scope}";;
    *.service|*.socket)
      if dnoprotected "$first"; then echo "none служба $first не останавливается мастером"
      else echo "system $first"; fi
      ;;
    *) echo "none вне служб";;
  esac
}
dnoholders() {
  # Держатели VNC-порта и процессы известных VNC-серверов.
  {
    if [ -n "$PORT" ]; then
      ss -H -tlnp "sport = :$PORT" | grep -o 'pid=[0-9]*' | cut -d= -f2
    fi
    pgrep -x x11vnc || true
    pgrep -x kmsvnc || true
    pgrep -x krfb || true
  } | sort -un
}
dnodescribe() {
  comm=$(cat "/proc/$1/comm" 2>/dev/null || echo '?')
  owner=$(ps -o user= -p "$1" 2>/dev/null | tr -d ' ' || true)
  printf '%s (PID %s, пользователь %s)' "$comm" "$1" "${owner:-?}"
}
"""


class UnmanagedVncError(RuntimeError):
    """На хосте VNC-сервер, запущенный вне служб: его состояние не восстановить."""


class PortReleaseError(RuntimeError):
    """Чужую службу на VNC-порту не удалось остановить."""


class VncSwitch:
    def __init__(self, run, as_user_func, port=None):
        self.run, self.as_user_func = run, as_user_func
        self.port = int(port) if port else None
        self.path = None
        self.had_service = False
        # Чужие службы на VNC-порту: строки «system UNIT» / «user U UNIT».
        self.foreign = []

    def _header(self):
        return (
            self.as_user_func
            + "; D="
            + shlex.quote(self.path)
            + "; PORT="
            + (str(self.port) if self.port else "")
            + "\n"
            + _OWNER_FUNCS
        )

    @staticmethod
    def describe(entry):
        """Подпись чужой службы для журнала."""
        parts = entry.split()
        if parts[0] == "user" and len(parts) >= 3:
            return f"служба пользователя {parts[1]} {parts[2]}"
        return f"служба {parts[-1]}"

    def prepare(self):
        code, path, _ = self.run("umask 077; mktemp -d /tmp/dno-vnc.XXXXXXXXXX")
        if code or not re.fullmatch(r"/tmp/dno-vnc\.[A-Za-z0-9]+", path or ""):
            raise RuntimeError("Не удалось создать резервную копию VNC")
        self.path = path
        script = self._header() + r"""
set -e
set -o pipefail
getent passwd | awk -F: '{print $1 ":" $6}' > "$D/accounts"
: > "$D/files"
: > "$D/foreign"
: > "$D/created"
for p in PATHS; do
  if [ -e "$p" ] || [ -L "$p" ]; then printf '%s\n' "$p" >> "$D/files"; fi
done
systemctl is-active x11vnc.service > "$D/x11-active" || true
systemctl is-active kmsvnc.service > "$D/kms-active" || true
ss -H -tlnp | awk '/x11vnc|krfb|kmsvnc/ {print $4}' > "$D/listeners"
: > "$D/users"
while IFS=: read -r u h; do
  [ -d "$h" ] || continue
  for p in "$h"/.config/krfbrc "$h"/.config/systemd/user/krfb.service \
      "$h"/.config/systemd/user/krfb-dno.service \
      "$h"/.config/systemd/user/app-org.kde.krfb.service \
      "$h"/.config/systemd/user/*target.wants/krfb.service \
      "$h"/.config/systemd/user/*target.wants/krfb-dno.service \
      "$h"/.config/systemd/user/*target.wants/app-org.kde.krfb.service; do
    if [ -e "$p" ] || [ -L "$p" ]; then printf '%s\n' "$p" >> "$D/files"; fi
  done
  for unit in krfb.service krfb-dno.service app-org.kde.krfb.service; do
    if dnoasuser "$u" systemctl --user is-active --quiet "$unit"; then
      printf '%s %s\n' "$u" "$unit" >> "$D/users"
    fi
  done
done < "$D/accounts"
unmanaged=
for pid in $(dnoholders); do
  owner=$(dnoowner "$pid")
  case "$owner" in
    known) ;;
    system\ *|user\ *) printf '%s\n' "$owner" >> "$D/foreign";;
    *) printf 'UNMANAGED\t%s: %s\n' "$(dnodescribe "$pid")" "${owner#none }"; unmanaged=1;;
  esac
done
[ -z "$unmanaged" ] || exit 50
sort -u "$D/foreign" -o "$D/foreign"
# Автозапуск чужой службы — ссылки в *.wants и файл автозапуска KDE:
# они попадают в копию, откат вернёт их распаковкой.
while read -r kind a b; do
  case "$kind" in
    system)
      for p in /etc/systemd/system/*.wants/"$a" /etc/systemd/system/*.requires/"$a"; do
        if [ -L "$p" ]; then printf '%s\n' "$p" >> "$D/files"; fi
      done
      ;;
    user)
      h=$(getent passwd "$a" | cut -d: -f6)
      for p in /etc/systemd/user/*.wants/"$b" "$h"/.config/systemd/user/*.wants/"$b"; do
        if [ -L "$p" ]; then printf '%s\n' "$p" >> "$D/files"; fi
      done
      case "$b" in
        app-*@autostart.service)
          id=${b#app-}; id=$(systemd-escape -u -- "${id%@autostart.service}")
          p="$h/.config/autostart/$id.desktop"
          if [ -f "$p" ]; then printf '%s\n' "$p" >> "$D/files"; fi
          ;;
      esac
      ;;
  esac
  printf 'FOREIGN\t%s %s %s\n' "$kind" "$a" "$b"
done < "$D/foreign"
if [ -s "$D/foreign" ] && [ -n "$PORT" ]; then echo "*:$PORT" >> "$D/listeners"; fi
sort -u "$D/files" -o "$D/files"
tar -P -cf "$D/config.tar" -T "$D/files"
if grep -qx active "$D/x11-active" || grep -qx active "$D/kms-active" \
    || [ -s "$D/users" ] || [ -s "$D/foreign" ]; then echo HAD_SERVICE; fi
echo SNAPSHOT_OK
""".replace("PATHS", _PATHS)
        code, out, _ = self.run(
            "bash -s", stdin_data=script, timeout=60, sensitive=True
        )
        out = out or ""
        if code == 50:
            details = [
                line.split("\t", 1)[1].strip()
                for line in out.splitlines()
                if line.startswith("UNMANAGED\t")
            ]
            raise UnmanagedVncError(
                "VNC-порт или VNC-сервер занят процессом, который мастер не "
                "может остановить и вернуть"
                + (": " + "; ".join(details) if details else "")
                + ". Остановите его и повторите настройку."
            )
        if code or "SNAPSHOT_OK" not in out:
            raise RuntimeError("Не удалось сохранить исходное состояние VNC")
        self.had_service = "HAD_SERVICE" in out
        self.foreign = [
            " ".join(line.split("\t", 1)[1].split())
            for line in out.splitlines()
            if line.startswith("FOREIGN\t")
        ]

    def release_foreign(self):
        """Остановить и снять с автозапуска чужие службы на VNC-порту.

        Возвращает строки для журнала. Если порт после остановки держит
        не служба мастера, бросает ``PortReleaseError``: откат вернёт
        остановленное.
        """
        if not self.foreign:
            return []
        script = self._header() + r"""
while read -r kind a b; do
  case "$kind" in
    system)
      systemctl stop -- "$a" >/dev/null 2>&1 || true
      systemctl disable -- "$a" >/dev/null 2>&1 || true
      state=$(systemctl is-enabled -- "$a" 2>/dev/null || true)
      case "$state" in
        disabled|masked|"") ;;
        *)
          # Статическую или чужую по зависимости службу disable не снимает.
          p=/etc/systemd/system/$a
          if [ ! -e "$p" ] && [ ! -L "$p" ] && systemctl mask -- "$a" >/dev/null 2>&1; then
            printf '%s\n' "$p" >> "$D/created"; state=masked
          fi
          ;;
      esac
      printf 'RELEASED\t%s %s\t%s\n' "$kind" "$a" "$state"
      ;;
    user)
      h=$(getent passwd "$a" | cut -d: -f6)
      case "$b" in
        app-*@autostart.service)
          # Служба создана из автозапуска KDE: скрыть файл автозапуска.
          id=${b#app-}; id=$(systemd-escape -u -- "${id%@autostart.service}")
          p="$h/.config/autostart/$id.desktop"
          if [ -f "$p" ]; then
            sed -i -e '/^Hidden=/d' -e '/^\[Desktop Entry\]/a Hidden=true' "$p"
          else
            dnoasuser "$a" sh -c 'mkdir -p "$HOME/.config/autostart" && printf "[Desktop Entry]\nHidden=true\n" > "$HOME/.config/autostart/$1.desktop"' sh "$id" \
              && printf '%s\n' "$p" >> "$D/created"
          fi
          dnoasuser "$a" systemctl --user stop "$b" >/dev/null 2>&1 || true
          dnoasuser "$a" systemctl --user daemon-reload >/dev/null 2>&1 || true
          state=disabled
          grep -qs '^Hidden=true' "$p" || state=enabled
          ;;
        *)
          dnoasuser "$a" systemctl --user stop "$b" >/dev/null 2>&1 || true
          dnoasuser "$a" systemctl --user disable "$b" >/dev/null 2>&1 || true
          systemctl --global disable -- "$b" >/dev/null 2>&1 || true
          rm -f -- /etc/systemd/user/*.wants/"$b" "$h"/.config/systemd/user/*.wants/"$b"
          dnoasuser "$a" systemctl --user daemon-reload >/dev/null 2>&1 || true
          state=$(dnoasuser "$a" systemctl --user is-enabled "$b" 2>/dev/null || true)
          case "$state" in
            disabled|masked|"") ;;
            *)
              p="$h/.config/systemd/user/$b"
              if [ ! -e "$p" ] && [ ! -L "$p" ] \
                  && dnoasuser "$a" systemctl --user mask "$b" >/dev/null 2>&1; then
                printf '%s\n' "$p" >> "$D/created"; state=masked
              fi
              ;;
          esac
          ;;
      esac
      printf 'RELEASED\t%s %s %s\t%s\n' "$kind" "$a" "$b" "$state"
      ;;
  esac
done < "$D/foreign"
# Порт освобождается не сразу: ждём до 10 с, пока его держит чужая служба.
for i in $(seq 1 10); do
  busy=
  for pid in $(dnoholders); do
    owner=$(dnoowner "$pid")
    [ "$owner" = known ] || busy="$(dnodescribe "$pid"): ${owner#none }"
  done
  [ -n "$busy" ] || break
  sleep 1
done
if [ -n "$busy" ]; then printf 'PORT_BUSY\t%s\n' "$busy"; exit 1; fi
echo RELEASE_OK
"""
        code, out, _ = self.run("bash -s", stdin_data=script, timeout=120)
        out = out or ""
        notes = []
        for line in out.splitlines():
            if not line.startswith("RELEASED\t"):
                continue
            _, entry, state = (line.split("\t") + ["", ""])[:3]
            entry = " ".join(entry.split())
            label = self.describe(entry)
            port = f"порт {self.port}" if self.port else "VNC-порт"
            if state.strip() in ("disabled", "masked", ""):
                notes.append(f"{port} занимала {label} — остановлена и снята с автозапуска")
            else:
                notes.append(
                    f"WARNING: {port} занимала {label} — остановлена, но "
                    f"автозапуск снять не удалось ({state.strip()})"
                )
        if code != 0 or "RELEASE_OK" not in out:
            busy = [
                line.split("\t", 1)[1].strip()
                for line in out.splitlines()
                if line.startswith("PORT_BUSY\t")
            ]
            raise PortReleaseError(
                "Не удалось освободить VNC-порт"
                + (": его держит " + busy[0] if busy else "")
                + ("\n" + "\n".join(notes) if notes else "")
            )
        return notes

    def rollback(self):
        if not self.path:
            return False
        script = self._header() + r"""
set -e
systemctl stop x11vnc.service || true
systemctl stop kmsvnc.service || true
while IFS=: read -r u h; do
  [ -d "$h" ] || continue
  dnoasuser "$u" systemctl --user stop krfb.service krfb-dno.service app-org.kde.krfb.service || true
  for p in "$h"/.config/krfbrc "$h"/.config/systemd/user/krfb.service \
      "$h"/.config/systemd/user/krfb-dno.service \
      "$h"/.config/systemd/user/app-org.kde.krfb.service \
      "$h"/.config/systemd/user/*target.wants/krfb.service \
      "$h"/.config/systemd/user/*target.wants/krfb-dno.service \
      "$h"/.config/systemd/user/*target.wants/app-org.kde.krfb.service; do
    if [ -f "$p" ] || [ -L "$p" ]; then rm -f -- "$p"; fi
  done
done < "$D/accounts"
pkill -x krfb || true
for p in PATHS; do
  if [ -f "$p" ] || [ -L "$p" ]; then rm -f -- "$p"; fi
done
# Созданное при остановке чужих служб: маски и скрытие автозапуска.
if [ -f "$D/created" ]; then
  while IFS= read -r p; do
    if [ -f "$p" ] || [ -L "$p" ]; then rm -f -- "$p"; fi
  done < "$D/created"
fi
tar -P -xf "$D/config.tar"
systemctl daemon-reload
while IFS=: read -r u h; do
  [ -d "$h" ] || continue
  dnoasuser "$u" systemctl --user daemon-reload || true
done < "$D/accounts"
if grep -qx active "$D/x11-active"; then
  systemctl start x11vnc.service
  systemctl is-active --quiet x11vnc.service
fi
if grep -qx active "$D/kms-active"; then
  systemctl start kmsvnc.service
  systemctl is-active --quiet kmsvnc.service
fi
while read -r u unit; do
  dnoasuser "$u" systemctl --user start "$unit"
  dnoasuser "$u" systemctl --user is-active --quiet "$unit"
done < "$D/users"
if [ -f "$D/foreign" ]; then
  while read -r kind a b; do
    case "$kind" in
      system)
        systemctl start -- "$a"
        systemctl is-active --quiet -- "$a"
        ;;
      user)
        dnoasuser "$a" systemctl --user start "$b"
        dnoasuser "$a" systemctl --user is-active --quiet "$b"
        ;;
    esac
  done < "$D/foreign"
fi
# Серверу нужно время открыть порт: kmsvnc инициализирует VAAPI, KRFB
# ждёт сеанс. Ждём все прежние порты до 20 с.
missing=1
for i in $(seq 1 20); do
  missing=0
  while read -r addr; do
    port=${addr##*:}
    ss -H -tln | awk '{print $4}' | grep -qE ":${port}$" || missing=1
  done < "$D/listeners"
  [ "$missing" = 0 ] && break
  sleep 1
done
[ "$missing" = 0 ]
echo RESTORE_OK
""".replace("PATHS", _PATHS)
        code, out, _ = self.run(
            "bash -s", stdin_data=script, timeout=90, sensitive=True, ignore_abort=True
        )
        return code == 0 and "RESTORE_OK" in out

    def cleanup(self):
        if self.path:
            # Only fixed files in the validated private temporary directory.
            directory = shlex.quote(self.path)
            files = " ".join(f"{directory}/{name}" for name in _SNAPSHOT_FILES.split())
            self.run(
                f"rm -f -- {files}; rmdir -- {directory}",
                timeout=10,
                ignore_abort=True,
            )
