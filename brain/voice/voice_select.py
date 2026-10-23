"""Seed-based voice selection for remote TTS engines that derive a voice
from a numeric seed. Voice precedence is sample > seed > engine default, so
an entry with a `voice_sample` is skipped here, as is any engine without
seed support.

The user curates seed pools per body type in `config.toml`
(`[voice.tts.remote.<name>.seed_voices]`). The companion picks one seed at
random from the pool matching its current look and persists it in
`agent_state.current_voice_id`, so the voice stays identical across
sentences and restarts. It is re-picked only when the stored seed is no
longer valid for the look -- i.e. a redesign moved the companion to a
different body type, or the config's pools were edited. While the look still
has the default femininity, the configured `default_seed` is used instead of
a random pick, so a fresh install starts on a known voice. Trait-based scoring
(docs/ADR_AUTONOMOUS_VOICE_SELECTION.md) is the V2+ replacement for the
random pick.

Evaluated lazily at the start of each utterance, so a change always lands
on an utterance boundary and never mid-sentence.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from brain.appearance.spec import LookSpec, ProportionsSpec
from brain.config import SeedVoices, Settings
from brain.personality.state import PersonalityStore

logger = logging.getLogger("deskmate.voice.select")

_ID_PREFIX = "remote:"
_ID_MARK = ":seed:"


@dataclass(frozen=True)
class VoiceSelection:
    instance: str  # [voice.tts.remote.<instance>] the seed belongs to
    seed: int

    @property
    def voice_id(self) -> str:
        return f"{_ID_PREFIX}{self.instance}{_ID_MARK}{self.seed}"


def parse_voice_id(voice_id: str | None) -> VoiceSelection | None:
    if not voice_id or not voice_id.startswith(_ID_PREFIX) or _ID_MARK not in voice_id:
        return None
    instance, _, seed = voice_id[len(_ID_PREFIX) :].partition(_ID_MARK)
    try:
        return VoiceSelection(instance, int(seed))
    except ValueError:
        return None


def voice_groups_for(look_spec: LookSpec) -> list[str]:
    """Seed-pool group names to try, most specific first. Gender comes from
    an explicit humanoid base template, otherwise from the femininity
    proportion (>= 0.5 reads female); `primitive_mechanical` additionally
    tries the matching robot pool first, falling back to the plain gendered
    pool when no robot seeds are configured."""
    template = look_spec.base_template
    if template == "standard_humanoid_male":
        gender = "male"
    elif template == "standard_humanoid_female":
        gender = "female"
    else:
        gender = "female" if look_spec.proportions.femininity >= 0.5 else "male"
    if template == "primitive_mechanical":
        return [f"{gender}_robot", gender]
    return [gender]


def _candidates(pools: SeedVoices, groups: list[str]) -> list[int]:
    for group in groups:
        seeds = getattr(pools, group)
        if seeds:
            return seeds
    return []


class SeedVoiceSelector:
    def __init__(self, settings: Settings, store: PersonalityStore, get_look_spec, *, rng: random.Random | None = None):
        self._settings = settings
        self._store = store
        self._get_look_spec = get_look_spec
        self._rng = rng or random.Random()

    def _instance(self):
        """The highest-priority enabled tts_serve instance that defines any
        seed voices, or None. Only active when remote TTS is the active
        backend -- with active_backend = "local" no seed is ever needed."""
        tts_config = self._settings.voice.tts
        if tts_config.active_backend != "remote":
            return None
        names = {id(entry): name for name, entry in tts_config.remote.items()}
        for entry in tts_config.enabled_remote_instances():
            # A voice_sample outranks a seed, so such an entry never gets one.
            if entry.provider_type == "tts_serve" and not entry.voice_sample and not entry.seed_voices.is_empty():
                return names[id(entry)], entry
        return None

    def current_voice_id(self) -> str | None:
        return self._store.get_agent_state("current_voice_id")

    def select(self) -> VoiceSelection | None:
        """Returns the seed to use for the next utterance (choosing and
        persisting one if needed), or None when no seed pool applies."""
        found = self._instance()
        if found is None:
            return None
        name, entry = found
        candidates = _candidates(entry.seed_voices, voice_groups_for(self._get_look_spec()))
        if not candidates:
            return None

        current_id = self._store.get_agent_state("current_voice_id")
        current = parse_voice_id(current_id)
        if current is not None and current.instance == name and current.seed in candidates:
            return current

        default_seed = entry.seed_voices.default_seed
        if (
            default_seed in candidates
            and self._get_look_spec().proportions.femininity == ProportionsSpec.model_fields["femininity"].default
        ):
            # Look is still uncustomized: start on the configured known-good voice.
            chosen = VoiceSelection(name, default_seed)
        else:
            chosen = VoiceSelection(name, self._rng.choice(candidates))
        if current_id:
            self._store.set_agent_state("previous_voice_id", current_id)
        self._store.set_agent_state("current_voice_id", chosen.voice_id)
        logger.info("selected voice %s (pool of %d)", chosen.voice_id, len(candidates))
        return chosen
