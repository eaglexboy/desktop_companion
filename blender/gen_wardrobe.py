"""
Procedural accessories for a body's sockets: eyewear, headwear, neckwear,
chestwear, waistwear, handhelds and orbiting items.
"""

from __future__ import annotations

import math

from blender.gen_garments import (
    _BODY,
    _DARK,
    _body_half_width_at,
    _body_surface_y,
    _darker,
    _prim,
)
from blender.gen_helpers import (
    hex_to_rgba,
    make_material,
)

_SOCKET_LOCATIONS = {
    "socket_head_top": lambda head_loc: (
        head_loc[0],
        head_loc[1],
        head_loc[2] + 0.65,
    ),
    "socket_eyes": lambda head_loc: (
        head_loc[0],
        head_loc[1] - 0.5,
        head_loc[2] + 0.05,
    ),
    "socket_neck": lambda head_loc: (
        head_loc[0],
        head_loc[1],
        head_loc[2] - 0.35,
    ),
    "socket_chest": lambda _: (0, 0, 1.1),
    "socket_waist": lambda _: (0, 0, 0.75),
    "socket_hand_R": lambda _: (_body_half_width_at(0.9) + 0.2, -0.2, 0.9),
    "socket_hand_L": lambda _: (-(_body_half_width_at(0.9) + 0.2), -0.2, 0.9),
    "socket_orbit": lambda head_loc: (
        head_loc[0],
        head_loc[1],
        head_loc[2] + 0.65,
    ),
}


_HEAD_R = 0.55  # build_head's radius


def _eyewear(item: str, loc, palette: dict):
    """Everything faces -Y: rings/discs are rotated so their axis points at the
    viewer (a torus is built with its axis on Z, which seen from the front is
    a flat line across the eyes)."""
    x0, y0, z0 = loc
    y0 -= 0.04  # just in front of the eyeballs
    face = (math.pi / 2, 0.0, 0.0)
    accent = make_material(
        f"EyewearFrame_{item}", hex_to_rgba(palette["accent"])
    )
    glow = make_material(
        f"EyewearLens_{item}",
        hex_to_rgba(palette["emissive"]),
        material="glossy",
        emissive=True,
    )
    dark = make_material(f"EyewearDark_{item}", _DARK, material="glossy")
    parts = []
    if item == "glasses":
        for x in (-0.22, 0.22):
            parts.append(
                (
                    _prim(
                        "torus",
                        (x0 + x, y0, z0),
                        rotation=face,
                        major=0.13,
                        minor=0.014,
                    ),
                    accent,
                )
            )
        parts.append(
            (
                _prim(
                    "cylinder",
                    (x0, y0, z0 + 0.02),
                    r=0.012,
                    depth=0.1,
                    rotation=(0, math.pi / 2, 0),
                ),
                accent,
            )
        )
    elif item == "sunglasses":
        for x in (-0.22, 0.22):
            parts.append(
                (
                    _prim(
                        "cylinder",
                        (x0 + x, y0, z0),
                        r=0.15,
                        depth=0.02,
                        rotation=face,
                    ),
                    dark,
                )
            )
            parts.append(
                (
                    _prim(
                        "torus",
                        (x0 + x, y0, z0),
                        rotation=face,
                        major=0.15,
                        minor=0.015,
                    ),
                    accent,
                )
            )
        parts.append(
            (
                _prim(
                    "cylinder",
                    (x0, y0, z0 + 0.03),
                    r=0.012,
                    depth=0.1,
                    rotation=(0, math.pi / 2, 0),
                ),
                accent,
            )
        )
    elif item == "goggles":
        for x in (-0.22, 0.22):
            parts.append(
                (
                    _prim(
                        "cylinder",
                        (x0 + x, y0 - 0.005, z0),
                        r=0.15,
                        depth=0.03,
                        rotation=face,
                    ),
                    glow,
                )
            )
            parts.append(
                (
                    _prim(
                        "torus",
                        (x0 + x, y0, z0),
                        rotation=face,
                        major=0.16,
                        minor=0.035,
                    ),
                    accent,
                )
            )
        parts.append(
            (_prim("cube", (x0, y0, z0), scale=(0.1, 0.05, 0.06)), accent)
        )
        # Strap round the head at eye height (a torus, axis up, is a ring seen from above).
        parts.append(
            (
                _prim(
                    "torus",
                    (x0, y0 + 0.5, z0),
                    major=_HEAD_R + 0.01,
                    minor=0.03,
                ),
                accent,
            )
        )
    else:  # visor: one wide glowing band across both eyes
        parts.append(
            (
                _prim(
                    "sphere",
                    (x0, y0 - 0.02, z0),
                    r=1.0,
                    scale=(0.5, 0.08, 0.12),
                ),
                glow,
            )
        )
        parts.append(
            (
                _prim(
                    "cube", (x0, y0 - 0.02, z0 + 0.12), scale=(0.9, 0.07, 0.03)
                ),
                accent,
            )
        )
        for x in (-0.5, 0.5):
            parts.append(
                (
                    _prim(
                        "sphere",
                        (x0 + x, y0 + 0.1, z0),
                        r=0.07,
                        scale=(0.8, 1.6, 1.4),
                    ),
                    accent,
                )
            )
    return parts


def _headwear(item: str, loc, palette: dict):
    x0, y0, z0 = loc
    top = z0 - 0.1  # top of the head
    # Kept low on purpose: the overlay's fixed camera frames the head close to
    # the top edge, so anything much taller than ~0.3m above it is cropped.
    accent = make_material(f"HeadMat_{item}", hex_to_rgba(palette["accent"]))
    glow = make_material(
        f"HeadGlow_{item}", hex_to_rgba(palette["emissive"]), emissive=True
    )
    parts = []
    if item == "crown":
        parts.append(
            (_prim("cylinder", (x0, y0, top + 0.05), r=0.3, depth=0.1), accent)
        )
        for i in range(5):
            a = i * 2 * math.pi / 5
            parts.append(
                (
                    _prim(
                        "cone",
                        (
                            x0 + 0.26 * math.cos(a),
                            y0 + 0.26 * math.sin(a),
                            top + 0.14,
                        ),
                        r=0.05,
                        depth=0.12,
                    ),
                    accent,
                )
            )
    elif item == "horns":
        for sign in (-1, 1):
            parts.append(
                (
                    _prim(
                        "cone",
                        (x0 + sign * 0.32, y0, top + 0.08),
                        r=0.08,
                        depth=0.26,
                        rotation=(0, sign * 0.55, 0),
                    ),
                    accent,
                )
            )
    elif item == "antenna":
        parts.append(
            (
                _prim("cylinder", (x0, y0, top + 0.09), r=0.015, depth=0.18),
                accent,
            )
        )
        parts.append((_prim("sphere", (x0, y0, top + 0.2), r=0.055), glow))
    else:  # pointy_hat
        parts.append(
            (
                _prim("cylinder", (x0, y0, top + 0.02), r=0.42, depth=0.03),
                accent,
            )
        )
        parts.append(
            (_prim("cone", (x0, y0, top + 0.12), r=0.27, depth=0.22), accent)
        )
    return parts


def _neckwear(item: str, loc, palette: dict):
    x0, y0, z0 = loc
    ring_r = (
        math.sqrt(_HEAD_R**2 - 0.35**2) + 0.02
    )  # head radius at this height
    accent = make_material(f"NeckMat_{item}", hex_to_rgba(palette["accent"]))
    parts = []
    if item == "tie":
        chin = (
            z0 - 0.2
        )  # head centre is 0.35 above this socket; its lowest point is another 0.2 down
        knot_y = _body_surface_y(0.0, chin - 0.1) - 0.01
        parts.append(
            (_prim("sphere", (x0, knot_y, chin - 0.06), r=0.06), accent)
        )
        parts.append(
            (
                _prim(
                    "cone",
                    (x0, knot_y, chin - 0.25),
                    r=0.1,
                    r2=0.03,
                    depth=0.28,
                    rotation=(math.pi, 0, 0),
                    scale=(1.0, 0.35, 1.0),
                ),
                accent,
            )
        )
    elif item == "collar":
        parts.append(
            (
                _prim("torus", (x0, y0, z0), major=ring_r + 0.03, minor=0.05),
                accent,
            )
        )
        for x in (-0.08, 0.08):
            parts.append(
                (
                    _prim(
                        "cone",
                        (x0 + x, y0 - ring_r - 0.02, z0 - 0.06),
                        r=0.08,
                        depth=0.18,
                        rotation=(math.pi, 0, 0),
                    ),
                    accent,
                )
            )
    else:  # scarf
        parts.append(
            (_prim("torus", (x0, y0, z0), major=ring_r, minor=0.085), accent)
        )
        parts.append(
            (
                _prim(
                    "cube",
                    (x0 + 0.2, y0 - ring_r + 0.02, z0 - 0.2),
                    scale=(0.14, 0.05, 0.4),
                ),
                accent,
            )
        )
    return parts


def _chestwear(item: str, loc, palette: dict):
    accent = make_material(f"ChestMat_{item}", hex_to_rgba(palette["accent"]))
    dark = make_material(
        f"ChestDark_{item}", _darker(hex_to_rgba(palette["accent"]), 0.5)
    )
    if item == "backpack":
        z = (
            loc[2] - 0.1
        )  # sits just below the chest socket (1.1 on the procedural bodies)
        back = _body_surface_y(0.0, z, front=False)
        return [
            (
                _prim("cube", (0.0, back + 0.14, z), scale=(0.55, 0.3, 0.65)),
                accent,
            ),
            (
                _prim(
                    "cube",
                    (0.0, back + 0.32, z - 0.15),
                    scale=(0.35, 0.06, 0.25),
                ),
                dark,
            ),
        ]
    # The procedural bodies are drawn wide enough for 0.28; an installed body's torso
    # is measured (below the arms, which a T-pose holds out at chest height).
    x = (
        0.5 * min(_body_half_width_at(loc[2] - d) for d in (0.0, 0.15, 0.3))
        if _BODY.get("imported")
        else 0.28
    )
    y = _body_surface_y(x, loc[2])
    return [
        (
            _prim(
                "cylinder",
                (x, y, loc[2]),
                r=0.09,
                depth=0.04,
                rotation=(math.pi / 2, 0, 0),
            ),
            accent,
        )
    ]  # badge


def _waist_section(z: float, half_w: float):
    """(centre y, half depth) of the body's cross-section at height z, from rays at several
    x positions: the centre line alone can hit the same surface from both sides on a body
    modelled with a gap there, which would flatten anything sized from it."""
    xs = (0.0, 0.4 * half_w, -0.4 * half_w, 0.8 * half_w, -0.8 * half_w)
    front = min(_body_surface_y(x, z) for x in xs)
    back = max(_body_surface_y(x, z, front=False) for x in xs)
    return (front + back) / 2.0, max((back - front) / 2.0, 0.05)


def _waistwear(item: str, loc, palette: dict):
    z = loc[2]
    accent = make_material(f"WaistMat_{item}", hex_to_rgba(palette["accent"]))
    dark = make_material(
        f"WaistDark_{item}", _darker(hex_to_rgba(palette["accent"]), 0.5)
    )
    half_w = _body_half_width_at(z)
    cy, half_d = _waist_section(z, half_w)
    if item == "pouch":
        # A small box that hangs on its own thin strap, with a darker flap and a clasp.
        x = half_w * 0.55
        top = z + 0.07
        sy = _body_surface_y(x, z) - 0.05
        rx, ry = (half_w * 1.1), half_d * 1.1
        return [
            (
                _prim(
                    "torus",
                    (0.0, cy, top),
                    major=1.0,
                    minor=0.012 / max(rx, 0.1),
                    scale=(rx, ry, max(rx, 0.1)),
                ),
                dark,
            ),
            (_prim("cube", (x, sy, z), scale=(0.13, 0.07, 0.12)), accent),
            (
                _prim(
                    "cube",
                    (x, sy - 0.006, top - 0.025),
                    scale=(0.14, 0.08, 0.055),
                ),
                dark,
            ),
            (_prim("sphere", (x, sy - 0.046, top - 0.055), r=0.014), accent),
        ]
    # belt: a ring scaled to the body's cross-section at this height (+ margin for an outfit)
    rx, ry = (half_w * 1.1), half_d * 1.1
    return [
        (
            _prim(
                "torus",
                (0.0, cy, z),
                major=1.0,
                minor=0.04 / max(rx, 0.1),
                scale=(rx, ry, max(rx, 0.1)),
            ),
            dark,
        ),  # z scale keeps the strap's section round
        (
            _prim(
                "cube",
                (0.0, (cy - half_d) * 1.1 - 0.01, z),
                scale=(0.16, 0.04, 0.12),
            ),
            accent,
        ),
    ]


def _handheld(item: str, loc, palette: dict):
    x0, y0, z0 = loc
    accent = make_material(f"HandMat_{item}", hex_to_rgba(palette["accent"]))
    glow = make_material(
        f"HandGlow_{item}", hex_to_rgba(palette["emissive"]), emissive=True
    )
    dark = make_material(
        f"HandDark_{item}", _darker(hex_to_rgba(palette["secondary"]), 0.5)
    )
    if item == "wand":
        return [
            (
                _prim(
                    "cylinder",
                    (x0, y0, z0 + 0.1),
                    r=0.02,
                    depth=0.7,
                    rotation=(0, 0.25, 0),
                ),
                dark,
            ),
            (_prim("sphere", (x0 + 0.09, y0, z0 + 0.45), r=0.06), glow),
        ]
    if item == "tool":
        return [
            (_prim("cylinder", (x0, y0, z0), r=0.03, depth=0.6), dark),
            (
                _prim("cube", (x0, y0, z0 + 0.34), scale=(0.2, 0.06, 0.1)),
                accent,
            ),
        ]
    if item == "shield":
        return [
            (
                _prim(
                    "cylinder",
                    (x0, y0 - 0.05, z0),
                    r=0.3,
                    depth=0.05,
                    rotation=(math.pi / 2, 0, 0),
                ),
                accent,
            ),
            (_prim("sphere", (x0, y0 - 0.1, z0), r=0.07), dark),
        ]
    return [(_prim("sphere", (x0, y0, z0), r=0.16), glow)]  # orb


def _orbiting(item: str, loc, palette: dict):
    x0, y0, z0 = loc
    accent = make_material(f"OrbitMat_{item}", hex_to_rgba(palette["accent"]))
    glow = make_material(
        f"OrbitGlow_{item}", hex_to_rgba(palette["emissive"]), emissive=True
    )
    if item == "floating_cube":
        return [
            (
                _prim(
                    "cube",
                    (x0 + 0.85, y0 - 0.1, z0 - 0.5),
                    scale=(0.2, 0.2, 0.2),
                    rotation=(0.6, 0.4, 0.5),
                ),
                glow,
            )
        ]
    return [
        (
            _prim(
                "torus",
                (x0, y0, z0),
                rotation=(0.15, 0, 0),
                major=0.4,
                minor=0.03,
            ),
            accent,
        )
    ]  # crystal_ring (a halo)


_ACCESSORY_BUILDERS = {
    "socket_eyes": _eyewear,
    "socket_head_top": _headwear,
    "socket_neck": _neckwear,
    "socket_chest": _chestwear,
    "socket_waist": _waistwear,
    "socket_hand_R": _handheld,
    "socket_hand_L": _handheld,
    "socket_orbit": _orbiting,
}
