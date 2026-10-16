#!/bin/sh
# Starts Local Agent's virtual computer. The app runs this as root in its WSL distribution each
# time; it closes the ways out to the Windows desktop, shares the work folder, then hands over to
# guest.py as the unprivileged "agent" user. stdout is the app's channel: only guest.py writes there.
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
SIZE=1280x800
WORK=""
while [ $# -gt 0 ]; do
    case "$1" in
        --size) SIZE=$2; shift 2 ;;
        --work-b64) WORK=$(printf '%s' "$2" | base64 -d 2>/dev/null || true); shift 2 ;;
        *) shift ;;
    esac
done
fail() {
    printf '{"event": "error", "message": "%s"}\n' "$1"
    exit 1
}
[ -x /opt/localagent/venv/bin/python ] && id agent >/dev/null 2>&1 \
    || fail "The virtual computer isn't fully set up. Press Set up in Settings to finish it."

# WSLg puts the Windows desktop's display, sound and X socket in every distribution. Unmounted
# here (this distribution only), nothing started in the virtual computer can show up on Windows.
if mountpoint -q /tmp/.X11-unix 2>/dev/null; then umount -l /tmp/.X11-unix 2>/dev/null; fi
[ -L /tmp/.X11-unix ] && rm -f /tmp/.X11-unix
mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
if mountpoint -q /mnt/wslg 2>/dev/null; then umount -l /mnt/wslg 2>/dev/null; fi

# The work folder is the one Windows folder it can see, as ~/Work.
H=/home/agent
mkdir -p "$H/Work"
if mountpoint -q "$H/Work" 2>/dev/null; then umount -l "$H/Work" 2>/dev/null; fi
if [ -n "$WORK" ]; then
    mount -t drvfs "$WORK" "$H/Work" -o "uid=$(id -u agent),gid=$(id -g agent)" >&2 \
        || echo "start.sh: couldn't share the work folder $WORK" >&2
fi
mountpoint -q "$H/Work" || chown agent:agent "$H/Work"

# WSL 1 (no virtual machine; its kernel name ends in "-Microsoft") lacks some of what Linux programs
# expect: shared memory for Firefox and LibreOffice, and the kernel features Firefox's own sandbox is
# built on (the whole computer is the agent's sandbox here).
EXTRA=""
case "$(uname -r)" in
    *-Microsoft)
        mountpoint -q /dev/shm 2>/dev/null || mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /dev/shm >&2 || true
        EXTRA="MOZ_DISABLE_CONTENT_SANDBOX=1 MOZ_DISABLE_GMP_SANDBOX=1 MOZ_DISABLE_RDD_SANDBOX=1"
        EXTRA="$EXTRA MOZ_DISABLE_SOCKET_PROCESS_SANDBOX=1 MOZ_DISABLE_UTILITY_SANDBOX=1"
        ;;
esac

# shellcheck disable=SC2086 # EXTRA is a list of NAME=value words
exec runuser -u agent -- env -i HOME="$H" USER=agent LOGNAME=agent SHELL=/bin/bash LANG=C.UTF-8 \
    PATH=/usr/local/bin:/usr/bin:/bin $EXTRA \
    /opt/localagent/venv/bin/python -u /opt/localagent/app/guest.py --size "$SIZE" --work "$H/Work"
