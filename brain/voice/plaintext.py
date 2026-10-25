"""Turns a model reply into plain text fit to be spoken and shown in a bubble.

The companion talks out loud, so Markdown (asterisks, headings, bullets,
backticks) and emoji get read out as noise or dropped by the voice. The
system prompt asks for plain speech; this is the backstop for when the model
does it anyway. Applied once, in the chat path, so the spoken text, the
bubble, the stored chat and the chat_response event all agree.
"""

from __future__ import annotations

import re

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
_VOCAL_CUES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), sound)
    for pattern, sound in (
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


# Verbs a model writes as a one-word silent stage direction ("*smiles*",
# "*nodding*"). Only their INFLECTED forms count as an action: "*look* at
# this" is emphasis on a base word, but "*looks*" can only be a stage
# direction. Vocal cues ("*laughs*", "*sighs*") are caught earlier and become
# sounds, so they are deliberately not here.
_ACTION_VERBS = frozenset(
    """smile grin nod wink wave blush beam shrug smirk bow pause blink tilt lean
    nuzzle bounce wiggle twirl clap salute step turn look glance stare gaze frown
    pout grimace wince shudder shiver tremble fidget squint perk flicker glow
    pulse wag point gesture hug poke nudge tap snap spin hop jump dance sway
    stretch cheer curtsy kneel sit stand rise blink bob shake lift raise lower
    adjust straighten wring clasp fold cross uncross rub scratch tug fiddle
    tinker peek peer squeal skip wander float drift settle relax stiffen
    brighten dim sparkle twinkle light""".split()
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
        candidates = {stem, stem + "e", stem[:-1] if len(stem) > 2 and stem[-1] == stem[-2] else stem}
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


def _star_span(match: re.Match[str]) -> str:
    inner = match.group(1).strip()
    for pattern, sound in _VOCAL_CUES:
        if pattern.search(inner):
            return sound
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
_EMOTICON = re.compile(r"(?<!\w)[:;=8xX][-'^o]?[)(DPpOo3/\\|*$@]+(?!\w)|(?<!\w)<3(?!\w)|(?<!\w)\^_\^(?!\w)|(?<!\w)[oO]_[oO](?!\w)")
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # pictographs, emoticons, transport, symbols, newer emoji
    "\U00002600-\U000027BF"  # misc symbols, dingbats
    "\U00002B00-\U00002BFF"  # arrows, stars
    "\U0001F1E6-\U0001F1FF"  # flags
    "\U00002190-\U000021FF"
    "\U000025A0-\U000025FF"
    "\U00002300-\U000023FF"
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D\U000020E3\U000E0020-\U000E007F"
    "]+"
)
_SENTENCE_END = (".", "!", "?", ":", ";", ",")


def to_plain_speech(text: str) -> str:
    text = _CODE_FENCE.sub(lambda m: m.group(1), text)
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _BARE_URL.sub("", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _SINGLE_STAR.sub(_star_span, text)
    text = re.sub(r"[ \t]*\x00(?=[ \t]*[.,!?;:…])", "", text).replace(_DROPPED, "")
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
