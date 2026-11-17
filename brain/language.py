"""Language tags, offline language identification, and the per-turn language
resolver (docs/ADR_MULTI_LANGUAGE_CHAT.md, sections 2.1-2.2).

Tags are lower-case BCP 47: a primary subtag and an optional region joined
by a hyphen (`en`, `es`, `es-mx`). Input from config, engines or users in
other spellings (`es_MX`, `es-MX`, `spa`) is normalised here and nowhere else.
A region tag matches a plain tag of its primary language (`es-mx` is
spoken by an engine that speaks `es`), never the other way round.
"""

from __future__ import annotations

import re
import unicodedata
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

_T = TypeVar("_T")

DEFAULT_LANGUAGE = "en"
AUTO = "auto"

LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "es-mx": "Mexican Spanish",
    "de": "German",
    "fr": "French",
    "it": "Italian",
    "pt": "Portuguese",
    "pt-br": "Brazilian Portuguese",
    "ru": "Russian",
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
    "ar": "Arabic",
    "he": "Hebrew",
}

_THREE_LETTER = {
    "eng": "en",
    "spa": "es",
    "deu": "de",
    "ger": "de",
    "fra": "fr",
    "fre": "fr",
    "ita": "it",
    "por": "pt",
    "rus": "ru",
    "jpn": "ja",
    "zho": "zh",
    "chi": "zh",
    "kor": "ko",
    "ara": "ar",
    "heb": "he",
}
_FULL_NAMES = {
    name.lower(): tag for tag, name in LANGUAGE_NAMES.items() if "-" not in tag
}
RTL_LANGUAGES = frozenset({"ar", "he", "fa", "ur"})

_TAG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,4})?$")


def normalize_language(value: str | None) -> str | None:
    """`es_MX` / `es-MX` -> `es-mx`; `spa`, `Spanish` -> `es`. Anything that
    is not recognisably a language returns None. "zh-hans-cn" style tags are
    cut to primary + one subtag."""
    if not value:
        return None
    text = value.strip().lower().replace("_", "-")
    if text in _FULL_NAMES:
        return _FULL_NAMES[text]
    parts = text.split("-")
    primary = _THREE_LETTER.get(parts[0], parts[0])
    tag = primary
    if len(parts) > 1:
        # Skip a script subtag (4 letters), keep a region (2 letters/3 digits).
        region = next(
            (p for p in parts[1:] if len(p) == 2 or p.isdigit()), None
        )
        if region:
            tag = f"{primary}-{region}"
    return tag if _TAG_RE.match(tag) else None


def primary_language(tag: str) -> str:
    return tag.split("-", 1)[0]


def languages_match(wanted: str, offered: str) -> bool:
    """True if something offering `offered` can serve a turn in `wanted`.
    `*` offers everything. `es` serves `es-mx`; `es-mx` does not serve `es-ar`.
    """
    wanted_n = normalize_language(wanted) or wanted
    if offered == "*":
        return True
    offered_n = normalize_language(offered) or offered
    return wanted_n == offered_n or (
        "-" not in offered_n and primary_language(wanted_n) == offered_n
    )


def language_name(tag: str) -> str:
    tag = normalize_language(tag) or tag
    return (
        LANGUAGE_NAMES.get(tag)
        or LANGUAGE_NAMES.get(primary_language(tag))
        or tag
    )


NATIVE_NAMES: dict[str, str] = {
    "en": "English",
    "es": "español",
    "es-mx": "español de México",
    "de": "Deutsch",
    "fr": "français",
    "it": "italiano",
    "pt": "português",
    "pt-br": "português do Brasil",
    "ru": "русский",
    "ja": "日本語",
    "zh": "中文",
    "ko": "한국어",
    "ar": "العربية",
    "he": "עברית",
}


def native_name(tag: str) -> str:
    """The language's name in itself, for lines said in that language."""
    tag = normalize_language(tag) or tag
    return lookup_by_language(NATIVE_NAMES, tag) or language_name(tag)


def is_rtl(tag: str | None) -> bool:
    return bool(tag) and primary_language(tag or "") in RTL_LANGUAGES


def lookup_by_language(table: dict[str, _T], tag: str) -> _T | None:
    """Exact tag, then its primary language (`es-mx` falls back to `es`)."""
    tag = normalize_language(tag) or tag
    return table.get(tag) or table.get(primary_language(tag))


# --- identification -----------------------------------------------------------

Identification = list[tuple[str, float]]
Identifier = Callable[[str], Identification]

_SCRIPTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ja", re.compile(r"[぀-ヿ]")),  # kana: checked before Han
    ("ko", re.compile(r"[가-힯ᄀ-ᇿ]")),
    ("zh", re.compile(r"[一-鿿]")),
    ("ar", re.compile(r"[؀-ۿ]")),
    ("he", re.compile(r"[֐-׿]")),
    ("ru", re.compile(r"[Ѐ-ӿ]")),
)

# Small stop-word lists: enough to tell the Latin-script languages apart on a
# sentence, free of network calls and dependencies. A better detector can be
# plugged in through `LanguageResolver(identify=...)`.
_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        [
            "the",
            "and",
            "is",
            "are",
            "you",
            "your",
            "what",
            "how",
            "this",
            "that",
            "with",
            "for",
            "have",
            "not",
            "do",
            "does",
            "it",
            "i",
            "me",
            "my",
            "we",
            "can",
            "will",
            "would",
            "to",
            "of",
            "in",
            "on",
            "at",
            "was",
            "be",
            "but",
            "just",
            "please",
            "hello",
            "hi",
            "thanks",
        ]
    ),
    "es": frozenset(
        [
            "el",
            "la",
            "los",
            "las",
            "un",
            "una",
            "unos",
            "y",
            "es",
            "son",
            "tu",
            "tus",
            "que",
            "qué",
            "cómo",
            "como",
            "esto",
            "eso",
            "con",
            "para",
            "por",
            "no",
            "sí",
            "si",
            "yo",
            "me",
            "mi",
            "mis",
            "nos",
            "puedes",
            "puedo",
            "quiero",
            "pero",
            "muy",
            "bien",
            "hola",
            "gracias",
            "está",
            "estás",
            "estoy",
            "de",
            "en",
            "del",
            "al",
            "se",
            "lo",
            "le",
            "te",
            "ya",
            "más",
            "porque",
            "cuál",
            "dónde",
            "cuándo",
            "órale",
            "ándale",
            "ahorita",
            "güey",
            "chido",
            "padre",
            "mande",
        ]
    ),
    "de": frozenset(
        [
            "der",
            "die",
            "das",
            "und",
            "ist",
            "sind",
            "du",
            "dein",
            "was",
            "wie",
            "ich",
            "mir",
            "mein",
            "wir",
            "nicht",
            "mit",
            "für",
            "ein",
            "eine",
            "einen",
            "aber",
            "sehr",
            "bitte",
            "danke",
            "hallo",
            "ja",
            "nein",
            "auf",
            "zu",
            "von",
            "den",
            "dem",
        ]
    ),
    "fr": frozenset(
        [
            "le",
            "la",
            "les",
            "un",
            "une",
            "des",
            "et",
            "est",
            "sont",
            "tu",
            "ton",
            "ta",
            "que",
            "quoi",
            "comment",
            "je",
            "me",
            "mon",
            "nous",
            "pas",
            "avec",
            "pour",
            "mais",
            "très",
            "bonjour",
            "merci",
            "oui",
            "non",
            "dans",
            "de",
            "du",
            "au",
            "vous",
            "votre",
        ]
    ),
    "it": frozenset(
        [
            "il",
            "lo",
            "la",
            "gli",
            "le",
            "un",
            "una",
            "e",
            "è",
            "sono",
            "tu",
            "tuo",
            "che",
            "cosa",
            "come",
            "io",
            "mi",
            "mio",
            "noi",
            "non",
            "con",
            "per",
            "ma",
            "molto",
            "ciao",
            "grazie",
            "sì",
            "no",
            "di",
            "del",
            "nel",
        ]
    ),
    "pt": frozenset(
        [
            "o",
            "a",
            "os",
            "as",
            "um",
            "uma",
            "e",
            "é",
            "são",
            "você",
            "seu",
            "sua",
            "que",
            "como",
            "eu",
            "meu",
            "nós",
            "não",
            "com",
            "para",
            "mas",
            "muito",
            "olá",
            "obrigado",
            "obrigada",
            "sim",
            "de",
            "do",
            "da",
            "no",
            "na",
        ]
    ),
}
_ACCENT_HINTS = {
    "es": "ñ¿¡áéíóú",
    "de": "äöüß",
    "fr": "çèêëîôûœ",
    "pt": "ãõç",
    "it": "àèìòù",
}
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def identify_text(text: str) -> Identification:
    """The built-in `identify(text) -> [(language, confidence)]`. Best first;
    empty when the text carries no signal (digits, emoji, one ambiguous word).
    """
    letters = [c for c in text if unicodedata.category(c).startswith("L")]
    if not letters:
        return []
    for tag, pattern in _SCRIPTS:
        hits = sum(1 for c in letters if pattern.match(c))
        if hits / len(letters) >= 0.3:
            return [(tag, min(1.0, 0.6 + hits / len(letters) * 0.4))]
    words = [w.lower() for w in _WORD_RE.findall(text)]
    if not words:
        return []
    scores: dict[str, float] = {}
    lowered = text.lower()
    for tag, stop in _STOPWORDS.items():
        hits = sum(1 for w in words if w in stop)
        accents = sum(lowered.count(c) for c in _ACCENT_HINTS.get(tag, ""))
        score = hits + 0.7 * min(accents, 3)
        if score:
            scores[tag] = score
    if not scores:
        return []
    total = sum(scores.values())
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    # Confidence shrinks for very short text: one stop word is a weak signal.
    strength = min(1.0, len(words) / 4)
    return [(tag, round(s / total * strength, 3)) for tag, s in ranked]


# --- per-turn resolution (ADR 2.2) -------------------------------------------


# A single message this clearly in another language switches the turn language.
CLEAR_CONFIDENCE = 0.7


@dataclass
class _Observation:
    scores: dict[str, float]


class LanguageResolver:
    """Keeps a recency-weighted score per language over the last few user
    messages. The current language only changes when another language's score
    overtakes it by `margin`, so "ok" or "si" continues the established one.
    A pin (explicit user instruction) wins outright and is stored by the
    caller; this class only holds the detection state."""

    def __init__(
        self,
        default: str = DEFAULT_LANGUAGE,
        *,
        window: int = 5,
        margin: float = 0.25,
        decay: float = 0.5,
        identify: Identifier = identify_text,
        initial: str | None = None,
        known: Sequence[str] = (),
    ) -> None:
        self._known = [n for k in known if (n := normalize_language(k))]
        self.default = normalize_language(default) or DEFAULT_LANGUAGE
        self.current = normalize_language(initial) or self.default
        self._window: deque[_Observation] = deque(maxlen=max(1, window))
        self._margin = margin
        self._decay = decay
        self._identify = identify

    def observe(
        self,
        text: str,
        *,
        reported: tuple[str | None, float | None] = (None, None),
    ) -> str:
        """Fold one user message in and return the language to answer in.
        `reported` is a recognizer's own (language, confidence) for voice input.

        The identifier's confidence already shrinks for short text, so a one-word
        message ("ok", "si") barely counts. A clear message in another language
        (confidence >= `CLEAR_CONFIDENCE`) switches at once, because a four-word
        sentence in Spanish is not ambiguous just because the last two messages
        were English; weaker evidence has to build up over the window."""
        result = self._identify(text)
        scores: dict[str, float] = {}
        rep_lang = normalize_language(reported[0])
        if rep_lang and reported[1]:
            scores[rep_lang] = float(reported[1]) * min(
                1.0, len(text.split()) / 2
            )
        else:
            for tag, conf in result:
                # Detector tags are plain; keep the current regional variant.
                scores[tag] = scores.get(tag, 0.0) + conf
        if scores:
            self._window.append(_Observation(scores))
        self.current = self._decide(scores)
        return self.current

    def _totals(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        weight = 1.0
        for obs in reversed(self._window):
            for tag, score in obs.scores.items():
                key = self._family(tag)
                totals[key] = totals.get(key, 0.0) + score * weight
            weight *= self._decay
        return totals

    def _family(self, tag: str) -> str:
        """`es` detected while the turn language is `es-mx` is the same language."""
        return primary_language(tag)

    def _decide(self, newest: dict[str, float] | None = None) -> str:
        current_family = self._family(self.current)
        if newest:
            top, top_score = max(newest.items(), key=lambda kv: kv[1])
            if (
                self._family(top) != current_family
                and top_score >= CLEAR_CONFIDENCE
            ):
                return self._regional(self._family(top))
        totals = self._totals()
        if not totals:
            return self.current
        best, best_score = max(totals.items(), key=lambda kv: kv[1])
        if best == current_family:
            return self.current
        if best_score - totals.get(current_family, 0.0) >= self._margin:
            return self._regional(best)
        return self.current

    def _regional(self, family: str) -> str:
        """Prefer a configured regional tag (`es-mx`) over the bare family."""
        for tag in (self.default, *self._known):
            if primary_language(tag) == family:
                return tag
        return family


def pick_dominant(options: Sequence[tuple[str, float]], fallback: str) -> str:
    return max(options, key=lambda o: o[1])[0] if options else fallback


class TurnLanguage:
    """The turn language for the running brain: the pin (persisted), the
    detection state, and the last language spoken. Reads and writes go
    through the per-user state store (ADR_MULTI_USER: one store per user)."""

    PIN_KEY = "language_pin"
    LAST_KEY = "last_language"

    def __init__(self, config, store, emit: Callable[..., None] | None = None):
        self._store = store
        self._emit = emit or (lambda *a, **k: None)
        self.apply(config, initial=store.get_agent_state(self.LAST_KEY))

    def apply(self, config, *, initial: str | None = None) -> None:
        """(Re)load the settings: the character's profile and the detection
        parameters. Safe while running (the Admin app's Language settings page
        calls it on save); the language in use is kept."""
        keep = initial or getattr(
            getattr(self, "resolver", None), "current", None
        )
        self.default = config.home  # the home language is the auto fallback
        self.profile = LanguageProfile(config)
        self.resolver = LanguageResolver(
            config.home,
            window=config.window,
            margin=config.margin,
            known=config.enabled,
            initial=keep,
        )

    @property
    def pin(self) -> str | None:
        return normalize_language(self._store.get_agent_state(self.PIN_KEY))

    def set_pin(self, value: str | None) -> str | None:
        """`None`/"auto" clears the pin. Returns the stored tag."""
        tag = None if value in (None, "", AUTO) else normalize_language(value)
        if value not in (None, "", AUTO) and tag is None:
            raise ValueError(f"not a language: {value!r}")
        self._store.set_agent_state(self.PIN_KEY, tag)
        if tag:
            self._set_current(tag)
        return tag

    def stt_hint(self) -> str | None:
        """The language to tell a recognizer: only a pin is a hint."""
        return self.pin

    def resolve(
        self,
        text: str,
        reported: tuple[str | None, float | None] = (None, None),
    ) -> str:
        pinned = self.pin
        # Detection keeps learning while pinned, so unpinning starts informed.
        detected = self.resolver.observe(text, reported=reported)
        language = pinned or detected
        self._set_current(language)
        return language

    def prompt_notes(
        self, language: str, message: str | None = None
    ) -> list[str]:
        known = {
            primary_language(t)
            for t in (self.resolver.default, *self.resolver._known)
        }
        return self.profile.prompt_notes(language, message, known)

    def reply_reminder(self, language: str, message: str) -> str:
        """A short line for the end of the user's turn when the message mixes
        languages and the character answers in one: the system prompt alone is
        not enough for a small model, which tends to follow the opening words.
        """
        if self.profile.code_switching > 0:
            return ""
        known = {
            primary_language(t)
            for t in (self.resolver.default, *self.resolver._known)
        }
        if not mixes_languages(message, language, known=known):
            return ""
        name = language_name(language)
        return f"(The message above mixes languages. Write your whole reply in {name} only.)"

    def regional(self, tag: str) -> str:
        """A detected plain tag (`es`) as the configured regional one (`es-mx`)."""
        return (
            self.resolver._regional(primary_language(tag))
            if "-" not in tag
            else tag
        )

    def autonomous(self) -> str:
        """Greetings and idle chatter: the pin, else the last turn language."""
        return self.pin or self.resolver.current

    def _set_current(self, language: str) -> None:
        previous = self._store.get_agent_state(self.LAST_KEY)
        self.resolver.current = language
        if previous != language:
            self._store.set_agent_state(self.LAST_KEY, language)
            self._emit(
                "language.changed",
                {"from": previous or self.default, "to": language},
            )


# --- character: home language, fluency, code-switching (ADR 2.6) --------------


class LanguageProfile:
    """What the active character can do in each language. Built from
    `LanguageConfig`; a personality profile will supply the same values."""

    def __init__(self, config) -> None:
        self.home = config.home
        self._fluency = config.profile.fluency
        self._by_language = dict(config.profile.fluency_by_language)
        self.voice_languages = list(config.profile.voice_languages)
        self._threshold = config.native_threshold
        self._floor = config.wording_floor
        profile_cs = config.profile.code_switching
        self.code_switching = (
            config.code_switching if profile_cs is None else profile_cs
        )

    def describe(self) -> dict:
        """The character's language settings, for /health and the status page."""
        return {
            "home": self.home,
            "fluency": self._fluency,
            "fluency_by_language": dict(self._by_language),
            "native_threshold": self._threshold,
            "wording_floor": self._floor,
            "code_switching": self.code_switching,
            "voice_languages": list(self.voice_languages),
        }

    def is_home(self, language: str) -> bool:
        return primary_language(language) == primary_language(self.home)

    def fluency(self, language: str) -> int:
        """Home language is always 100; otherwise the per-language override,
        else the general level."""
        if self.is_home(language):
            return 100
        tag = normalize_language(language) or language
        for key in (tag, primary_language(tag)):
            if key in self._by_language:
                return self._by_language[key]
        return self._fluency

    def below_floor(self, language: str) -> bool:
        """True when the character answers mainly in its home language."""
        return self.fluency(language) < self._floor

    def mixes(self, language: str) -> bool:
        """Whether a reply in `language` may contain several languages, so
        speech must be split into spans."""
        return self.code_switching > 0 or self.below_floor(language)

    def accented(self, language: str) -> bool:
        """True when the character speaks `language` with the home accent."""
        return self.fluency(language) < self._threshold

    def prompt_notes(
        self,
        language: str,
        message: str | None = None,
        known: set[str] | None = None,
    ) -> list[str]:
        """English prompt lines for fluency and code-switching (ADR 2.4, 2.6).
        `message` is the user's text, so a mixed one can be answered in the
        language the companion resolved rather than the one it happens to open in.
        """
        notes: list[str] = []
        level = self.fluency(language)
        if level < self._floor:
            notes.append(
                f"You barely speak {language_name(language)}. Answer mainly in {language_name(self.home)}, "
                f"with the odd {language_name(language)} word. If the user asks you to translate or "
                "explain, do so properly."
            )
        elif level < self._threshold:
            notes.append(
                f"You are still learning {language_name(language)}: use simple vocabulary and make the "
                "occasional small slip, but never garble the meaning. If the user asks you to translate "
                "or explain, do so properly."
            )
        cs = self.code_switching
        if cs >= 50:
            notes.append(
                "Mix languages naturally, as the user does, and sometimes switch language yourself "
                "mid-reply when it feels natural."
            )
        elif cs > 0:
            notes.append(
                "If the user mixes languages, you may mix them the same way."
            )
        elif message and mixes_languages(message, language, known=known):
            notes.append(
                f"The user's message mixes languages. Its main language is {language_name(language)}: "
                f"answer entirely in {language_name(language)}, even if the message opens in another "
                "language. Do not mix languages in the reply."
            )
        else:
            notes.append(
                "Use one language per reply. If the user mixes languages, answer entirely in the "
                "dominant language of their message."
            )
        return notes


_CLAUSE_SPLIT = re.compile(
    r"(?<=[,;:—–])\s+|\s+(?=(?:but|and|y|pero|aber|mais)\s)", re.IGNORECASE
)
_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")


def split_language_spans(
    text: str,
    turn_language: str,
    *,
    by_phrase: bool = False,
    identify: Identifier = identify_text,
) -> list[tuple[str, str]]:
    """Splits a mixed reply into (text, language) spans (ADR 2.6.1): per
    sentence, and per phrase when `by_phrase`. A span too short or unclear to
    identify inherits the language before it (the turn language to start).
    Adjacent spans of one language are merged. The detector's plain tag
    (`es`) becomes the turn's regional tag (`es-mx`) when they match."""
    pieces: list[str] = []
    for sentence in _SENT_SPLIT.split(text.strip()):
        if not sentence:
            continue
        pieces.extend(
            p
            for p in (
                _CLAUSE_SPLIT.split(sentence) if by_phrase else [sentence]
            )
            if p
        )
    spans: list[tuple[str, str]] = []
    current = turn_language
    for piece in pieces:
        words = _WORD_RE.findall(piece)
        guess = identify(piece)
        language = current
        if guess and len(words) >= 3 and guess[0][1] >= 0.5:
            language = guess[0][0]
            if primary_language(language) == primary_language(turn_language):
                language = turn_language
        current = language
        if spans and spans[-1][1] == language:
            spans[-1] = (f"{spans[-1][0]} {piece}", language)
        else:
            spans.append((piece, language))
    return spans


def mixes_languages(
    text: str,
    language: str,
    *,
    known: set[str] | None = None,
    identify: Identifier = identify_text,
) -> bool:
    """True when part of `text` (a sentence or clause of two words or more) is
    clearly in a different (and, when `known` is given, configured) language
    from `language`. Looser than the reply
    splitter, which lets short pieces inherit: "Esta bien. Let's test it" mixes.
    """
    own = primary_language(language)
    for sentence in _SENT_SPLIT.split(text.strip()):
        for piece in _CLAUSE_SPLIT.split(sentence):
            if len(_WORD_RE.findall(piece)) < 2:
                continue
            guess = identify(piece)
            if not guess or guess[0][1] < 0.5:
                continue
            other = primary_language(guess[0][0])
            if other != own and (known is None or other in known):
                return True
    return False


def dominant_language(spans: Sequence[tuple[str, str]], fallback: str) -> str:
    """The language most of the text is in (by length)."""
    totals: dict[str, int] = {}
    for text, language in spans:
        totals[language] = totals.get(language, 0) + len(text)
    return max(totals, key=totals.__getitem__) if totals else fallback


# --- explicit language requests, read from the user's own words ----------------
# A backstop for a model that says "okay, switching to German" without sending
# the action (the same failure the look-change backstop covers). It reads only
# the user's message and only unmistakable instructions; a one-off translation
# ("how do you say cat in German?") never matches.

_REQUEST_NAMES = {
    **{
        name.lower(): tag
        for tag, name in LANGUAGE_NAMES.items()
        if "-" not in tag
    },
    "mexican spanish": "es-mx",
    "spanish (mexico)": "es-mx",
    "inglés": "en",
    "ingles": "en",
    "español": "es",
    "espanol": "es",
    "castellano": "es",
    "alemán": "de",
    "aleman": "de",
    "deutsch": "de",
    "francés": "fr",
    "frances": "fr",
    "italiano": "it",
    "portugués": "pt",
    "portugues": "pt",
    "ruso": "ru",
    "japonés": "ja",
    "japones": "ja",
    "chino": "zh",
    "coreano": "ko",
    "árabe": "ar",
    "arabe": "ar",
    "hebreo": "he",
    "español de méxico": "es-mx",
    "español mexicano": "es-mx",
}
_NAME_ALT = "|".join(
    sorted((re.escape(n) for n in _REQUEST_NAMES), key=len, reverse=True)
)
_PIN_PATTERNS = (
    re.compile(
        rf"\b(?:talk|speak|reply|respond|answer|write|chat)\s+(?:to me\s+|with me\s+)?in\s+({_NAME_ALT})\b"
    ),
    re.compile(
        rf"\b(?:from now on|always|only)\b[^.!?]{{0,30}}\bin\s+({_NAME_ALT})\b"
    ),
    re.compile(rf"\blet'?s\s+(?:talk|speak|chat)\s+in\s+({_NAME_ALT})\b"),
    re.compile(
        rf"\b(?:h[aá]bla(?:me)?|hablemos|responde(?:me)?|cont[eé]stame|escr[ií]beme)\s+en\s+({_NAME_ALT})\b"
    ),
)
_AUTO_PATTERNS = (
    re.compile(
        r"\b(?:detect|follow|match)\b[^.!?]{0,30}\b(?:my|the)?\s*language\b"
    ),
    re.compile(r"\blanguage\b[^.!?]{0,20}\bauto(?:matic(?:ally)?)?\b"),
    re.compile(r"\bauto(?:matic(?:ally)?)?\b[^.!?]{0,20}\blanguage\b"),
    re.compile(r"\b(?:detecta|sigue)\b[^.!?]{0,30}\bidioma\b"),
    re.compile(r"\bidioma\b[^.!?]{0,20}\bautom[aá]tic"),
)


def detect_language_request(text: str) -> str | None:
    """`"auto"`, a language tag, or None when the message is not a plain
    instruction to change (or release) the conversation language."""
    lowered = " ".join(text.lower().split())
    for pattern in _AUTO_PATTERNS:
        if pattern.search(lowered):
            return AUTO
    for pattern in _PIN_PATTERNS:
        match = pattern.search(lowered)
        if match:
            return _REQUEST_NAMES[match.group(1)]
    return None
