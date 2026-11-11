"""The daemon's shared state and how it is built from the settings."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from brain.config import (
    Settings,
)
from brain.evolution import AvatarEvolution
from brain.ipc.server import OverlayHub
from brain.llm_pool import LLMPool
from brain.personality.state import PersonalityStore
from brain.presence.desktop_bridge import DesktopPresenceBridge
from brain.presence.placement import (
    Placement,
    ScreenInfo,
    WindowState,
)
from brain.router import Router
from brain.skills.registry import SkillRegistry
from brain.skills.state import SkillStateStore
from brain.voice.lipsync import RhubarbLipSync
from brain.voice.stt import StreamingSTT
from brain.voice.tts import TTSPool
from brain.voice.voice_select import SeedVoiceSelector

logger = logging.getLogger("deskmate.main")


@dataclass
class AppState:
    settings: Settings
    store: PersonalityStore
    router: Router
    evolution: AvatarEvolution
    local_pool: LLMPool
    tts_pool: TTSPool
    streaming_stt: StreamingSTT
    lipsync: RhubarbLipSync
    voice_selector: SeedVoiceSelector
    hub: OverlayHub
    desktop_bridge: DesktopPresenceBridge | None
    skills: SkillRegistry
    skill_state: SkillStateStore
    interaction_active: bool = False
    # Last window/screen telemetry from the sensing adapter, kept so a
    # (re)connecting overlay can be placed immediately instead of waiting
    # for the next window change.
    last_presence: tuple[WindowState | None, list[ScreenInfo]] | None = None
    last_placement: Placement | None = None
    background_tasks: list[asyncio.Task] = field(default_factory=list)
    # (text, created_at) of a return greeting that found no overlay connected;
    # delivered when one connects, unless it has gone stale.
    pending_greeting: tuple[str, float] | None = None
