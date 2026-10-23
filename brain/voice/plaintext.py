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


def _star_span(match: re.Match[str]) -> str:
    inner = match.group(1).strip()
    for pattern, sound in _VOCAL_CUES:
        if pattern.search(inner):
            return sound
    # One word is emphasis and keeps its word; anything longer is a silent
    # action with nothing to say aloud.
    return inner if len(inner.split()) == 1 else ""


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
