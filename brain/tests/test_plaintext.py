from brain.voice.plaintext import to_plain_speech


def test_strips_markdown_and_emoji_from_a_typical_reply():
    raw = "*Antenna flickers, then settles.*\n\nSystem check:\n\n1. **Identity:** Verified. 🧶\n2. Vibes only :)\n- goggles on ✨"
    assert to_plain_speech(raw) == "System check: Identity: Verified. Vibes only. goggles on."


def test_short_emphasis_keeps_the_word_but_long_stage_directions_go():
    assert to_plain_speech("That is a *huge* fan moment.") == "That is a huge fan moment."
    assert to_plain_speech("*Antenna flickers, then settles into a calm pulse.* Hi") == "Hi."


def test_links_code_and_headings():
    assert to_plain_speech("## Title\nSee [the docs](http://x.y) and `code`.") == "Title. See the docs and code."


def test_plain_text_is_untouched_and_identifiers_survive():
    assert to_plain_speech("I'm a blob in a hoodie, using snake_case at 3:30 pm.") == "I'm a blob in a hoodie, using snake_case at 3:30 pm."


def test_all_markup_collapses_to_empty():
    assert to_plain_speech("🧶✨") == ""


def test_vocal_cues_become_sounds_the_voice_can_make():
    assert to_plain_speech("*sigh* Fine, I will do it.") == "Ahhh... Fine, I will do it."
    assert to_plain_speech("That is funny. *laughs* Really.") == "That is funny. Ha ha ha. Really."
    assert to_plain_speech("*clears throat* Attention.") == "Ahem. Attention."
    assert to_plain_speech("*Sighs heavily and rubs eyes* Ok.") == "Ahhh... Ok."


def test_silent_actions_are_dropped_not_read_aloud():
    assert to_plain_speech("*waves a tiny hand* Hello there.") == "Hello there."
