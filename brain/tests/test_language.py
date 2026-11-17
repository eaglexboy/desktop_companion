"""docs/ADR_MULTI_LANGUAGE_CHAT.md. `es_mx` (Mexican Spanish) is the second
language every end-to-end path is exercised with."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from brain.config import LanguageConfig, TTSLocalConfig
from brain.language import (
    LanguageResolver,
    TurnLanguage,
    identify_text,
    language_name,
    languages_match,
    normalize_language,
)
from brain.messages import message
from brain.personality.prompts import (
    build_system_prompt,
    rank_memory_notes,
)
from brain.personality.state import MemoryNote, PersonalityStore
from brain.skills.builtin import _auto_language, _get_language, _set_language
from brain.voice.plaintext import to_plain_speech
from brain.voice.tts import (
    SpeechUnavailable,
    TTSBackend,
    TTSCapabilities,
    TTSPool,
)
from brain.voice.tts_local import PiperTTSBackend

ES_MX = "Hola, ¿cómo estás? Quiero que me ayudes con esto, por favor."
EN = "Hello, how are you? I want you to help me with this, please."


# --- tags ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,tag",
    [
        ("es_MX", "es-mx"),
        ("es-MX", "es-mx"),
        ("es_mx", "es-mx"),
        ("ES", "es"),
        ("spa", "es"),
        ("Spanish", "es"),
        ("zh-Hans-CN", "zh-cn"),
        ("pt_BR", "pt-br"),
        ("", None),
        ("not a language", None),
    ],
)
def test_normalize_language(raw, tag):
    assert normalize_language(raw) == tag


def test_region_tag_matches_its_primary_language_not_the_reverse():
    assert languages_match("es-mx", "es")
    assert languages_match("es-mx", "es-mx")
    assert not languages_match("es-mx", "es-ar")
    assert not languages_match("es", "es-mx")
    assert languages_match("de", "*")
    assert not languages_match("es-mx", "en")


def test_language_name_falls_back_to_primary():
    assert language_name("es_MX") == "Mexican Spanish"
    assert language_name("es-ar") == "Spanish"


# --- identification and resolution ------------------------------------------


def test_identify_separates_spanish_and_english():
    assert identify_text(ES_MX)[0][0] == "es"
    assert identify_text(EN)[0][0] == "en"
    assert identify_text("こんにちは、元気ですか")[0][0] == "ja"
    assert identify_text("12345 !!!") == []


def test_auto_follows_language_but_short_messages_do_not_flip_it():
    r = LanguageResolver("en", known=["en", "es_MX"])
    assert r.observe(EN) == "en"
    # One Spanish message does not yet clearly outweigh the English history;
    # a second does, and the configured regional tag wins.
    r.observe(ES_MX)
    assert r.observe(ES_MX) == "es-mx"
    # "ok" carries no signal; the established language continues.
    assert r.observe("ok") == "es-mx"
    # One short English message does not flip back.
    assert r.observe("yes please") == "es-mx"


def test_recognizer_reported_language_is_used_for_voice_input():
    r = LanguageResolver("en", known=["es-mx"])
    assert r.observe("hola", reported=("es", 0.95)) in ("es", "es-mx")


class _Store:
    def __init__(self):
        self.d = {}

    def get_agent_state(self, k):
        return self.d.get(k)

    def set_agent_state(self, k, v):
        self.d[k] = v


def _turn(emitted=None):
    cfg = LanguageConfig(default="en", enabled=["en", "es_MX"])
    store = _Store()
    events = [] if emitted is None else emitted
    return (
        TurnLanguage(cfg, store, lambda t, p=None: events.append((t, p))),
        store,
    )


def test_pin_beats_detection_and_persists():
    turn, store = _turn()
    assert turn.set_pin("es_MX") == "es-mx"
    assert turn.resolve(EN) == "es-mx"
    assert turn.stt_hint() == "es-mx"
    assert store.d["language_pin"] == "es-mx"
    # A fresh process reads the same pin back from the store.
    again = TurnLanguage(LanguageConfig(), store)
    assert again.pin == "es-mx"
    assert again.set_pin("auto") is None
    assert again.stt_hint() is None


def test_unknown_language_pin_is_rejected():
    turn, _ = _turn()
    with pytest.raises(ValueError):
        turn.set_pin("klingon!!")


def test_language_changed_event_only_on_change():
    events: list = []
    turn, _ = _turn(events)
    turn.resolve(ES_MX)
    turn.resolve(ES_MX)
    changed = [p for t, p in events if t == "language.changed"]
    assert changed == [{"from": "en", "to": "es-mx"}]


async def test_set_and_get_language_skills():
    turn, _ = _turn()
    state = SimpleNamespace(language=turn)
    assert "Mexican Spanish" in await _set_language({"code": "es_MX"}, state)
    assert "Mexican Spanish" in await _get_language({}, state)
    assert "unknown" in await _set_language({"code": "??"}, state)
    assert "auto" in await _set_language({"code": "auto"}, state)
    await _set_language({"code": "de"}, state)
    assert turn.pin == "de"
    assert "auto" in await _auto_language({}, state)
    assert turn.pin is None


# --- prompt, memory, plain speech, messages -----------------------------------


def _prompt(language):
    return build_system_prompt(
        agent_name="Deskmate",
        traits={},
        mood={},
        memory_notes=[],
        language=language,
    )


def test_prompt_names_the_reply_language():
    assert "Reply in English." in _prompt("en")
    assert "Reply in Mexican Spanish." in _prompt("es_MX")


def test_keyword_ranking_handles_accents_and_unspaced_scripts():
    notes = [
        MemoryNote(1, 3.0, "le gusta el café"),
        MemoryNote(2, 2.0, "tiene un perro llamado Luna"),
        MemoryNote(3, 1.0, "好きな食べ物は寿司"),
    ]
    assert rank_memory_notes(notes, "quiero un café")[0].id == 1
    assert rank_memory_notes(notes, "寿司が食べたい")[0].id == 3


def test_vocal_cues_are_rewritten_per_language():
    assert "Ejem." in to_plain_speech("*se aclara la garganta* Bueno.", "es_MX")
    assert "Ja ja ja." in to_plain_speech("*ríe* Qué bueno.", "es-mx")
    # The English word inside a Spanish reply is still mapped to a Spanish sound.
    assert "Ahhh" in to_plain_speech("*sigh* Bueno.", "es-mx")
    assert "Ahhh" in to_plain_speech("*sigh* Fine.", "en")
    # A language without a table drops the cue instead of speaking English.
    out = to_plain_speech("*sigh* Gut.", "de")
    assert "Ahhh" not in out and "sigh" not in out


def test_messages_fall_back_es_mx_to_es_to_english():
    assert message("es_MX", "greet.return.hey") == "¡Ey, ya regresaste!"
    assert message("es-mx", "greet.reveal").startswith("¡Bienvenido")
    assert message("es-mx", "greet.return.welcome", part="morning") == (
        "¡Qué bueno verte de nuevo! Buenos días."
    )
    assert message("de", "greet.return.hey") == "Hey, you're back!"
    assert message("en", "greet.return.ready", part="evening") == (
        "Good evening! Ready when you are."
    )


# --- state --------------------------------------------------------------------


def test_chat_record_gains_a_nullable_language_column(tmp_path):
    db = tmp_path / "state.db"
    old = sqlite3.connect(db)
    old.executescript(
        "CREATE TABLE interactions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
        " role TEXT NOT NULL, text TEXT NOT NULL, provider TEXT,"
        " escalated INTEGER NOT NULL DEFAULT 0, reflected INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO interactions(ts, role, text) VALUES (1, 'user', 'old row');"
    )
    old.commit()
    old.close()
    store = PersonalityStore(db, (), ())
    store.log_interaction("user", "hola", language="es-mx")
    rows = store._conn.execute(
        "SELECT text, language FROM interactions ORDER BY id"
    ).fetchall()
    assert [(r["text"], r["language"]) for r in rows] == [
        ("old row", None),
        ("hola", "es-mx"),
    ]


# --- speech routing -----------------------------------------------------------


class _Backend(TTSBackend):
    def __init__(self):
        self.calls: list[dict] = []

    async def synthesize(
        self, text, out_wav_path, *, voice_id=None, params=None, language=None
    ):
        self.calls.append(
            {"text": text, "params": params, "language": language}
        )


EN_ONLY = TTSCapabilities()
ES_PER_REQUEST = TTSCapabilities(
    languages=("en", "es"), selection="per_request", cloning=True
)
EN_FOREIGN = TTSCapabilities(foreign_text=True, cloning=True)


def _pool(**caps):
    backends = {name: _Backend() for name in caps}
    pool = TTSPool([(n, backends[n]) for n in caps], None, capabilities=caps)
    return pool, backends


async def test_native_route_picks_engine_speaking_es_mx_and_sets_language(
    tmp_path,
):
    pool, b = _pool(english=EN_ONLY, multi=ES_PER_REQUEST)
    plan = pool.plan("es-mx")
    assert (plan.mode, plan.entries) == ("native", ("multi",))
    used = await pool.synthesize("Hola", tmp_path / "a.wav", language="es-mx")
    assert used == "multi"
    assert b["multi"].calls[0]["params"] == {"language": "es"}
    assert b["english"].calls == []


async def test_language_field_only_sent_to_per_request_engines(tmp_path):
    fixed_es = TTSCapabilities(languages=("es",), selection="fixed")
    pool, b = _pool(spanish=fixed_es)
    await pool.synthesize("Hola", tmp_path / "a.wav", language="es-mx")
    assert b["spanish"].calls[0]["params"] is None


def test_english_turn_routes_to_english_engine():
    pool, _ = _pool(english=EN_ONLY, multi=ES_PER_REQUEST)
    assert pool.plan("en").entries == ("english", "multi")


async def test_accented_fallback_uses_home_language_engine(tmp_path):
    pool, b = _pool(english=EN_FOREIGN)
    plan = pool.plan("es-mx")
    assert (plan.mode, plan.voice_language) == ("accented", "en")
    assert (
        await pool.synthesize("Hola", tmp_path / "a.wav", language="es-mx")
        == "english"
    )
    assert b["english"].calls[0]["text"] == "Hola"  # text is untouched


def test_no_native_and_no_foreign_text_means_speech_is_skipped():
    pool, _ = _pool(english=EN_ONLY)
    with pytest.raises(SpeechUnavailable):
        pool.plan("es-mx")


async def test_synthesize_raises_unavailable_not_a_wrong_language(tmp_path):
    pool, b = _pool(english=EN_ONLY)
    with pytest.raises(SpeechUnavailable):
        await pool.synthesize("Hola", tmp_path / "a.wav", language="es-mx")
    assert b["english"].calls == []


def test_speakable_languages_lists_declared_native_languages():
    pool, _ = _pool(english=EN_ONLY, multi=ES_PER_REQUEST)
    assert pool.speakable_languages() == ["en", "es"]


def test_untagged_calls_behave_as_before(tmp_path):
    pool, _ = _pool(english=EN_ONLY)
    import asyncio

    assert asyncio.run(pool.synthesize("Hi", tmp_path / "a.wav")) == "english"


# --- Piper voice map ----------------------------------------------------------


def _piper(tmp_path, models=None):
    for name in ("default.onnx", "es_MX-voice.onnx"):
        (tmp_path / name).write_bytes(b"")
    return PiperTTSBackend(
        TTSLocalConfig(
            voice_models=(
                {"es_MX": "es_MX-voice.onnx"} if models is None else models
            )
        ),
        tmp_path,
        default_language="en",
    )


def test_piper_uses_per_language_voice_map(tmp_path):
    piper = _piper(tmp_path)
    assert piper.languages == ("en", "es-mx")
    assert piper._resolve_voice_path(None, "es-mx").name == "es_MX-voice.onnx"
    assert piper._resolve_voice_path(None, "en").name == "default.onnx"
    assert piper._resolve_voice_path(None, None).name == "default.onnx"


def test_piper_primary_entry_serves_regional_turns(tmp_path):
    piper = _piper(tmp_path, {"es": "es_MX-voice.onnx"})
    assert piper._resolve_voice_path(None, "es-mx").name == "es_MX-voice.onnx"


def test_piper_with_no_voice_for_language_falls_through(tmp_path):
    from brain.voice.tts_local import PiperVoiceNotFound

    piper = _piper(tmp_path, {})
    with pytest.raises(PiperVoiceNotFound):
        piper._resolve_voice_path(None, "es-mx")


def test_pool_with_local_piper_speaks_only_its_mapped_languages(tmp_path):
    piper = _piper(tmp_path)
    pool = TTSPool([], piper, local_languages=piper.languages)
    assert pool.plan("es-mx").local
    with pytest.raises(SpeechUnavailable):
        pool.plan("de")


# --- config -------------------------------------------------------------------


def test_config_normalizes_language_tags():
    cfg = LanguageConfig(default="ES_mx", enabled=["en", "es_MX"])
    assert cfg.default == "es-mx"
    assert cfg.enabled == ["en", "es-mx"]
    with pytest.raises(ValueError):
        LanguageConfig(default="nope!!")


def test_remote_entry_capability_defaults_are_english_only():
    from brain.config import TTSRemoteEntry

    e = TTSRemoteEntry(base_url="http://x")
    assert (e.languages, e.selection, e.foreign_text, e.cloning) == (
        ["en"],
        "fixed",
        False,
        False,
    )
    e = TTSRemoteEntry(
        base_url="http://x", languages=["en", "es_MX"], selection="per_request"
    )
    assert e.languages == ["en", "es-mx"]


# --- character: home language, fluency, code-switching ------------------------

from brain.config import LanguageProfileConfig
from brain.language import (
    LanguageProfile,
    dominant_language,
    split_language_spans,
)


def _profile(**profile) -> LanguageProfile:
    return LanguageProfile(
        LanguageConfig(default="en", profile=LanguageProfileConfig(**profile))
    )


def test_default_character_is_fully_fluent_and_native():
    p = _profile()
    assert p.fluency("es-mx") == 100
    assert not p.accented("es-mx")
    notes = p.prompt_notes("es-mx")
    assert len(notes) == 1 and "one language per reply" in notes[0]


def test_fluency_tiers_and_per_language_override():
    p = _profile(fluency=70, fluency_by_language={"es": 30, "de": 95})
    assert p.is_home("en") and p.fluency("en") == 100
    assert p.fluency("es-mx") == 30  # `es` override serves the regional tag
    assert p.fluency("de") == 95 and not p.accented("de")
    assert p.fluency("fr") == 70 and p.accented("fr")


def test_wording_hint_scales_with_fluency():
    assert "still learning" in " ".join(
        _profile(fluency=60).prompt_notes("es-mx")
    )
    low = " ".join(_profile(fluency=10).prompt_notes("es-mx"))
    assert "barely speak Mexican Spanish" in low and "mainly in English" in low
    assert "learning" not in " ".join(
        _profile(fluency=90).prompt_notes("es-mx")
    )


def test_code_switching_levels_change_the_prompt():
    assert "mix them" in " ".join(
        _profile(code_switching=20).prompt_notes("es-mx")
    )
    assert "switch language yourself" in " ".join(
        _profile(code_switching=80).prompt_notes("es-mx")
    )
    # A profile value beats the global default.
    cfg = LanguageConfig(
        code_switching=60, profile=LanguageProfileConfig(code_switching=0)
    )
    assert LanguageProfile(cfg).code_switching == 0


def test_home_language_is_the_auto_fallback():
    cfg = LanguageConfig(
        default="en", profile=LanguageProfileConfig(home="es_MX")
    )
    turn = TurnLanguage(cfg, _Store())
    assert turn.resolver.default == "es-mx"
    assert turn.autonomous() == "es-mx"


def test_spans_split_a_mixed_reply_by_sentence_and_phrase():
    text = "Claro que sí, con mucho gusto te ayudo. I will check the weather for you, por favor espera un momento."
    sentences = split_language_spans(text, "es-mx")
    langs = [lang for _, lang in sentences]
    assert langs[0] == "es-mx" and "en" in langs
    phrases = split_language_spans(text, "es-mx", by_phrase=True)
    assert [lang for _, lang in phrases][-1] == "es-mx"
    assert len(phrases) >= len(sentences)


def test_short_span_inherits_and_adjacent_spans_merge():
    spans = split_language_spans(
        "Hola, ¿cómo estás hoy? Ok. Muy bien, gracias por todo.", "es-mx"
    )
    assert [lang for _, lang in spans] == ["es-mx"]


def test_dominant_language_is_by_length():
    spans = [("hola amigo mío", "es-mx"), ("ok", "en")]
    assert dominant_language(spans, "en") == "es-mx"
    assert dominant_language([], "en") == "en"


def test_profile_config_validation():
    with pytest.raises(ValueError):
        LanguageProfileConfig(fluency=120)
    with pytest.raises(ValueError):
        LanguageProfileConfig(fluency_by_language={"es": 500})
    assert LanguageProfileConfig(
        fluency_by_language={"es_MX": 40}
    ).fluency_by_language == {"es-mx": 40}


# --- accented routing by fluency ----------------------------------------------

EN_ACCENT_CLONER = TTSCapabilities(foreign_text=True, cloning=True)


async def test_fluency_accent_prefers_the_home_engine_even_when_native_exists(
    tmp_path,
):
    pool, b = _pool(
        english=EN_ACCENT_CLONER, spanish=TTSCapabilities(languages=("es",))
    )
    native = pool.plan("es-mx")
    assert (native.mode, native.entries) == ("native", ("spanish",))
    accent = pool.plan("es-mx", accented=True)
    assert (accent.mode, accent.reason, accent.entries) == (
        "accented",
        "fluency",
        ("english",),
    )
    assert (
        await pool.synthesize(
            "Hola", tmp_path / "a.wav", language="es-mx", accented=True
        )
        == "english"
    )
    assert b["spanish"].calls == []


def test_accent_without_a_foreign_text_engine_falls_back_to_native():
    pool, _ = _pool(english=EN_ONLY, spanish=TTSCapabilities(languages=("es",)))
    assert pool.plan("es-mx", accented=True).mode == "native"


def test_no_native_engine_fallback_is_marked_as_such():
    pool, _ = _pool(english=EN_ACCENT_CLONER)
    plan = pool.plan("es-mx")
    assert (plan.mode, plan.reason) == ("accented", "fallback")


# --- reflection writes notes in the user's language ---------------------------


async def test_reflection_asks_for_notes_in_the_users_language(tmp_path):
    from brain.config import MOOD_NAMES, TRAIT_NAMES
    from brain.personality.reflect import run_reflection

    store = PersonalityStore(tmp_path / "s.db", TRAIT_NAMES, MOOD_NAMES)
    store.log_interaction("user", "me gusta el café", language="es-mx")
    sent: list[str] = []

    class Pool:
        is_empty = False

        async def chat(self, messages, **kw):
            sent.append(messages[0]["content"])
            return "local", "{}"

    await run_reflection(store, Pool(), "es-mx")  # type: ignore[arg-type]
    assert len(sent) == 1
    assert "Write the memory notes in Mexican Spanish." in sent[0]
    sent.clear()
    store.log_interaction("user", "i like coffee", language="en")
    await run_reflection(store, Pool(), "en")  # type: ignore[arg-type]
    assert "Write the memory notes" not in sent[0]


# --- explicit language requests -------------------------------------------------

from brain.language import detect_language_request


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Please talk to me in German from now on.", "de"),
        ("Speak to me in Mexican Spanish", "es-mx"),
        ("let's chat in French", "fr"),
        ("From now on, answer only in Spanish.", "es"),
        ("Háblame en español de México, por favor", "es-mx"),
        ("responde en inglés", "en"),
        ("Go back to detecting my language automatically.", "auto"),
        ("Set your language back to auto.", "auto"),
        ("Detecta mi idioma otra vez", "auto"),
        ("How do you say cat in German?", None),
        ("I'm learning Spanish and German", None),
        ("What's the weather like?", None),
    ],
)
def test_detect_language_request(text, expected):
    assert detect_language_request(text) == expected


def test_profile_describe_reports_the_characters_language_settings():
    p = _profile(
        fluency=60, fluency_by_language={"es_MX": 30}, code_switching=20
    )
    assert p.describe() == {
        "home": "en",
        "fluency": 60,
        "fluency_by_language": {"es-mx": 30},
        "native_threshold": 80,
        "wording_floor": 20,
        "code_switching": 20,
        "voice_languages": [],
    }


def test_a_short_but_clear_message_switches_even_after_english_messages():
    """Seen live: "Como te sientes ahorita?" after two English messages stayed English."""
    r = LanguageResolver("en", known=["en", "es-mx"])
    assert r.observe("How are you systems functioning?") == "en"
    assert r.observe("Change to miss minutes model") == "en"
    assert r.observe("Como te sientes ahorita?") == "es-mx"


def test_ambiguous_one_word_messages_do_not_switch():
    r = LanguageResolver("en", known=["en", "es-mx"])
    r.observe("How are you systems functioning?")
    assert r.observe("si") == "en"
    assert r.observe("ok") == "en"
    assert r.observe("Gracias") == "en"


def test_mixed_message_is_answered_in_the_resolved_language_not_the_opening_one():
    from brain.language import mixes_languages

    known = {"en", "es"}
    mixed = "Esta bien. Let's test how well you can handle multi-language communications."
    assert mixes_languages(mixed, "en", known=known)
    notes = " ".join(_profile().prompt_notes("en", mixed, known))
    assert "mixes languages" in notes and "answer entirely in English" in notes
    assert "even if the message opens in another language" in notes
    # One language, or a stray word the detector guesses wrongly: the plain note.
    assert not mixes_languages("Let's test it.", "en", known=known)
    assert not mixes_languages(
        "A bueno. Y que crees que te pueda ayudar?", "es-mx", known=known
    )
    assert "one language per reply" in " ".join(
        _profile().prompt_notes("en", "Let's test it.", known)
    )
    # With code-switching on, the companion may mix back, so no such note.
    assert "mixes languages" not in " ".join(
        _profile(code_switching=80).prompt_notes("en", mixed, known)
    )


def test_mixed_message_gets_a_reminder_next_to_it_only_when_it_helps():
    turn, _ = _turn()
    mixed = "Okay, vamos a evaluar el system. Then we take the results and tweak things."
    assert "English only" in turn.reply_reminder("en", mixed)
    assert turn.reply_reminder("en", "Let's test it.") == ""
    assert (
        turn.reply_reminder("es-mx", "Y que crees que te pueda ayudar?") == ""
    )
