"""What the dashboard does with the lights: status, sending schedules
(backup -> write -> read back and compare), dim mode, and live control
sessions."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime

from . import schedule as s
from . import store
from .config import BACKUP_DIR, Config
from .devices.prime_16hd import Prime16HD
from .devices.prime_hd import PrimeHD

log = logging.getLogger(__name__)

STATUS_16HD_MAX_AGE = 60  # Bluetooth reads are slow; reuse a recent status
LIVE_TICK = 1


DIM_KEY = "dim"              # {"on", "percent"}: dim mode as you last set it
ON_LIGHTS_KEY = "on_lights"  # {light: {"written", "attempt"}}: what the dashboard wrote, see Lights._undimmed
DIM_DEFAULT = 70


class Busy(RuntimeError):
    pass


def default_draft() -> dict:
    return {"points": [{"time": 600, "levels": {}}, {"time": 1380, "levels": {}}],
            "tweaks": {light: s.Tweak().to_json() for light in s.LIGHTS}, "shift": 0}


def parse_draft(draft: dict) -> tuple[list[s.Point], dict[str, s.Tweak], int]:
    points = s.normalize([s.Point.from_json(p) for p in draft["points"]])
    tweaks = {light: s.Tweak.from_json(draft.get("tweaks", {}).get(light)) for light in s.LIGHTS}
    return points, tweaks, s.clamp_shift(draft.get("shift", 0))


def clean_draft(draft: dict) -> dict:
    """Only the parts of a draft you edit (history entries also carry the dim)."""
    points, tweaks, shift = parse_draft(draft)
    return {"points": [p.to_json() for p in points], "tweaks": {k: v.to_json() for k, v in tweaks.items()},
            "shift": shift}


def dim_state() -> dict:
    saved = store.get(DIM_KEY) or {}
    return {"on": bool(saved.get("on")), "percent": int(saved.get("percent", DIM_DEFAULT))}


def current_dim() -> int | None:
    """The % the lights run at in dim mode, or None when it's off."""
    d = dim_state()
    return d["percent"] if d["on"] else None


def draft_view(draft: dict, dim: int | None = None) -> dict:
    """The draft plus what each light will run: tweaks, shift and dim mode applied."""
    points, tweaks, shift = parse_draft(draft)
    return {**clean_draft(draft), "dim": dim,
            "effective": {light: [p.to_json() for p in s.dimmed(s.effective(points, tweaks[light], light, shift), dim)]
                          for light in s.LIGHTS}}


def _remember(light: str, **changes) -> None:
    on_lights = store.get(ON_LIGHTS_KEY) or {}
    on_lights.setdefault(light, {}).update(changes)
    store.put(ON_LIGHTS_KEY, on_lights)


class Lights:
    def __init__(self, cfg: Config):
        self.hd = PrimeHD(cfg.prime_hd_host)
        self.hd16 = Prime16HD(cfg.prime_16hd_address, cfg.prime_16hd_serial)
        self._status_16hd: dict = {"online": None, "checked": None}
        self._status_16hd_at = 0.0
        self._status_16hd_task: asyncio.Task | None = None
        self._writing = asyncio.Lock()  # one schedule write (send or dim) at a time

    async def close(self):
        await self.hd.close()
        await self.hd16.close()

    # ---- status -------------------------------------------------------------

    async def _refresh_16hd(self):
        try:
            status = await self.hd16.status()
        except Exception as e:
            status = {"online": False, "error": str(e)}
        status["checked"] = datetime.now().isoformat(timespec="seconds")
        self._status_16hd = status
        self._status_16hd_at = time.monotonic()

    async def status(self, refresh_16hd: bool = False) -> dict:
        try:
            hd = await self.hd.status()
        except Exception as e:
            hd = {"online": False, "error": str(e)}
        hd["checked"] = datetime.now().isoformat(timespec="seconds")
        stale = time.monotonic() - self._status_16hd_at > STATUS_16HD_MAX_AGE
        if (refresh_16hd or stale) and not (self._status_16hd_task and not self._status_16hd_task.done()):
            self._status_16hd_task = asyncio.create_task(self._refresh_16hd())
        if refresh_16hd:
            await self._status_16hd_task
        return {"prime_hd": hd, "prime_16hd": self._status_16hd}

    # ---- schedules ----------------------------------------------------------

    def _io(self, light: str):
        """read, write, encode, decode, equal for one light's schedule."""
        if light == "prime_hd":
            return self.hd.get_schedule, self.hd.set_schedule, s.to_prime_hd, s.from_prime_hd, s.prime_hd_equal
        return (self.hd16.get_schedule, self.hd16.set_schedule,
                s.to_prime_16hd, s.from_prime_16hd, s.prime_16hd_equal)

    def _undimmed(self, light: str, raw) -> tuple[list[s.Point], int]:
        """The schedule on a light as it is without dim mode, and the shift in it.

        The dashboard keeps the exact undimmed schedule it last wrote to each
        light ("written"), and the one it was writing ("attempt") in case a
        write failed half-way. If the light holds one of those, dimmed as
        recorded, that's the answer: exact, so dimming and undimming never
        drifts. A schedule the dashboard didn't write can't be dimmed (dim
        mode only exists here), so it's taken as it is, unshifted."""
        _, _, encode, decode, equal = self._io(light)
        record = (store.get(ON_LIGHTS_KEY) or {}).get(light) or {}
        for cand in (record.get("written"), record.get("attempt")):
            if not cand:
                continue
            points = [s.Point.from_json(p) for p in cand["points"]]
            try:
                if equal(raw, encode(s.dimmed(points, cand.get("dim")))):
                    return points, int(cand.get("shift", 0))
            except (ValueError, KeyError, TypeError):
                continue
        return decode(raw), 0

    async def import_schedule(self, light: str) -> list[s.Point]:
        """A light's schedule with dim mode and the shift taken out, ready to
        edit as the base (per-light tweaks stay in, as before)."""
        read = self._io(light)[0]
        points, shift = self._undimmed(light, await read())
        return s.shifted(points, -shift) if shift else points

    async def _write_one(self, light: str, base: list[s.Point] | None, shift: int, dim: int | None,
                         stamp: str) -> dict:
        """Backup -> write -> read back and compare. `base` is the light's
        schedule before dim mode (tweaks and shift applied); None keeps the
        one already on the light and only changes the dim."""
        result = {"ok": False}
        read, write, encode, _, equal = self._io(light)
        try:
            before = await read()
            backup = BACKUP_DIR / f"{light}_{stamp}.json"
            BACKUP_DIR.mkdir(exist_ok=True)
            backup.write_text(json.dumps(before, indent=2))
            result["backup"] = str(backup)
            if base is None:
                base, shift = self._undimmed(light, before)
            payload = encode(s.dimmed(base, dim))
            record = {"points": [p.to_json() for p in base], "shift": shift, "dim": dim}
            if equal(before, payload):
                _remember(light, written=record)
                result.update(ok=True, unchanged=True)
                return result
            _remember(light, attempt=record)
            await write(payload)
            after = await read()
            result["verified"] = equal(after, payload)
            result["ok"] = result["verified"]
            if result["verified"]:
                _remember(light, written=record, attempt=None)
            else:
                result["error"] = "the light's schedule didn't match what was sent when read back"
        except Exception as e:
            log.exception("writing the schedule to %s failed", light)
            result["error"] = str(e)
        return result

    @asynccontextmanager
    async def _exclusive(self):
        if self._writing.locked():
            raise Busy("the lights' schedules are being written right now; try again when that's done")
        async with self._writing:
            yield

    @property
    def busy(self) -> bool:
        return self._writing.locked()

    async def send(self, draft: dict) -> dict:
        points, tweaks, shift = parse_draft(draft)
        for light in s.LIGHTS:
            s.validate(s.effective(points, tweaks[light], light, shift))
        dim = current_dim()
        async with self._exclusive():
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            results = dict(zip(s.LIGHTS, await asyncio.gather(
                *(self._write_one(light, s.effective(points, tweaks[light], light, shift), shift, dim, stamp)
                  for light in s.LIGHTS))))
        history_id = store.add_history({**clean_draft(draft), "dim": dim}, results)
        return {"history_id": history_id, "results": results}

    # ---- dim mode -------------------------------------------------------------

    def dim_snapshot(self) -> dict:
        """Dim mode as set, plus what each light last got from the dashboard
        ("behind": lights not at the setting yet, e.g. after a failed write)."""
        d = dim_state()
        want = d["percent"] if d["on"] else None
        on_lights = store.get(ON_LIGHTS_KEY) or {}
        lights = {light: ((on_lights.get(light) or {}).get("written") or {}).get("dim") for light in s.LIGHTS}
        return {**d, "busy": self.busy, "lights": lights,
                "behind": [light for light in s.LIGHTS if lights[light] != want]}

    async def set_dim(self, on: bool, percent: int) -> dict:
        """Turns dim mode on or off, or changes its level, by rewriting both
        lights' schedules at that % (backed up and checked like a send). The
        schedule itself is unchanged; it lives on the lights, so it holds
        with the PC off. Changing the level while dim is off only saves it."""
        percent = int(percent)
        if not s.DIM_MIN <= percent <= s.DIM_MAX:
            raise ValueError(f"dim level must be {s.DIM_MIN}-{s.DIM_MAX}%")
        if not on and not dim_state()["on"] and not self.dim_snapshot()["behind"]:
            store.put(DIM_KEY, {"on": False, "percent": percent})
            return {"dim": self.dim_snapshot(), "results": {}}
        async with self._exclusive():
            store.put(DIM_KEY, {"on": on, "percent": percent})
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            dim = percent if on else None
            results = dict(zip(s.LIGHTS, await asyncio.gather(
                *(self._write_one(light, None, 0, dim, stamp) for light in s.LIGHTS))))
        return {"dim": self.dim_snapshot(), "results": results}


HOLD_SECONDS = 60000          # 16HD held scene: ~16.7 h, like the off scene
HOLD_RENEW = 4 * 3600         # renewed well before it runs out
HOLD_RETRY = 300              # after a failed 16HD send, try again in 5 min
LIVE_KEY = "live"
BEFORE_MAINTENANCE_KEY = "live_before_maintenance"


class LiveControl:
    """Live levels on one or both lights, held until "Back to schedule" or a
    light's on/off switch (decided 2026-11-06), even with the PC off:

    - Prime HD: manual mode at those levels, which the light keeps by itself.
    - Prime 16HD: a scene lasting ~16.7 h, renewed every 4 h while the
      dashboard runs (the same way as the off scene). Without the dashboard it
      holds up to ~16.7 h, then the light goes back to its schedule.

    16HD changes go through one worker (Bluetooth is ~0.5 s per update), always
    sending the newest levels. Held levels are saved and re-applied when the
    dashboard starts. `hold` marks maintenance mode's lights.
    """

    def __init__(self, lights: Lights, idle_timeout: int, power: "Power"):
        self.lights = lights
        self.power = power
        self.idle_timeout = idle_timeout   # no longer used: settings hold until stopped
        self.levels: dict[str, dict[str, int]] = {}
        self.errors: dict[str, str] = {}
        self.hold = False
        self.last_change = 0.0
        self._hd_manual = False
        self._16hd_pending: dict[str, int] | None = None
        self._16hd_sent = 0.0
        self._16hd_task: asyncio.Task | None = None
        self._ticker: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return bool(self.levels)

    def snapshot(self) -> dict:
        return {"active": self.active, "levels": self.levels, "hold": self.hold, "errors": self.errors,
                "seconds_left": None, "idle_timeout": None,
                "pending": {"prime_16hd": self._16hd_pending is not None or
                            bool(self._16hd_task and not self._16hd_task.done())}}

    def _save(self):
        store.put(LIVE_KEY, {"levels": self.levels, "hold": self.hold})

    async def restore(self):
        """Re-applies a setting that was being held when the dashboard stopped."""
        saved = store.get(LIVE_KEY) or {}
        levels = saved.get("levels") or {}
        if not levels:
            return
        log.info("re-applying held live settings for %s", ", ".join(levels))
        self.hold = bool(saved.get("hold"))
        for light, light_levels in levels.items():
            if light in s.LIGHTS:
                await self.set([light], light_levels)

    async def set(self, targets: list[str], levels: dict[str, int], hold: bool | None = None) -> dict:
        if hold is not None:
            self.hold = hold
        self.last_change = time.monotonic()
        for light in targets:
            light_levels = {c: s.clamp(levels.get(c, 0)) for c in s.LIGHT_CHANNELS[light]}
            self.levels[light] = light_levels
            self.errors.pop(light, None)
            if light == "prime_hd":
                await self._send_hd()
            else:
                self._16hd_pending = light_levels
                if not (self._16hd_task and not self._16hd_task.done()):
                    self._16hd_task = asyncio.create_task(self._16hd_worker())
        self._save()
        if not (self._ticker and not self._ticker.done()):
            self._ticker = asyncio.create_task(self._tick())
        return self.snapshot()

    async def _send_hd(self):
        try:
            if self._hd_manual:
                await self.lights.hd.set_levels(self.levels["prime_hd"])
            else:
                await self.lights.hd.set_manual(self.levels["prime_hd"])
                self._hd_manual = True
        except Exception as e:
            self._hd_manual = False
            self.errors["prime_hd"] = str(e)

    async def _16hd_worker(self):
        while self._16hd_pending is not None:
            levels, self._16hd_pending = self._16hd_pending, None
            try:
                await self.lights.hd16.set_live(levels, HOLD_SECONDS)
                self._16hd_sent = time.monotonic()
            except Exception as e:
                self.errors["prime_16hd"] = str(e)
                self._16hd_sent = time.monotonic() - HOLD_RENEW + HOLD_RETRY

    async def _tick(self):
        """Renews the 16HD's held scene every few hours."""
        while self.active:
            await asyncio.sleep(LIVE_TICK * 30)
            if ("prime_16hd" in self.levels and self._16hd_pending is None
                    and time.monotonic() - self._16hd_sent > HOLD_RENEW
                    and not (self._16hd_task and not self._16hd_task.done())):
                self._16hd_pending = self.levels["prime_16hd"]
                self._16hd_task = asyncio.create_task(self._16hd_worker())

    async def stop(self, targets: list[str] | None = None) -> dict:
        """Hands each light back to its schedule, or to off if it's switched off."""
        targets = [t for t in (targets or list(self.levels)) if t in self.levels]
        for light in targets:
            del self.levels[light]
        if not self.levels:
            self.hold = False
        self._save()
        if "prime_hd" in targets:
            self._hd_manual = False
            try:
                if self.power.is_off("prime_hd"):
                    await self.lights.hd.set_off()
                else:
                    await self.lights.hd.resume_schedule()
            except Exception as e:
                self.errors["prime_hd"] = str(e)
        if "prime_16hd" in targets:
            self._16hd_pending = None
            if self._16hd_task and not self._16hd_task.done():
                await self._16hd_task
            try:
                await self.lights.hd16.stop_live(then_off=self.power.is_off("prime_16hd"))
            except Exception as e:
                self.errors["prime_16hd"] = str(e)
        return self.snapshot()

    async def close(self):
        """The dashboard is shutting down (Quit in the notification area, Ctrl+C):
        stops the background work but leaves the lights as they are, the same as
        closing the window. A held setting stays on the lights and is re-applied
        at the next start; only "Back to schedule" or a light's switch ends it."""
        if self._ticker and not self._ticker.done():
            self._ticker.cancel()
        if self._16hd_task and not self._16hd_task.done():
            await self._16hd_task  # let the newest levels reach the light


class MaintenanceLights:
    """Maintenance mode's lights: white only, at a low level, held through live
    control until maintenance is done. A setting you were holding before is
    given back afterwards; otherwise the lights go back to their schedule."""

    def __init__(self, live: LiveControl):
        self.live = live

    async def hold_white(self, permille: int):
        if self.live.active and not self.live.hold:
            store.put(BEFORE_MAINTENANCE_KEY, {k: dict(v) for k, v in self.live.levels.items()})
        levels = {c: 0 for c in s.CHANNELS} | {"cool_white": s.clamp(permille)}
        await self.live.set(list(s.LIGHTS), levels, hold=True)

    async def release(self):
        before = store.get(BEFORE_MAINTENANCE_KEY) or {}
        store.put(BEFORE_MAINTENANCE_KEY, None)
        self.live.hold = False
        if not self.live.active:
            return
        for light in s.LIGHTS:
            if light in before:
                await self.live.set([light], before[light], hold=False)
            elif light in self.live.levels:
                await self.live.stop([light])


OFF_RENEW = 4 * 3600  # the 16HD's off scene lasts ~16.7 h; renew well before


class Power:
    """Each light is either on (following its schedule) or off (held dark).
    The choice is saved, so it survives dashboard restarts.

    Prime HD off = manual mode at zero, which holds with the PC off too.
    Prime 16HD off = a long all-zero scene, renewed while the dashboard runs;
    with the dashboard closed it lasts ~16.7 h, then the schedule resumes.
    Live control can still take over an off light; when it ends, the light
    goes back to off, not to its schedule."""

    def __init__(self, lights: Lights):
        self.lights = lights
        self.live: LiveControl | None = None
        self.state = {light: True for light in s.LIGHTS} | (store.get_power() or {})
        self.errors: dict[str, str] = {}
        self._renewer: asyncio.Task | None = None

    def is_off(self, light: str) -> bool:
        return not self.state.get(light, True)

    def snapshot(self) -> dict:
        return {"on": dict(self.state), "errors": dict(self.errors)}

    def _live_on(self, light: str) -> bool:
        return bool(self.live and light in self.live.levels)

    async def _apply(self, light: str):
        try:
            if light == "prime_hd":
                await (self.lights.hd.resume_schedule() if self.state[light] else self.lights.hd.set_off())
            else:
                await (self.lights.hd16.set_on() if self.state[light] else self.lights.hd16.set_off())
            self.errors.pop(light, None)
        except Exception as e:
            log.warning("turning %s %s failed: %s", light, "on" if self.state[light] else "off", e)
            self.errors[light] = str(e)

    async def set(self, targets: list[str], on: bool) -> dict:
        for light in targets:
            self.state[light] = on
        store.put_power(self.state)
        live_targets = [t for t in targets if self._live_on(t)]
        if live_targets:
            await self.live.stop(live_targets)  # hands over to on/off per the new state
        await asyncio.gather(*(self._apply(t) for t in targets if t not in live_targets))
        return self.snapshot()

    async def reassert_off(self):
        """After a schedule write (Send, dim mode): a light that's switched off
        is switched off again, in case writing its schedule woke it up."""
        await asyncio.gather(*(self._apply(light) for light in s.LIGHTS
                               if self.is_off(light) and not self._live_on(light)))

    def start(self):
        self._renewer = asyncio.create_task(self._renew_loop())

    async def _renew_loop(self):
        """Re-applies off at start-up (an off scene may have run out while
        the dashboard was closed) and every few hours after that."""
        while True:
            await asyncio.gather(*(self._apply(light) for light in s.LIGHTS
                                   if self.is_off(light) and not self._live_on(light)))
            await asyncio.sleep(OFF_RENEW)

    def close(self):
        if self._renewer:
            self._renewer.cancel()
