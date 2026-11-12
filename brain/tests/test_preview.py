"""Preview builds (brain/appearance/preview.py): throwaway, never published."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from brain.appearance import blender_gen
from brain.appearance import preview as preview_mod
from brain.appearance.preview import (
    PreviewBusy,
    PreviewStore,
    clear_previews,
    previews_dir,
)
from brain.appearance.spec import LookSpec, example_look_spec
from brain.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "storage": {
                "workspace_dir": str(tmp_path / "ws"),
                "deploy_dir": str(tmp_path / "deploy"),
            },
            "llm": {
                "local": {"base_url": "http://127.0.0.1:1/v1", "model": "m"}
            },
        }
    )


class Fake:
    """Stands in for Blender: records what ran, and can be held open or made to fail."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.gate.set()
        self.built: list[Path] = []
        self.fail: str | None = None
        self.picture_ok = True
        self.lock_held_during_build = False


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fake]:
    f = Fake()

    async def run_blender(
        settings: Settings, spec_path: Path, out_glb: Path, build_dir: Path
    ) -> dict[str, Any]:
        f.lock_held_during_build = blender_gen.is_build_running()
        await f.gate.wait()
        if f.fail:
            raise blender_gen.GenerationError(f.fail)
        out_glb.write_bytes(b"glTF")
        f.built.append(build_dir)
        return {"status": "ok"}

    async def render(
        settings: Settings, glb: Path, target: Path, label: str
    ) -> Path | None:
        if not f.picture_ok:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG")
        return target

    monkeypatch.setattr(blender_gen, "_run_blender", run_blender)
    monkeypatch.setattr(
        blender_gen, "_verify_build_output", lambda *a, **k: None
    )
    monkeypatch.setattr(preview_mod, "render_picture", render)
    yield f


async def _finished(store: PreviewStore, preview_id: str) -> dict[str, object]:
    for _ in range(200):
        info = store.get(preview_id)
        assert info is not None
        if info.state not in ("queued", "building", "rendering"):
            return info.public()
        await asyncio.sleep(0.01)
    raise AssertionError("preview did not finish")


def _spec() -> LookSpec:
    return example_look_spec()


async def test_a_preview_makes_a_picture_and_publishes_nothing(
    settings: Settings, fake: Fake
) -> None:
    settings.ensure_dirs()
    deploy_before = sorted(p for p in settings.deploy_dir.rglob("*"))
    store = PreviewStore()
    started = store.start(settings, _spec())
    info = await _finished(store, started.id)

    assert info["state"] == "ready" and info["has_picture"] is True
    assert fake.lock_held_during_build  # it takes the build lock like a build
    assert not blender_gen.is_build_running()  # and lets it go
    # the live look, its manifest and its files were not touched
    assert not (settings.deploy_dir / "manifest.json").exists()
    created = set(settings.deploy_dir.rglob("*")) - set(deploy_before)
    assert all(
        "previews" in str(p) or "blender_build" in str(p) for p in created
    )
    assert not list(settings.assets_dir.glob("*.glb"))
    assert not list(settings.current_dir.glob("*.glb"))
    # the scratch build is gone
    assert not list(settings.blender_build_dir.glob("preview-*"))


async def test_only_one_preview_runs_at_a_time(
    settings: Settings, fake: Fake
) -> None:
    fake.gate.clear()
    store = PreviewStore()
    first = store.start(settings, _spec())
    with pytest.raises(PreviewBusy):
        store.start(settings, _spec())
    fake.gate.set()
    await _finished(store, first.id)
    second = store.start(settings, _spec())  # free again
    await _finished(store, second.id)


async def test_a_failed_build_says_why_and_leaves_no_picture(
    settings: Settings, fake: Fake
) -> None:
    fake.fail = "blender build failed: boom"
    store = PreviewStore()
    started = store.start(settings, _spec())
    info = await _finished(store, started.id)
    assert info["state"] == "failed"
    assert "boom" in str(info["error"])
    assert info["has_picture"] is False
    assert not list(settings.blender_build_dir.glob("preview-*"))


async def test_a_picture_that_cannot_be_rendered_is_a_failure(
    settings: Settings, fake: Fake
) -> None:
    fake.picture_ok = False
    store = PreviewStore()
    info = await _finished(store, store.start(settings, _spec()).id)
    assert info["state"] == "failed" and info["error"]


async def test_discarding_a_finished_preview_deletes_its_picture(
    settings: Settings, fake: Fake
) -> None:
    store = PreviewStore()
    started = store.start(settings, _spec())
    await _finished(store, started.id)
    picture = previews_dir(settings) / f"{started.id}.png"
    assert picture.is_file()
    assert store.discard(started.id) is True
    assert not picture.exists()
    assert store.get(started.id) is None
    assert store.discard(started.id) is False


async def test_a_preview_waiting_for_a_build_can_be_discarded(
    settings: Settings, fake: Fake
) -> None:
    async with blender_gen._build_lock:  # a real build holds the lock
        store = PreviewStore()
        started = store.start(settings, _spec())
        await asyncio.sleep(0.02)
        queued = store.get(started.id)
        assert queued is not None and queued.state == "queued"
        store.discard(started.id)
    await asyncio.sleep(0.02)
    assert not fake.built  # it never built
    assert not blender_gen.is_build_running()


async def test_only_the_newest_few_pictures_are_kept(
    settings: Settings, fake: Fake
) -> None:
    store = PreviewStore()
    ids = []
    for _ in range(preview_mod.KEEP_FINISHED + 2):
        started = store.start(settings, _spec())
        await _finished(store, started.id)
        ids.append(started.id)
    pictures = sorted(p.stem for p in previews_dir(settings).glob("*.png"))
    assert len(pictures) == preview_mod.KEEP_FINISHED
    assert ids[-1] in pictures


def test_clearing_removes_leftovers(settings: Settings) -> None:
    settings.ensure_dirs()
    previews_dir(settings).mkdir(parents=True)
    (previews_dir(settings) / "old.png").write_bytes(b"x")
    stale = settings.blender_build_dir / "preview-abc"
    stale.mkdir()
    keep = settings.blender_build_dir / "look-1"
    keep.mkdir()
    clear_previews(settings)
    assert not previews_dir(settings).exists() and not stale.exists()
    assert keep.exists()  # only previews are cleared
