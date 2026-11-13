# Architecture Decision Record: Multi-Language Chat

* **Status:** Proposed.
* **Date:** 2026-11-12
* **Deciders:** Architecture & Core Engineering Team
* **Related:** [ADR_PERSONALITY_PROFILES.md](ADR_PERSONALITY_PROFILES.md) (voice override, profile format), [ADR_AUTONOMOUS_VOICE_SELECTION.md](ADR_AUTONOMOUS_VOICE_SELECTION.md) (voice pools), [ADR_MULTI_USER.md](ADR_MULTI_USER.md) (one brain and one state store per user), [SPEC_BRAIN.md](SPEC_BRAIN.md), [SPEC_OVERLAY.md](SPEC_OVERLAY.md)

---

## 0. Scope and terminology

This ADR is **engine-agnostic**. It defines what the companion must do to chat in several languages and the contract each pluggable component (speech recognition, language model, speech synthesis, lip-sync, text detection) must meet. It names no specific engine; any implementation that satisfies the contracts conforms. Concrete engines, models and configuration keys are implementation choices recorded elsewhere.

| Term | Meaning |
| --- | --- |
| **Turn language** | The language the companion replies in for one turn (section 2.2). |
| **Home language** | The native language of a personality (section 2.6). |
| **Speech engine** | Any component that turns text into audio, local or remote. |
| **Recognizer** | Any component that turns audio into text. |
| **Native speech** | Speech with the pronunciation of the language being spoken. |
| **Accented speech** | Speech in a language other than the speaker's home language, pronounced with the home language's phonetics. |

## 1. Context

The companion works in English by default and accidentally, not by design. Nothing forbids another language, but each stage of the chat and voice path has an unstated English assumption:

| Stage | Typical assumption today |
| --- | --- |
| Speech recognition | An English-only model is configured; callers never request a language and never receive one back |
| Reply generation | The system prompt never names a reply language, so the model drifts with the last message |
| Memory and retrieval | Keyword ranking tokenises on ASCII words, which fails for scripts without spaces and for accented characters |
| Speech synthesis | A language parameter may exist but is never sent; engines are assumed English; the fallback voice is one fixed model |
| Lip-sync | Some recognisers are language-independent, others English-only |
| Text clean-up for speech | Spoken stand-ins for vocal cues (sighs, laughs) are English words |
| Canned lines and UI | English literals |

The wanted behaviour: **the user can talk to the companion in their own language, by voice or text, and it answers in that language, aloud, in a voice that can pronounce it**, including switching mid-conversation, without hand-editing configuration.

Three forces shape the design:

1. The languages an installation can *speak* are bounded by its speech engines, not by the language model. "Supports Spanish" is a property of the installed pipeline, not a switch, and different installs will have one language, several, or many.
2. Engines differ structurally in how they handle language. Some run one language per running instance, some select a language per request, some infer it from the text. The design must route correctly across all three without assuming one.
3. The companion is a character ([ADR_PERSONALITY_PROFILES.md](ADR_PERSONALITY_PROFILES.md)). A character whose voice changes identity, or loses its accent, every time language changes is a regression. A character that speaks a foreign language *with an accent* is often exactly right.

## 2. Decision

Introduce a single **turn language**, resolved per turn, and thread it through every stage. The language model writes in it, the recognizer is told to listen for it, the speech engine is routed by it, and anything that cannot be done is reported plainly instead of failing silently.

### 2.1 Language identifiers

Languages are **BCP 47 primary subtags in lower case** (`en`, `es`, `de`, `ja`, `zh`), with an optional region only where a component requires it (`pt-br`). One helper normalises and compares tags. Components that use other codes (three-letter, full names) are adapted at their boundary; no other part of the system parses language tags.

### 2.2 Resolving the turn language

Resolution order, first hit wins:

1. **Explicit user instruction** ("talk to me in German", "go back to detecting my language"), handled by a companion tool `set_language(code | "auto")`. It is stored in the persistent per-user state and survives restarts. It is the only thing that *pins* a language. The state store is per user ([ADR_MULTI_USER.md](ADR_MULTI_USER.md)), so the pin is per user with no further mechanism; a conversation is one-to-one whatever number of accounts share the device.
2. **Detected language**, when the setting is `auto` (the default). For voice input the recognizer's own detection is used; for typed input a language-identification component is used (below).
3. **The language of the previous turn**, when detection is not confident enough to decide.
4. **A configured default language** (default `en`).

The result is attached to the turn, stored with the chat record (a nullable `language` field added by the same additive migration pattern the project uses; absent reads as the default), and passed to every later stage. Autonomous utterances (greetings, idle chatter) use the pinned language if set, otherwise the last turn language.

**Detection is context-driven, not per-message.** In `auto` mode the language is the one with the highest running score over a short window of recent user messages (recency-weighted), not the verdict on the latest message alone. Each message contributes its detected language weighted by detector confidence and by length, so a long, clear message moves the score a lot and a one-word message ("ok", "si") barely moves it. The current language is kept until another language's score clearly overtakes it, so a short or ambiguous message continues the established pattern. A fresh conversation starts from the previous session's language, then the default. Window size and overtake margin are configuration, tuned on real chat history.

**Language-identification contract.** The detector is an offline component with one operation: `identify(text) -> [(language, confidence)]`, fast enough to run on every message (milliseconds) and free of network calls. Which library or model satisfies it is an implementation choice.

### 2.3 Speech recognition

Contract for any recognizer used with this feature:

* It must accept an **optional language hint**. With a hint (pinned mode) it uses it, which is faster and avoids misdetection on short utterances. Without one it detects.
* It must return **`(text, language, confidence)`**; when a backend cannot report detection, `language` and `confidence` are absent and the system falls back to text-based identification (2.2).
* It must be a **multilingual** model for any non-English use. An English-only model cannot transcribe other languages; the system logs a warning, and tells the user once, when the turn language needs more than the configured model offers. English-only installs may keep an English-only model deliberately (it is usually faster and slightly more accurate for English).
* The default shipped configuration uses a multilingual model of the same size class.

### 2.4 Prompting the language model

The system prompt gains one instruction near the top, written in English regardless of the reply language (instructions are most reliable in the language models are tuned on):

> Reply in {language name}. If the user writes in another language, follow their language unless told otherwise. Keep the same personality, humour and warmth in that language; do not translate idioms literally.

The structural prompt text, trait phrasing, skills block and tool schemas stay in English. Translating them would multiply a maintained table by the number of languages for no clear gain. Existing output rules (plain speech, no Markdown, bracketed vocal cues) are kept verbatim.

Memory notes are stored in the language they were written in; reflection is told to write notes in the user's primary language so that user-tier notes read naturally when shown.

### 2.5 Speech synthesis

#### 2.5.1 Engine capability declaration

Every speech engine entry declares its language behaviour. This is the single mechanism that lets one design cover all engine architectures:

* `languages`: the languages the engine can speak without further configuration. For a one-language-per-instance engine this is the language its loaded model speaks. For a per-request engine it is the list it accepts. `["*"]` means it infers the language from the text and handles any.
* `selection`: how a language is chosen. One of
  * `fixed`: the running instance speaks one language; the request's language field, if any, is ignored by the engine.
  * `per_request`: the request's language field selects among `languages`.
  * `inferred`: the engine detects from the text.
* `foreign_text`: whether the engine will read text *outside* its `languages` using its own phonetics, rather than rejecting it or producing garbage. This is what makes accented speech possible (2.5.3). Default `false`; set `true` only after listening.
* `cloning`: whether it can speak in a voice given by a reference sample.

Absent declarations default to `languages = ["en"], selection = fixed, foreign_text = false, cloning = false`, so existing configurations behave exactly as today. A configuration with several instances of the same engine, one per language, simply has one entry per instance.

The language request field is **sent only to engines whose `selection` is `per_request`**. Sending it to others is harmless but meaningless and is not relied on.

#### 2.5.2 Native routing

For a turn in language L the pool tries, in priority order, the engines whose `languages` include L (or `*`). The request carries the personality's reference voice and parameters where the engine supports them (voice override per [ADR_PERSONALITY_PROFILES.md](ADR_PERSONALITY_PROFILES.md)). Zero-shot cloning engines keep the character's timbre across languages; engines that derive voices from seeds or presets are not guaranteed to, and the design accepts the drift rather than fighting it.

#### 2.5.3 Accented routing

Accented speech is a **routing decision, not a parameter**. For a turn in language L where the personality's home language is H (and fluency calls for an accent, section 2.6), the pool routes the unmodified L text to an engine that can speak H and has `foreign_text = true`, preferring one with `cloning` so the character keeps its voice. The engine reads the foreign text with H's phonetics, which is the accent. For a `per_request` engine the language field is set to H; for a `fixed` engine, whatever is requested is spoken by the H-model anyway. If no such engine exists, the pool falls back to native routing and logs a notice once.

**Accented routing is also the fallback when no native engine exists.** For any personality, including the fully fluent default, if no engine speaks L natively but an engine with `foreign_text = true` can speak the home language, the turn is spoken accented rather than skipped. The companion says once, in the user's language, that it has no native voice for L yet and will speak it with an accent. Intelligible accented speech beats silence; the user can then add a native engine when they want one.

#### 2.5.4 Cannot speak L

If no engine can produce intelligible speech for L, whether natively or by the accented route (including the accented fallback in 2.5.3), the reply is still shown and stored, speech is skipped, and the companion says once that it can read and write that language but has no voice for it. Mispronounced speech presented as correct is worse than none, so there is no silent fallback to the wrong language's phonetics. "Intelligible" is a property of the combination of engine and language, established by listening and recorded as `foreign_text` / `languages` in configuration.

#### 2.5.5 Fallback engines

A local fallback engine that is one voice per language (voice models tied to a language) takes a **map from language to voice** rather than a single voice. A missing language falls through to 2.5.4. The existing single voice setting is read as the entry for the default language.

#### 2.5.6 Vocal cues

Vocal cues in replies (a sigh, a laugh) are rewritten to a sound the engine can make. The rewriting table becomes keyed by language with the English table as the fallback. A language without a table drops the cue rather than speaking an English word inside foreign speech.

#### 2.5.7 Lip-sync

Viseme extraction must be language-independent. If an implementation offers a language-specific recogniser (for example one tuned for English only), it is used only when the turn language matches and the language-independent one is used otherwise.

### 2.6 Personality: home language, fluency and accent

A character has a **home language** and a **fluency** in every other language. This makes a character more interesting: a character from an English-speaking background that answers in Spanish with an American accent is expected, not a defect.

Profile fields ([ADR_PERSONALITY_PROFILES.md](ADR_PERSONALITY_PROFILES.md), next schema version; files without them load with the defaults below):

* `language.home`: native language (default: the configured default language). Also the `auto` fallback in 2.2 step 4, applied on load. A pinned language still wins, because pinning is an explicit user instruction.
* `language.fluency`: integer 0-100, the character's level in any language other than home. Default 100. Optional `language.fluency_by_language` overrides it per language.
* `voice.languages`: languages the stored voice is known to work for. Informational; pre-filters engines and appears on the status page.

**The default personality is fully fluent.** With no profile active, fluency is fixed at 100 and the home language is the configured default: it switches to the turn language and speaks it natively (2.5.2).

**What fluency does:**

1. **Native (fluency at or above the native threshold, default 80).** Native routing (2.5.2).
2. **Accented (below the threshold).** Accented routing (2.5.3): the text is in the turn language, spoken with the home language's accent.
3. **Wording.** The prompt (2.4) gains a proficiency hint scaled to fluency: nothing at high levels; at lower levels the model is told the character is still learning the language and should use simpler vocabulary and the occasional small slip, never garbling meaning. Below a floor (default 20) the character answers mainly in its home language with the odd word in the user's. Wording never overrides an explicit request to translate or explain.

Only the thresholds are discrete. The scalar exists so one field drives the voice tier, the wording hint and the fallback, and so a profile can say "mostly fluent" (70) or "just learning" (30) without new mechanisms. Gradual accent blending is a capability the contract does not assume engines have, so it is not promised; the tiers map onto what engines can actually do.

**Why a profile property rather than whatever the engine does.** Engines can produce an accent as a side effect, differently per engine and language. Making accent a profile decision, implemented by routing, makes it a character choice that is consistent across engines and settable by the user.

#### 2.6.1 Mixed-language messages (code-switching)

A scalar `language.code_switching` (0-100, profile field with a global default) controls whether the companion mixes languages the way the user does:

* **0 (default).** One language per reply. If the user mixes languages, the companion picks the dominant language of the message (ties go to the turn language from 2.2) and answers fully in it. Communication works, which is the goal.
* **Higher values.** The prompt tells the model it may mix languages naturally, mirroring the user's mix; at high values it initiates switches itself, within the limits of its fluency.
* **Speech.** A mixed reply is split into language spans (by sentence, and by phrase inside a sentence at higher values) using the language-identification component. Each span is synthesised on its own with its language and the routing rules above, and the audio is concatenated. Spans too short to identify inherit the surrounding language. A span no engine can speak is spoken in the nearest speakable language and logged.
* Memory, reflection and the stored `language` record the **dominant** language of the turn.

### 2.7 Memory and retrieval

* The user tier and self tier ([ADR_PERSONALITY_PROFILES.md](ADR_PERSONALITY_PROFILES.md)) are language-agnostic text; there is no translation at write time. A fact told in Spanish stays Spanish. Models read memory in any language, so a Spanish note is usable in an English turn.
* Keyword ranking tokenises per script: Latin and Cyrillic by Unicode word characters (not just ASCII), languages written without spaces by character n-grams. Cross-language retrieval ("what is my dog called" matching "mi perro se llama Luna") is **not** attempted; it needs embeddings, which this ADR does not introduce. Recency-based retrieval still surfaces recent notes in any language.

### 2.8 Canned lines and presentation

* The companion's own canned lines (greetings, status messages, errors shown to the user) move from literals to a message catalogue keyed by id, with English as source of truth and fallback. Missing keys fall back to English and are logged once. Adding a language is adding one catalogue.
* **Reference languages.** English and Spanish are the first two shipped: catalogues, vocal-cue tables and an end-to-end tested path for both, so the mechanism is proven. Further languages are contributions of one catalogue each plus whatever engines they need.
* Overlay and status-page chrome (labels, tab names) is out of scope. Chat itself is the goal; chrome can adopt the same catalogue later.
* **Text direction follows the reply's language.** A reply in a right-to-left language (Arabic, Hebrew, Persian, Urdu) renders right-to-left in the bubble and transcript, with alignment, tail position and flow mirrored for that bubble; left-to-right replies are unchanged. Direction is decided per bubble from the reply's language (the dominant language for mixed replies), and the toolkit's bidirectional support handles embedded opposite-direction words. The language travels with the reply event, so clients need no detection of their own. Clients need a font with fallback coverage for each supported script.

### 2.9 Interface

* **Companion tools:** `set_language(code | "auto")`, `get_language()`. Fluency, home language and code-switching are profile and configuration values, not companion tools.
* **Configuration:** a default language, the detection window and margin, the native-fluency threshold and wording floor, a global code-switching default, the engine capability declarations of 2.5.1, and a language-to-voice map for local fallback engines.
* **Health:** the health endpoint reports the pinned language (or `auto`) and the list of languages the installed pipeline can actually *speak*, computed from the engine declarations.
* **Events:** a `language.changed` event when the effective turn language changes, so the timeline shows switches.
* **Status page:** read-only. A language column on the chat view and a "speakable languages" line on the voice view. The user changes language by asking the companion.

### 2.10 Migration

With no configuration change behaviour is English as today, with deliberate exceptions: the shipped recognizer default becomes multilingual, the prompt gains a "reply in the turn language" instruction, and the chat record gains a nullable field. Engine entries without capability declarations are read as English-only, one-language-per-instance, no foreign text. Existing state needs only the additive field; old rows read as the default language.

## 3. Consequences

* A user can speak or type in a supported language and get a spoken reply in it, without editing configuration, provided the install has a speech engine for that language. Where it does not, they still get text and an honest explanation.
* The set of speakable languages is explicit and visible, instead of discovered by hearing a mangled sentence.
* Because capability is declared per engine, the same design works for installs with one language, several, or many, and for engines that run one instance per language, select per request, or infer from text. Adding a language is configuration, plus an engine entry when no existing one covers it.
* A character's accent is a profile property: the same sentence sounds native for one personality and accented for another. The default personality stays fully fluent.
* `foreign_text` is a human judgement made by listening, not something the system can verify. A wrong declaration produces bad speech; the declaration is documented as such.
* Automatic detection adds a small per-message cost for typed input and can misfire on very short text. The context window and the pin are the mitigations, and `set_language` is the escape hatch.
* Voice timbre across languages is engine-dependent. Cloning engines keep it reasonably; seed and preset voices may not.
* Mixed-language replies cost extra synthesis calls (one per span) and are off by default.
* Mixed-language memory is allowed, which keeps writes cheap but means keyword retrieval will not bridge languages.
* Changing the recognizer's return type touches every caller of it and the tests that fake it.

## 4. Alternatives considered

* **Translate everything to English internally.** Keeps prompts, memory and trait phrasing single-language, but adds two model calls per turn, loses tone and wordplay, breaks proper nouns, and doubles latency. Rejected.
* **Always reply in the language the model infers from the last message.** Flips on short or ambiguous messages and gives speech synthesis no signal to pick an engine. Rejected as the sole mechanism; kept as the fallback when detection is not confident.
* **Detection only, no pin.** Simpler, but a bilingual user cannot insist on a language. The pin costs one state key.
* **Pin only, no detection.** Predictable, but the user must announce every switch.
* **Assume every speech engine takes a language parameter.** Wrong for engines whose language is fixed at load time, where the parameter is ignored and the user would silently get the wrong phonetics. The capability declaration (2.5.1) replaces the assumption.
* **Express accent as an engine parameter.** Engines do not agree on whether or how; routing to an engine that speaks the home language works with any engine that tolerates foreign text.
* **Translate the system prompt per language.** More native-feeling instructions, but trait phrasing and the skills block would need a maintained translation per language, and current models follow English instructions to answer in other languages reliably. Revisit if a target language shows prompt-following problems.
* **Embeddings for cross-language memory retrieval.** The right long-term answer for retrieval, but it adds a model, a vector store and a backfill. Deferred; 2.7 stays compatible.
* **One personality per language.** Works with no code, but fragments the character's memory and identity along language lines. Profiles instead gain a home language and fluency.
* **Fall back to the default language's phonetics when no engine speaks L.** Always produces sound, but it is mispronunciation the user hears as a bug, unless it is the deliberate accented route (2.5.3), which is reached only through a personality's fluency or an engine declared to tolerate foreign text.

## 5. Open questions

1. **Threshold values.** The native threshold (80), the wording floor (20) and the proficiency-hint wording are starting values to tune by ear.
2. **Detection tuning.** Window length and overtake margin need measuring on real chat history.
3. **Span-level identification.** Reliability of identifying language inside a single sentence is unmeasured; the sentence-level split is the safe default for code-switching.
4. **Cross-language voice consistency.** For engines that do not clone, whether a per-language voice choice can be kept recognisably "the same character" is unmeasured.
5. **Fonts for non-Latin scripts.** Clients need fallback font coverage for each added script; verify per client stack when a script is added.
