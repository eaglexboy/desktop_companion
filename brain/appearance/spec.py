"""The LookSpec schema: a constrained, parametric description of how the
agent wants to look. This is deliberately NOT "let the LLM write bpy code" —
arbitrary LLM-authored Blender Python is a code-execution risk worth its own
sandboxing pass. Instead, the LLM (or the user) picks from known catalogs
here; blender/generator.py is the only thing that actually touches bpy, and
it only ever builds what these enums allow.

The LookSpec JSON Schema is deliberately closed and parametric:
base_template, proportions, a 4-color palette, surface_material, wardrobe,
and 8 named accessory sockets -- no free-form body description.

Catalog-driven validation (`validate_against_catalogs`) lets this module
reject an invalid base_template/wardrobe/accessory choice *without invoking
Blender* at all. It deliberately does NOT check proportion support or
template/socket compatibility -- unsupported proportions/sockets are
harmless no-ops for whichever template ignores them, not validation errors.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

SurfaceMaterial = Literal["matte", "metallic", "glossy", "glass", "emissive_circuit"]
Outfit = Literal["none", "hoodie", "wizard_robe", "tshirt_jeans", "lab_coat", "vest", "armor_light"]
Footwear = Literal["none", "sneakers", "boots", "sandals"]

_SOCKET_NAMES: tuple[str, ...] = (
    "socket_head_top",
    "socket_eyes",
    "socket_neck",
    "socket_chest",
    "socket_waist",
    "socket_hand_R",
    "socket_hand_L",
    "socket_orbit",
)

CATALOGS_SEED_DIR = Path(__file__).resolve().parent / "catalogs_seed"


class _Strict(BaseModel):
    """additionalProperties: false at every level of the schema -- an
    unknown field is a loud error, not a silent drop."""

    model_config = ConfigDict(extra="forbid")


class ProportionsSpec(_Strict):
    muscularity: float = Field(default=0.5, ge=0.0, le=1.0)
    femininity: float = Field(default=0.5, ge=0.0, le=1.0)
    height: float = Field(default=1.0, ge=0.5, le=1.5)
    build: float = Field(default=1.0, ge=0.5, le=1.5)


class PaletteSpec(_Strict):
    primary: str = Field(pattern=_HEX_COLOR_RE)
    secondary: str = Field(pattern=_HEX_COLOR_RE)
    accent: str = Field(pattern=_HEX_COLOR_RE)
    emissive: str = Field(pattern=_HEX_COLOR_RE)


class WardrobeSpec(_Strict):
    outfit: Outfit = "none"
    footwear: Footwear = "none"


class AccessoriesSpec(_Strict):
    socket_head_top: str | None = None
    socket_eyes: str | None = None
    socket_neck: str | None = None
    socket_chest: str | None = None
    socket_waist: str | None = None
    socket_hand_R: str | None = None
    socket_hand_L: str | None = None
    socket_orbit: str | None = None

    def items(self) -> list[tuple[str, str | None]]:
        return [(name, getattr(self, name)) for name in _SOCKET_NAMES]


class LookSpec(_Strict):
    base_template: str
    has_face: bool
    seed: int = 42
    proportions: ProportionsSpec = Field(default_factory=ProportionsSpec)
    palette: PaletteSpec
    surface_material: SurfaceMaterial = "matte"
    wardrobe: WardrobeSpec
    accessories: AccessoriesSpec = Field(default_factory=AccessoriesSpec)


class CatalogError(ValueError):
    """Raised when a LookSpec references a base_template/wardrobe/accessory
    value that no catalog or on-disk custom template declares."""


def load_templates_catalog(catalogs_dir: Path) -> dict:
    path = catalogs_dir / "templates_catalog.json"
    if not path.is_file():
        path = CATALOGS_SEED_DIR / "templates_catalog.json"
    return json.loads(path.read_text())


def load_wardrobe_catalog(catalogs_dir: Path) -> dict:
    path = catalogs_dir / "wardrobe_catalog.json"
    if not path.is_file():
        path = CATALOGS_SEED_DIR / "wardrobe_catalog.json"
    return json.loads(path.read_text())


def ensure_catalogs_seeded(deploy_dir: Path) -> Path:
    """Copies the repo-committed seed catalogs into <deploy_dir>/assets/catalogs/
    the first time they're needed, so a fresh install works even before the
    install script runs. A no-op if the catalogs are already there -- never
    overwrites a deployment's own (possibly hand-edited) catalog files."""
    catalogs_dir = deploy_dir / "assets" / "catalogs"
    catalogs_dir.mkdir(parents=True, exist_ok=True)
    for name in ("templates_catalog.json", "wardrobe_catalog.json"):
        dest = catalogs_dir / name
        if not dest.is_file():
            dest.write_text((CATALOGS_SEED_DIR / name).read_text())
    return catalogs_dir


def _custom_template_exists(deploy_dir: Path, name: str) -> bool:
    custom_dir = deploy_dir / "assets" / "custom_templates"
    return any((custom_dir / f"{name}{ext}").is_file() for ext in (".glb", ".blend"))


def validate_against_catalogs(look_spec: LookSpec, deploy_dir: Path) -> None:
    """Rejects a base_template/wardrobe/accessory value that no catalog (or,
    for Tier 1, no on-disk custom template file) declares -- entirely
    without invoking Blender. Does not check proportion support or
    template/socket compatibility; see this module's docstring."""
    templates = load_templates_catalog(deploy_dir / "assets" / "catalogs")["templates"]
    wardrobe_catalog = load_wardrobe_catalog(deploy_dir / "assets" / "catalogs")

    base_template = look_spec.base_template
    if base_template.startswith("custom:"):
        custom_name = base_template[len("custom:") :]
        if not _custom_template_exists(deploy_dir, custom_name):
            raise CatalogError(
                f"custom base_template {base_template!r} has no file under "
                f"{deploy_dir / 'assets' / 'custom_templates'} (expected {custom_name}.glb or .blend)"
            )
    elif base_template not in templates:
        raise CatalogError(f"base_template {base_template!r} is not in templates_catalog.json: {sorted(templates)}")

    if look_spec.wardrobe.outfit not in wardrobe_catalog["outfits"]:
        raise CatalogError(f"wardrobe.outfit {look_spec.wardrobe.outfit!r} is not in wardrobe_catalog.json")
    if look_spec.wardrobe.footwear not in wardrobe_catalog["footwear"]:
        raise CatalogError(f"wardrobe.footwear {look_spec.wardrobe.footwear!r} is not in wardrobe_catalog.json")

    for socket_name, item in look_spec.accessories.items():
        if item is None:
            continue
        allowed = wardrobe_catalog["accessories"].get(socket_name, [])
        if item not in allowed:
            raise CatalogError(f"accessories.{socket_name} {item!r} is not in wardrobe_catalog.json: {allowed}")


def _deep_merge(base: dict, delta: dict) -> dict:
    result = dict(base)
    for key, value in delta.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def merge_delta(current: LookSpec, delta: dict) -> LookSpec:
    """Applies a (possibly partial/nested) delta dict against the active
    LookSpec, used by the delta-synthesis appearance-change flow -- the LLM
    emits only the fields it wants to change, not a full LookSpec."""
    merged = _deep_merge(current.model_dump(mode="json"), delta)
    return LookSpec.model_validate(merged)


def example_look_spec() -> LookSpec:
    """A ready-to-use spec for manual smoke tests -- mirrors examples/sample_look.json."""
    return LookSpec(
        base_template="primitive_blob",
        has_face=True,
        seed=42,
        palette=PaletteSpec(primary="#4A90D9", secondary="#FFFFFF", accent="#F5A623", emissive="#7FD1FF"),
        wardrobe=WardrobeSpec(),
    )


def look_summary(look_spec: LookSpec) -> str:
    """Builds the one-line `[Appearance: ...]` system-prompt injection, e.g.
    "[Appearance: standard_humanoid_female, wearing wizard_robe, glasses,
    pointy_hat, palette #1E1E2E/#F38BA8]". Pure LookSpec -> str; prompts.py
    deliberately knows nothing about LookSpec internals (see its own
    docstring), so this lives here instead."""
    parts = [look_spec.base_template]
    if look_spec.wardrobe.outfit != "none":
        parts.append(f"wearing {look_spec.wardrobe.outfit}")
    if look_spec.wardrobe.footwear != "none":
        parts.append(look_spec.wardrobe.footwear)
    accessories = [item for _socket, item in look_spec.accessories.items() if item is not None]
    parts.extend(accessories)
    parts.append(f"palette {look_spec.palette.primary}/{look_spec.palette.accent}")
    return f"[Appearance: {', '.join(parts)}]"
