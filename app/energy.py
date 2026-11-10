"""Energy history for the Power page: totals per device, and trends.

Where the numbers come from (PLAN.md, Phase 3b):
- Strip outlets: the strip's own meter, read back from its memory (per hour
  for recent days, per day for this and last quarter, per month for this and
  last year). That covers times the PC was off.
- Prime HD: estimated from its color levels and its own per-color figures,
  logged every minute while the dashboard runs.
- Prime 16HD: measured by the light itself (input voltage x supply current),
  read over Bluetooth every ~2 minutes while the dashboard runs.
- Hours either light missed are estimated from its schedule ("schedule"
  source), only from the day energy tracking started. For the 16HD that
  estimate is scaled to match its own readings (idle draw + a ratio of the
  rated figures, learned from the last 7 days).
- Not measured: the heater.

Trends use `outlet_days` (daily summaries of the per-minute log, kept forever)
and the ATO run log.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from datetime import date, datetime, timedelta

from . import store
from .config import PUMP_ROLES, Config

log = logging.getLogger(__name__)

STATE_KEY = "energy"
HD = "light:prime_hd"
HD16 = "light:prime_16hd"
LIGHT_DEVICES = {"prime_hd": HD, "prime_16hd": HD16}
HD16_EVERY = 115             # s between 16HD Bluetooth reads, so the myAI app can still connect
HD16_IDLE_W, HD16_RATIO = 1.3, 0.72   # starting guesses (2026-11-06 readings) until learned
STRIP_REFRESH = 900          # today's strip figures, every 15 min
HOURLY_BACKFILL_DAYS = 7
TREND_WEEKS = 12
TREND_FLAG = 0.15            # weekly draw this far from the recent usual gets flagged
DAY_MIN_LOGGED = 10 * 60     # a day counts for pump trends with 10 h+ of readings
ATO_DAY_MIN_LOGGED = 20 * 60


def _midnight(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0)


def _quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def _epoch(d: date) -> int:
    return int(datetime(d.year, d.month, d.day).timestamp())


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def hd_minute_watts(ramps: list[dict], full_mw: dict[str, float]) -> list[float]:
    """Estimated watts for each minute of the day from a Prime HD schedule
    (ramps of {time (min), intensity 0-1000} per color, linear in between)."""
    out = [0.0] * 1440
    for ramp in ramps:
        full = full_mw.get(ramp.get("color"))
        pts = sorted((p["time"], p["intensity"]) for p in ramp.get("points", []))
        if not full or not pts:
            continue
        for m in range(1440):
            prev = next((p for p in reversed(pts) if p[0] <= m), (pts[-1][0] - 1440, pts[-1][1]))
            nxt = next((p for p in pts if p[0] > m), (pts[0][0] + 1440, pts[0][1]))
            span = nxt[0] - prev[0] or 1440
            level = prev[1] + (nxt[1] - prev[1]) * (m - prev[0]) / span
            out[m] += min(max(level, 0), 1000) / 1000 * full / 1000
    return out


def points_minute_watts(points: list[dict], full_mw: dict[str, float], master: str = "Brightness") -> list[float]:
    """Linear estimate per minute of the day from a 16HD schedule: points of
    {time (min), channels {VisualID name: 0-1000}}, linear in between, with the
    master Brightness channel scaling the others."""
    pts = sorted(points, key=lambda p: p["time"])
    out = [0.0] * 1440
    if not pts:
        return out
    names = {n for p in pts for n in p["channels"]}
    for m in range(1440):
        prev = next((p for p in reversed(pts) if p["time"] <= m), None)
        nxt = next((p for p in pts if p["time"] > m), None)
        prev_t = prev["time"] if prev else pts[-1]["time"] - 1440
        prev = prev or pts[-1]
        nxt_t = nxt["time"] if nxt else pts[0]["time"] + 1440
        nxt = nxt or pts[0]
        frac = (m - prev_t) / ((nxt_t - prev_t) or 1440)

        def level(n):
            a, b = prev["channels"].get(n, 0), nxt["channels"].get(n, 0)
            return min(max(a + (b - a) * frac, 0), 1000)
        bright = level(master) / 1000 if master in names else 1.0
        out[m] = bright * sum(level(n) / 1000 * full_mw[n] for n in names if n != master and n in full_mw) / 1000
    return out


def _minute_of_day(minute: int) -> int:
    t = datetime.fromtimestamp(minute * 60)
    return t.hour * 60 + t.minute


class EnergyService:
    def __init__(self, cfg: Config, strip=None, hd=None, hd16=None, clock=time.time):
        """strip: the StripService (its driver and outlet names). hd, hd16: the light drivers."""
        self.cfg = cfg
        self.strip = strip
        self.hd = hd
        self.hd16 = hd16
        self.clock = clock
        saved = store.get(STATE_KEY, {})
        self.since: float = saved.get("since") or clock()
        self.hd16_model: dict = saved.get("hd16_model") or {"idle": HD16_IDLE_W, "ratio": HD16_RATIO, "learned": False}
        if not saved.get("since"):
            self._save()
        self._schedule_hours: dict[str, tuple[str, list[float]]] = {}   # light -> (date, Wh per hour of day)
        self._hd16_linear: list[float] | None = None    # 16HD rated-figure estimate per minute of day
        self._last16 = 0.0
        self._last16_minute: int | None = None
        self._last_strip = 0.0
        self._last_daily = ""
        self._last_rollup = ""
        self._need_backfill = True
        self._task: asyncio.Task | None = None

    # ---- devices ----------------------------------------------------------------------

    def devices(self) -> list[dict]:
        out = []
        if self.strip:
            for pos, o in sorted(self.strip.outlets.items()):
                out.append({"id": f"outlet:{pos}", "name": self.strip.name(pos), "role": o.role,
                            "kind": "outlet", "measured": True})
        if self.hd:
            out.append({"id": HD, "name": "Prime HD", "role": "light", "kind": "light", "measured": False})
        if self.hd16:
            out.append({"id": HD16, "name": "Prime 16HD", "role": "light", "kind": "light", "measured": True})
        return out

    def _save(self):
        store.put(STATE_KEY, {"since": self.since, "hd16_model": self.hd16_model})

    def _lights(self) -> list[tuple[str, object]]:
        return [(light, drv) for light, drv in (("prime_hd", self.hd), ("prime_16hd", self.hd16)) if drv]

    # ---- background work ----------------------------------------------------------------

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def close(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("energy upkeep failed")
            await asyncio.sleep(60)

    async def tick(self):
        """Each step is only marked done once it worked, so a strip that isn't
        answering yet (e.g. right after start-up) is simply tried again."""
        now = self.clock()
        await self._log_lights(now)
        today = _midnight(now).date().isoformat()
        if self._last_rollup != today:
            self._last_rollup = today
            for light, _ in self._lights():
                await self._light_schedule_hours(light, now, force=True)
            self._learn_hd16(now)
            self.roll_outlet_days(now)
        if self._last_daily != today and await self._strip_long(now):
            self._last_daily = today
        if self._need_backfill or now - self._last_strip > STRIP_REFRESH:
            if await self._strip_hours(now, HOURLY_BACKFILL_DAYS if self._need_backfill else 2):
                self._last_strip = now
                self._need_backfill = False
        await self.roll_light_hours(now)

    async def _log_lights(self, now: float):
        minute = int(now // 60)
        if self.hd:
            try:
                w = await self.hd.current_watts()
                if w is not None:
                    store.add_light_minute(minute, "prime_hd", w)
            except Exception as e:
                log.debug("Prime HD reading for energy failed: %s", e)
        if self.hd16 and now - self._last16 >= HD16_EVERY:
            self._last16 = now
            try:
                w = (await self.hd16.power()).get("watts")
            except Exception as e:
                log.debug("Prime 16HD reading for energy failed: %s", e)
                return
            if w is None:
                return
            # Each reading stands for the minutes since the previous one (up to 3).
            first = minute - 1 if self._last16_minute is None else max(self._last16_minute + 1, minute - 2)
            for m in range(first, minute + 1):
                store.add_light_minute(m, "prime_16hd", w)
            self._last16_minute = minute

    # ---- the strip's own memory -------------------------------------------------------------

    def _outlet_positions(self) -> list[int]:
        return sorted(self.strip.outlets) if self.strip else []

    async def _strip_hours(self, now: float, days: int) -> bool:
        if not self.strip:
            return True
        if not self.strip.online:
            return False
        mid = _midnight(now)
        reqs = []
        for pos in self._outlet_positions():
            for d in range(days):
                start = int((mid - timedelta(days=d)).timestamp())
                reqs.append((pos, start, start + 86399, 60))
        rows = []
        for (pos, start, _, _), data in zip(reqs, await self.strip.driver.energy_data(reqs)):
            if data is None:
                continue
            day = datetime.fromtimestamp(start)
            for h, wh in enumerate(data[:25]):
                t = int((day + timedelta(hours=h)).timestamp())
                if t <= now:
                    rows.append(("H", t, f"outlet:{pos}", float(wh), "strip"))
        # Today's daily figure too, so day totals are current.
        reqs = [(pos, _epoch(_quarter_start(mid.date())), int(now), 1440) for pos in self._outlet_positions()]
        for (pos, start, _, _), data in zip(reqs, await self.strip.driver.energy_data(reqs)):
            if data:
                rows += self._day_rows(pos, start, data, now)
        store.put_energy(rows)
        return True

    def _day_rows(self, pos: int, start: int, data: list[int], now: float) -> list[tuple]:
        rows, d0 = [], date.fromtimestamp(start)
        for i, wh in enumerate(data):
            d = d0 + timedelta(days=i)
            if _epoch(d) > now:
                break
            rows.append(("D", _epoch(d), f"outlet:{pos}", float(wh), "strip"))
        return rows

    async def _strip_long(self, now: float) -> bool:
        """Per day for this and last quarter, per month for this and last year."""
        if not self.strip:
            return True
        if not self.strip.online:
            return False
        today = date.fromtimestamp(now)
        q = _quarter_start(today)
        prev_q = _quarter_start(q - timedelta(days=1))
        reqs, kinds = [], []
        for pos in self._outlet_positions():
            reqs.append((pos, _epoch(prev_q), _epoch(q) - 1, 1440)); kinds.append("D")
            reqs.append((pos, _epoch(q), int(now), 1440)); kinds.append("D")
            reqs.append((pos, _epoch(date(today.year - 1, 1, 1)), _epoch(date(today.year, 1, 1)) - 1, 43200)); kinds.append("M")
            reqs.append((pos, _epoch(date(today.year, 1, 1)), int(now), 43200)); kinds.append("M")
        rows = []
        for (pos, start, _, _), kind, data in zip(reqs, kinds, await self.strip.driver.energy_data(reqs)):
            if not data:
                continue
            if kind == "D":
                rows += self._day_rows(pos, start, data, now)
            else:
                m0 = date.fromtimestamp(start)
                for i, wh in enumerate(data[:12]):
                    m = _add_months(m0, i)
                    if _epoch(m) <= now:
                        rows.append(("M", _epoch(m), f"outlet:{pos}", float(wh), "strip"))
        store.put_energy(rows)
        return True

    # ---- lights: logged readings, or a schedule estimate when not logged --------------------

    async def _light_schedule_hours(self, light: str, now: float, force: bool = False) -> list[float] | None:
        """Estimated Wh for each hour of the day from a light's current schedule."""
        today = date.fromtimestamp(now).isoformat()
        cached = self._schedule_hours.get(light)
        if cached and cached[0] == today and not force:
            return cached[1]
        try:
            if light == "prime_hd" and self.hd:
                full = await self.hd.full_power_mw()
                if not full:
                    return cached[1] if cached else None
                per_min = hd_minute_watts((await self.hd.get_schedule()).get("ramps", []), full)
            elif light == "prime_16hd" and self.hd16:
                full = await self.hd16.channel_power_mw()
                self._hd16_linear = points_minute_watts(await self.hd16.get_schedule(), full)
                per_min = self._hd16_scaled()
            else:
                return None
        except Exception as e:
            log.debug("%s schedule for energy failed: %s", light, e)
            return cached[1] if cached else None
        hours = [sum(per_min[h * 60:(h + 1) * 60]) / 60 for h in range(24)]
        self._schedule_hours[light] = (today, hours)
        return hours

    def _hd16_scaled(self) -> list[float]:
        idle, ratio = self.hd16_model["idle"], self.hd16_model["ratio"]
        return [idle + ratio * w for w in (self._hd16_linear or [0.0] * 1440)]

    def _learn_hd16(self, now: float):
        """Fits the 16HD's schedule estimate to its own readings (last 7 days):
        idle draw from dark minutes, and the measured/rated ratio from lit ones."""
        if not self._hd16_linear:
            return
        minute = int(now // 60)
        rows = store.light_minutes("prime_16hd", minute - 7 * 1440, minute + 1)
        dark = [w for m, w in rows if self._hd16_linear[_minute_of_day(m)] < 0.3]
        idle = statistics.median(dark) if len(dark) >= 10 else HD16_IDLE_W
        ratios = []
        for m, w in rows:
            lin = self._hd16_linear[_minute_of_day(m)]
            if lin > 3:
                ratios.append((w - idle) / lin)
        ratio = min(1.5, max(0.3, statistics.median(ratios))) if len(ratios) >= 10 else HD16_RATIO
        self.hd16_model = {"idle": round(idle, 2), "ratio": round(ratio, 3), "learned": len(ratios) >= 10}
        self._save()
        if "prime_16hd" in self._schedule_hours:
            per_min = self._hd16_scaled()
            day = self._schedule_hours["prime_16hd"][0]
            self._schedule_hours["prime_16hd"] = (day, [sum(per_min[h * 60:(h + 1) * 60]) / 60 for h in range(24)])

    async def roll_light_hours(self, now: float):
        """Fills in each light's finished hours since tracking started (last 7 days at most)."""
        this_hour = int(now // 3600 * 3600)
        start = max(int(self.since // 3600 * 3600), this_hour - 7 * 86400)
        for light, _ in self._lights():
            dev = LIGHT_DEVICES[light]
            sched = await self._light_schedule_hours(light, now)
            done = {r["start"] for r in store.energy("H", start, this_hour) if r["device"] == dev}
            rows = []
            for t in range(start, this_hour, 3600):
                if t in done and t < this_hour - 3 * 3600:
                    continue           # settled hours stay as they are
                mins = store.light_minutes(light, t // 60, t // 60 + 60)
                sched_avg = sched[datetime.fromtimestamp(t).hour] if sched else None
                logged = sum(w for _, w in mins) / 60
                if len(mins) >= 30 or (mins and sched_avg is None):
                    rest = (60 - len(mins)) / 60 * (sched_avg if sched_avg is not None else (logged * 60 / len(mins)))
                    rows.append(("H", t, dev, round(logged + rest, 2), "log"))
                elif sched_avg is not None:
                    rows.append(("H", t, dev, round(logged + (60 - len(mins)) / 60 * sched_avg, 2), "schedule"))
            store.put_energy(rows)

    roll_hd_hours = roll_light_hours   # older name

    # ---- daily outlet summaries (for trends) -------------------------------------------------

    def roll_outlet_days(self, now: float):
        """Summarises each finished day of the per-minute log (the last 120 days)."""
        if not self.strip:
            return
        today = date.fromtimestamp(now)
        first = today - timedelta(days=120)
        have = {(r["day"], r["outlet"]) for r in store.outlet_days(first.isoformat())}
        rows = []
        for pos in self._outlet_positions():
            minutes = store.outlet_minutes(pos, _epoch(first) // 60)
            by_day: dict[str, list] = {}
            for m, w, on in minutes:
                day = date.fromtimestamp(m * 60).isoformat()
                b = by_day.setdefault(day, [0, 0, 0.0, 0])
                b[0] += 1
                if on and w is not None:
                    b[1] += 1
                    b[2] += w
                if on:
                    b[3] += 1
            for day, (logged, on_with_w, w_sum, on_min) in by_day.items():
                if day >= today.isoformat() or (day, pos) in have:
                    continue
                rows.append((day, pos, logged, on_min, round(w_sum / on_with_w, 2) if on_with_w else None))
        store.put_outlet_days(rows)

    # ---- what the Power page asks for -------------------------------------------------------

    def _hourly(self, start: int, end: int) -> dict[str, dict[int, tuple[float, str]]]:
        """{device: {hour start: (Wh, source)}} for hours in [start, end).

        Outlets combine two meters: the strip's own hourly figure (whole Wh,
        covers times the PC was off) and the dashboard's per-minute readings
        (fractions of a Wh, and a running total for the hour in progress). A
        logged hour uses the readings, topped up from the strip's figure for any
        minutes the dashboard missed. The Prime HD uses its rolled-up hours plus
        the hour in progress from its minute log."""
        now = self.clock()
        current = int(now // 3600 * 3600)
        strip_h: dict[tuple[str, int], float] = {}
        light_rows: dict[str, dict[int, tuple[float, str]]] = {dev: {} for dev in LIGHT_DEVICES.values()}
        for r in store.energy("H", start, end):
            if r["device"] in light_rows:
                light_rows[r["device"]][r["start"]] = (r["wh"], r["source"])
            else:
                strip_h[(r["device"], r["start"])] = r["wh"]
        logs = store.outlet_hour_sums(start // 60, end // 60 + 1)
        out: dict[str, dict[int, tuple[float, str]]] = {}
        for pos in self._outlet_positions():
            dev = f"outlet:{pos}"
            res = {}
            for h in {h for (d, h) in strip_h if d == dev} | {h for (p, h) in logs if p == pos}:
                if not start <= h < end:
                    continue
                s = strip_h.get((dev, h))
                n, total_w = logs.get((pos, h), (0, 0.0))
                logged = total_w / 60
                if n == 0:
                    res[h] = (s or 0.0, "strip")
                elif h >= current:
                    res[h] = (logged, "log")                    # the hour in progress, so far
                else:
                    fill = s if s is not None else logged * 60 / n
                    res[h] = (logged + fill * max(0, 60 - n) / 60, "log" if n >= 30 else "strip")
            out[dev] = res
        for light, _ in self._lights():
            dev = LIGHT_DEVICES[light]
            res = dict(light_rows[dev])
            for h, (n, total_w) in store.light_hour_sums(light, start // 60, end // 60 + 1).items():
                if h not in res and start <= h < end:
                    res[h] = (total_w / 60, "log")              # not rolled up yet (hour in progress)
            out[dev] = res
        return out

    def summary(self, span: str = "week") -> dict:
        now = self.clock()
        mid = _midnight(now)
        if span == "day":
            period, start, buckets = "H", mid, [mid + timedelta(hours=h) for h in range(24)]
        elif span == "month":
            period, start = "D", mid - timedelta(days=29)
            buckets = [start + timedelta(days=i) for i in range(30)]
        elif span == "year":
            first = _add_months(mid.date().replace(day=1), -11)
            period, start = "M", datetime(first.year, first.month, 1)
            buckets = [datetime.combine(_add_months(first, i), datetime.min.time()) for i in range(12)]
        else:
            span, period, start = "week", "D", mid - timedelta(days=6)
            buckets = [start + timedelta(days=i) for i in range(7)]
        keys = [int(b.timestamp()) for b in buckets]
        end = int(now)
        devices = self.devices()
        values = {d["id"]: [0.0] * len(keys) for d in devices}
        sources = {d["id"]: set() for d in devices}

        def bucket_of(t: int) -> int | None:
            for i in range(len(keys) - 1, -1, -1):
                if t >= keys[i]:
                    return i
            return None

        hourly = self._hourly(keys[0], end + 1)
        strip_d: dict[str, list[dict]] = {}
        strip_m: dict[str, list[dict]] = {}
        if period != "H":
            for r in store.energy("D", keys[0], end + 1):
                strip_d.setdefault(r["device"], []).append(r)
        if period == "M":
            for r in store.energy("M", keys[0], end + 1):
                strip_m.setdefault(r["device"], []).append(r)

        def day_of(t: int) -> int:
            return int(_midnight(t).timestamp())

        for d in devices:
            dev = d["id"]
            hrs = hourly.get(dev, {})
            for _, src in hrs.values():
                sources[dev].add(src)
            if period == "H":
                for h, (wh, _) in hrs.items():
                    i = bucket_of(h)
                    if i is not None:
                        values[dev][i] += wh
                continue
            # A day is its hours added up, or the strip's day figure if that's
            # larger (hours nobody logged, e.g. before tracking started).
            days: dict[int, float] = {}
            for h, (wh, _) in hrs.items():
                days[day_of(h)] = days.get(day_of(h), 0.0) + wh
            for r in strip_d.get(dev, []):
                if r["wh"] > days.get(r["start"], 0.0):
                    days[r["start"]] = r["wh"]
                    sources[dev].add("strip")
            for t, wh in days.items():
                i = bucket_of(t)
                if i is not None:
                    values[dev][i] += wh
            for r in strip_m.get(dev, []):      # likewise a month vs the strip's month figure
                i = bucket_of(r["start"])
                if i is not None and r["wh"] > values[dev][i]:
                    values[dev][i] = r["wh"]
                    sources[dev].add("strip")
        hours = max(1 / 60, (now - keys[0]) / 3600)
        total = sum(sum(v) for v in values.values())
        out_devices = []
        for d in devices:
            wh = sum(values[d["id"]])
            out_devices.append({**d, "wh": round(wh, 1), "avg_watts": round(wh / hours, 1),
                                "share": round(wh / total * 100, 1) if total else 0,
                                "sources": sorted(sources[d["id"]])})
        label_fmt = {"H": "%H:00", "D": "%a %d", "M": "%b"}[period]
        return {
            "span": span, "period": period,
            "buckets": [{"start": k, "label": datetime.fromtimestamp(k).strftime(label_fmt),
                         "values": {dev: round(v[i], 1) for dev, v in values.items()}} for i, k in enumerate(keys)],
            "devices": out_devices, "total_wh": round(total, 1), "hours": round(hours, 1),
            "since": self.since,
            "not_included": ["Heater (on the Inkbird)"],
        }

    def trends(self) -> dict:
        now = self.clock()
        today = date.fromtimestamp(now)
        monday = today - timedelta(days=today.weekday())
        first = monday - timedelta(weeks=TREND_WEEKS)
        days = store.outlet_days(first.isoformat())
        weeks = [first + timedelta(weeks=i) for i in range(TREND_WEEKS)]   # complete weeks only

        def week_of(day: str) -> int | None:
            d = date.fromisoformat(day)
            i = (d - first).days // 7
            return i if 0 <= i < TREND_WEEKS else None

        pumps = []
        for pos, o in sorted(self.strip.outlets.items()) if self.strip else []:
            if o.role not in PUMP_ROLES and o.role != "skimmer":
                continue
            acc = [[0.0, 0] for _ in weeks]
            daily = []
            for r in days:
                if r["outlet"] != pos or r["logged_minutes"] < DAY_MIN_LOGGED or r["avg_on_watts"] is None:
                    continue
                daily.append({"day": r["day"], "watts": r["avg_on_watts"]})
                i = week_of(r["day"])
                if i is not None and r["on_minutes"]:
                    acc[i][0] += r["avg_on_watts"] * r["on_minutes"]
                    acc[i][1] += r["on_minutes"]
            weekly = [round(s / n, 1) if n else None for s, n in acc]
            flag = None
            known = [w for w in weekly if w is not None]
            if len(known) >= 3:
                latest, before = known[-1], known[-5:-1]
                usual = statistics.median(before)
                change = (latest - usual) / usual if usual else 0
                if abs(change) >= TREND_FLAG:
                    flag = (f"Last week {latest:g} W vs usual {usual:g} W "
                            f"({'+' if change > 0 else ''}{change * 100:.0f}%)")
            pumps.append({"position": pos, "name": self.strip.name(pos), "role": o.role,
                          "weekly": weekly, "daily": daily[-30:], "flag": flag})

        # Evaporation: ATO runs and pump time per watched day, averaged per week.
        ato = self.strip.ato if self.strip else None
        watched = {r["day"] for r in days if r["outlet"] == ato and r["logged_minutes"] >= ATO_DAY_MIN_LOGGED}
        runs = store.ato_runs(_epoch(first))
        per_day: dict[str, list] = {}
        for r in runs:
            day = date.fromtimestamp(r["started"]).isoformat()
            b = per_day.setdefault(day, [0, 0.0])
            b[0] += 1
            b[1] += r["seconds"]
        evap = [[0, 0.0, 0] for _ in weeks]
        for day in watched:
            i = week_of(day)
            if i is not None:
                n, s = per_day.get(day, [0, 0.0])
                evap[i][0] += n
                evap[i][1] += s
                evap[i][2] += 1
        evaporation = [{"week": w.isoformat(),
                        "runs_per_day": round(n / k, 1) if k else None,
                        "seconds_per_day": round(s / k) if k else None,
                        "days": k} for w, (n, s, k) in zip(weeks, evap)]

        # Each light per day, with the days a schedule was sent.
        mid = _midnight(now)
        start = mid - timedelta(days=29)
        lights = [d for d in self.devices() if d["kind"] == "light"]
        ids = {d["id"] for d in lights}
        hourly = [r for r in store.energy("H", int(start.timestamp()), int(now) + 1) if r["device"] in ids]
        light_days = []
        for i in range(30):
            d0 = int((start + timedelta(days=i)).timestamp())
            d1 = int((start + timedelta(days=i + 1)).timestamp())
            values, estimated = {}, {}
            for d in lights:
                rs = [r for r in hourly if r["device"] == d["id"] and d0 <= r["start"] < d1]
                values[d["id"]] = round(sum(r["wh"] for r in rs), 1) if rs else None
                estimated[d["id"]] = sum(1 for r in rs if r["source"] == "schedule")
            light_days.append({"day": (start + timedelta(days=i)).date().isoformat(),
                               "values": values, "estimated_hours": estimated})
        sends = sorted({h["created"][:10] for h in store.list_history(200)
                        if h["created"][:10] >= start.date().isoformat()})
        return {"weeks": [w.isoformat() for w in weeks], "pumps": pumps, "evaporation": evaporation,
                "lights": [{"id": d["id"], "name": d["name"]} for d in lights],
                "light_days": light_days, "schedule_sends": sends, "hd16_model": self.hd16_model}
