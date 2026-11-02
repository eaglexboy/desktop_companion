# Architecture Decision Record: Keeping an Imported Look

* **Status:** Accepted. Implemented.
* **Date:** 2026-11-01
* **Related:** [SPEC_BLENDER.md](SPEC_BLENDER.md) (importing a model), [ADR_LOOK_HISTORY.md](ADR_LOOK_HISTORY.md), [DESIGN_AVATAR_EVOLUTION_FLOW.md](DESIGN_AVATAR_EVOLUTION_FLOW.md)

---

## 1. Context

Getting an authored model to import, animate and speak well is real work (cleaning its transforms, baking its pose, laying a face over a painted one, baking limb motion as morph targets). The appearance designer cannot add to it: it is a language model picking from catalogs, an imported model takes no wardrobe, and most requests it could act on would replace the model with a generated body. Two things can trigger a redesign: the user asking (`change_look`), and the companion's own reflection cycle. Left alone, either would quietly discard the imported model within a day.

## 2. Decision

1. **The look is locked while the current base template is an imported model** (`custom:<name>`). The lock is not a stored flag: it is derived from the current manifest entry and `agent_state.look_unlocked_for`, so it cannot disagree with what is actually worn, and a model that is installed by any route (the API, a request, a restored version) is locked from the moment it is current.
2. **While locked, the companion does not change its look.** The autonomous (reflection) redesign is skipped. A user request to change the look is declined in character, saying how to free it; nothing is built.
3. **The user frees it in words**, by saying the companion may change its look ("you can change your look if you want", "feel free to change your appearance"). This is recognised in the `change_appearance` handler itself, so it works whether the request arrives through the keyword router or as a `change_look` skill call chosen by the model, and there is also an explicit `unlock_look` skill / `unlock_appearance` tool. Unlocking records the model it was given for in `look_unlocked_for`.
4. **Or by asking for a generated look** ("go back to a generated look"): the companion restores its most recent look that was not an imported model (kept file: a pointer rewrite; pruned: rebuilt from its stored spec, see ADR_LOOK_HISTORY), which also ends the lock.
5. **An unlock covers only the model it was given for.** Moving to a different look forgets it, so the same model is locked again if it comes back, and a different imported model starts locked.
6. **Explicit, numbered, and API actions are not blocked.** "Go back to version N" and `POST /appearance/generate` are the user acting directly; whatever look they make current decides the lock.
7. **The status page's Looks tab shows it** (read-only, derived the same way from the manifest and `state.db`), so it is clear why a change request was declined.

## 3. Consequences

* The imported model survives indefinitely unless the user decides otherwise. The only way to lose it by accident is to ask for a numbered older look, which is an explicit act.
* The decline costs a turn of conversation; the reply names both ways out.
* A request that is *not* about the look is never affected. A user who wants "a pirate look" while locked must free it first (or ask for a generated look, then ask for the pirate one).
* The lock phrases are matched as text. A phrasing that does not match is treated as an ordinary change request and declined, which fails safe; the `unlock_look` skill covers a model that understands the intent but phrases the request differently.

## 4. Alternatives considered

* **A stored on/off flag** (as first sketched). It can drift from reality (a version restored or an API call changes the look without clearing it), and "unlock" would have to be remembered per model anyway.
* **A config setting.** The user wants to free it by talking to the companion, in the moment, not by editing a file.
* **Letting the designer change a locked look in small ways.** An imported model takes no wardrobe, and a palette tweak over a textured model does nothing visible, so there is nothing safe to offer.
