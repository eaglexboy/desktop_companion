"""The example animation packs the tests use (see conftest.py)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from blender import pack_profiles

DATA = Path(__file__).resolve().parent / "data" / "profiles"
EXAMPLE_PACKS = ("mixamo", "mesh2motion")


def example_profiles() -> list[dict]:
    """The shipped Quaternius profile, then the example packs, as profile dicts."""
    paths = [
        pack_profiles.PACKS_DIR / "quaternius.json",
        *(DATA / f"{n}.json" for n in EXAMPLE_PACKS),
    ]
    return [json.loads(p.read_text()) for p in paths]


def install_example_packs(deploy_dir: Path) -> None:
    """Puts the example packs where a user's profiles go in `deploy_dir`, for a test that runs
    Blender (which reads them from the deploy directory)."""
    target = deploy_dir.joinpath(*pack_profiles.USER_PROFILES_DIR)
    target.mkdir(parents=True, exist_ok=True)
    for name in EXAMPLE_PACKS:
        shutil.copy2(DATA / f"{name}.json", target / f"{name}.json")
