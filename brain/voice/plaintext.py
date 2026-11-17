"""Turns a model reply into plain text fit to be spoken and shown in a bubble.

The companion talks out loud, so Markdown (asterisks, headings, bullets,
backticks) and emoji get read out as noise or dropped by the voice. The
system prompt asks for plain speech; this is the backstop for when the model
does it anyway. Applied once, in the chat path, so the spoken text, the
bubble, the stored chat and the chat_response event all agree.
"""

from __future__ import annotations

import re
from functools import partial

from brain.language import lookup_by_language

_CODE_FENCE = re.compile(r"```[^\n]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL = re.compile(r"https?://\S+")
# *single asterisks* are emphasis ("a *huge* fan"), a vocal cue the voice
# should make ("*sigh*"), or a silent stage direction ("*antenna flickers*").
# Neither TTS engine takes emotion or non-verbal tags (Pocket has no style
# controls; VoxCPM only a voice-design prefix, which cloning ignores), so a cue
# is rewritten as a sound the engine can actually say. Tune the table by ear.
_SINGLE_STAR = re.compile(r"(?<![*\w])\*(?!\*)([^*\n]{1,200}?)\*(?![*\w])")


def _cue_table(
    rows: tuple[tuple[str, str], ...],
) -> tuple[tuple[re.Pattern[str], str], ...]:
    return tuple((re.compile(p, re.IGNORECASE), sound) for p, sound in rows)


_VOCAL_CUES_EN = _cue_table(
    (
        (r"\bsigh", "Ahhh..."),
        (r"\bgasp", "Oh!"),
        (r"\bgiggl|\btitter", "Hee hee."),
        (r"\bchuckl|\bsnicker", "Heh heh."),
        (r"\blaugh|\bcackl|\bguffaw", "Ha ha ha."),
        (r"\bgroan|\bgrumbl|\bmutter|\bugh\b", "Ugh."),
        (r"\bhum\b|\bhums\b|\bhumming|\bhmm|\bponder", "Hmm."),
        (r"throat|\bcough|\bahem", "Ahem."),
        (r"\byawn", "Yaaawn."),
        (r"\bscoff|\bsnort|\bhmph", "Hmph."),
        (r"\bsniff", "Sniff."),
        (r"\bwhistl|\bwhew|\bphew", "Whew."),
    )
)
_VOCAL_CUES = _VOCAL_CUES_EN

# Spoken stand-ins per language (docs/ADR_MULTI_LANGUAGE_CHAT.md 2.5.6). The
# model may write the cue word in either language, so each table also matches
# the English word. A language with no table drops the cue rather than
# speaking an English sound inside foreign speech. `es-mx` uses `es`.
_VOCAL_CUES_ES = _cue_table(
    (
        (r"\bsuspir|\bsigh", "Ahhh..."),
        (r"\bjadea|\bgasp|\bsorprend", "¡Oh!"),
        (r"\bris(?:it|ill)|\bgiggl", "Ji ji."),
        (r"\bcarcajea|\bríe|\brie\b|\bríen|\blaugh|\bchuckl", "Ja ja ja."),
        (r"\bgru[ñn]e|\brefunfu|\bugh\b|\bgroan", "Ugh."),
        (r"\btarare|\bmmm|\bhmm|\bpiensa|\bponder", "Mmm."),
        (r"garganta|\btose|\btos\b|\bahem", "Ejem."),
        (r"\bbosteza|\byawn", "Aaaah."),
        (r"\bresopla|\bbufa|\bhmph", "Bah."),
        (r"\bsilba|\bwhew|\bphew|\buf\b", "Uf."),
    )
)
VOCAL_CUES: dict[str, tuple[tuple[re.Pattern[str], str], ...]] = {
    "en": _VOCAL_CUES_EN,
    "es": _VOCAL_CUES_ES,
}


# Verbs a model writes as a one-word silent stage direction ("*smiles*",
# "*nodding*"). Only their INFLECTED forms count as an action: "*look* at
# this" is emphasis on a base word, but "*looks*" can only be a stage
# direction. Vocal cues ("*laughs*", "*sighs*") are caught earlier and become
# sounds, so they are deliberately not here.
_ACTION_VERBS = frozenset(
    [
        "smile",
        "grin",
        "nod",
        "wink",
        "wave",
        "blush",
        "beam",
        "shrug",
        "smirk",
        "bow",
        "pause",
        "blink",
        "tilt",
        "lean",
        "nuzzle",
        "bounce",
        "wiggle",
        "twirl",
        "clap",
        "salute",
        "step",
        "turn",
        "look",
        "glance",
        "stare",
        "gaze",
        "frown",
        "pout",
        "grimace",
        "wince",
        "shudder",
        "shiver",
        "tremble",
        "fidget",
        "squint",
        "perk",
        "flicker",
        "glow",
        "pulse",
        "wag",
        "point",
        "gesture",
        "hug",
        "poke",
        "nudge",
        "tap",
        "snap",
        "spin",
        "hop",
        "jump",
        "dance",
        "sway",
        "stretch",
        "cheer",
        "curtsy",
        "kneel",
        "sit",
        "stand",
        "rise",
        "blink",
        "bob",
        "shake",
        "lift",
        "raise",
        "lower",
        "adjust",
        "straighten",
        "wring",
        "clasp",
        "fold",
        "cross",
        "uncross",
        "rub",
        "scratch",
        "tug",
        "fiddle",
        "tinker",
        "peek",
        "peer",
        "squeal",
        "skip",
        "wander",
        "float",
        "drift",
        "settle",
        "relax",
        "stiffen",
        "brighten",
        "dim",
        "sparkle",
        "twinkle",
        "light",
    ]
)
_ACTION_INFLECTIONS = ("s", "es", "ed", "d", "ing")


def _is_silent_action(word: str) -> bool:
    word = word.lower()
    for suffix in _ACTION_INFLECTIONS:
        if len(word) <= len(suffix) + 1 or not word.endswith(suffix):
            continue
        stem = word[: -len(suffix)]
        # smiles -> smile, nodded -> nod (doubled consonant), hopping -> hop,
        # waving -> wave (dropped e), curtsied -> curtsy.
        candidates = {
            stem,
            stem + "e",
            stem[:-1] if len(stem) > 2 and stem[-1] == stem[-2] else stem,
        }
        if stem.endswith("i"):
            candidates.add(stem[:-1] + "y")
        if suffix in ("es", "ed") and stem.endswith("i"):
            candidates.add(stem[:-1] + "y")
        if candidates & _ACTION_VERBS:
            return True
    return False


# Marks where a stage direction was removed, so the gap it leaves can be
# closed ("Sure thing *winks*." must not become "Sure thing .").
_DROPPED = "\x00"


def _star_span(match: re.Match[str], language: str = "en") -> str:
    inner = match.group(1).strip()
    table = lookup_by_language(VOCAL_CUES, language)
    for pattern, sound in table or ():
        if pattern.search(inner):
            return sound
    if table is None and _is_cue_like(inner):
        return _DROPPED
    words = inner.split()
    # Anything longer than a word is a silent action with nothing to say
    # aloud. One word is emphasis and keeps its word -- unless it is an
    # inflected action verb ("*smiles*"), which is a stage direction.
    if len(words) != 1 or _is_silent_action(words[0].strip(".,!?;:…")):
        return _DROPPED
    return inner


_BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_ITALIC_UNDERSCORE = re.compile(r"(?<![\w])_([^_\n]+)_(?![\w])")
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d{1,2}[.)])\s+", re.MULTILINE)
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
_TABLE_PIPES = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", re.MULTILINE)
_EMOTICON = re.compile(
    r"(?<!\w)[:;=8xX][-'^o]?[)(DPpOo3/\\|*$@]+(?!\w)|(?<!\w)<3(?!\w)|(?<!\w)\^_\^(?!\w)|(?<!\w)[oO]_[oO](?!\w)"
)
_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"  # pictographs, emoticons, transport, symbols, newer emoji
    "\U00002600-\U000027bf"  # misc symbols, dingbats
    "\U00002b00-\U00002bff"  # arrows, stars
    "\U0001f1e6-\U0001f1ff"  # flags
    "\U00002190-\U000021ff"
    "\U000025a0-\U000025ff"
    "\U00002300-\U000023ff"
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U0000200d\U000020e3\U000e0020-\U000e007f"
    "]+"
)
_SENTENCE_END = (".", "!", "?", ":", ";", ",")


def _is_cue_like(inner: str) -> bool:
    """With no cue table for the language, a starred English cue word still must
    not be spoken: it is dropped. Other starred words are emphasis."""
    return any(p.search(inner) for p, _ in _VOCAL_CUES_EN)


def to_plain_speech(text: str, language: str = "en") -> str:
    text = _CODE_FENCE.sub(lambda m: m.group(1), text)
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _BARE_URL.sub("", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _SINGLE_STAR.sub(partial(_star_span, language=language), text)
    text = re.sub(r"[ \t]*\x00(?=[ \t]*[.,!?;:…])", "", text).replace(
        _DROPPED, ""
    )
    text = _BOLD.sub(r"\2", text)
    text = _STRIKE.sub(r"\1", text)
    text = _ITALIC_UNDERSCORE.sub(r"\1", text)
    text = _RULE.sub("", text)
    text = _TABLE_PIPES.sub("", text)
    text = _HEADING.sub("", text)
    text = _BLOCKQUOTE.sub("", text)
    text = _BULLET.sub("", text)
    text = _EMOJI.sub("", text)
    text = _EMOTICON.sub("", text)
    text = text.replace("*", "").replace("|", " ")

    # One line per thought becomes one sentence per thought, so the voice
    # pauses where the list or paragraph broke instead of running on.
    pieces = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            continue
        pieces.append(line if line.endswith(_SENTENCE_END) else line + ".")
    return " ".join(pieces).strip()
