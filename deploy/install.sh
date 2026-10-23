#!/usr/bin/env bash
# One-time (and safely re-runnable) setup: checks for required binaries,
# seeds config.toml and the appearance catalogs, installs the KWin
# presence adapter, and installs the systemd units with their @REPO_ROOT@/
# @VENV_PYTHON@ placeholders substituted for real paths.
#
# Does NOT build the Python venv -- that's deploy/sync_pip.sh's own job,
# run separately (its docstring explains why: this project's venv lives
# and runs from storage.workspace_dir directly, which is commonly a
# *different* path from this repo checkout).
#
# Usage: deploy/install.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/deskmate"
CONFIG_FILE="${DESKMATE_CONFIG_FILE:-$CONFIG_DIR/config.toml}"

echo "=========================================================="
echo " Deskmate install.sh"
echo "=========================================================="

# --- 1. Dependency checks -------------------------------------------------------
#
# Error messages deliberately mirror the ones already in brain/voice/tts_local.py
# and brain/voice/lipsync.py, rather than inventing new wording for the same
# facts twice.

missing=0

check_apt_binary() {
    local bin="$1" pkg="$2"
    if command -v "$bin" >/dev/null 2>&1; then
        echo "-- found $bin: $(command -v "$bin")"
    else
        echo "!! '$bin' not found. Install it with: sudo apt install $pkg" >&2
        missing=1
    fi
}

check_apt_binary blender blender
check_apt_binary bwrap bubblewrap

config_value() {
    # Reads one dotted key (e.g. "storage.workspace_dir") out of config.toml;
    # prints nothing (not an error) if the file or key doesn't exist yet --
    # callers check for an empty result themselves.
    [ -f "$CONFIG_FILE" ] || return 0
    python3 - "$CONFIG_FILE" "$1" <<'PYEOF' 2>/dev/null || true
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    data = tomllib.load(f)
for key in sys.argv[2].split("."):
    data = data.get(key, {}) if isinstance(data, dict) else {}
if isinstance(data, str):
    print(data)
PYEOF
}

WORKSPACE_DIR="$(config_value storage.workspace_dir)"
VENV_PYTHON=""
if [ -n "$WORKSPACE_DIR" ] && [ -x "$WORKSPACE_DIR/.venv/bin/python3" ]; then
    VENV_PYTHON="$WORKSPACE_DIR/.venv/bin/python3"
fi

if command -v piper >/dev/null 2>&1 || { [ -n "$VENV_PYTHON" ] && [ -x "$(dirname "$VENV_PYTHON")/piper" ]; }; then
    echo "-- found piper"
else
    echo "!! 'piper' not found. It installs into this project's own venv, not system-wide --" >&2
    echo "   run: deploy/sync_pip.sh -e '.[dev,voice]'" >&2
    missing=1
fi

RHUBARB_BIN="$(config_value voice.rhubarb_bin)"
[ -n "$RHUBARB_BIN" ] || RHUBARB_BIN="rhubarb"
if command -v "$RHUBARB_BIN" >/dev/null 2>&1 || [ -x "$RHUBARB_BIN" ]; then
    echo "-- found rhubarb: $RHUBARB_BIN"
else
    echo "!! rhubarb not found at '$RHUBARB_BIN'. No apt/pip package exists for it --" >&2
    echo "   download a release from https://github.com/DanielSWolf/rhubarb-lip-sync/releases," >&2
    echo "   extract it anywhere, and set voice.rhubarb_bin in config.toml to its path." >&2
    missing=1
fi

if [ "$missing" -ne 0 ]; then
    echo "--------------------------------------------------------------"
    echo " One or more dependencies are missing (see '!!' lines above)."
    echo " Continuing with the rest of setup regardless -- fix these"
    echo " before actually running the daemon."
    echo "--------------------------------------------------------------"
fi

# --- 2. Seed config.toml --------------------------------------------------------

mkdir -p "$CONFIG_DIR"
if [ -f "$CONFIG_FILE" ]; then
    echo "-- config already exists at $CONFIG_FILE, leaving it alone"
else
    cp "$REPO_ROOT/config.example.toml" "$CONFIG_FILE"
    echo "-- seeded $CONFIG_FILE from config.example.toml -- edit it before running the daemon"
fi

# --- 3. Seed appearance catalogs ------------------------------------------------

DEPLOY_DIR="$(config_value storage.deploy_dir)"
if [ -n "$DEPLOY_DIR" ]; then
    CATALOGS_DIR="$DEPLOY_DIR/assets/catalogs"
    mkdir -p "$CATALOGS_DIR"
    for f in templates_catalog.json wardrobe_catalog.json; do
        if [ -f "$CATALOGS_DIR/$f" ]; then
            echo "-- $CATALOGS_DIR/$f already seeded, leaving it alone"
        else
            cp "$REPO_ROOT/brain/appearance/catalogs_seed/$f" "$CATALOGS_DIR/$f"
            echo "-- seeded $CATALOGS_DIR/$f"
        fi
    done
else
    echo "!! storage.deploy_dir not resolvable from $CONFIG_FILE -- skipping catalog seeding" >&2
fi

# --- 4. Install the KWin presence adapter --------------------------------------

if command -v kpackagetool6 >/dev/null 2>&1; then
    if kpackagetool6 --type KWin/Script --show deskmate-presence >/dev/null 2>&1; then
        echo "-- KWin adapter already installed; upgrading"
        kpackagetool6 --type KWin/Script --upgrade "$REPO_ROOT/adapters/kwin/"
    else
        kpackagetool6 --type KWin/Script --install "$REPO_ROOT/adapters/kwin/"
    fi
    echo "-- enable it with:"
    echo "     kwriteconfig6 --file kwinrc --group Plugins --key deskmate-presenceEnabled true"
    echo "     busctl --user call org.kde.KWin /KWin org.kde.KWin reconfigure"
else
    echo "!! kpackagetool6 not found -- skipping KWin adapter installation (not on KDE Plasma?)" >&2
fi

# --- 5. Install systemd units, substituting placeholders -----------------------

SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"

if [ -z "$VENV_PYTHON" ]; then
    echo "!! no venv python found at \$workspace_dir/.venv/bin/python3 -- deskmate-brain.service" >&2
    echo "   will be installed with an empty @VENV_PYTHON@ substitution. Run deploy/sync_pip.sh" >&2
    echo "   and re-run install.sh, or fix it by hand afterward." >&2
fi

for unit in deskmate-brain.service deskmate-overlay.service deskmate-status.service deskmate-backup.service deskmate-backup.timer; do
    SRC="$REPO_ROOT/deploy/systemd/$unit"
    if [ ! -f "$SRC" ]; then
        echo "!! $SRC missing, skipping" >&2
        continue
    fi
    sed -e "s|@REPO_ROOT@|$REPO_ROOT|g" -e "s|@VENV_PYTHON@|$VENV_PYTHON|g" "$SRC" > "$SYSTEMD_USER_DIR/$unit"
    echo "-- installed $SYSTEMD_USER_DIR/$unit"
done

# --- 6. Install the WirePlumber rule that gives the overlay its own volume ------
#
# Qt tags the overlay's audio with the shared "music" role, so its saved volume
# collided with Spotify and other music apps. See the comments in the file.

WIREPLUMBER_DROPIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/wireplumber/wireplumber.conf.d"
mkdir -p "$WIREPLUMBER_DROPIN_DIR"
cp "$REPO_ROOT/deploy/wireplumber/50-deskmate-overlay.conf" "$WIREPLUMBER_DROPIN_DIR/50-deskmate-overlay.conf"
echo "-- installed $WIREPLUMBER_DROPIN_DIR/50-deskmate-overlay.conf"
echo "   takes effect after: systemctl --user restart wireplumber"

systemctl --user daemon-reload
echo "-- ran: systemctl --user daemon-reload"
echo "   enable with: systemctl --user enable --now deskmate-brain.service deskmate-status.service deskmate-backup.timer"
echo "   (deskmate-overlay.service once overlay/build/deskmate_overlay has been built)"

echo "=========================================================="
echo " install.sh complete."
echo "=========================================================="
