"""deploy/backup_state.sh: consistent state.db snapshot, honours [storage] backup_dir, refuses a missing mount."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "backup_state.sh"


def _setup(tmp_path: Path, backup_dir: Path | None):
    deploy = tmp_path / "deploy"
    (deploy / "assets").mkdir(parents=True)
    (deploy / "assets" / "look.glb").write_bytes(b"glb")
    db = sqlite3.connect(deploy / "state.db")
    db.execute("CREATE TABLE interactions(id INTEGER PRIMARY KEY, text TEXT)")
    db.executemany("INSERT INTO interactions(text) VALUES (?)", [("a",), ("b",), ("c",)])
    db.commit()
    db.close()
    cfg = tmp_path / "config.toml"
    backup_line = f'backup_dir = "{backup_dir}"\n' if backup_dir else ""
    cfg.write_text(f'[storage]\nworkspace_dir = "{tmp_path}/ws"\ndeploy_dir = "{deploy}"\n{backup_line}')
    return cfg


def _run(cfg: Path, **env):
    import os

    return subprocess.run(
        [str(SCRIPT)], capture_output=True, text=True, timeout=60,
        env={**os.environ, "DESKMATE_CONFIG_FILE": str(cfg), "DESKMATE_BACKUP_DIR": "", **env},
    )


def test_backs_up_to_the_configured_dir_with_a_consistent_snapshot(tmp_path):
    target = tmp_path / "other" / "bk"
    (tmp_path / "other").mkdir()
    result = _run(_setup(tmp_path, target))
    assert result.returncode == 0, result.stderr
    assert (target / "assets" / "look.glb").read_bytes() == b"glb"
    snap = sqlite3.connect(target / "state.db")
    assert snap.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 3
    assert snap.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not (target / "state.db.partial").exists()


def test_refuses_when_the_mount_is_missing_instead_of_creating_it(tmp_path):
    target = tmp_path / "not_mounted" / "bk"
    result = _run(_setup(tmp_path, target))
    assert result.returncode != 0
    assert "not mounted" in result.stderr
    assert not (tmp_path / "not_mounted").exists()


def test_a_later_run_mirrors_removals_but_keeps_the_snapshot(tmp_path):
    target = tmp_path / "bk"
    cfg = _setup(tmp_path, target)
    assert _run(cfg).returncode == 0
    (tmp_path / "deploy" / "assets" / "look.glb").unlink()
    assert _run(cfg).returncode == 0
    assert not (target / "assets" / "look.glb").exists()
    assert (target / "state.db").is_file()  # excluded from --delete, then refreshed


@pytest.mark.skipif(not shutil.which("findmnt"), reason="needs findmnt to identify the disk")
def test_warns_when_target_shares_a_physical_disk_with_the_data(tmp_path):
    # tmp_path holds both the "deploy_dir" and the target, so they are necessarily on one disk.
    result = _run(_setup(tmp_path, tmp_path / "bk"))
    assert result.returncode == 0, result.stderr          # a warning, not a failure: the backup is still made
    assert "same physical disk" in result.stderr
