"""Message catalogue for the companion's own lines (greetings, notices).
English is the source of truth and the fallback; a missing key or language
falls back to English and is logged once. `es-mx` entries override `es`,
which overrides nothing else: lookup goes exact tag, primary language, English.
Adding a language is adding one table here.
"""

from __future__ import annotations

import logging

from brain.language import normalize_language, primary_language

logger = logging.getLogger("deskmate.messages")

CATALOGUE: dict[str, dict[str, str | tuple[str, ...]]] = {
    "en": {
        "greet.return.welcome": "Welcome back! Good {part}.",
        "greet.return.hey": "Hey, you're back!",
        "greet.return.ready": "Good {part}! Ready when you are.",
        "greet.reveal": "Welcome back! While you were away, I decided to try out a new look. What do you think?",
        "part.morning": "morning",
        "part.afternoon": "afternoon",
        "part.evening": "evening",
        "lang.accented": "I don't have a native voice for {language} yet, so I'll speak it with an accent.",
        "lang.no_voice": "I can read and write {language}, but I don't have a voice for it yet.",
        "lang.pinned": "Okay, I'll talk to you in {language} from now on.",
        "lang.auto": "Okay, I'll follow whichever language you use.",
        "lang.unknown": "I don't know a language called {value!r}.",
        "lang.current": "Talking in {language}; pinned: {pinned}.",
        "stt.english_only": "My speech recognition is English-only, so I can't understand {language} by voice yet. You can still type to me.",
    },
    "es": {
        "greet.return.welcome": "¡Qué bueno verte de nuevo! Buen{part_suffix}.",
        "greet.return.hey": "¡Hola, ya volviste!",
        "greet.return.ready": "¡Buen{part_suffix}! Lista para cuando quieras.",
        "greet.reveal": "¡Bienvenido de vuelta! Mientras no estabas, decidí probar un nuevo look. ¿Qué te parece?",
        "part.morning": "os días",
        "part.afternoon": "as tardes",
        "part.evening": "as noches",
        "lang.accented": "Todavía no tengo una voz nativa para {language}, así que lo voy a hablar con acento.",
        "lang.no_voice": "Puedo leer y escribir en {language}, pero todavía no tengo voz para ese idioma.",
        "lang.pinned": "Listo, de ahora en adelante te hablo en {language}.",
        "lang.auto": "Listo, voy a seguir el idioma que uses.",
        "lang.unknown": "No conozco un idioma llamado {value!r}.",
        "lang.current": "Estoy hablando en {language}; fijado: {pinned}.",
        "stt.english_only": "Mi reconocimiento de voz solo entiende inglés, así que todavía no puedo entender {language} por voz. Pero puedes escribirme.",
    },
    "es-mx": {
        "greet.return.hey": "¡Ey, ya regresaste!",
        "greet.return.ready": "¡Buen{part_suffix}! Aquí ando, lista cuando quieras.",
    },
}


def has_language(language: str) -> bool:
    """Whether the catalogue has any entry for this language (or its primary)."""
    tag = normalize_language(language) or language
    return tag in CATALOGUE or primary_language(tag) in CATALOGUE


_warned: set[tuple[str, str]] = set()


def _lookup(language: str, key: str) -> str | None:
    tag = normalize_language(language) or language
    for candidate in (tag, primary_language(tag)):
        value = CATALOGUE.get(candidate, {}).get(key)
        if isinstance(value, str):
            return value
    return None


def message(lang: str, key: str, /, **values: object) -> str:
    """The line `key` in `language`, formatted with `values`."""
    template = _lookup(lang, key)
    if template is None:
        if (lang, key) not in _warned:
            _warned.add((lang, key))
            logger.warning(
                "no %r message for language %r; using English", key, lang
            )
        template = _lookup("en", key) or key
    # Day-part words carry their own plural ending in Spanish ("os días").
    if "{part_suffix}" in template and "part" in values:
        values = {
            **values,
            "part_suffix": _lookup(lang, f"part.{values['part']}") or "",
        }
    elif "part" in values:
        values = {
            **values,
            "part": _lookup(lang, f"part.{values['part']}") or values["part"],
        }
    return template.format(**values)
