#!/usr/bin/env bash
# Periodic cold backup of deploy_dir (state.db, manifests, generated assets)
# from its fast local storage to the backup target. This is a cold rsync
# mirror, not live daemon I/O against the backup target — deploy_dir itself
# must stay on fast local storage: besides the latency a slow drive or
# network mount would add to every state write, SQLite's own docs call
# locking over network filesystems unreliable, and some network shares
# reject in-place permission changes on files outright. Run periodically via
# deploy/systemd/deskmate-backup.timer, or by hand.
#
# Where it goes (first match wins):
#   1. $DESKMATE_BACKUP_DIR
#   2. [storage] backup_dir in config.toml        <- the normal way
#   3. ~/.local/share/deskmate/state_backup        (same machine, probably the same disk:
#      protects against a bad delete, NOT against losing the drive; a warning is printed)
#
# What matters is that the target is on a *different disk* from deploy_dir: another
# drive in this machine, or a remote location reached through a mount (NFS/SMB/SSHFS).
# The script warns when both sit on the same physical disk.
#
# state.db is not copied as a plain file: it is live, and a file copy taken
# mid-write can be torn. It is snapshotted with SQLite's online backup API,
# integrity-checked, and moved into place atomically; everything else is
# rsync'd. The target's parent directory must already exist, so an unmounted
# drive or share can never turn into a silent backup onto the local disk under the mount point.
#
# Usage: deploy/backup_state.sh

set -euo pipefail

CONFIG_FILE="${DESKMATE_CONFIG_FILE:-$HOME/.config/deskmate/config.toml}"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "No config at $CONFIG_FILE — copy config.example.toml there and fill it in first." >&2
    exit 1
fi
storage_value() { python3 -c "import tomllib,sys; print(tomllib.load(open(sys.argv[1],'rb'))['storage'].get(sys.argv[2],''))" "$CONFIG_FILE" "$1"; }

SOURCE="$(storage_value deploy_dir)"
CONFIGURED_TARGET="$(storage_value backup_dir)"
DEFAULT_TARGET="${XDG_DATA_HOME:-$HOME/.local/share}/deskmate/state_backup"
BACKUP_TARGET="${DESKMATE_BACKUP_DIR:-${CONFIGURED_TARGET:-$DEFAULT_TARGET}}"

if [ ! -d "$SOURCE" ]; then
    echo "deploy_dir ($SOURCE) doesn't exist yet — nothing to back up." >&2
    exit 0
fi
if [ "$BACKUP_TARGET" = "$DEFAULT_TARGET" ] && [ -z "${DESKMATE_BACKUP_DIR:-}" ] && [ -z "$CONFIGURED_TARGET" ]; then
    echo "!! no backup_dir configured: using $DEFAULT_TARGET on this machine." >&2
    echo "   Set [storage] backup_dir in config.toml to a path on a different disk." >&2
fi
if [ ! -d "$(dirname "$BACKUP_TARGET")" ]; then
    echo "!! $(dirname "$BACKUP_TARGET") does not exist (drive or share not mounted?) — refusing to create it. Nothing backed up." >&2
    exit 1
fi
# The disk behind a path: a partition's parent disk (nvme0n1p9 and nvme0n1p10 are one disk),
# or the share itself for a network mount. Best effort; empty if it can't be told.
physical_disk() {
    local src
    src="$(findmnt -no SOURCE --target "$1" 2>/dev/null | head -1 || true)"
    case "$src" in
        "") ;;
        /dev/*) lsblk -no PKNAME "$src" 2>/dev/null | head -1 | grep . || basename "$src" ;;
        *) echo "$src" ;;
    esac
}
SOURCE_DISK="$(physical_disk "$SOURCE")"
TARGET_DISK="$(physical_disk "$(dirname "$BACKUP_TARGET")")"
if [ -n "$SOURCE_DISK" ] && [ "$SOURCE_DISK" = "$TARGET_DISK" ]; then
    echo "!! $BACKUP_TARGET is on the same physical disk as deploy_dir ($SOURCE_DISK): it will not survive that disk failing." >&2
fi

mkdir -p "$BACKUP_TARGET"

echo "--> Backing up $SOURCE -> $BACKUP_TARGET"
# --delete keeps this a clean mirror of current state rather than an
# ever-growing pile; this is a durability backstop for drive failure, not a
# version-history/undo mechanism (state.db's own content already carries
# history where it matters — interactions, memories, manifest versions).
# state.db is excluded here (and so protected from --delete) and snapshotted below.
rsync -a --delete --exclude 'state.db' --exclude 'state.db-journal' --exclude 'state.db-wal' --exclude 'state.db-shm' --exclude 'state.db.partial' "$SOURCE/" "$BACKUP_TARGET/"

if [ -f "$SOURCE/state.db" ]; then
    python3 - "$SOURCE/state.db" "$BACKUP_TARGET/state.db" <<'PY'
import os, sqlite3, sys

src_path, dest_path = sys.argv[1], sys.argv[2]
partial = dest_path + ".partial"
if os.path.exists(partial):
    os.remove(partial)
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(partial)
try:
    src.backup(dst)                       # consistent snapshot even while the brain is writing
    verdict = dst.execute("PRAGMA integrity_check").fetchone()[0]
    rows = dst.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
finally:
    dst.close()
    src.close()
if verdict != "ok":
    os.remove(partial)
    sys.exit(f"snapshot failed integrity_check: {verdict}")
os.replace(partial, dest_path)            # atomic: the target never holds a half-written state.db
print(f"--> state.db snapshot ok ({rows} chat rows)")
PY
fi

echo "--> Backup complete."
