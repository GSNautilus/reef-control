"""Shared light schedule, per-light tweaks, and translation to each light's
own format.

Levels are 0-1000 (permille) everywhere, the scale both lights use. The
dashboard caps every channel at 1000: the Prime HD only power-limits above
100% ("HD" boost), and the 16HD's boost range isn't explored yet.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CHANNELS = ["uv", "violet", "royal_blue", "blue", "cool_white", "deep_red", "green", "moonlight"]
SHARED_CHANNELS = CHANNELS[:-1]  # moonlight exists only on the 16HD
LIGHTS = ["prime_hd", "prime_16hd"]
LIGHT_CHANNELS = {"prime_hd": SHARED_CHANNELS, "prime_16hd": CHANNELS}
MAX_LEVEL = 1000
DAY_MINUTES = 1440


@dataclass
class Point:
    time: int  # minutes since midnight
    levels: dict[str, int]

    def to_json(self) -> dict:
        return {"time": self.time, "levels": dict(self.levels)}

    @classmethod
    def from_json(cls, data: dict) -> "Point":
        return cls(int(data["time"]), {c: int(data.get("levels", {}).get(c, 0)) for c in CHANNELS})


@dataclass
class Tweak:
    scale: float = 100.0  # percent applied to every channel
    offsets: dict[str, int] = field(default_factory=dict)  # permille added per channel

    def to_json(self) -> dict:
        return {"scale": self.scale, "offsets": dict(self.offsets)}

    @classmethod
    def from_json(cls, data: dict | None) -> "Tweak":
        data = data or {}
        return cls(float(data.get("scale", 100.0)), {c: int(v) for c, v in data.get("offsets", {}).items()})


def clamp(value: float) -> int:
    return max(0, min(MAX_LEVEL, round(value)))


def normalize(points: list[Point]) -> list[Point]:
    """Sorted by time, one point per minute (the last one wins), levels clamped."""
    by_time: dict[int, Point] = {}
    for p in points:
        t = int(p.time) % DAY_MINUTES
        by_time[t] = Point(t, {c: clamp(p.levels.get(c, 0)) for c in CHANNELS})
    return [by_time[t] for t in sorted(by_time)]


def validate(points: list[Point]) -> None:
    if len(points) < 2:
        raise ValueError("a schedule needs at least two points")


MAX_SHIFT = 720  # minutes either way; ±12 h covers every time of day
DIM_MIN, DIM_MAX = 1, 99  # dim mode: lights at this % of their schedule


def clamp_shift(minutes) -> int:
    return max(-MAX_SHIFT, min(MAX_SHIFT, int(round(float(minutes or 0)))))


def shifted(points: list[Point], minutes: int) -> list[Point]:
    """Every point moved by `minutes` (negative = earlier), wrapping at midnight."""
    return sorted((Point((p.time + minutes) % DAY_MINUTES, dict(p.levels)) for p in points), key=lambda p: p.time)


def dimmed(points: list[Point], percent: int | None) -> list[Point]:
    """Every channel at `percent` of its level (None = not dimmed). A channel
    that's on never rounds down to 0, so lit and dark points stay as they are
    (the 16HD's sunrise/sunset flags depend on that)."""
    if percent is None:
        return [Point(p.time, dict(p.levels)) for p in points]
    return [Point(p.time, {c: max(1, clamp(v * percent / 100)) if v else 0 for c, v in p.levels.items()})
            for p in points]


def effective(points: list[Point], tweak: Tweak, light: str, shift: int = 0) -> list[Point]:
    """The base schedule with one light's tweak and the shift applied, limited
    to its channels. Offsets only adjust channels that are on, so dark stays
    dark. Dim mode is applied separately, when writing to the light."""
    out = []
    for p in normalize(points):
        levels = {}
        for c in LIGHT_CHANNELS[light]:
            base = p.levels.get(c, 0)
            levels[c] = clamp(base * tweak.scale / 100 + tweak.offsets.get(c, 0)) if base else 0
        out.append(Point(p.time, levels))
    return shifted(out, shift) if shift else out


def interpolate(pairs: list[tuple[int, float]], minute: int) -> float:
    """Linear interpolation over time-sorted (minute, value) pairs, wrapping at midnight."""
    if not pairs:
        return 0.0
    for i, (t, v) in enumerate(pairs):
        if t == minute:
            return float(v)
        if t > minute:
            (t0, v0), (t1, v1) = pairs[i - 1], (t, v)
            break
    else:
        (t0, v0), (t1, v1) = pairs[-1], pairs[0]
    span = (t1 - t0) % DAY_MINUTES or DAY_MINUTES
    return v0 + (v1 - v0) * ((minute - t0) % DAY_MINUTES) / span


def level_at(points: list[Point], channel: str, minute: int) -> float:
    return interpolate([(p.time, p.levels.get(channel, 0)) for p in normalize(points)], minute)


def levels_at(points: list[Point], minute: int, channels: list[str] = CHANNELS) -> dict[str, int]:
    return {c: clamp(level_at(points, c, minute)) for c in channels}


# --------------------------------------------------------------------------
# Prime HD: {"name": "default", "ramps": [{"color", "points": [{"time", "intensity"}]}]}
# --------------------------------------------------------------------------

HD_COLORS = {"deep_red": "deep_red", "uv": "uv", "violet": "violet", "cool_white": "cool_white",
             "green": "green", "blue": "blue", "royal": "royal_blue"}  # order as the light stores it


def to_prime_hd(points: list[Point]) -> dict:
    pts = normalize(points)
    validate(pts)
    if pts[0].time != 0:
        pts = [Point(0, levels_at(pts, 0, SHARED_CHANNELS))] + pts
    ramps = [{"color": hd, "points": [{"time": p.time, "intensity": p.levels.get(ours, 0)} for p in pts]}
             for hd, ours in HD_COLORS.items()]
    return {"name": "default", "ramps": ramps}


def from_prime_hd(data: dict) -> list[Point]:
    per_color = {HD_COLORS[r["color"]]: sorted((p["time"], p["intensity"]) for p in r["points"])
                 for r in data["ramps"]}
    times = sorted({t for pairs in per_color.values() for t, _ in pairs})
    points = [Point(t, {c: clamp(interpolate(per_color[c], t)) if c in per_color else 0 for c in CHANNELS})
              for t in times]
    # The light always stores a midnight point; drop it when it only repeats
    # the wrap-around between the last and first real points.
    if len(points) > 2 and points[0].time == 0:
        rest = points[1:]
        if levels_at(rest, 0, SHARED_CHANNELS) == {c: points[0].levels[c] for c in SHARED_CHANNELS}:
            points = rest
    return points


def prime_hd_equal(a: dict, b: dict) -> bool:
    def norm(d):
        return {r["color"]: sorted((p["time"], p["intensity"]) for p in r["points"]) for r in d["ramps"]}
    return norm(a) == norm(b)


# --------------------------------------------------------------------------
# Prime 16HD: python-mobius SchedulePoint(time_minutes, flags, LightPrimitive)
# --------------------------------------------------------------------------

FLAG_ACTIVE, FLAG_NIGHT, FLAG_SUNRISE_BIT, FLAG_SUNSET_BIT = 1, 2, 4, 8
VISUAL_IDS = {"uv": "UV", "violet": "Violet", "royal_blue": "RoyalBlue", "blue": "Blue",
              "green": "Green", "deep_red": "DeepRed", "moonlight": "Moonlight", "cool_white": "CoolWhite"}
BRIGHTNESS_FULL = 1000


def flags_16hd(points: list[Point]) -> list[int]:
    """NIGHT on dark points, SUNRISE on the first lit point after dark, SUNSET
    on the first dark point after light. Matches the flags the myAI app
    wrote on the current schedule (3, 7, 1, 1, 1, 1, 11)."""
    def dark(p):
        return not any(p.levels.get(c, 0) for c in SHARED_CHANNELS)

    flags = []
    for i, p in enumerate(points):
        prev_dark = dark(points[i - 1])
        f = FLAG_ACTIVE
        if dark(p):
            f |= FLAG_NIGHT | (FLAG_SUNSET_BIT if not prev_dark else 0)
        elif prev_dark:
            f |= FLAG_NIGHT | FLAG_SUNRISE_BIT
        flags.append(f)
    return flags


def to_prime_16hd(points: list[Point]) -> list[dict]:
    """Plain-data points: {"time", "flags", "channels": {VisualID name: level}}."""
    pts = normalize(points)
    validate(pts)
    out = []
    for p, f in zip(pts, flags_16hd(pts)):
        channels = {VISUAL_IDS[c]: p.levels.get(c, 0) for c in VISUAL_IDS}
        channels["Brightness"] = BRIGHTNESS_FULL
        out.append({"time": p.time, "flags": f, "channels": channels})
    return out


def from_prime_16hd(points: list[dict]) -> list[Point]:
    reverse = {v: k for k, v in VISUAL_IDS.items()}
    out = []
    for p in points:
        scale = p["channels"].get("Brightness", BRIGHTNESS_FULL) / BRIGHTNESS_FULL
        levels = {c: 0 for c in CHANNELS}
        for name, value in p["channels"].items():
            if name in reverse:
                levels[reverse[name]] = clamp(value * scale)
        out.append(Point(p["time"], levels))
    return normalize(out)


def prime_16hd_equal(a: list[dict], b: list[dict]) -> bool:
    def norm(pts):
        return sorted((p["time"], p["flags"], tuple(sorted(p["channels"].items()))) for p in pts)
    return norm(a) == norm(b)
