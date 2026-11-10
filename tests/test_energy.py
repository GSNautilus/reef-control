"""Energy history: strip back-fill, Prime HD estimates, totals and trends.
Run: python -m unittest discover -s tests -t ."""
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app import store
from app.config import Config, Outlet
from app.energy import HD, HD16, EnergyService, hd_minute_watts, points_minute_watts

OUTLETS = {1: Outlet(1, "return", "Return 1"), 3: Outlet(3, "circulation", "Circulation 1"),
           5: Outlet(5, "skimmer", "Skimmer"), 6: Outlet(6, "ato", "ATO")}
FULL = {"royal": 13440.0, "blue": 8712.0}
# Noon peak: royal 0 until 10:00, 100% at 12:00, 0 again at 23:00.
RAMPS = [{"color": "royal", "points": [{"time": 0, "intensity": 0}, {"time": 600, "intensity": 0},
                                       {"time": 720, "intensity": 1000}, {"time": 1380, "intensity": 0}]}]
NOW = datetime(2026, 11, 18, 15, 30).timestamp()   # a Wednesday afternoon


class FakeDriver:
    def __init__(self):
        self.requests = []

    async def energy_data(self, reqs):
        self.requests += reqs
        out = []
        for pos, start, end, interval in reqs:
            n = (end - start) // (interval * 60) + 1
            out.append([pos * 10] * min(int(n), 92 if interval == 1440 else 400))
        return out


class FakeHD:
    def __init__(self, watts=20.0):
        self.watts = watts

    async def current_watts(self):
        return self.watts

    async def full_power_mw(self):
        return FULL

    async def get_schedule(self):
        return {"name": "default", "ramps": RAMPS}


FULL16 = {"RoyalBlue": 13440.0, "Blue": 13478.0}
POINTS16 = [{"time": 600, "flags": 1, "channels": {"Brightness": 1000}},
            {"time": 720, "flags": 1, "channels": {"Brightness": 1000, "RoyalBlue": 1000}},
            {"time": 1380, "flags": 1, "channels": {"Brightness": 1000}}]


class FakeHD16:
    """Measures idle 1.3 W + 70% of its rated figures, like the real one roughly does."""

    def __init__(self, clock):
        self.clock = clock
        self.reads = 0

    async def power(self):
        self.reads += 1
        t = datetime.fromtimestamp(self.clock())
        lin = points_minute_watts(POINTS16, FULL16)[t.hour * 60 + t.minute]
        return {"watts": round(1.3 + 0.7 * lin, 2)}

    async def channel_power_mw(self):
        return FULL16

    async def get_schedule(self):
        return POINTS16


class EnergyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db = store.DB_PATH
        store.DB_PATH = Path(self.tmp.name) / "test.db"
        self.t = NOW
        self.driver = FakeDriver()
        strip = SimpleNamespace(outlets=OUTLETS, online=True, driver=self.driver, ato=6,
                                name=lambda pos: OUTLETS[pos].name)
        cfg = Config(port=0, prime_hd_host="", prime_16hd_address="", prime_16hd_serial="", live_idle_timeout=120)
        self.hd = FakeHD()
        self.svc = EnergyService(cfg, strip, self.hd, clock=lambda: self.t)

    async def asyncTearDown(self):
        store.DB_PATH = self._db
        self.tmp.cleanup()

    def test_schedule_minute_watts(self):
        w = hd_minute_watts(RAMPS, FULL)
        self.assertAlmostEqual(w[720], 13.44)
        self.assertAlmostEqual(w[660], 6.72)
        self.assertEqual(w[300], 0)

    async def test_backfill_waits_for_the_strip(self):
        self.svc.strip.online = None                        # just started, no answer yet
        await self.svc.tick()
        self.assertEqual(self.driver.requests, [])
        self.svc.strip.online = True
        self.t += 60
        await self.svc.tick()                                # tried again a minute later
        self.assertEqual(len([r for r in self.driver.requests if r[3] == 60]), 4 * 7)
        self.assertTrue(store.energy("M", 0, int(self.t)))

    async def test_strip_backfill_stores_hours_and_days(self):
        await self.svc.tick()
        mid = int(datetime(2026, 11, 18).timestamp())
        hours = [r for r in store.energy("H", mid, mid + 86400) if r["device"] == "outlet:1"]
        self.assertEqual(len(hours), 16)                     # 00:00 .. 15:00, nothing in the future
        self.assertEqual(hours[0]["wh"], 10.0)
        days = [r for r in store.energy("D", mid - 3 * 86400, mid + 86400) if r["device"] == "outlet:3"]
        self.assertEqual([r["wh"] for r in days], [30.0] * 4)
        intervals = {r[3] for r in self.driver.requests}
        self.assertEqual(intervals, {60, 1440, 43200})

    async def test_prime_hd_hours_logged_then_estimated(self):
        self.svc.since = NOW - 3 * 3600                     # tracking started at 12:30
        hour = int(datetime(2026, 11, 18, 13).timestamp())
        for m in range(hour // 60, hour // 60 + 60):         # 13:00 logged at 20 W all hour
            store.add_light_minute(m, "prime_hd", 20.0)
        await self.svc.roll_hd_hours(self.t)
        rows = {r["start"]: r for r in store.energy("H", hour - 7200, hour + 3 * 3600) if r["device"] == HD}
        self.assertEqual(rows[hour]["source"], "log")
        self.assertAlmostEqual(rows[hour]["wh"], 20.0)
        self.assertEqual(rows[hour + 3600]["source"], "schedule")      # 14:00 not logged
        expected = sum(hd_minute_watts(RAMPS, FULL)[14 * 60:15 * 60]) / 60
        self.assertAlmostEqual(rows[hour + 3600]["wh"], expected, places=1)
        self.assertIn(hour - 3600, rows)                                # 12:00: tracking began at 12:30
        self.assertNotIn(hour - 7200, rows)                             # 11:00: before tracking started
        self.assertNotIn(hour + 2 * 3600, rows)                         # 15:00 isn't finished yet

    async def test_summary_week_splits_by_device(self):
        mid = datetime(2026, 11, 18)
        rows = []
        for d in range(7):
            start = int((mid - timedelta(days=d)).timestamp())
            rows += [("D", start, "outlet:1", 960.0, "strip"), ("D", start, "outlet:3", 240.0, "strip")]
            rows.append(("H", start + 12 * 3600, HD, 100.0, "log"))
        store.put_energy(rows)
        s = self.svc.summary("week")
        self.assertEqual(len(s["buckets"]), 7)
        by = {d["id"]: d for d in s["devices"]}
        self.assertEqual(by["outlet:1"]["wh"], 6720.0)
        self.assertEqual(by[HD]["wh"], 700.0)
        self.assertEqual(s["total_wh"], 6720 + 1680 + 700)
        self.assertAlmostEqual(sum(d["share"] for d in s["devices"]), 100, delta=0.5)
        self.assertEqual(s["buckets"][-1]["values"]["outlet:1"], 960.0)

    async def test_running_total_counts_small_loads_in_the_hour_in_progress(self):
        hour = int(datetime(2026, 11, 18, 15).timestamp())          # NOW is 15:30
        store.put_energy([("H", hour, "outlet:6", 0.0, "strip"),     # the strip: whole Wh only
                          ("D", int(datetime(2026, 11, 18).timestamp()), "outlet:6", 0.0, "strip")])
        store.add_outlet_minutes([(hour // 60 + m, 6, 3.6, True) for m in range(23)])
        day = {d["id"]: d for d in self.svc.summary("day")["devices"]}
        self.assertAlmostEqual(day["outlet:6"]["wh"], 1.4, places=1)     # 3.6 W x 23 min = 1.38 Wh
        week = self.svc.summary("week")
        self.assertAlmostEqual(week["buckets"][-1]["values"]["outlet:6"], 1.4, places=1)

    async def test_partly_logged_hour_is_topped_up_from_the_strip(self):
        hour = int(datetime(2026, 11, 18, 13).timestamp())
        store.put_energy([("H", hour, "outlet:1", 40.0, "strip")])
        store.add_outlet_minutes([(hour // 60 + m, 1, 40.0, True) for m in range(30)])
        hourly = self.svc._hourly(hour, hour + 3600)["outlet:1"]
        self.assertAlmostEqual(hourly[hour][0], 40.0)                  # 20 Wh logged + half of 40 Wh

    async def test_prime_hd_hour_in_progress_counts(self):
        hour = int(datetime(2026, 11, 18, 15).timestamp())
        for m in range(30):
            store.add_light_minute(hour // 60 + m, "prime_hd", 12.0)
        day = {d["id"]: d for d in self.svc.summary("day")["devices"]}
        self.assertAlmostEqual(day[HD]["wh"], 6.0)                       # 12 W for half an hour

    async def test_older_days_use_the_strip_day_figure(self):
        old_day = int(datetime(2026, 11, 2).timestamp())               # no hourly data that far back
        store.put_energy([("D", old_day, "outlet:3", 432.0, "strip")])
        month = self.svc.summary("month")
        i = [b["start"] for b in month["buckets"]].index(old_day)
        self.assertEqual(month["buckets"][i]["values"]["outlet:3"], 432.0)

    def test_16hd_schedule_minute_watts_with_master_brightness(self):
        w = points_minute_watts(POINTS16, FULL16)
        self.assertAlmostEqual(w[720], 13.44)
        self.assertAlmostEqual(w[660], 6.72)
        dimmed = [dict(p, channels={**p["channels"], "Brightness": 500}) for p in POINTS16]
        self.assertAlmostEqual(points_minute_watts(dimmed, FULL16)[720], 6.72)

    async def test_16hd_is_read_every_two_minutes_and_fills_its_minutes(self):
        hd16 = FakeHD16(lambda: self.t)
        svc = EnergyService(self.svc.cfg, self.svc.strip, None, hd16, clock=lambda: self.t)
        svc.since = NOW - 3600
        for _ in range(6):                                  # six minutes of ticks
            await svc._log_lights(self.t)
            self.t += 60
        self.assertEqual(hd16.reads, 3)                     # every ~2 minutes, not every minute
        mins = store.light_minutes("prime_16hd", int(NOW // 60) - 5, int(NOW // 60) + 10)
        self.assertEqual(len(mins), 6)                      # each reading covers the minutes since the last
        self.assertIn(HD16, {d["id"] for d in svc.devices()})
        self.assertNotIn("16HD", " ".join(svc.summary("day")["not_included"]))

    async def test_16hd_estimate_learns_from_its_readings(self):
        hd16 = FakeHD16(lambda: self.t)
        svc = EnergyService(self.svc.cfg, None, None, hd16, clock=lambda: self.t)
        await svc._light_schedule_hours("prime_16hd", self.t, force=True)
        lin = points_minute_watts(POINTS16, FULL16)
        day0 = int(datetime(2026, 11, 17).timestamp()) // 60
        store_rows = [(day0 + m, round(1.1 + 0.6 * lin[m], 2)) for m in range(0, 1440, 5)]
        for m, w in store_rows:
            store.add_light_minute(m, "prime_16hd", w)
        svc._learn_hd16(self.t)
        self.assertTrue(svc.hd16_model["learned"])
        self.assertAlmostEqual(svc.hd16_model["idle"], 1.1, places=2)
        self.assertAlmostEqual(svc.hd16_model["ratio"], 0.6, places=2)
        noon = svc._schedule_hours["prime_16hd"][1][12]      # the hour from 12:00 uses the learned fit
        self.assertAlmostEqual(noon, 1.1 + 0.6 * sum(lin[720:780]) / 60, places=1)

    async def test_daily_rollup_and_pump_trend_flag(self):
        monday = date(2026, 11, 16)
        rows = []
        for w in range(1, 7):                                # six complete weeks before this one
            for d in range(7):
                day = monday - timedelta(weeks=w) + timedelta(days=d)
                watts = 30.0 if w == 1 else 40.0             # last week 25% lower
                rows.append((day.isoformat(), 1, 1440, 1440, watts))
                rows.append((day.isoformat(), 3, 1440, 1440, 18.0))
        store.put_outlet_days(rows)
        t = self.svc.trends()
        pumps = {p["position"]: p for p in t["pumps"]}
        self.assertIn("-25%", pumps[1]["flag"])
        self.assertIsNone(pumps[3]["flag"])
        self.assertEqual(pumps[1]["weekly"][-1], 30.0)

    async def test_roll_outlet_days_from_minutes(self):
        day0 = int(datetime(2026, 11, 17).timestamp()) // 60
        store.add_outlet_minutes([(day0 + m, 1, 40.0 if m % 2 else 42.0, True) for m in range(720)] +
                                 [(day0 + 720 + m, 1, 0.0, False) for m in range(60)])
        self.svc.roll_outlet_days(self.t)
        r = [x for x in store.outlet_days("2026-11-17") if x["outlet"] == 1][0]
        self.assertEqual((r["logged_minutes"], r["on_minutes"]), (780, 720))
        self.assertAlmostEqual(r["avg_on_watts"], 41.0)

    async def test_evaporation_counts_only_watched_days(self):
        monday = date(2026, 11, 16)
        last_week = monday - timedelta(weeks=1)
        store.put_outlet_days([((last_week + timedelta(days=d)).isoformat(), 6, 1440 if d < 5 else 60, 1440, 1.0)
                               for d in range(7)])
        for d in range(7):
            t0 = datetime.combine(last_week + timedelta(days=d), datetime.min.time()).timestamp()
            for k in range(4):
                store.add_ato_run(t0 + 3600 * (k + 1), 30, 8.0)
        ev = self.svc.trends()["evaporation"][-1]
        self.assertEqual(ev["days"], 5)
        self.assertEqual(ev["runs_per_day"], 4.0)
        self.assertEqual(ev["seconds_per_day"], 120)


if __name__ == "__main__":
    unittest.main()
