#!/usr/bin/env bash
# Build/update the brain daemon's venv on fast local scratch storage
# (storage.workspace_dir) — the venv lives and runs there permanently:
# venvs are thousands of small files, which is exactly what network
# filesystems are bad at, so we never `pip install` directly against a
# network-mounted path.
#
# Optionally also mirrors the finished venv to a second location (e.g. a
# backup target, or another machine's mount) via DESKMATE_VENV_MIRROR_DIR
# — this is NOT storage.deploy_dir: that key is the brain daemon's own
# runtime state directory (state.db, assets, manifests) and must stay on
# fast local storage for SQLite reliability; mirroring the venv there
# would mix reproducible dependencies into that tier for no reason.
#
# Usage: deploy/sync_pip.sh [pip install args...]
#   deploy/sync_pip.sh                    # installs this project (.[dev])
#   deploy/sync_pip.sh -e '.[dev,voice]'  # with the optional voice extra
#   deploy/sync_pip.sh some-extra-package # anything pip install accepts
#
#   DESKMATE_VENV_MIRROR_DIR=/path/to/venv_mirror deploy/sync_pip.sh
#                                          # also rsyncs the built venv there

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"  # repo root

# Personal paths come from the same config.toml everything else uses.
CONFIG_FILE="${DESKMATE_CONFIG_FILE:-$HOME/.config/deskmate/config.toml}"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "No config at $CONFIG_FILE — copy config.example.toml there and fill it in first." >&2
    exit 1
fi
workspace_dir() { python3 -c "import tomllib,sys; print(tomllib.load(open(sys.argv[1],'rb'))['storage']['workspace_dir'])" "$CONFIG_FILE"; }

LOCAL_SOURCE="$(workspace_dir)"
MIRROR_TARGET="${DESKMATE_VENV_MIRROR_DIR:-}"
VENV_DIR=".venv"

if [ "$#" -eq 0 ]; then
    set -- -e "${SCRIPT_DIR}[dev]"
fi

echo "=========================================================="
echo " Starting Isolated Local Build Sequence"
echo "=========================================================="

echo "--> Moving to local workspace: $LOCAL_SOURCE"
mkdir -p "$LOCAL_SOURCE"
cd "$LOCAL_SOURCE"

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "--> Local .venv not found. Creating a fresh virtual environment..."
    python3 -m venv --upgrade-deps "$VENV_DIR" || python3 -m venv "$VENV_DIR"
fi

if [ ! -f "$VENV_DIR/bin/pip" ]; then
    echo "--> Bootstrapping pip into local virtual environment..."
    "$VENV_DIR/bin/python3" -m ensurepip --upgrade
fi

echo "--> Upgrading core packaging tools..."
"$VENV_DIR/bin/python3" -m pip install --upgrade pip setuptools wheel

echo "--> Installing: $*"
"$VENV_DIR/bin/python3" -m pip install "$@"

echo "=========================================================="
echo " Local Build Complete: $LOCAL_SOURCE/$VENV_DIR"
echo "=========================================================="

if [ -z "$MIRROR_TARGET" ]; then
    echo "--> DESKMATE_VENV_MIRROR_DIR not set — skipping mirror step."
    echo "    The venv at $LOCAL_SOURCE/$VENV_DIR is what actually runs; nothing else to do."
    exit 0
fi

echo "--> Executing rsync mirror to $MIRROR_TARGET..."
mkdir -p "$MIRROR_TARGET"
rsync -av --delete "$LOCAL_SOURCE/$VENV_DIR/" "$MIRROR_TARGET/$VENV_DIR/"

echo "--> Fixing absolute paths in mirrored virtual environment..."
# NOTE: deliberately NOT "sed -i" here. On an NFS target, sed -i's
# create-temp-then-rename-and-preserve-permissions dance can fail with
# "Operation not permitted" while trying to replicate NFSv4 ACLs onto the
# new inode — and GNU sed aborts the *entire* remaining file-argument list
# the moment that happens on any one file, silently leaving every file
# after it in glob order unpatched. Writing the replacement into the
# *existing* inode via `>` avoids the rename/ACL step entirely, so it
# works the same on NFS and local filesystems, and one file's failure
# can't take out the rest since each is its own small loop iteration
# rather than one big multi-file sed invocation.
sed -i "s|$LOCAL_SOURCE/$VENV_DIR|$MIRROR_TARGET/$VENV_DIR|g" "$MIRROR_TARGET/$VENV_DIR/bin/activate"
for f in "$MIRROR_TARGET/$VENV_DIR/bin/"*; do
    [ -f "$f" ] || continue
    grep -Iq . "$f" 2>/dev/null || continue  # skip binaries (ELF interpreters etc.)
    if sed "s|#!$LOCAL_SOURCE/$VENV_DIR/bin/|#!$MIRROR_TARGET/$VENV_DIR/bin/|" "$f" > "$f.deskmate_tmp" 2>/dev/null; then
        cat "$f.deskmate_tmp" > "$f"
        rm -f "$f.deskmate_tmp"
    else
        rm -f "$f.deskmate_tmp" 2>/dev/null || true
        echo "    !! failed to process $f" >&2
    fi
done

echo "--> Verifying console-script shebangs were actually patched..."
bad=0
for f in "$MIRROR_TARGET/$VENV_DIR/bin/"*; do
    [ -f "$f" ] || continue
    head -1 "$f" 2>/dev/null | grep -q "^#!" || continue
    # Match the full old shebang prefix, not just $LOCAL_SOURCE bare — if
    # LOCAL_SOURCE and MIRROR_TARGET ever share a path prefix, a bare
    # substring check false-positives on every correctly-patched file.
    if head -1 "$f" | grep -qF "#!$LOCAL_SOURCE/$VENV_DIR/bin/"; then
        echo "    !! $f still points at $LOCAL_SOURCE" >&2
        bad=1
    fi
done
if [ "$bad" -ne 0 ]; then
    echo "--> WARNING: one or more console scripts still have the old shebang." >&2
    echo "    They will fail if run directly; use '$MIRROR_TARGET/$VENV_DIR/bin/python3 -m <module>' instead," >&2
    echo "    or investigate the failure(s) named above." >&2
else
    echo "--> All console-script shebangs patched correctly."
fi

echo "=========================================================="
echo " Success! Mirror Complete."
echo "=========================================================="
