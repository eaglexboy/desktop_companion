"""What is particular to a model downloaded from Sketchfab, and the rules for cleaning it up.

Pure Python (no bpy), shared by `blender/sketchfab_convert.py`, which does the cleaning, and by
`brain.appearance.models`, which recognises such a file, records who made it and under what
licence, and says what that licence asks of the user.

A Sketchfab .glb is the model its author uploaded, re-exported by Sketchfab: the same meshes
and skeleton, but wrapped in a few transform nodes (the author's centimetre scale among them),
with every node name made unique by a numeric suffix (`CC_Base_Head` becomes `CC_Base_Head_038`),
and Blender's importer adds a helper bone (`<name>_scaleCompensation`) above each bone whose node
has a scale. The file says where it came from in `asset.extras`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GENERATOR_PREFIX = "Sketchfab"
HELPER_BONE_SUFFIX = "_scaleCompensation"
# The number Sketchfab appends to make a node name unique: `_038`, `_09`, `_00`.
_UNIQUE_SUFFIX = re.compile(r"_\d{2,3}$")


def is_sketchfab(gltf: dict) -> bool:
    """True for a glTF (its parsed JSON) that Sketchfab exported."""
    return str(gltf.get("asset", {}).get("generator", "")).startswith(GENERATOR_PREFIX)


def is_helper_bone(name: str) -> bool:
    """A bone the importer added to carry a node's scale (`..._scaleCompensation`), or the joint
    Sketchfab puts above the skeleton (`_rootJoint`): neither is part of the model's own skeleton.
    """
    return name.endswith(HELPER_BONE_SUFFIX) or name == "_rootJoint"


def clean_name(name: str) -> str:
    """The name the author gave: Sketchfab's uniqueness suffix and the importer's helper suffix
    taken off (`CC_Base_L_Hand_055_scaleCompensation` -> `CC_Base_L_Hand`)."""
    name = name.removesuffix(HELPER_BONE_SUFFIX)
    return _UNIQUE_SUFFIX.sub("", name, count=1)


def clean_names(names: list[str]) -> dict[str, str]:
    """{old name: new name} for the bones of one skeleton: each name cleaned, unless that would
    make two bones share one name (then both keep the Sketchfab name, which is unique).
    """
    wanted = {name: clean_name(name) for name in names}
    count: dict[str, int] = {}
    for new in wanted.values():
        count[new] = count.get(new, 0) + 1
    taken = set(names)
    out: dict[str, str] = {}
    for old, new in wanted.items():
        if new == old or count[new] > 1 or (new in taken and new != old):
            out[old] = old
        else:
            out[old] = new
    return out


@dataclass(frozen=True)
class Licence:
    """What a Creative Commons licence on a model asks of the person using it."""

    id: str  # `CC-BY-4.0`
    attribution: bool  # the author must be credited
    non_commercial: bool  # not for commercial use
    no_derivatives: (
        bool  # modified versions may not be shared (we always modify: shrink, rebind)
    )
    share_alike: bool  # modified versions must carry the same licence
    free: bool  # a licence we know; anything else is the author's own terms

    def notes(self) -> list[str]:
        """What to tell the user, in a line each."""
        notes = []
        if not self.free:
            notes.append(
                f"licence {self.id or 'unknown'} is not a Creative Commons one: read the author's terms before using it"
            )
            return notes
        if self.attribution:
            notes.append("credit the author wherever the model is shown or shared")
        if self.non_commercial:
            notes.append("non-commercial use only")
        if self.no_derivatives:
            notes.append(
                "no derivatives: the model is shrunk and re-rigged here, so keep it for your own use and do not share it"
            )
        if self.share_alike:
            notes.append("anything made from it must carry the same licence")
        return notes


_KNOWN = re.compile(r"^(CC0|CC-BY(-NC)?(-SA|-ND)?)(-\d(\.\d)?)?$", re.IGNORECASE)


def parse_licence(text: str) -> Licence:
    """The licence in a Sketchfab `asset.extras.license` string (`CC-BY-4.0 (http://...)`)."""
    token = (text or "").strip().split(" ")[0].strip()
    if not _KNOWN.match(token):
        return Licence(token, False, False, False, False, False)
    upper = token.upper()
    if upper.startswith("CC0"):
        return Licence(token, False, False, False, False, True)
    return Licence(
        token,
        attribution=True,
        non_commercial="-NC" in upper,
        no_derivatives="-ND" in upper,
        share_alike="-SA" in upper,
        free=True,
    )


def credits(gltf: dict) -> dict:
    """Who made a Sketchfab model and under what licence, for the `.credits.json` kept beside it."""
    extras = gltf.get("asset", {}).get("extras", {}) or {}
    licence = parse_licence(str(extras.get("license", "")))
    return {
        "title": extras.get("title", ""),
        "author": extras.get("author", ""),
        "source": extras.get("source", ""),
        "license": extras.get("license", ""),
        "attribution_required": licence.attribution,
        "non_commercial": licence.non_commercial,
        "no_derivatives": licence.no_derivatives,
        "share_alike": licence.share_alike,
        "notes": licence.notes(),
    }
