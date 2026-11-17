"""The Language settings page's backend (brain/language_settings.py and /language/*)."""

from __future__ import annotations

import json

import pytest

from brain import language_settings as ls
from brain.config import LanguageConfig, LanguageProfileConfig


def _doc(**over):
    doc = LanguageConfig().model_dump(mode="json")
    doc.update(over)
    return doc


def test_parse_accepts_a_full_document_and_normalizes_tags():
    cfg = ls.parse(
        _doc(
            enabled=["en", "es_MX"],
            profile={
                "home": "en",
                "fluency": 60,
                "fluency_by_language": {"es_MX": 30},
            },
        )
    )
    assert cfg.enabled == ["en", "es-mx"]
    assert cfg.profile.fluency_by_language == {"es-mx": 30}


@pytest.mark.parametrize(
    "bad,where",
    [
        (_doc(code_switching=150), "code_switching"),
        (_doc(default="nope!!"), "default"),
        (_doc(profile={"fluency": -1}), "profile.fluency"),
        (_doc(native_threshold=30, wording_floor=60), "wording_floor"),
        (_doc(window=0), "window"),
        (_doc(unknown_field=1), "unknown_field"),
    ],
)
def test_parse_rejects_bad_values_naming_the_field(bad, where):
    with pytest.raises(ls.LanguageSettingsError, match=where):
        ls.parse(bad)


def test_parse_rejects_non_objects():
    with pytest.raises(ls.LanguageSettingsError):
        ls.parse([1, 2])


def test_save_load_reset_round_trip(tmp_path):
    assert ls.load_saved(tmp_path) is None
    cfg = LanguageConfig(profile=LanguageProfileConfig(fluency=40))
    ls.save(tmp_path, cfg)
    saved = ls.load_saved(tmp_path)
    assert saved is not None and saved.profile.fluency == 40
    assert ls.reset(tmp_path) is True
    assert ls.load_saved(tmp_path) is None
    assert ls.reset(tmp_path) is False


def test_a_damaged_file_falls_back_to_config(tmp_path):
    ls.path_for(tmp_path).write_text("{not json")
    assert ls.load_saved(tmp_path) is None
    ls.path_for(tmp_path).write_text(json.dumps({"code_switching": 999}))
    assert ls.load_saved(tmp_path) is None


def test_failed_write_keeps_the_previous_file(tmp_path, monkeypatch):
    ls.save(tmp_path, LanguageConfig(code_switching=10))

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("brain.language_settings.os.replace", boom)
    with pytest.raises(OSError):
        ls.save(tmp_path, LanguageConfig(code_switching=90))
    kept = ls.load_saved(tmp_path)
    assert kept is not None and kept.code_switching == 10
    assert not [
        p
        for p in tmp_path.iterdir()
        if p.name.startswith(".language_settings.")
    ]
