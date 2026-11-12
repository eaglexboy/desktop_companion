"""Throwaway builds for the Build page's Preview button
(docs/ADR_BUILD_YOUR_LOOK.md, decision 5).

A preview compiles a `LookSpec` in scratch with the same generator as a real
build, renders a picture of it, and throws the `.glb` away. Nothing is
published: no manifest entry, no version number, no change to the live look,
its files, `data/current/` or the agent's memory, and no progress event, so the
overlay never reloads for one. A preview takes the build lock like any build, so
it waits for one under way and a look change waits for it. Only one preview runs
at a time.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from brain.appearance import blender_gen
from brain.appearance.spec import LookSpec, look_summary
from brain.appearance.thumbnails import render_picture
from brain.config import Settings

logger = logging.getLogger("deskmate.appearance")

PreviewState = Literal[
    "queued", "building", "rendering", "ready", "failed", "cancelled"
]
_ACTIVE: tuple[PreviewState, ...] = ("queued", "building", "rendering")
# How many finished previews are kept (their pictures are small); older ones are dropped.
KEEP_FINISHED = 3


class PreviewBusy(RuntimeError):
    """A preview is already queued or running; one at a time."""


@dataclass
class Preview:
    id: str
    summary: str
    state: PreviewState = "queued"
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    picture: Path | None = None

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "state": self.state,
            "summary": self.summary,
            "error": self.error,
            "created_at": self.created_at,
            "has_picture": self.picture is not None and self.picture.is_file(),
        }


def previews_dir(settings: Settings) -> Path:
    return settings.blender_build_dir / "previews"


def clear_previews(settings: Settings) -> None:
    """Removes every preview picture and scratch build left on disk; run at brain
    start, since a preview never outlives the process that made it."""
    shutil.rmtree(previews_dir(settings), ignore_errors=True)
    for stale in settings.blender_build_dir.glob("preview-*"):
        shutil.rmtree(stale, ignore_errors=True)


class PreviewStore:
    """The previews of one brain. Not persistent: they die with the process."""

    def __init__(self) -> None:
        self._items: dict[str, Preview] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def get(self, preview_id: str) -> Preview | None:
        return self._items.get(preview_id)

    def active(self) -> Preview | None:
        return next(
            (p for p in self._items.values() if p.state in _ACTIVE), None
        )

    def start(self, settings: Settings, spec: LookSpec) -> Preview:
        """Queues a preview of `spec` (already validated against the catalogs).
        Raises `PreviewBusy` while another is queued or running."""
        if self.active() is not None:
            raise PreviewBusy("a preview is already being made")
        preview = Preview(id=uuid.uuid4().hex, summary=look_summary(spec))
        self._items[preview.id] = preview
        task = asyncio.create_task(self._run(settings, preview, spec))
        self._tasks[preview.id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(preview.id, None))
        return preview

    async def _run(
        self, settings: Settings, preview: Preview, spec: LookSpec
    ) -> None:
        build_dir = settings.blender_build_dir / f"preview-{preview.id}"
        try:
            async with blender_gen._build_lock:
                preview.state = "building"
                settings.ensure_dirs()
                build_dir.mkdir(parents=True, exist_ok=True)
                spec_path = build_dir / "spec.json"
                spec_path.write_text(spec.model_dump_json())
                out_glb = build_dir / "look.glb"
                build_json = await blender_gen._run_blender(
                    settings, spec_path, out_glb, build_dir
                )
                blender_gen._verify_build_output(
                    out_glb,
                    build_json,
                    build_json.get("has_face", spec.has_face),
                )
            # Outside the lock: the picture must not hold up a real build.
            preview.state = "rendering"
            target = previews_dir(settings) / f"{preview.id}.png"
            picture = await render_picture(
                settings, out_glb, target, f"preview {preview.id[:8]}"
            )
            if picture is None:
                preview.state = "failed"
                preview.error = "The picture could not be rendered."
            else:
                preview.picture = picture
                preview.state = "ready"
                self._forget_old(preview.id)
        except asyncio.CancelledError:
            preview.state = "cancelled"
            raise
        except blender_gen.GenerationError as exc:
            logger.warning("preview %s failed: %s", preview.id[:8], exc)
            preview.state = "failed"
            preview.error = str(exc)[:300]
        except Exception as exc:
            logger.exception("preview %s crashed", preview.id[:8])
            preview.state = "failed"
            preview.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)

    def _forget_old(self, keep_id: str) -> None:
        """Drops the pictures of all but the newest few finished previews."""
        finished = sorted(
            (p for p in self._items.values() if p.state not in _ACTIVE),
            key=lambda p: p.created_at,
            reverse=True,
        )
        for old in finished[KEEP_FINISHED:]:
            if old.id != keep_id:
                self.discard(old.id)

    def discard(self, preview_id: str) -> bool:
        """Cancels a preview if it is still running and deletes its picture.
        Returns False for an id that is not known."""
        preview = self._items.pop(preview_id, None)
        if preview is None:
            return False
        task = self._tasks.pop(preview_id, None)
        if preview.state == "building":
            # We hold the build lock, so the running Blender is ours. Stop it and let the
            # task finish on its own (cancelling the task instead would leave Blender running).
            blender_gen.cancel_running_build()
        elif task is not None and not task.done():
            task.cancel()
        if preview.picture is not None:
            preview.picture.unlink(missing_ok=True)
        return True

    def discard_all(self) -> None:
        for preview_id in list(self._items):
            self.discard(preview_id)
