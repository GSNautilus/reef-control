"""The power strip: outlet control, feed and maintenance modes, the skimmer's
restart after a power cut, ATO run logging and pump health.

Safety model (PLAN.md, Phase 3):
- A pump turned off from the dashboard always has a countdown *on the strip*
  that turns it back on, so a PC crash can't leave it off. Maintenance is the
  exception, by the user's choice: they're there, and "Done" ends it.
- After a power cut the strip itself brings pumps and ATO back on and leaves
  the skimmer off; this service turns the skimmer on once the pumps have run
  for the skimmer delay (judged by their on-time, so it also works if the
  dashboard starts later).
- The skimmer and ATO may be held off "until I turn it back on".

`expect` is what the dashboard means each outlet to be. Comparing it with the
strip tells a power cut (or a press of the strip's own button) apart from the
dashboard's own changes.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from datetime import datetime

from . import store
from .config import PUMP_ROLES, Config

log = logging.getLogger(__name__)

STATE_KEY = "strip"
ATO_RUN_WATTS = 2.0          # above the ATO's idle draw = its pump is running
LATE_GRACE = 90              # a strip countdown this late counts as failed
HEALTH_WINDOW = 300          # seconds of readings judged against the normal draw
HEALTH_MIN_ON = 300          # don't judge a pump in its first minutes after switching on
BASELINE_MIN_MINUTES = 30    # minutes of data needed to learn a normal draw
LOW, HIGH = 0.5, 1.6         # health thresholds, as a share of the normal draw
DAILY = 24 * 3600
KEEP_DAYS = 120
SETTINGS_LIMITS = {
    "feed_minutes": (1, 120),
    "skimmer_delay_minutes": (0, 60),
    "maintenance_white_percent": (0, 100),
    "ato_long_run_seconds": (10, 3600),
    "ato_runs_factor": (1.1, 10),
}


class StripService:
    def __init__(self, cfg: Config, driver, lights=None, clock=time.time):
        """driver: app.devices.p316m.P316M (or a fake with the same methods).
        lights: object with async hold_white(permille) / release(), for maintenance."""
        self.cfg = cfg
        self.driver = driver
        self.lights = lights
        self.clock = clock
        self.outlets = {o.position: o for o in cfg.outlets}
        self.pumps = [p for p, o in self.outlets.items() if o.role in PUMP_ROLES]
        self.skimmer = next((p for p, o in self.outlets.items() if o.role == "skimmer"), None)
        self.ato = next((p for p, o in self.outlets.items() if o.role == "ato"), None)

        saved = store.get(STATE_KEY, {})
        self.names: dict[int, str] = {int(k): v for k, v in saved.get("names", {}).items()}
        self.settings: dict = {**cfg.defaults, **saved.get("settings", {})}
        self.mode: dict | None = saved.get("mode")
        self.timers: dict[int, float] = {int(k): v for k, v in saved.get("timers", {}).items()}
        self.held_off: set[int] = set(saved.get("held_off", []))
        self.expect: dict[int, bool] = {int(k): v for k, v in saved.get("expect", {}).items()}
        self.relearn: dict[int, float] = {int(k): v for k, v in saved.get("relearn", {}).items()}

        self.reading: dict | None = None
        self.online: bool | None = None
        self.error: str | None = None
        self.updated: float | None = None
        self.notes: deque = deque(maxlen=20)
        self._recent: dict[int, deque] = {p: deque() for p in self.outlets}
        self._minute: int | None = None
        self._bucket: dict[int, list] = {}
        self._ato_idle: deque = deque(maxlen=720)
        self._ato_run: dict | None = None
        self._baselines: dict[int, tuple[float, float | None]] = {}
        self._usual_cache: tuple[float, float | None] | None = None
        self._last_daily = 0.0
        self._restore_tried = 0.0
        self._task: asyncio.Task | None = None
        self._cmd_lock = asyncio.Lock()

    # ---- persistence & helpers -------------------------------------------------------

    def _save(self):
        store.put(STATE_KEY, {
            "names": self.names, "settings": self.settings, "mode": self.mode,
            "timers": self.timers, "held_off": sorted(self.held_off),
            "expect": self.expect, "relearn": self.relearn,
        })

    def name(self, pos: int) -> str:
        return self.names.get(pos) or self.outlets[pos].name

    def _note(self, text: str):
        log.info("strip: %s", text)
        self.notes.appendleft({"time": self.clock(), "text": text})

    def _out(self, pos: int) -> dict:
        return (self.reading or {}).get("outlets", {}).get(pos, {})

    def _expects(self, pos: int) -> bool:
        return self.expect.get(pos, pos not in self.held_off)

    def _delay(self) -> int:
        return int(self.settings["skimmer_delay_minutes"] * 60)

    # ---- lifecycle -------------------------------------------------------------------

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def close(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._flush_minute(force=True)
        await self.driver.close()

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except Exception as e:  # never let the loop die
                log.exception("strip poll failed")
                self.error = str(e)
            await asyncio.sleep(self.cfg.strip_poll_seconds if self.online else 15)

    async def _daily(self):
        """At start-up and once a day: strip settings, clock, old history."""
        power_cut_on = {p: o.role != "skimmer" for p, o in self.outlets.items()}
        for change in await self.driver.ensure_settings(power_cut_on):
            self._note(f"Strip setting fixed: {change}")
        await self.driver.set_clock()
        store.prune(int(self.clock() // 60) - KEEP_DAYS * 1440)
        if self.mode and self.mode.get("name") == "maintenance":
            await self._lights("hold")  # the dashboard restarted mid-maintenance
        await self._adopt_strip_countdowns()

    async def _adopt_strip_countdowns(self):
        """Countdowns running on the strip that the dashboard doesn't know about."""
        for pos, cd in (await self.driver.countdowns()).items():
            if cd["on"] and pos not in self.timers and cd["remain"] > 0:
                self.timers[pos] = self.clock() + cd["remain"]
                self.expect[pos] = False
        self._save()

    # ---- polling ---------------------------------------------------------------------

    async def tick(self):
        now = self.clock()
        try:
            reading = await self.driver.read()
        except Exception as e:
            if self.online is not False:
                self._note(f"Power strip not answering: {e}")
            self.online, self.error = False, str(e)
            return
        if self.online is False:
            self._note("Power strip answering again")
        self.reading, self.online, self.error, self.updated = reading, True, None, now
        if now - self._last_daily > DAILY:
            self._last_daily = now
            try:
                await self._daily()
            except Exception as e:
                log.warning("strip daily upkeep failed: %s", e)
        self._record(now)
        self._track_ato(now)
        async with self._cmd_lock:
            await self._reconcile(now)

    def _record(self, now: float):
        for pos in self.outlets:
            o = self._out(pos)
            dq = self._recent[pos]
            dq.append((now, o.get("watts"), o.get("on")))
            while dq and dq[0][0] < now - HEALTH_WINDOW:
                dq.popleft()
        minute = int(now // 60)
        if self._minute is not None and minute != self._minute:
            self._flush_minute()
        self._minute = minute
        for pos in self.outlets:
            o = self._out(pos)
            b = self._bucket.setdefault(pos, [0.0, 0, False])
            if o.get("watts") is not None:
                b[0] += o["watts"]
                b[1] += 1
            b[2] = b[2] or bool(o.get("on"))

    def _flush_minute(self, force: bool = False):
        if self._minute is None or not self._bucket:
            return
        rows = [(self._minute, pos, (s / n) if n else None, on) for pos, (s, n, on) in self._bucket.items()]
        try:
            store.add_outlet_minutes(rows)
        except Exception as e:
            log.warning("couldn't save outlet history: %s", e)
        self._bucket = {}

    # ---- reconciling what the dashboard expects with what the strip does ---------

    async def _reconcile(self, now: float):
        changed = False
        # Timers: once due, the outlet should be back on (the strip does it).
        for pos, until in list(self.timers.items()):
            on = self._out(pos).get("on")
            if now >= until and on:
                del self.timers[pos]
                self.expect[pos] = True
                changed = True
            elif now >= until + LATE_GRACE and not on and pos != self.skimmer:
                self._note(f"{self.name(pos)} should have come back on by itself; turning it on")
                await self._switch({pos: True})
                del self.timers[pos]
                self.expect[pos] = True
                changed = True
            elif now >= until + LATE_GRACE and pos == self.skimmer:
                del self.timers[pos]           # the restart rule below takes over
                self.expect[pos] = True
                changed = True

        # Modes ending on their own.
        if self.mode and self.mode["name"] == "feed" and now >= self.mode["ends"]:
            self.mode = None
            changed = True
        if self.mode and self.mode["name"] == "maintenance":
            back_on = [p for p in self.outlets if self._out(p).get("on") and not self._expects(p)]
            if back_on:  # a power cut ends maintenance: the strip brings pumps back on
                self._note("Outlets came back on during maintenance (power cut?); maintenance ended")
                self.mode = None
                for p in self.outlets:
                    self.expect[p] = p not in self.held_off
                self.timers.clear()
                await self._lights("release")
                changed = True

        # Outlets switched by something else (a power cut, the strip's own buttons).
        for pos in self.outlets:
            on = self._out(pos).get("on")
            if on and not self._expects(pos) and pos not in self.timers and not self.mode:
                self.expect[pos] = True          # switched on at the strip: go with it
                self.held_off.discard(pos)
                changed = True

        # Skimmer: off without anyone asking (power cut) -> on once the pumps have run a while.
        if self._skimmer_waiting() is not None:
            left = self._skimmer_waiting()
            if left <= 0 and now - self._restore_tried > 60:
                self._restore_tried = now
                self._note("Skimmer back on after a power cut")
                await self._switch({self.skimmer: True})
        if changed:
            self._save()

    def _skimmer_waiting(self) -> float | None:
        """Seconds until the skimmer gets turned back on after a power cut;
        None when it isn't waiting for that."""
        p = self.skimmer
        if p is None or self.mode or p in self.timers or p in self.held_off or not self._expects(p):
            return None
        o = self._out(p)
        if o.get("on") is not False:
            return None
        pumps = [self._out(q) for q in self.pumps]
        if not pumps or not all(q.get("on") for q in pumps):
            return float(self._delay())          # waits for the pumps first
        ran = min((q.get("on_time") or 0) for q in pumps)
        return max(0.0, self._delay() - ran)

    # ---- ATO ------------------------------------------------------------------------

    def _track_ato(self, now: float):
        if self.ato is None:
            return
        o = self._out(self.ato)
        w = o.get("watts")
        if w is None:
            return
        if not o.get("on"):
            self._end_ato_run(now)
            return
        # Runs are short, so the low end of the last hour's readings is the idle draw.
        self._ato_idle.append(w)
        idle = self._ato_idle_watts()
        running = idle is not None and w >= idle + ATO_RUN_WATTS
        if running:
            if self._ato_run is None:
                half = self.cfg.strip_poll_seconds / 2
                self._ato_run = {"started": now - half, "peak": w}
            else:
                self._ato_run["peak"] = max(self._ato_run["peak"], w)
        else:
            self._end_ato_run(now)

    def _ato_idle_watts(self) -> float | None:
        if len(self._ato_idle) < 12:   # about a minute of readings first
            return None
        vals = sorted(self._ato_idle)
        return vals[len(vals) // 10]

    def _end_ato_run(self, now: float):
        run, self._ato_run = self._ato_run, None
        if run:
            seconds = max(1.0, now - self.cfg.strip_poll_seconds / 2 - run["started"])
            store.add_ato_run(run["started"], seconds, run["peak"])

    def ato_summary(self, now: float | None = None) -> dict:
        now = now or self.clock()
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        runs = store.ato_runs(midnight)
        week = store.ato_runs(midnight - 7 * 86400)
        usual = self._usual_runs(midnight, week)
        running_for = (now - self._ato_run["started"]) if self._ato_run else None
        long_run = self.settings["ato_long_run_seconds"]
        warnings = []
        if running_for and running_for > long_run:
            warnings.append(f"ATO has been running for {_mmss(running_for)}")
        longest = max((r["seconds"] for r in runs), default=0)
        if longest > long_run:
            warnings.append(f"An ATO run today lasted {_mmss(longest)}")
        if usual and len(runs) > usual * self.settings["ato_runs_factor"]:
            warnings.append(f"{len(runs)} ATO runs today; usually about {usual:g}")
        today_kwh = self._out(self.ato).get("today_kwh") if self.ato else None
        return {
            "runs": [{"started": r["started"], "seconds": round(r["seconds"])} for r in runs],
            "count": len(runs), "usual": usual, "longest": round(longest),
            "pump_seconds": round(sum(r["seconds"] for r in runs)),
            "running_for": round(running_for) if running_for else None,
            "strip_wh_today": None if today_kwh is None else round(today_kwh * 1000, 1),
            "warnings": warnings,
        }

    def _usual_runs(self, midnight: float, week: list[dict]) -> float | None:
        """Median runs per day over the last 7 days the dashboard watched (20 h+).
        Only changes at midnight, so it's worked out once a day."""
        if self.ato is None:
            return None
        if self._usual_cache and self._usual_cache[0] == midnight:
            return self._usual_cache[1]
        usual = self._usual_runs_uncached(midnight, week)
        self._usual_cache = (midnight, usual)
        return usual

    def _usual_runs_uncached(self, midnight: float, week: list[dict]) -> float | None:
        start_min = int((midnight - 7 * 86400) // 60)
        minutes = store.outlet_minutes(self.ato, start_min)
        counts = []
        for d in range(7):
            day0 = midnight - (7 - d) * 86400
            covered = sum(1 for m, _, _ in minutes if day0 // 60 <= m < (day0 + 86400) // 60)
            if covered >= 20 * 60:
                counts.append(sum(1 for r in week if day0 <= r["started"] < day0 + 86400))
        return statistics.median(counts) if len(counts) >= 3 else None

    # ---- pump health ------------------------------------------------------------------

    def _baseline(self, pos: int, now: float) -> float | None:
        cached = self._baselines.get(pos)
        if cached and now - cached[0] < 300:
            return cached[1]
        since = max(int(now // 60) - 1440, int(self.relearn.get(pos, 0) // 60))
        rows = store.outlet_minutes(pos, since)
        vals, run = [], 0
        for _, w, on in rows:
            run = run + 1 if on else 0
            # Skip each switch-on's first minutes, and minutes with nothing drawing
            # power (an empty outlet), so 0 W is never learned as "normal".
            if on and w is not None and w >= 0.5 and run > 2:
                vals.append(w)
        base = statistics.median(vals) if len(vals) >= BASELINE_MIN_MINUTES else None
        if base is not None and base < 0.5:
            base = None
        self._baselines[pos] = (now, base)
        return base

    def health(self, pos: int, now: float) -> dict:
        role = self.outlets[pos].role
        if role == "ato":
            return {"state": "na", "text": ""}
        o = self._out(pos)
        base = self._baseline(pos, now)
        if not o.get("on"):
            return {"state": "off", "text": f"Normal {base:.0f} W" if base else "", "normal": base}
        if (o.get("on_time") or 0) < HEALTH_MIN_ON:
            return {"state": "starting", "text": "Just switched on", "normal": base}
        ws = [w for t, w, on in self._recent[pos] if w is not None and on and t >= now - HEALTH_WINDOW]
        if not ws:
            return {"state": "unknown", "text": "No power reading", "normal": base}
        avg = sum(ws) / len(ws)
        if base is None:
            if avg < 0.5:
                return {"state": "learning", "text": "Drawing nothing: plugged in?", "normal": None}
            return {"state": "learning", "text": "Learning its normal draw", "normal": None}
        if base >= 2 and avg < 0.5:
            return {"state": "bad", "text": f"Drawing nothing (normal {base:.0f} W)", "normal": base}
        if avg < base * LOW:
            return {"state": "warn", "text": f"{100 - avg / base * 100:.0f}% below normal ({base:.0f} W)",
                    "normal": base}
        if avg > base * HIGH:
            return {"state": "warn", "text": f"{avg / base * 100 - 100:.0f}% above normal ({base:.0f} W)",
                    "normal": base}
        return {"state": "ok", "text": f"Normal ({base:.0f} W)", "normal": base}

    # ---- commands ---------------------------------------------------------------------

    async def _switch(self, changes: dict):
        await self.driver.switch(changes)
        self.reading = await self.driver.read()

    def _require_online(self):
        if not self.online:
            raise RuntimeError("The power strip isn't answering right now")

    async def outlet(self, pos: int, action: str, minutes: float | None = None) -> dict:
        if pos not in self.outlets:
            raise ValueError(f"no outlet {pos}")
        role = self.outlets[pos].role
        async with self._cmd_lock:
            self._require_online()
            now = self.clock()
            if action == "on":
                await self._switch({pos: True})
                self.timers.pop(pos, None)
                self.held_off.discard(pos)
                self.expect[pos] = True
            elif action == "off_for":
                if not minutes or not (1 <= minutes <= 24 * 60):
                    raise ValueError("pick between 1 minute and 24 hours")
                if self.mode:
                    raise ValueError(f"{self.mode['name'].capitalize()} mode is running")
                await self._switch({pos: (False, int(minutes * 60))})
                self.timers[pos] = now + minutes * 60
                self.held_off.discard(pos)
                self.expect[pos] = False
            elif action == "off":
                if role in PUMP_ROLES:
                    raise ValueError("pumps only go off with a timer, so they always come back on")
                if self.mode:
                    raise ValueError(f"{self.mode['name'].capitalize()} mode is running")
                await self._switch({pos: False})
                self.timers.pop(pos, None)
                self.held_off.add(pos)
                self.expect[pos] = False
            else:
                raise ValueError(f"unknown action {action!r}")
            self._save()
        return self.snapshot()

    async def feed(self, action: str) -> dict:
        async with self._cmd_lock:
            self._require_online()
            now = self.clock()
            feed, delay = int(self.settings["feed_minutes"] * 60), self._delay()
            if action == "start":
                if self.mode:
                    raise ValueError(f"{self.mode['name'].capitalize()} mode is already running")
                changes = {p: (False, feed) for p in self.pumps}
                if self.skimmer is not None and self._expects(self.skimmer) and self.skimmer not in self.held_off:
                    changes[self.skimmer] = (False, feed + delay)
                await self._switch(changes)
                for p, (_, secs) in changes.items():
                    self.timers[p] = now + secs
                    self.expect[p] = False
                self.mode = {"name": "feed", "started": now, "pumps_back": now + feed,
                             "ends": now + max(secs for _, secs in changes.values())}
                self._note("Feed started")
            elif action == "end":
                if not self.mode or self.mode["name"] != "feed":
                    raise ValueError("Feed mode isn't running")
                await self._switch({p: True for p in self.pumps})
                for p in self.pumps:
                    self.timers.pop(p, None)
                    self.expect[p] = True
                if self.skimmer in self.timers:
                    await self.driver.countdown_on(self.skimmer, max(1, delay))
                    self.timers[self.skimmer] = now + delay
                self.mode = None
                self._note("Feed ended early")
            else:
                raise ValueError(f"unknown action {action!r}")
            self._save()
        return self.snapshot()

    def _white_permille(self) -> int:
        return round(self.settings["maintenance_white_percent"] * 10)

    async def _lights(self, what: str):
        """Maintenance lights; a light problem never blocks the strip side."""
        if not self.lights:
            return
        try:
            if what == "hold":
                await self.lights.hold_white(self._white_permille())
            else:
                await self.lights.release()
        except Exception as e:
            log.warning("maintenance lights (%s) failed: %s", what, e)
            self._note(f"Lights couldn't be {'dimmed' if what == 'hold' else 'handed back'}: {e}")

    async def maintenance(self, action: str) -> dict:
        async with self._cmd_lock:
            self._require_online()
            now = self.clock()
            if action == "start":
                if self.mode and self.mode["name"] == "maintenance":
                    raise ValueError("Maintenance is already running")
                await self._switch({p: False for p in self.outlets})
                self.timers.clear()
                for p in self.outlets:
                    self.expect[p] = False
                self.mode = {"name": "maintenance", "started": now}
                self._save()
                self._note("Maintenance started")
                await self._lights("hold")
            elif action == "done":
                if not self.mode or self.mode["name"] != "maintenance":
                    raise ValueError("Maintenance isn't running")
                await self._switch({p: True for p in self.pumps})
                for p in self.pumps:
                    self.expect[p] = True
                delay = self._delay()
                for p in (self.skimmer, self.ato):
                    if p is None or p in self.held_off:
                        continue
                    if delay:
                        await self.driver.countdown_on(p, delay)
                        self.timers[p] = now + delay
                    else:
                        await self._switch({p: True})
                        self.expect[p] = True
                self.mode = None
                self._save()
                self._note("Maintenance done")
                await self._lights("release")
            else:
                raise ValueError(f"unknown action {action!r}")
        return self.snapshot()

    def update_settings(self, changes: dict) -> dict:
        for key, value in changes.items():
            if key not in SETTINGS_LIMITS:
                raise ValueError(f"unknown setting {key!r}")
            lo, hi = SETTINGS_LIMITS[key]
            value = float(value)
            if not lo <= value <= hi:
                raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
            self.settings[key] = value
        self._save()
        return self.snapshot()

    def rename(self, pos: int, name: str) -> dict:
        if pos not in self.outlets:
            raise ValueError(f"no outlet {pos}")
        name = name.strip()[:40]
        if name:
            self.names[pos] = name
        else:
            self.names.pop(pos, None)
        self._save()
        return self.snapshot()

    def relearn_normal(self, pos: int) -> dict:
        self.relearn[pos] = self.clock()
        self._baselines.pop(pos, None)
        self._save()
        return self.snapshot()

    # ---- history for the detail pages ----------------------------------------------------

    def outlet_history(self, pos: int, hours: float = 24) -> dict:
        """Average watts per minute (per 10 minutes beyond two days) while the
        dashboard was running; gaps are times the PC was off."""
        if pos not in self.outlets:
            raise ValueError(f"no outlet {pos}")
        hours = max(1, min(float(hours), 24 * 31))
        now = self.clock()
        rows = store.outlet_minutes(pos, int((now - hours * 3600) // 60))
        step = 10 if hours > 48 else 1
        buckets: dict[int, list] = {}
        for minute, w, on in rows:
            b = buckets.setdefault(minute // step * step, [0.0, 0, False])
            if w is not None:
                b[0] += w
                b[1] += 1
            b[2] = b[2] or on
        points = [[m * 60, round(s / n, 2) if n else None, on] for m, (s, n, on) in sorted(buckets.items())]
        return {"position": pos, "name": self.name(pos), "hours": hours, "step_minutes": step,
                "points": points, "normal": self._baseline(pos, now)}

    def ato_history(self, days: int = 14) -> dict:
        """Runs and pump time per day, and every run, for the last `days` days."""
        days = max(1, min(int(days), 120))
        now = self.clock()
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        start = (midnight.timestamp() - (days - 1) * 86400)
        runs = store.ato_runs(start)
        watched = {}
        if self.ato is not None:
            for minute, _, _ in store.outlet_minutes(self.ato, int(start // 60)):
                day = datetime.fromtimestamp(minute * 60).strftime("%Y-%m-%d")
                watched[day] = watched.get(day, 0) + 1
        out = []
        for d in range(days):
            day0 = datetime.fromtimestamp(start + d * 86400 + 3600).replace(hour=0, minute=0, second=0, microsecond=0)
            key = day0.strftime("%Y-%m-%d")
            day_runs = [r for r in runs if day0.timestamp() <= r["started"] < day0.timestamp() + 86400]
            out.append({"date": key, "runs": len(day_runs),
                        "seconds": round(sum(r["seconds"] for r in day_runs)),
                        "watched_hours": round(watched.get(key, 0) / 60, 1)})
        return {"days": out,
                "runs": [{"started": r["started"], "seconds": round(r["seconds"]),
                          "peak_watts": r["peak_watts"]} for r in runs],
                "settings": {k: self.settings[k] for k in ("ato_long_run_seconds", "ato_runs_factor")}}

    # ---- what the page shows ---------------------------------------------------------------

    def snapshot(self) -> dict:
        now = self.clock()
        outlets, warnings = [], []
        skimmer_wait = self._skimmer_waiting()
        # Energy today: the dashboard's own readings (fractions of a Wh) or the
        # strip's meter (whole Wh, includes time the PC was off), whichever is larger.
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        logged_today: dict[int, float] = {}
        for (pos, _), (_, total_w) in store.outlet_hour_sums(int(midnight // 60), int(now // 60) + 1).items():
            logged_today[pos] = logged_today.get(pos, 0.0) + total_w / 60
        for pos, cfg_o in sorted(self.outlets.items()):
            o = self._out(pos)
            until = self.timers.get(pos)
            health = self.health(pos, now) if self.reading else {"state": "unknown", "text": ""}
            unexpected = (o.get("on") is False and self._expects(pos) and pos not in self.timers
                          and not self.mode and pos != self.skimmer)
            item = {
                "position": pos, "role": cfg_o.role, "name": self.name(pos),
                "on": o.get("on"), "watts": o.get("watts"),
                "today_wh": (None if o.get("today_kwh") is None and pos not in logged_today
                             else round(max((o.get("today_kwh") or 0) * 1000, logged_today.get(pos, 0.0)), 2)),
                "timer_left": None if until is None else max(0, round(until - now)),
                "held_off": pos in self.held_off,
                "unexpected_off": unexpected,
                "restart_in": round(skimmer_wait) if pos == self.skimmer and skimmer_wait is not None else None,
                "health": health,
            }
            outlets.append(item)
            if unexpected:
                warnings.append(f"{item['name']} is off, but not by the dashboard")
            if health["state"] in ("warn", "bad"):
                warnings.append(f"{item['name']}: {health['text']}")
        if skimmer_wait is not None:
            warnings.append("Skimmer off after a power cut; it comes back on once the pumps have run "
                            f"{self.settings['skimmer_delay_minutes']:g} min")
        ato = self.ato_summary(now) if self.ato else None
        warnings += (ato or {}).get("warnings", [])
        mode = None
        if self.mode:
            mode = {"name": self.mode["name"], "started": self.mode["started"]}
            if self.mode["name"] == "feed":
                mode["pumps_left"] = max(0, round(self.mode["pumps_back"] - now))
                mode["ends_left"] = max(0, round(self.mode["ends"] - now))
        if self.online is False:
            warnings.insert(0, f"Power strip not answering ({self.error})")
        return {
            "online": self.online, "error": self.error, "updated": self.updated, "now": now,
            "clock": (self.reading or {}).get("clock"), "mode": mode, "settings": self.settings,
            "outlets": outlets, "ato": ato, "warnings": warnings, "notes": list(self.notes),
        }


def _mmss(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
