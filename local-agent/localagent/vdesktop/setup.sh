#!/bin/sh
# Sets up Local Agent's virtual computer. The app runs this as root inside its own WSL
# distribution (LocalAgentDesktop): once after adding it, and again from "Set up" to repair or
# update it. Lines starting with @@ are read by the app (progress, warnings).
set -eu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8
APT="apt-get -y -q -o Dpkg::Use-Pty=0 -o APT::Color=0"
STEPS=7
step() { echo "@@step $1 $STEPS $2"; }
warn() { echo "@@warn $*"; }

step 1 "Updating the package lists"
$APT update

step 2 "Installing the desktop"
$APT install --no-install-recommends \
    xvfb openbox tint2 x11-xserver-utils x11-utils xdotool wmctrl xclip xdg-utils dbus-x11 \
    python3-venv python3-tk ca-certificates curl gpg \
    pcmanfm mousepad xterm adwaita-icon-theme \
    fonts-dejavu-core fonts-liberation2 fonts-noto-color-emoji
# Ubuntu Pro's link to a Windows agent: not wanted on a computer nobody logs in to
$APT purge wsl-pro-service >/dev/null 2>&1 || true

step 3 "Installing LibreOffice"
$APT install --no-install-recommends libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-gtk3 \
    || warn "LibreOffice couldn't be installed (see the messages above)."

step 4 "Installing Firefox"
# Ubuntu's own Firefox is a snap, which doesn't run in WSL without systemd: Mozilla's package instead.
MOZ_KEY=/etc/apt/keyrings/packages.mozilla.org.asc
MOZ_FINGERPRINT=35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3
install_firefox() {
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg -o "$MOZ_KEY" || return 1
    fp=$(gpg -n -q --import --import-options import-show "$MOZ_KEY" 2>/dev/null \
         | awk '/^pub/ { getline; gsub(/^ +| +$/, ""); print; exit }')
    if [ "$fp" != "$MOZ_FINGERPRINT" ]; then
        echo "Mozilla's signing key has the wrong fingerprint ($fp), so it isn't used."
        rm -f "$MOZ_KEY"
        return 1
    fi
    echo "deb [signed-by=$MOZ_KEY] https://packages.mozilla.org/apt mozilla main" > /etc/apt/sources.list.d/mozilla.list
    printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > /etc/apt/preferences.d/mozilla
    $APT update && $APT install --no-install-recommends firefox
}
if command -v firefox >/dev/null 2>&1 && [ -f /etc/apt/sources.list.d/mozilla.list ]; then
    $APT install --no-install-recommends firefox || true      # an update
else
    install_firefox || warn "Firefox couldn't be installed from Mozilla (see the messages above). Press Set up again later to retry."
fi
mkdir -p /etc/firefox/policies
cat > /etc/firefox/policies/policies.json <<'EOF'
{
  "policies": {
    "DisableAppUpdate": true,
    "DisableTelemetry": true,
    "DisableFirefoxStudies": true,
    "DontCheckDefaultBrowser": true,
    "OverrideFirstRunPage": "",
    "OverridePostUpdatePage": "",
    "NoDefaultBookmarks": true,
    "OfferToSaveLogins": false,
    "HardwareAcceleration": false,
    "UserMessaging": {
      "ExtensionRecommendations": false,
      "FeatureRecommendations": false,
      "SkipOnboarding": true,
      "MoreFromMozilla": false
    },
    "FirefoxHome": {"SponsoredTopSites": false, "SponsoredPocket": false},
    "Preferences": {
      "datareporting.policy.dataSubmissionPolicyBypassNotification": {"Value": true, "Status": "default"}
    }
  }
}
EOF

step 5 "Installing the screen tools"
VENV=/opt/localagent/venv
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install -q --disable-pip-version-check --no-input \
    "mss==10.2.0" "pillow==12.3.0" "pyautogui==0.9.54" "pyperclip==1.11.0"
# (pyautogui itself can't be imported without a screen to connect to)
"$VENV/bin/python" -c "import importlib.util, mss, PIL, pyperclip, Xlib; assert importlib.util.find_spec('pyautogui')" \
    || { echo "The screen tools' packages didn't install."; exit 1; }

step 6 "Making the agent's account"
# An ordinary account without sudo: it can't mount the Windows drives or switch interop back on.
id agent >/dev/null 2>&1 || useradd -m -s /bin/bash -c "Local Agent" agent
H=/home/agent
for d in Work Desktop Documents Downloads .config .config/libreoffice/4/user; do
    mkdir -p "$H/$d"
done
cat > "$H/.config/mimeapps.list" <<'EOF'
[Default Applications]
x-scheme-handler/http=firefox.desktop
x-scheme-handler/https=firefox.desktop
text/html=firefox.desktop
application/pdf=firefox.desktop
inode/directory=pcmanfm.desktop
text/plain=org.xfce.mousepad.desktop
EOF
XCU="$H/.config/libreoffice/4/user/registrymodifications.xcu"
[ -f "$XCU" ] || cat > "$XCU" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="ShowTipOfTheDay" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="FirstRun" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Setup/Office"><prop oor:name="ooSetupInstCompleted" oor:op="fuse"><value>true</value></prop></item>
</oor:items>
EOF
# not into Work: that can be the Windows work folder, mounted
for d in Desktop Documents Downloads .config; do chown -R agent:agent "$H/$d"; done
mountpoint -q "$H/Work" || chown agent:agent "$H/Work"
chown agent:agent "$H"

step 7 "Finishing"
cat > /etc/wsl.conf <<'EOF'
# Local Agent's virtual computer: it may not see the Windows drives or start Windows programs.
[boot]
systemd=false
[automount]
enabled=false
mountFsTab=false
[interop]
enabled=false
appendWindowsPath=false
[gpu]
enabled=false
[user]
default=agent
EOF
$APT autoremove >/dev/null 2>&1 || true
apt-get clean
date -u +%Y-%m-%dT%H:%M:%SZ > /opt/localagent/setup-done
echo "@@done"
