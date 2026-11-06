"""Remote vnc scripts and templates."""

_X11VNC_WRAPPER_TEMPLATE = """#!/bin/bash
# x11vnc-dno-tool.sh — wrapper with Xauth auto-discovery for display :0.
# Finds Xauth from Xorg process args, then DM paths, then loginctl.
# Works on login screen and in user session (auto-switch via -reopen).
PORT="$1"
export HOME=/root
LOG=/var/log/x11vnc.log

find_auth() {
    # 1. From Xorg process args (most reliable — DM/sddm-helper passes -auth <path>)
    local a
    a=$(ps -C Xorg -C X -o args= 2>/dev/null | grep -oP '(?<=-auth )\\S+' | head -1)
    [ -n "$a" ] && [ -f "$a" ] && { echo "$a"; return 0; }
    # 2. Standard DM paths (login screen)
    local p
    for p in /var/lib/sddm/.Xauthority /var/lib/gdm/.Xauthority \\
             /var/lib/lightdm/.Xauthority /run/sddm/auth/*; do
        [ -f "$p" ] && { echo "$p"; return 0; }
    done
    # 3. loginctl: active X11 session -> uid -> /run/user/<uid>/xauth_*
    local uid
    uid=$(loginctl list-sessions --no-legend 2>/dev/null | \\
          awk '$3=="x11" || $4=="x11" {print $2; exit}')
    if [ -n "$uid" ]; then
        for p in /run/user/$uid/xauth_* /run/user/$uid/.Xauthority; do
            [ -f "$p" ] && { echo "$p"; return 0; }
        done
    fi
    return 1
}

AUTH=$(find_auth)
if [ -z "$AUTH" ]; then
    {
        echo "$(date '+%d/%m/%Y %H:%M:%S') x11vnc-dno-tool: cannot find Xauth for :0"
        echo "  Tried: ps Xorg args, DM paths, loginctl sessions."
    } >> "$LOG"
    exit 1
fi

exec /usr/bin/x11vnc -display :0 -auth "$AUTH" -reopen \\
    $([ -f /etc/x11vnc/passwd ] && echo -n '-rfbauth /etc/x11vnc/passwd') \\
    -rfbport "$PORT" \\
    -forever -shared -o "$LOG"
"""


_X11VNC_SERVICE_TEMPLATE = """[Unit]
Description=x11vnc remote desktop
After=graphical.target

[Service]
Type=simple
Environment=HOME=/root
ExecStart=/usr/local/bin/x11vnc-dno-tool.sh {port}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical.target
"""


_KRFB_CONFIG_SCRIPT = """RC="$HOME/.config/krfbrc"
mkdir -p "$HOME/.config"
touch "$RC"
command -v kwriteconfig5 >/dev/null || {{ echo 'no-kwriteconfig5'; exit 4; }}
kwriteconfig5 --file "$RC" --group TCP --key port {port} --type int || exit 4
kwriteconfig5 --file "$RC" --group TCP --key useDefaultPort false --type bool || exit 4
kwriteconfig5 --file "$RC" --group Security --key noWallet true --type bool || exit 4
kwriteconfig5 --file "$RC" --group Security --key allowUnattendedAccess true --type bool || exit 4
kwriteconfig5 --file "$RC" --group Security --key allowDesktopControl true --type bool || exit 4
kwriteconfig5 --file "$RC" --group MainWindow --key startMinimized true --type bool || exit 4
[ "$(kreadconfig5 --file "$RC" --group TCP --key port)" = "{port}" ] || exit 4
PW_REAL=$(python3 - <<'PYEOF'
pwd = {password_literal}
import struct
raw = pwd.encode("utf-16-le")
units = struct.unpack("<" + "H" * (len(raw) // 2), raw)
scrambled = [unit if unit <= 0x21 else 0x1001F - unit for unit in units]
print(struct.pack("<" + "H" * len(scrambled), *scrambled).decode("utf-16-le", "surrogatepass"))
PYEOF
) || exit 4
if [ -n "$PW_REAL" ]; then
  PW_DUMMY=$(python3 -c "import secrets,string; r=''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(16)); print(''.join(chr(0x1001F-ord(c)) for c in r))") || exit 4
  kwriteconfig5 --file "$RC" --group Security --key desktopPassword "$PW_DUMMY" || exit 4
  kwriteconfig5 --file "$RC" --group Security --key unattendedPassword "$PW_REAL" || exit 4
  PW_READ=$(kreadconfig5 --file "$RC" --group Security --key unattendedPassword) || exit 4
  [ "$PW_READ" = "$PW_REAL" ] || exit 4
  echo "password-set"
else
  kwriteconfig5 --file "$RC" --group Security --key desktopPassword --delete || exit 4
  kwriteconfig5 --file "$RC" --group Security --key unattendedPassword --delete || exit 4
  kwriteconfig5 --file "$RC" --group Security --key allowUnattendedAccess false --type bool || exit 4
  echo "no-password-mode"
fi
echo CONFIG_OK
"""


# Служба kmsvnc, пишется мастером, как у x11vnc (в пакете только программа).
# -w будит экран при запуске: при погашенном экране kmsvnc не стартует.
# -d — видеокарта с монитором (/dev/dri/by-path/...), её выбирает мастер.
# Без файла пароля служба не запускается. Повторы запуска не ограничены:
# экран может быть недоступен дольше, чем лимит systemd по умолчанию.
_KMSVNC_SERVICE_TEMPLATE = """[Unit]
Description=kmsvnc remote desktop (DRM/KMS)
After=systemd-user-sessions.service network.target
Conflicts=x11vnc.service
StartLimitIntervalSec=0

[Service]
Type=simple
Environment=XKB_DEFAULT_LAYOUT=us
ExecStartPre=/usr/bin/test -s /etc/kmsvnc/passwd
ExecStart=/usr/bin/kmsvnc -w -p {port} -d {card} --password-file /etc/kmsvnc/passwd
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


_KRFB_UNIT_TEMPLATE = "[Unit]\nDescription=DNO Tool krfb service\nAfter=graphical-session.target\n\n[Service]\nType=exec\nExecStartPre=-/usr/bin/kwriteconfig5 --file %h/.config/krfbrc --group MainWindow --key startMinimized --type bool true\nExecStart=/usr/bin/krfb --nodialog\nRestart=always\nRestartSec=3\n\n[Install]\nWantedBy=graphical-session.target\n"
_KRFB_WAYLAND_UNIT_TEMPLATE = _KRFB_UNIT_TEMPLATE.replace(
    "ExecStart=/usr/bin/krfb --nodialog",
    "ExecStart=/usr/bin/systemd-run --user --unit=app-org.kde.krfb --collect --service-type=exec --wait -- /usr/bin/krfb --nodialog",
)


_KRFB_HIDE_TRAY_SCRIPT = r'''CFG="$HOME/.config/plasma-org.kde.plasma.desktop-appletsrc"
[ -f "$CFG" ] || { echo TRAY_CONFIG_MISSING; exit 0; }
KWRITE=$(command -v kwriteconfig6 || command -v kwriteconfig5) || {
    echo TRAY_KWRITECONFIG_MISSING
    exit 0
}
KREAD=$(command -v kreadconfig6 || command -v kreadconfig5) || {
    echo TRAY_KREADCONFIG_MISSING
    exit 0
}
TRAY_GROUPS=$(awk '
    /^\[Containments\]\[[0-9]+\]\[Applets\]\[[0-9]+\]$/ {
        section=$0
        applet=1
        next
    }
    /^\[/ { applet=0 }
    applet && /^plugin=org\.kde\.plasma\.systemtray$/ { print section }
' "$CFG")
[ -n "$TRAY_GROUPS" ] || { echo TRAY_APPLET_NOT_FOUND; exit 0; }

ITEMS=$(busctl --user get-property org.kde.StatusNotifierWatcher \
    /StatusNotifierWatcher org.kde.StatusNotifierWatcher \
    RegisteredStatusNotifierItems 2>/dev/null | grep -o '"[^"]*"' | tr -d '"')
HIDE_IDS=xdg-desktop-portal-kde
ACTIVE_HIDE_IDS=
for item in $ITEMS; do
    service=${item%%/*}
    path=/${item#*/}
    id=$(python3 - "$service" "$path" Id <<'PYID'
import json
import subprocess
import sys

result = subprocess.run(
    ["busctl", "--user", "--json=short", "get-property", sys.argv[1], sys.argv[2],
     "org.kde.StatusNotifierItem", sys.argv[3]],
    capture_output=True,
    text=True,
    check=False,
)
if result.returncode == 0:
    try:
        print(json.loads(result.stdout).get("data", ""))
    except (ValueError, AttributeError):
        pass
PYID
)
    title=$(python3 - "$service" "$path" Title <<'PYTITLE'
import json
import subprocess
import sys

result = subprocess.run(
    ["busctl", "--user", "--json=short", "get-property", sys.argv[1], sys.argv[2],
     "org.kde.StatusNotifierItem", sys.argv[3]],
    capture_output=True,
    text=True,
    check=False,
)
if result.returncode == 0:
    try:
        print(json.loads(result.stdout).get("data", ""))
    except (ValueError, AttributeError):
        pass
PYTITLE
)
    owner_pid=$(busctl --user call org.freedesktop.DBus /org/freedesktop/DBus \
        org.freedesktop.DBus GetConnectionUnixProcessID s "$service" 2>/dev/null |
        awk '{print $2}')
    owner_exe=
    if [ -n "$owner_pid" ] && [ -r "/proc/$owner_pid/exe" ]; then
        owner_exe=$(readlink -f "/proc/$owner_pid/exe" 2>/dev/null)
        owner_exe=${owner_exe##*/}
    fi
    if [ "$owner_exe" = krfb ]; then
        HIDE_IDS="${HIDE_IDS}${HIDE_IDS:+
}$id"
        ACTIVE_HIDE_IDS="${ACTIVE_HIDE_IDS}${ACTIVE_HIDE_IDS:+
}KRFB tray item: $id"
    fi
    is_remote_control=$(python3 - "$title" <<'PYREMOTE'
import sys
import unicodedata

title = unicodedata.normalize("NFKC", sys.argv[1]).casefold().replace("ё", "е")
if "удаленное управление" in title or "remote control" in title:
    print("yes")
PYREMOTE
)
    if [ "$id" = xdg-desktop-portal-kde ] && [ "$is_remote_control" = yes ]; then
        detail=$(python3 - "$service" "$path" <<'PYTIP'
import json
import subprocess
import sys

result = subprocess.run(
    ["busctl", "--user", "--json=short", "get-property", sys.argv[1], sys.argv[2],
     "org.kde.StatusNotifierItem", "ToolTip"],
    capture_output=True,
    text=True,
    check=False,
)
if result.returncode == 0:
    try:
        data = json.loads(result.stdout).get("data", [])
        print(" ".join(data[3].split()) if len(data) > 3 else "")
    except (ValueError, AttributeError, IndexError):
        pass
PYTIP
)
        HIDE_IDS="${HIDE_IDS}${HIDE_IDS:+
}$id"
        ACTIVE_HIDE_IDS="${ACTIVE_HIDE_IDS}${ACTIVE_HIDE_IDS:+
}$title — $detail"
    fi
done

for section in $TRAY_GROUPS; do
    ids=$(printf '%s\n' "$section" | sed -E \
        's/^\[Containments\]\[([0-9]+)\]\[Applets\]\[([0-9]+)\]$/\1 \2/')
    set -- $ids
    containment=$1
    applet=$2
    hidden=$("$KREAD" --file "$CFG" --group Containments --group "$containment" \
        --group Applets --group "$applet" --group General --key hiddenItems 2>/dev/null)
    hidden=$(python3 - "$hidden" "$HIDE_IDS" <<'PYMERGE'
import sys

obsolete = {
    "krfb", "org.kde.krfb", "veyon", "veyon-server",
    "io.veyon.veyon-server", "io.veyon.veyon-master",
    "Совместный доступ к рабочему столу",
}
items = [item for item in sys.argv[1].split(",") if item and item not in obsolete]
items.extend(item for item in sys.argv[2].splitlines() if item)
print(",".join(dict.fromkeys(items)))
PYMERGE
)
    shown=$("$KREAD" --file "$CFG" --group Containments --group "$containment" \
        --group Applets --group "$applet" --group General --key shownItems 2>/dev/null)
    shown=$(python3 - "$shown" "$HIDE_IDS" <<'PYSHOWN'
import sys

hidden = set(sys.argv[2].splitlines())
print(",".join(item for item in sys.argv[1].split(",") if item and item not in hidden))
PYSHOWN
)
    "$KWRITE" --file "$CFG" --group Containments --group "$containment" \
        --group Applets --group "$applet" --group General \
        --key hiddenItems "$hidden" || exit 1
    "$KWRITE" --file "$CFG" --group Containments --group "$containment" \
        --group Applets --group "$applet" --group General \
        --key shownItems "$shown"
    "$KWRITE" --file "$CFG" --group Containments --group "$containment" \
        --group Applets --group "$applet" --group General \
        --key showAllItems --type bool false
    actual=$("$KREAD" --file "$CFG" --group Containments --group "$containment" \
        --group Applets --group "$applet" --group General --key hiddenItems 2>/dev/null)
    while IFS= read -r id; do
        [ -n "$id" ] || continue
        case ",$actual," in
            *,$id,*) ;;
            *) echo "TRAY_VERIFY_FAILED:$id"; exit 1 ;;
        esac
    done <<EOF_VERIFY_IDS
$HIDE_IDS
EOF_VERIFY_IDS
    qdbus=$(command -v qdbus6 || command -v qdbus) || {
        echo TRAY_LIVE_APPLY_UNAVAILABLE
        continue
    }
    script='var wanted = '
    json_ids=$(printf '%s\n' "$HIDE_IDS" | python3 -c 'import json,sys; print(json.dumps([x.rstrip("\n") for x in sys.stdin if x.rstrip("\n")]))')
    script="$script$json_ids; var obsolete = [\"krfb\", \"org.kde.krfb\", \"veyon\", \"veyon-server\", \"io.veyon.veyon-server\", \"io.veyon.veyon-master\", \"Совместный доступ к рабочему столу\"]; var ps = panels(); for (var pi = 0; pi < ps.length; pi++) { var ws = ps[pi].widgets(); for (var wi = 0; wi < ws.length; wi++) { var w = ws[wi]; if (w.type === \"org.kde.plasma.systemtray\") { w.currentConfigGroup = [\"General\"]; var h = w.readConfig(\"hiddenItems\", \"\").split(\",\").filter(Boolean); var s = w.readConfig(\"shownItems\", \"\").split(\",\").filter(Boolean); h = h.filter(function(id) { return obsolete.indexOf(id) < 0; }); for (var ii = 0; ii < wanted.length; ii++) { if (h.indexOf(wanted[ii]) < 0) h.push(wanted[ii]); var si = s.indexOf(wanted[ii]); if (si >= 0) s.splice(si, 1); } w.writeConfig(\"hiddenItems\", h.join(\",\")); w.writeConfig(\"shownItems\", s.join(\",\")); w.writeConfig(\"showAllItems\", false); } } }"
    $qdbus org.kde.plasmashell /PlasmaShell evaluateScript "$script" >/dev/null 2>&1 || {
        echo TRAY_LIVE_APPLY_FAILED
        continue
    }
    actual=$("$KREAD" --file "$CFG" --group Containments --group "$containment" \
        --group Applets --group "$applet" --group General --key hiddenItems 2>/dev/null)
    shown_actual=$("$KREAD" --file "$CFG" --group Containments --group "$containment" \
        --group Applets --group "$applet" --group General --key shownItems 2>/dev/null)
    show_all=$("$KREAD" --file "$CFG" --group Containments --group "$containment" \
        --group Applets --group "$applet" --group General --key showAllItems 2>/dev/null)
    case "$show_all" in
        false|0) ;;
        *) echo TRAY_VERIFY_FAILED:showAllItems; exit 1 ;;
    esac
    while IFS= read -r id; do
        [ -n "$id" ] || continue
        case ",$actual," in
            *,$id,*) ;;
            *) echo "TRAY_VERIFY_FAILED:$id"; exit 1 ;;
        esac
        case ",$shown_actual," in
            *,$id,*) echo "TRAY_VERIFY_FAILED_SHOWN:$id"; exit 1 ;;
            *) ;;
        esac
    done <<EOF_LIVE_VERIFY
$HIDE_IDS
EOF_LIVE_VERIFY
    while IFS= read -r id; do
        [ -n "$id" ] || continue
        echo "TRAY_APPLIED:$id"
    done <<EOF_CONFIGURED
$ACTIVE_HIDE_IDS
EOF_CONFIGURED
    if [ -z "$ACTIVE_HIDE_IDS" ]; then
        echo "TRAY_CONFIGURED:xdg-desktop-portal-kde"
    fi
done
'''
