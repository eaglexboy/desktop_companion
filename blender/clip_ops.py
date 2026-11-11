"""
Animation clips as plain data (quaternion tracks), and the operations that reshape them: trim, loop, ease,
join, reverse, ping-pong, intensity variants, seating offsets. Pure Python (no bpy).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

# Every converted clip may also be mirrored (`<clip>_Mirror`: left and right swapped).
MIRROR_SUFFIX = "_Mirror"

Quat = tuple[float, float, float, float]


def q_dot(a: Quat, b: Quat) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]


def q_slerp(a: Quat, b: Quat, t: float) -> Quat:
    """Spherical interpolation from `a` to `b`; `t` outside 0..1 extrapolates (an exaggerated
    or damped motion about `a`)."""
    import math

    if q_dot(a, b) < 0.0:
        b = (-b[0], -b[1], -b[2], -b[3])
    dot = min(1.0, q_dot(a, b))
    angle = math.acos(dot)
    if angle < 1e-6:
        return a
    sa, sb = math.sin((1 - t) * angle), math.sin(t * angle)
    sn = math.sin(angle)
    out = tuple((sa * x + sb * y) / sn for x, y in zip(a, b))
    norm = math.sqrt(sum(c * c for c in out)) or 1.0
    return tuple(c / norm for c in out)  # type: ignore[return-value]


def q_angle_deg(a: Quat, b: Quat) -> float:
    import math

    return math.degrees(2 * math.acos(min(1.0, abs(q_dot(a, b)))))


IDENTITY_Q: Quat = (1.0, 0.0, 0.0, 0.0)


def q_mul(a: Quat, b: Quat) -> Quat:
    return (
        a[0] * b[0] - a[1] * b[1] - a[2] * b[2] - a[3] * b[3],
        a[0] * b[1] + a[1] * b[0] + a[2] * b[3] - a[3] * b[2],
        a[0] * b[2] - a[1] * b[3] + a[2] * b[0] + a[3] * b[1],
        a[0] * b[3] + a[1] * b[2] - a[2] * b[1] + a[3] * b[0],
    )


def q_conj(a: Quat) -> Quat:
    return (a[0], -a[1], -a[2], -a[3])


def q_rotate(
    q: Quat, v: tuple[float, float, float]
) -> tuple[float, float, float]:
    r = q_mul(q_mul(q, (0.0, v[0], v[1], v[2])), q_conj(q))
    return (r[1], r[2], r[3])


def q_about_z(angle: float) -> Quat:
    import math

    return (math.cos(angle / 2), 0.0, 0.0, math.sin(angle / 2))


def _smooth(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def lying_turn(
    a: Quat, b: Quat, rest: Quat | None
) -> Callable[[float], Quat] | None:
    """How the pelvis goes from rotation `a` to `b` when the body lies down in both (its long axis,
    the bone's y, is within 37 degrees of the floor): `None` (the plain slerp will do) or a function
    of t (0..1) giving the rotation. Turning from lying face down, head to one side, to lying on the
    back, head to the other, the slerp takes the shortest way: head over heels, through a handstand.
    This goes the way a body on the floor does: it spins about the vertical for the first 70 % of
    the time and rolls about its own length (before that spin: the roll is about the body, then the
    whole is turned) from 30 % on (`rest` is the pelvis bone's rest
    orientation in world space, which says which way is up; `a` and `b` are in the bone's frame).
    """
    import math

    if rest is None:
        return None
    base, goal = q_mul(rest, a), q_mul(rest, b)
    l1, l2 = q_rotate(base, (0.0, 1.0, 0.0)), q_rotate(goal, (0.0, 1.0, 0.0))
    if abs(l1[2]) > 0.6 or abs(l2[2]) > 0.6:
        return None
    yaw = math.atan2(
        l1[0] * l2[1] - l1[1] * l2[0], l1[0] * l2[0] + l1[1] * l2[1]
    )
    residual = q_mul(q_about_z(-yaw), q_mul(goal, q_conj(base)))
    back = q_conj(rest)

    def at(t: float) -> Quat:
        spin = q_about_z(yaw * _smooth(t / 0.7))
        roll = q_slerp(IDENTITY_Q, residual, _smooth((t - 0.3) / 0.7))
        return q_mul(back, q_mul(q_mul(spin, roll), base))

    return at


def pelvis_rest(meta: dict) -> Quat | None:
    value = meta.get("pelvis_rest")
    return tuple(value) if value else None  # type: ignore[return-value]


def blend_bone(
    bone: str, a: Quat, b: Quat, t: float, turn: Callable[[float], Quat] | None
) -> Quat:
    """A bone's rotation `t` of the way from `a` to `b`: the pelvis goes by `turn` (`lying_turn`)
    when there is one, every other bone by the shortest arc."""
    if bone == "pelvis" and turn is not None:
        return turn(t)
    return q_slerp(a, b, t)


def reduce_keys(
    frames: list[float], values: list, distance, tolerance: float
) -> list[int]:
    """The indices of the samples worth keeping: a sample is dropped when interpolating
    between its kept neighbours stays within `tolerance` of it (`distance(a, b)` says how far
    apart two values are, `values` being quaternions or positions). Always keeps the first
    and last. Douglas-Peucker on the interpolation error."""
    if len(frames) <= 2:
        return list(range(len(frames)))

    def lerp(a, b, t):
        if isinstance(a, tuple) and len(a) == 4:
            return q_slerp(a, b, t)
        return tuple(x + (y - x) * t for x, y in zip(a, b))

    keep = {0, len(frames) - 1}
    stack = [(0, len(frames) - 1)]
    while stack:
        lo, hi = stack.pop()
        worst, worst_i = 0.0, -1
        for i in range(lo + 1, hi):
            t = (frames[i] - frames[lo]) / (frames[hi] - frames[lo])
            err = distance(lerp(values[lo], values[hi], t), values[i])
            if err > worst:
                worst, worst_i = err, i
        if worst > tolerance and worst_i >= 0:
            keep.add(worst_i)
            stack += [(lo, worst_i), (worst_i, hi)]
    return sorted(keep)


def close_loop(samples: list, blend: int, mix) -> list:
    """Makes a clip loop without a jump: the last `blend` samples are eased towards the first one,
    so the clip ends where it starts. `mix(a, b, t)` blends two samples."""
    if len(samples) <= blend + 1:
        return list(samples)
    out = list(samples)
    start = len(samples) - blend
    for i in range(start, len(samples)):
        out[i] = mix(samples[i], samples[0], (i - start + 1) / blend)
    return out


Vec = tuple[float, float, float]


@dataclass
class Clip:
    """A clip as samples, one per source frame, in the Quaternius rig's terms: each bone's
    rotation (what goes in its pose-bone `rotation_quaternion`) and how far the hips have
    moved from their rest position, in world axes."""

    frames: list[float]
    quats: dict[str, list[Quat]]
    hips: list[Vec]
    foot_clearance: float = (
        0.0  # how far the lowest foot is above the floor in the first frame
    )
    meta: dict = field(default_factory=dict)

    def copy(self) -> Clip:
        return Clip(
            list(self.frames),
            {b: list(q) for b, q in self.quats.items()},
            list(self.hips),
            self.foot_clearance,
            dict(self.meta),
        )

    def __len__(self) -> int:
        return len(self.frames)


def trim_clip(clip: Clip, first: float, last: float) -> Clip:
    """The samples whose source frame is in first..last, restarted from frame 0."""
    keep = [i for i, f in enumerate(clip.frames) if first <= f <= last]
    if not keep:
        raise ValueError(f"no frames between {first} and {last}")
    lo = clip.frames[keep[0]]
    return Clip(
        [clip.frames[i] - lo for i in keep],
        {b: [q[i] for i in keep] for b, q in clip.quats.items()},
        [clip.hips[i] for i in keep],
        clip.foot_clearance,
        dict(clip.meta),
    )


def loop_clip(clip: Clip, blend: int = 12) -> Clip:
    """Eases the last `blend` samples back to the first, so that the clip ends where it started."""
    out = clip.copy()
    out.quats = {
        b: close_loop(q, blend, lambda a, c, t: q_slerp(a, c, t))
        for b, q in clip.quats.items()
    }
    out.hips = close_loop(
        clip.hips,
        blend,
        lambda a, c, t: tuple(x + (y - x) * t for x, y in zip(a, c)),
    )  # type: ignore[arg-type]
    return out


def in_place_clip(clip: Clip) -> Clip:
    """Takes the hips' sideways and forward travel out (it keeps their height)."""
    out = clip.copy()
    start = clip.hips[0]
    out.hips = [(start[0], start[1], h[2]) for h in clip.hips]
    return out


def offset_hips(clip: Clip, offset: Vec) -> Clip:
    out = clip.copy()
    out.hips = [
        (h[0] + offset[0], h[1] + offset[1], h[2] + offset[2])
        for h in clip.hips
    ]
    return out


def scale_intensity(clip: Clip, amount: float, speed: float = 1.0) -> Clip:
    """The same motion bigger (`amount` above 1) or smaller (below 1) about the clip's first
    pose, and faster (`speed` above 1) or slower: a calm or an eager version of one clip.
    """
    out = clip.copy()
    out.quats = {
        b: [q_slerp(q[0], x, amount) for x in q] for b, q in clip.quats.items()
    }
    first = clip.hips[0]
    out.hips = [
        (
            first[0] + (hip[0] - first[0]) * amount,
            first[1] + (hip[1] - first[1]) * amount,
            first[2] + (hip[2] - first[2]) * amount,
        )
        for hip in clip.hips
    ]
    out.frames = [f / speed for f in clip.frames]
    return out


INTENSITY_VARIANTS: dict[str, tuple[float, float]] = {
    "Low": (0.55, 0.85),
    "High": (1.4, 1.15),
}  # (amount, speed)


def seat_offset(
    first_hips_world: Vec, target_world: Vec, foot_clearance: float
) -> Vec:
    """How far to move a seated clip's hips so it sits where the sit clips seat the body, so there
    is no jump between them. The feet may not go through the floor: lowering the hips by more
    than the feet's clearance is cut short (the clip keeps the rest of its height difference).
    """
    dx, dy, dz = (t - f for t, f in zip(target_world, first_hips_world))
    if dz < 0 and foot_clearance + dz < 0:
        dz = -max(foot_clearance, 0.0)
    return (dx, dy, dz)


Pose = tuple[
    dict[str, Quat], Vec
]  # every bone's rotation and where the hips are


def pose_at(clip: Clip, index: int) -> Pose:
    """The pose of sample `index` (negative counts from the end)."""
    return {b: q[index] for b, q in clip.quats.items()}, clip.hips[index]


def _mix_hips(a: Vec, b: Vec, t: float) -> Vec:
    return tuple(x + (y - x) * t for x, y in zip(a, b))  # type: ignore[return-value]


def ease_ends(
    clip: Clip, start: Pose | None, end: Pose | None, frames: int
) -> Clip:
    """The clip with `frames` samples before it that ease in from the pose `start` and as many after
    it that ease out to the pose `end` (either may be None: that end is left alone). A clip that
    starts and ends in the pose another clip leaves the body in, so playing one after the other
    does not jump."""
    n = len(clip)
    step = (clip.frames[-1] - clip.frames[0]) / max(n - 1, 1)
    lead = frames if start is not None else 0
    tail = frames if end is not None else 0
    quats = {b: list(q) for b, q in clip.quats.items()}
    hips = list(clip.hips)
    if start is not None:
        ref_q, ref_h = start
        turn = (
            lying_turn(
                ref_q.get("pelvis", clip.quats["pelvis"][0]),
                clip.quats["pelvis"][0],
                pelvis_rest(clip.meta),
            )
            if "pelvis" in clip.quats
            else None
        )
        for b, q in clip.quats.items():
            quats[b] = [
                blend_bone(
                    b, ref_q.get(b, q[0]), q[0], (k + 1) / (frames + 1), turn
                )
                for k in range(frames)
            ] + quats[b]
        hips = [
            _mix_hips(ref_h, clip.hips[0], (k + 1) / (frames + 1))
            for k in range(frames)
        ] + hips
    if end is not None:
        ref_q, ref_h = end
        turn = (
            lying_turn(
                clip.quats["pelvis"][-1],
                ref_q.get("pelvis", clip.quats["pelvis"][-1]),
                pelvis_rest(clip.meta),
            )
            if "pelvis" in clip.quats
            else None
        )
        for b, q in clip.quats.items():
            quats[b] += [
                blend_bone(
                    b, q[-1], ref_q.get(b, q[-1]), (k + 1) / (frames + 1), turn
                )
                for k in range(frames)
            ]
        hips += [
            _mix_hips(clip.hips[-1], ref_h, (k + 1) / (frames + 1))
            for k in range(frames)
        ]
    out_frames = [step * k for k in range(lead + n + tail)]
    return Clip(out_frames, quats, hips, clip.foot_clearance, dict(clip.meta))


def blend_through(
    clip: Clip, ref_quats: dict[str, Quat], ref_hips: Vec, frames: int
) -> Clip:
    """The clip eased in from, and back out to, one reference pose: a seated variation that starts
    and ends in the pose the sit clips leave the body in, so playing it after one of them (and a
    sit loop after it) does not jump."""
    return ease_ends(clip, (ref_quats, ref_hips), (ref_quats, ref_hips), frames)


def reverse_clip(clip: Clip) -> Clip:
    """The clip played backwards (standing up becomes lying down)."""
    last = clip.frames[-1]
    out = clip.copy()
    out.frames = [last - f for f in reversed(clip.frames)]
    out.quats = {b: list(reversed(q)) for b, q in clip.quats.items()}
    out.hips = list(reversed(clip.hips))
    return out


def ping_pong(clip: Clip, slow: float = 1.0) -> Clip:
    """The clip out and back along the same path, `slow` times slower, so that it ends in the pose
    it began in (a roll in her sleep, not a snap back to the start). The time is eased (smoothstep),
    so the body is still at the start, at the turn and at the end instead of bouncing off the
    turn."""
    n = len(clip)
    if n < 2:
        return clip.copy()
    step = (clip.frames[-1] - clip.frames[0]) / (n - 1)
    total = max(2, round(2 * (n - 1) * max(slow, 0.1)) + 1)
    out = Clip(
        [],
        {b: [] for b in clip.quats},
        [],
        clip.foot_clearance,
        dict(clip.meta),
    )
    for m in range(total):
        u = m / (total - 1)
        v = 1 - abs(2 * u - 1)  # 0 -> 1 -> 0
        w = v * v * (3 - 2 * v)
        position = w * (n - 1)
        lo = min(int(position), n - 2)
        t = position - lo
        out.frames.append(step * m)
        for b, q in clip.quats.items():
            out.quats[b].append(q_slerp(q[lo], q[lo + 1], t))
        out.hips.append(_mix_hips(clip.hips[lo], clip.hips[lo + 1], t))
    return out


def join_clips(parts: list[Clip], ease: int) -> Clip:
    """The clips one after another, with `ease` samples between each pair that ease from where the
    first ends to where the next begins. Sampled at the first clip's rate."""
    bones = set().union(*(p.quats for p in parts))
    identity: Quat = (1.0, 0.0, 0.0, 0.0)
    quats: dict[str, list[Quat]] = {b: [] for b in bones}
    hips: list[Vec] = []
    for index, part in enumerate(parts):
        if index and ease:
            first = pose_at(part, 0)
            before = {b: quats[b][-1] for b in bones}
            before_hips = hips[-1]
            turn = (
                lying_turn(
                    before["pelvis"],
                    first[0].get("pelvis", before["pelvis"]),
                    pelvis_rest(parts[0].meta),
                )
                if "pelvis" in bones
                else None
            )
            for k in range(ease):
                t = (k + 1) / (ease + 1)
                for b in bones:
                    quats[b].append(
                        blend_bone(
                            b, before[b], first[0].get(b, before[b]), t, turn
                        )
                    )
                hips.append(_mix_hips(before_hips, first[1], t))
        for j in range(len(part)):
            for b in bones:
                quats[b].append(
                    part.quats[b][j] if b in part.quats else identity
                )
            hips.append(part.hips[j])
    step = (parts[0].frames[-1] - parts[0].frames[0]) / max(
        len(parts[0]) - 1, 1
    )
    return Clip(
        [step * k for k in range(len(hips))],
        quats,
        hips,
        parts[0].foot_clearance,
        dict(parts[0].meta),
    )


def finish_clips(
    made: dict[str, tuple[Clip, dict]],
    derived: list[dict],
    references: dict[str, Pose],
) -> dict[str, tuple[Clip, dict]]:
    """The converted clips joined up: {name: (clip, options)} in, the same plus the derived clips out.
    `ease_from` / `ease_to` name the clip (or `@name`, one of `references`) whose first pose this clip
    eases in from / out to, `blend_ref` does both to one pose, over `ease_frames`; `compose` (derived
    clips) joins the clips named, a `[name, repeat]` pair repeating one; `hold` is a pose held, to be
    eased into or out of. Mirrored clips (`_Mirror`) refer to the mirrored ones. A reference that is
    not there is ignored (the pack may lack it)."""

    def reference(key: str | None, suffix: str) -> Pose | None:
        if not key:
            return None
        if key.startswith("@"):
            return references.get(key)
        other = made.get(key + suffix)
        return pose_at(other[0], 0) if other else None

    def settle(clip: Clip, options: dict, suffix: str) -> Clip:
        frames = int(options.get("ease_frames", 24))
        if options.get("blend_ref"):
            pose = reference(options["blend_ref"], suffix)
            return ease_ends(clip, pose, pose, frames) if pose else clip
        start, end = (
            reference(options.get("ease_from"), suffix),
            reference(options.get("ease_to"), suffix),
        )
        return ease_ends(clip, start, end, frames) if start or end else clip

    out: dict[str, tuple[Clip, dict]] = {}
    for name, (clip, options) in made.items():
        suffix = MIRROR_SUFFIX if name.endswith(MIRROR_SUFFIX) else ""
        out[name] = (settle(clip, options, suffix), options)
    for entry in derived:
        options = entry["options"]
        if options.get("hold"):
            if options["hold"] in made:
                held = pose_at(made[options["hold"]][0], 0)
                still = Clip(
                    [0.0, 1.0],
                    {b: [q, q] for b, q in held[0].items()},
                    [held[1], held[1]],
                )
                out[entry["name"]] = (settle(still, options, ""), options)
            continue
        parts: list[Clip] = []
        for item in options["compose"]:
            part, repeat = (item, 1) if isinstance(item, str) else item
            if part not in made:
                break
            parts += [made[part][0]] * int(repeat)
        else:
            out[entry["name"]] = (
                settle(
                    join_clips(parts, int(options.get("ease", 14))), options, ""
                ),
                options,
            )
    return out
